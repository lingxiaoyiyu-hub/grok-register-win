#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi mailbox provider adapter (any-auto-register base_mailbox).

Used by grok_register_ttk to support dropdown providers:
  cfworker, moemail,
  maliapi, luckmail, skymail, cloudmail, freemail, opentrashmail, laoudo
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from mailbox_core import GROK_CODE_PATTERN, MailboxAccount as CoreMailboxAccount
except Exception:
    GROK_CODE_PATTERN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"
    CoreMailboxAccount = None

try:
    import base_mailbox as bm

    _IMPORT_OK = True
    _IMPORT_ERR = ""
except Exception as exc:
    bm = None
    _IMPORT_OK = False
    _IMPORT_ERR = str(exc)

MAIL_PROVIDER_CHOICES = [
    ("cfworker", "CF Worker / 自建域名"),
    ("moemail", "MoeMail (sall.cc)"),
    ("maliapi", "YYDS / MaliAPI"),
    ("luckmail", "LuckMail（接码/买邮）"),
    ("skymail", "SkyMail"),
    ("cloudmail", "CloudMail"),
    ("freemail", "Freemail 自建"),
    ("opentrashmail", "OpenTrashMail"),
    ("laoudo", "Laoudo 固定邮箱"),
]

# active mailbox for wait_for_code
_ACTIVE_BOX = None
_ACTIVE_ACCT = None


def import_ok() -> bool:
    return bool(_IMPORT_OK and bm is not None)


def import_error() -> str:
    return _IMPORT_ERR


def normalize_provider(name: str) -> str:
    p = str(name or "").strip().lower()
    aliases = {
        "custom": "cfworker",
        "cloudflare_temp_email": "cfworker",
        "cf-worker": "cfworker",
        "cf_worker": "cfworker",
        "cloudflare": "cfworker",
        # removed public providers (xAI 拒收), fall back to cfworker
        "tempmail": "cfworker",
        "tempmail.lol": "cfworker",
        "tempmail_lol": "cfworker",
        "duckmail": "cfworker",
        "gptmail": "cfworker",
        "yyds": "maliapi",
        "yy ds": "maliapi",
    }
    if p in ("tempmailer", "inboxkitten", "inbox_kitten"):
        return "cfworker"
    return aliases.get(p, p or "cfworker")


def extra_from_config(config: dict) -> dict:
    c = config or {}
    cf_url = str(c.get("cfworker_api_url") or c.get("cloudflare_api_base") or "").strip()
    cf_token = str(c.get("cfworker_admin_token") or c.get("cloudflare_api_key") or "").strip()
    cf_domain = str(c.get("cfworker_domain") or c.get("defaultDomains") or "").strip()
    return {
        "moemail_api_url": str(c.get("moemail_api_url") or "https://sall.cc").strip(),
        "moemail_api_key": str(c.get("moemail_api_key") or "").strip(),
        "skymail_api_base": str(c.get("skymail_api_base") or "https://api.skymail.ink").strip(),
        "skymail_token": str(c.get("skymail_token") or "").strip(),
        "skymail_domain": str(c.get("skymail_domain") or "").strip(),
        "cloudmail_api_base": str(c.get("cloudmail_api_base") or "").strip(),
        "cloudmail_admin_email": str(c.get("cloudmail_admin_email") or "").strip(),
        "cloudmail_admin_password": str(
            c.get("cloudmail_admin_password") or c.get("cloudflare_api_key") or ""
        ).strip(),
        "cloudmail_domain": str(c.get("cloudmail_domain") or c.get("defaultDomains") or "").strip(),
        "duckmail_api_url": str(c.get("duckmail_api_url") or "https://www.duckmail.sbs").strip(),
        "duckmail_provider_url": str(
            c.get("duckmail_provider_url") or "https://api.duckmail.sbs"
        ).strip(),
        "duckmail_bearer": str(c.get("duckmail_bearer") or "").strip(),
        "duckmail_domain": str(c.get("duckmail_domain") or "").strip(),
        "duckmail_api_key": str(c.get("duckmail_api_key") or "").strip(),
        "freemail_api_url": str(c.get("freemail_api_url") or "").strip(),
        "freemail_admin_token": str(c.get("freemail_admin_token") or "").strip(),
        "freemail_username": str(c.get("freemail_username") or "").strip(),
        "freemail_password": str(c.get("freemail_password") or "").strip(),
        "freemail_domain": str(c.get("freemail_domain") or "").strip(),
        "maliapi_base_url": str(c.get("maliapi_base_url") or "https://maliapi.215.im/v1").strip(),
        "maliapi_api_key": str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip(),
        "maliapi_domain": str(c.get("maliapi_domain") or "").strip(),
        "maliapi_auto_domain_strategy": str(c.get("maliapi_auto_domain_strategy") or "").strip(),
        "gptmail_base_url": str(c.get("gptmail_base_url") or "https://mail.chatgpt.org.uk").strip(),
        "gptmail_api_key": str(c.get("gptmail_api_key") or "").strip(),
        "gptmail_domain": str(c.get("gptmail_domain") or "").strip(),
        "opentrashmail_api_url": str(c.get("opentrashmail_api_url") or "").strip(),
        "opentrashmail_domain": str(c.get("opentrashmail_domain") or "").strip(),
        "opentrashmail_password": str(c.get("opentrashmail_password") or "").strip(),
        "cfworker_api_url": cf_url,
        "cfworker_admin_token": cf_token,
        "cfworker_domain": cf_domain,
        "cfworker_domain_override": str(c.get("cfworker_domain_override") or "").strip(),
        "cfworker_custom_auth": str(c.get("cfworker_custom_auth") or "").strip(),
        "cfworker_subdomain": str(c.get("cfworker_subdomain") or "").strip(),
        "cfworker_fingerprint": str(c.get("cfworker_fingerprint") or "").strip(),
        "luckmail_base_url": str(c.get("luckmail_base_url") or "https://mails.luckyous.com/").strip(),
        "luckmail_api_key": str(c.get("luckmail_api_key") or "").strip(),
        "luckmail_project_code": str(c.get("luckmail_project_code") or "grok").strip(),
        "luckmail_email_type": str(c.get("luckmail_email_type") or "").strip(),
        "luckmail_domain": str(c.get("luckmail_domain") or "").strip(),
        "laoudo_auth": str(c.get("laoudo_auth") or "").strip(),
        "laoudo_email": str(c.get("laoudo_email") or "").strip(),
        "laoudo_account_id": str(c.get("laoudo_account_id") or "").strip(),
    }


