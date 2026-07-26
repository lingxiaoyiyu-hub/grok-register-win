#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified mailbox receive helpers (adapted from any-auto-register style).

Receive flow used by register:
  1) create / obtain address
  2) snapshot current message ids (before_ids)
  3) after OTP is requested, poll inbox
  4) skip old ids / messages older than otp_sent_at
  5) extract verification code from subject + body

Default code shape for xAI/Grok: ABC-DEF (3-3 alnum).
"""

from __future__ import annotations

import html as html_lib
import quopri
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Set


# xAI confirmation codes look like "QII-8AM" / "7EC-OLY"
GROK_CODE_PATTERN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"


@dataclass
class MailboxAccount:
    email: str
    token: str = ""
    account_id: str = ""
    extra: dict = field(default_factory=dict)


def decode_mail_body(raw: str) -> str:
    """Best-effort decode of raw mail / html to plain text for code extraction."""
    text = str(raw or "")
    if not text:
        return ""
    # Only split headers when real RFC headers present
    if re.search(
        r"(?im)^(?:Return-Path|Received|Date|From|To|Subject|Content-Type):", text
    ):
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
    try:
        text = quopri.decodestring(text).decode("utf-8", errors="ignore")
    except Exception:
        pass
    text = html_lib.unescape(text)
    text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
    text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
    text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_code(
    text: str,
    subject: str = "",
    code_pattern: Optional[str] = None,
    *,
    prefer_grok: bool = True,
) -> Optional[str]:
    """Extract OTP/code from mail subject + body.

    prefer_grok=True: prioritize ABC-DEF (xAI), then 6-digit / custom pattern.
    """
    body = str(text or "")
    subj = str(subject or "")
    content = f"Subject: {subj}\n{body}" if subj else body
    if not content.strip():
        return None

    # Drop URLs to avoid tracking-parameter false positives (any-auto-register trick)
    content_no_url = re.sub(r"https?://\S+", " ", content)

    patterns: list[str] = []
    if code_pattern:
        patterns.append(code_pattern)

    if prefer_grok:
        patterns.extend(
            [
                # Subject: 6F2-CT8 xAI confirmation code
                r"(?:Subject:\s*)?([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI",
                r"(?:verification\s+code|验证码|your\s+code|confirmation\s+code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b",
                r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])",
                r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>",
            ]
        )

    patterns.extend(
        [
            r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{4,8})",
            r"(?is)\bcode\b[^0-9]{0,12}(\d{4,8})",
            r"verification\s+code[:\s]+(\d{4,8})",
            r"your\s+code[:\s]+(\d{4,8})",
            r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
            r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
        ]
    )

    for regex in patterns:
        m = re.search(regex, content_no_url, re.IGNORECASE)
        if not m:
            m = re.search(regex, content, re.IGNORECASE)
        if not m:
            continue
        code = (m.group(1) if m.groups() else m.group(0)).strip()
        if not code or code == "177010":
            continue
        # Normalize grok-style codes to upper
        if re.fullmatch(r"[A-Za-z0-9]{3}-[A-Za-z0-9]{3}", code):
            return code.upper()
        return code

    # HTML-wrapped 6-digit fallback
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code
    return None


def message_timestamp(msg: dict) -> Optional[float]:
    """Best-effort epoch seconds from common mail JSON fields."""
    if not isinstance(msg, dict):
        return None
    for key in (
        "date",
        "createdAt",
        "created_at",
        "timestamp",
        "time",
        "receivedAt",
        "received_at",
    ):
        val = msg.get(key)
        if val is None or val == "":
            continue
        try:
            if isinstance(val, (int, float)):
                ts = float(val)
                # ms vs s
                if ts > 1e12:
                    ts /= 1000.0
                return ts
            s = str(val).strip()
            if s.isdigit():
                ts = float(s)
                if ts > 1e12:
                    ts /= 1000.0
                return ts
            # ISO-ish
            from datetime import datetime

            s2 = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s2).timestamp()
        except Exception:
            continue
    return None


def message_id(msg: dict) -> str:
    if not isinstance(msg, dict):
        return ""
    for key in ("id", "msgid", "message_id", "messageId", "uid", "_id"):
        val = msg.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def message_text_blob(msg: dict) -> tuple[str, str]:
    """Return (subject, combined_text) from a heterogeneous mail JSON object."""
    if not isinstance(msg, dict):
        return "", ""
    subject = str(msg.get("subject") or msg.get("Subject") or "").strip()
    parts: list[str] = []
    for key in (
        "text",
        "raw",
        "content",
        "intro",
        "body",
        "snippet",
        "preview",
        "html_body",
        "htmlBody",
    ):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    html_list = msg.get("html") or []
    if isinstance(html_list, str):
        html_list = [html_list]
    if isinstance(html_list, list):
        for h in html_list:
            if isinstance(h, str) and h.strip():
                parts.append(h)
    combined = "\n".join(parts)
    combined = decode_mail_body(combined) if ("<" in combined and ">" in combined) else combined
    return subject, combined


def poll_for_code(
    poll_once: Callable[[], Optional[str]],
    *,
    timeout: float = 180,
    poll_interval: float = 3.0,
    cancel_callback: Optional[Callable[[], bool]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    timeout_message: str = "",
) -> str:
    """Poll until code found, cancelled, or timeout (any-auto-register style)."""
    timeout_seconds = max(float(timeout or 0), 1.0)
    deadline = time.time() + timeout_seconds
    sleeper = sleep_fn or time.sleep

    while time.time() < deadline:
        if cancel_callback and cancel_callback():
            raise Exception("用户停止注册")
        code = poll_once()
        if code:
            return str(code).strip()
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sleeper(min(float(poll_interval), remaining))

    raise Exception(timeout_message or f"等待验证码超时（{int(timeout_seconds)}s）")


def should_skip_message(
    msg: dict,
    *,
    seen_ids: Set[str],
    before_ids: Optional[Set[str]] = None,
    otp_sent_at: Optional[float] = None,
    skew_seconds: float = 2.0,
) -> bool:
    """Skip already-seen / pre-OTP messages."""
    mid = message_id(msg)
    if mid and mid in (before_ids or set()):
        return True
    if mid and mid in seen_ids:
        return True
    if otp_sent_at:
        ts = message_timestamp(msg)
        if ts is not None and ts < float(otp_sent_at) - float(skew_seconds):
            return True
    return False


def random_local_part(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(max(4, length)))


# ---------------------------------------------------------------------------
# Optional public providers (same receive style as any-auto-register)
# May still reject xAI mail; kept as optional backends, not default.
# ---------------------------------------------------------------------------


class TempMailLolClient:
    """tempmail.lol v2 — create inbox + poll (may block xAI like other public temps)."""

    API = "https://api.tempmail.lol/v2"

    def __init__(self, proxies: Optional[dict] = None, http_get=None, http_post=None):
        self.proxies = proxies or {}
        self._http_get = http_get
        self._http_post = http_post

    def create(self) -> MailboxAccount:
        if not self._http_post:
            raise RuntimeError("http_post not provided")
        resp = self._http_post(
            f"{self.API}/inbox/create", json={}, proxies=self.proxies, timeout=20
        )
        data = resp.json() if hasattr(resp, "json") else {}
        email = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or "").strip()
        if not email or not token:
            raise RuntimeError(f"tempmail.lol create failed: {data}")
        return MailboxAccount(
            email=email, token=token, account_id=token, extra={"provider": "tempmail_lol"}
        )

    def list_messages(self, account: MailboxAccount) -> list[dict]:
        if not self._http_get:
            raise RuntimeError("http_get not provided")
        resp = self._http_get(
            f"{self.API}/inbox",
            params={"token": account.token or account.account_id},
            proxies=self.proxies,
            timeout=15,
        )
        data = resp.json() if hasattr(resp, "json") else {}
        emails = data.get("emails") if isinstance(data, dict) else data
        return [m for m in (emails or []) if isinstance(m, dict)]

    def current_ids(self, account: MailboxAccount) -> Set[str]:
        try:
            return {message_id(m) for m in self.list_messages(account) if message_id(m)}
        except Exception:
            return set()


class GPTMailClient:
    """GPTMail-style API: generate-email + list emails by address."""

    def __init__(
        self,
        api_base: str = "https://mail.chatgpt.org.uk",
        api_key: str = "",
        domain: str = "",
        proxies: Optional[dict] = None,
        http_get=None,
    ):
        self.api = (api_base or "https://mail.chatgpt.org.uk").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.domain = (domain or "").strip().lstrip("@").lower()
        self.proxies = proxies or {}
        self._http_get = http_get

    def _headers(self) -> dict:
        h = {"accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def create(self) -> MailboxAccount:
        if self.domain:
            email = f"{random_local_part(10)}@{self.domain}"
            return MailboxAccount(
                email=email,
                token=email,
                account_id=email,
                extra={"provider": "gptmail", "local_address": True},
            )
        if not self._http_get:
            raise RuntimeError("http_get not provided")
        resp = self._http_get(
            f"{self.api}/api/generate-email",
            headers=self._headers(),
            proxies=self.proxies,
            timeout=20,
        )
        data = resp.json() if hasattr(resp, "json") else {}
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        email = str((data or {}).get("email") or "").strip()
        if not email:
            raise RuntimeError(f"GPTMail generate failed: {data}")
        return MailboxAccount(
            email=email, token=email, account_id=email, extra={"provider": "gptmail"}
        )

    def list_messages(self, account: MailboxAccount) -> list[dict]:
        if not self._http_get:
            raise RuntimeError("http_get not provided")
        resp = self._http_get(
            f"{self.api}/api/emails",
            params={"email": account.email},
            headers=self._headers(),
            proxies=self.proxies,
            timeout=15,
        )
        data = resp.json() if hasattr(resp, "json") else {}
        if isinstance(data, dict) and "data" in data:
            data = data.get("data")
        if isinstance(data, dict):
            messages = data.get("emails") or data.get("messages") or []
        else:
            messages = data or []
        return [m for m in messages if isinstance(m, dict)]

    def current_ids(self, account: MailboxAccount) -> Set[str]:
        try:
            return {message_id(m) for m in self.list_messages(account) if message_id(m)}
        except Exception:
            return set()


def wait_code_from_messages(
    list_messages: Callable[[], list[dict]],
    *,
    before_ids: Optional[Set[str]] = None,
    otp_sent_at: Optional[float] = None,
    timeout: float = 180,
    poll_interval: float = 3.0,
    cancel_callback: Optional[Callable[[], bool]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    code_pattern: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    detail_fetcher: Optional[Callable[[dict], dict]] = None,
    provider_label: str = "mail",
) -> str:
    """Shared receive loop: list → filter → extract (optional detail fetch)."""
    seen: Set[str] = set()
    before = set(before_ids or set())

    def poll_once() -> Optional[str]:
        try:
            messages = list_messages() or []
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] {provider_label} 拉取邮件失败: {exc}")
            return None
        if log_callback:
            log_callback(f"[Debug] {provider_label} 本轮邮件数量: {len(messages)}")

        for msg in messages:
            if should_skip_message(
                msg, seen_ids=seen, before_ids=before, otp_sent_at=otp_sent_at
            ):
                continue
            mid = message_id(msg)
            if mid:
                seen.add(mid)

            subject, combined = message_text_blob(msg)
            if detail_fetcher and (not combined or len(combined) < 20):
                try:
                    detail = detail_fetcher(msg) or {}
                    s2, c2 = message_text_blob(detail)
                    if s2 and not subject:
                        subject = s2
                    if c2:
                        combined = (combined + "\n" + c2).strip()
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] {provider_label} detail 失败: {exc}")

            if log_callback and (subject or combined):
                log_callback(f"[Debug] {provider_label} 收到邮件: {subject or '(no subject)'}")

            code = extract_code(combined, subject, code_pattern=code_pattern)
            if code:
                if log_callback:
                    log_callback(f"[*] {provider_label} 提取到验证码: {code}")
                return code
        return None

    return poll_for_code(
        poll_once,
        timeout=timeout,
        poll_interval=poll_interval,
        cancel_callback=cancel_callback,
        sleep_fn=sleep_fn,
        timeout_message=f"{provider_label} 在 {int(timeout)}s 内未收到验证码邮件",
    )