def provider_ready(config: dict, provider: str) -> bool:
    c = config or {}
    p = normalize_provider(provider)
    if p in ("tempmailer", "inboxkitten", "inbox_kitten"):
        return False
    if p == "moemail":
        return True
    if p == "maliapi":
        return bool(
            str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip()
            or str(c.get("yyds_jwt") or "").strip()
        )
    if p == "luckmail":
        return bool(str(c.get("luckmail_api_key") or "").strip())
    if p == "skymail":
        return bool(str(c.get("skymail_token") or "").strip())
    if p == "cloudmail":
        return bool(str(c.get("cloudmail_api_base") or "").strip())
    if p == "freemail":
        return bool(str(c.get("freemail_api_url") or "").strip())
    if p == "opentrashmail":
        return bool(str(c.get("opentrashmail_api_url") or "").strip())
    if p == "laoudo":
        return bool(str(c.get("laoudo_email") or "").strip())
    if p in ("cfworker", "cloudflare", "custom"):
        return bool(
            str(c.get("cfworker_api_url") or c.get("cloudflare_api_base") or "").strip()
        )
    return False


def make_mailbox(config: dict, provider: str, proxy: str = "", log_callback=None):
    if not import_ok():
        raise RuntimeError(f"base_mailbox 未加载: {_IMPORT_ERR}")
    prov = normalize_provider(provider)
    factory = prov
    extra = extra_from_config(config)
    box = bm.create_mailbox(factory, extra=extra, proxy=proxy or None)
    try:
        box._log_fn = log_callback
    except Exception:
        pass
    if log_callback:
        log_callback(f"[*] 邮箱适配器: {factory}")
    return box, factory


def get_email_and_token(
    config: dict,
    provider: str,
    proxy: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, str]:
    global _ACTIVE_BOX, _ACTIVE_ACCT
    box, prov = make_mailbox(config, provider, proxy=proxy, log_callback=log_callback)
    acct = box.get_email()
    email = str(getattr(acct, "email", "") or "").strip()
    token = str(getattr(acct, "account_id", "") or "").strip() or email
    extra = getattr(acct, "extra", None) or {}
    if isinstance(extra, dict):
        for k in ("jwt", "token", "session", "auth"):
            if extra.get(k):
                token = str(extra.get(k)).strip()
                break
    if not email:
        raise RuntimeError(f"{prov} 返回空邮箱")
    _ACTIVE_BOX = box
    _ACTIVE_ACCT = acct
    if log_callback:
        log_callback(f"[*] 已申请邮箱: {email}（源={prov}）")
    return email, token


def snapshot_ids(log_callback=None):
    global _ACTIVE_BOX, _ACTIVE_ACCT
    if _ACTIVE_BOX is None or _ACTIVE_ACCT is None:
        return set()
    try:
        ids = _ACTIVE_BOX.get_current_ids(_ACTIVE_ACCT) or set()
        ids = {str(x) for x in ids if x}
        if log_callback and ids:
            log_callback(f"[*] 发码前收件箱已有 {len(ids)} 封邮件，将忽略旧信")
        return ids
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 快照收件箱 id 失败（可忽略）: {exc}")
        return set()


def wait_for_code(
    email: str,
    dev_token: str,
    *,
    timeout: int = 180,
    cancel_callback: Optional[Callable[[], bool]] = None,
    before_ids=None,
    otp_sent_at=None,
    log_callback: Optional[Callable[[str], None]] = None,
    config: Optional[dict] = None,
    provider: str = "",
    proxy: str = "",
) -> str:
    global _ACTIVE_BOX, _ACTIVE_ACCT
    box = _ACTIVE_BOX
    acct = _ACTIVE_ACCT
    if box is None:
        box, _ = make_mailbox(config or {}, provider or "cfworker", proxy=proxy, log_callback=log_callback)
    if acct is None:
        acct = bm.MailboxAccount(email=email, account_id=dev_token or email)

    class _Ctl:
        def checkpoint(self, **kwargs):
            if cancel_callback and cancel_callback():
                raise RuntimeError("用户停止注册")

    try:
        box._task_control = _Ctl()
    except Exception:
        pass

    code = box.wait_for_code(
        acct,
        keyword="",
        timeout=int(timeout or 180),
        before_ids=set(before_ids or set()),
        code_pattern=GROK_CODE_PATTERN,
        otp_sent_at=otp_sent_at,
    )
    if not code:
        raise RuntimeError("未收到验证码")
    if log_callback:
        log_callback(f"[*] 验证码: {code}")
    return str(code).strip()
