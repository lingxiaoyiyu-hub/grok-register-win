#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grok Register 账号面板 + 启动注册（代理/节点由本机 Clash 管理）"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

# Project root = parent of panel/ (Windows / portable layout)
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("GROK_REGISTER_DIR", str(_DEFAULT_ROOT))).resolve()
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "admin")
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8787"))
SECRET = os.environ.get("PANEL_SECRET", "grok-register-panel-local-secret")
CLASH_API = os.environ.get("CLASH_API", "http://127.0.0.1:9090").rstrip("/")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")
# Prefer project venv; Windows uses Scripts\python.exe
_VENV_WIN = BASE_DIR / ".venv" / "Scripts" / "python.exe"
_VENV_UNIX = BASE_DIR / ".venv" / "bin" / "python"
_DEFAULT_PY = (
    str(_VENV_WIN)
    if _VENV_WIN.exists()
    else (str(_VENV_UNIX) if _VENV_UNIX.exists() else sys.executable)
)
VENV_PYTHON = os.environ.get("GROK_PYTHON", _DEFAULT_PY)
MAIN_SCRIPT = BASE_DIR / "grok_register_ttk.py"
CONFIG_PATH = BASE_DIR / "config.json"
PROXY_URL = os.environ.get("GROK_PROXY", "http://127.0.0.1:7890")
LOG_DIR = Path(os.environ.get("PANEL_LOG_DIR", str(BASE_DIR / "data" / "logs"))).resolve()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# SSO → real CPA (CLIProxyAPI OAuth JSON)
CPA_DIR = Path(os.environ.get("CPA_DIR", str(BASE_DIR / "data" / "cpa"))).resolve()
CPA_DIR.mkdir(parents=True, exist_ok=True)
CPA_INDEX_PATH = CPA_DIR / "index.json"
CPA_FAILED_PATH = CPA_DIR / "failed.jsonl"
SSO2CPA_PATH = Path(
    os.environ.get("SSO2CPA_PATH", str(BASE_DIR / "lib"))
).resolve()
AUTO_CPA = os.environ.get("AUTO_CPA", "1").strip() not in ("0", "false", "False", "no")
CPA_DELAY = float(os.environ.get("CPA_DELAY", "1.0"))
# Optional: talk to local Clash Meta external-controller for node list.
# Default: external Clash managed by user; node UI is best-effort.
ENABLE_CLASH_UI = os.environ.get("ENABLE_CLASH_UI", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)

# import shared convert core
for _p in (str(SSO2CPA_PATH), str(BASE_DIR / "lib"), str(Path(__file__).resolve().parent)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from sso2cpa_core import (  # type: ignore
        convert_one,
        normalize_sso,
        safe_filename as cpa_safe_filename,
        sso_fingerprint,
    )

    _CPA_CORE_OK = True
    _CPA_CORE_ERR = ""
except Exception as _e:  # pragma: no cover
    convert_one = None  # type: ignore
    normalize_sso = lambda t: (t or "").strip()  # type: ignore
    cpa_safe_filename = lambda s: re.sub(r"[^\w.@+-]+", "_", s or "unknown")[:100]  # type: ignore
    sso_fingerprint = lambda s: hashlib.sha256((s or "").encode()).hexdigest()  # type: ignore
    _CPA_CORE_OK = False
    _CPA_CORE_ERR = str(_e)

HK_RE = re.compile(r"(香港|Hong\s*Kong|\bHK\b|🇭🇰)", re.I)

app = Flask(__name__)
app.secret_key = SECRET

# --------------- job state ---------------
_job_lock = threading.Lock()
_job: Dict = {
    "running": False,
    "stop": False,
    "pid": None,
    "started_at": None,
    "finished_at": None,
    "count": 0,
    "success": 0,
    "fail": 0,
    "current_round": 0,
    "current_node": "",
    "node_mode": "fixed",  # fixed | rotate_on_fail | rotate_each
    "node_list": [],
    "node_index": 0,
    "log_path": "",
    "last_error": "",
    "status": "idle",
}
_logs: Deque[str] = deque(maxlen=2000)
_proc: Optional[subprocess.Popen] = None

# --------------- CPA auto-convert queue ---------------
_cpa_lock = threading.Lock()
_cpa_q: "queue.Queue[Optional[dict]]" = queue.Queue()
_cpa_state: Dict = {
    "enabled": AUTO_CPA,
    "core_ok": _CPA_CORE_OK,
    "core_error": _CPA_CORE_ERR,
    "pending": 0,
    "ok": 0,
    "fail": 0,
    "running": False,
    "last_error": "",
    "last_ok_email": "",
}
_cpa_done: Set[str] = set()  # sso fingerprints already converted
_cpa_inflight: Set[str] = set()


def log_line(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _logs.append(line)
    path = _job.get("log_path")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def require_login():
    if session.get("ok"):
        return None
    # API requests get JSON 401
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return redirect(url_for("login", next=request.path))


def list_account_files() -> List[Path]:
    return sorted(
        BASE_DIR.glob("accounts_*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def read_account_lines(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def collect_all_accounts() -> List[Tuple[str, str]]:
    items = []
    for f in list_account_files():
        for line in read_account_lines(f):
            items.append((f.name, line))
    return items


def parse_line(line: str):
    parts = line.split("----")
    if len(parts) >= 3:
        return {
            "email": parts[0],
            "password": parts[1],
            "sso": "----".join(parts[2:]),
            "raw": line,
        }
    return {"email": line, "password": "", "sso": "", "raw": line}


def _b64url_json(segment: str):
    import base64

    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def decode_sso_meta(sso: str) -> dict:
    """Best-effort parse web SSO JWT payload (not xAI OAuth)."""
    if not sso or sso.count(".") < 2:
        return {}
    return _b64url_json(sso.split(".")[1])


def unique_accounts() -> List[dict]:
    seen = set()
    out = []
    for source, line in collect_all_accounts():
        if line in seen:
            continue
        seen.add(line)
        info = parse_line(line)
        info["source"] = source
        meta = decode_sso_meta(info.get("sso") or "")
        info["session_id"] = meta.get("session_id") or meta.get("sid") or ""
        out.append(info)
    return out


def safe_filename_part(s: str) -> str:
    s = re.sub(r"[^\w.@+-]+", "_", s or "unknown")
    return s[:80] or "unknown"


def account_line_set() -> Set[str]:
    return {line for _, line in collect_all_accounts()}


def load_cpa_index() -> None:
    """Load converted SSO fingerprints + counts from disk."""
    global _cpa_done
    done: Set[str] = set()
    ok_count = 0
    if CPA_INDEX_PATH.exists():
        try:
            data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, dict):
                for fp, meta in items.items():
                    done.add(fp)
                    if isinstance(meta, dict) and meta.get("file"):
                        ok_count += 1
            elif isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("fp"):
                        done.add(it["fp"])
                        ok_count += 1
        except Exception:
            pass
    # also scan existing json files
    for p in CPA_DIR.glob("xai-*.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            sso = normalize_sso(obj.get("sso") or "")
            if sso:
                done.add(sso_fingerprint(sso))
                ok_count = max(ok_count, 1)
        except Exception:
            continue
    with _cpa_lock:
        _cpa_done = done
        if ok_count and not _cpa_state.get("ok"):
            _cpa_state["ok"] = len(done)


def save_cpa_index_item(fp: str, meta: dict) -> None:
    items: Dict[str, dict] = {}
    if CPA_INDEX_PATH.exists():
        try:
            data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), dict):
                items = data["items"]
        except Exception:
            items = {}
    items[fp] = meta
    CPA_INDEX_PATH.write_text(
        json.dumps(
            {"updated_at": datetime.now().isoformat(timespec="seconds"), "items": items},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def list_cpa_files() -> List[Path]:
    return sorted(CPA_DIR.glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def cpa_stats() -> dict:
    with _cpa_lock:
        st = dict(_cpa_state)
        done_n = len(_cpa_done)
    files = list_cpa_files()
    st["files"] = len(files)
    st["done"] = done_n
    st["dir"] = str(CPA_DIR)
    return st


def enqueue_cpa_convert(
    email: str,
    sso: str,
    password: str = "",
    source: str = "",
    force: bool = False,
) -> Tuple[bool, str]:
    """Queue one SSO for real OAuth CPA conversion. Returns (queued, reason)."""
    if not AUTO_CPA and not force:
        return False, "auto_cpa disabled"
    if not _CPA_CORE_OK or convert_one is None:
        return False, f"sso2cpa core unavailable: {_CPA_CORE_ERR}"
    sso = normalize_sso(sso)
    if not sso:
        return False, "empty sso"
    fp = sso_fingerprint(sso)
    with _cpa_lock:
        if not force and (fp in _cpa_done or fp in _cpa_inflight):
            return False, "already converted or queued"
        _cpa_inflight.add(fp)
        _cpa_state["pending"] = int(_cpa_state.get("pending") or 0) + 1
    _cpa_q.put(
        {
            "email": email or "",
            "sso": sso,
            "password": password or "",
            "source": source or "",
            "fp": fp,
            "force": force,
        }
    )
    return True, "queued"


def enqueue_new_accounts(before: Set[str]) -> int:
    """Diff account lines after a round and queue new ones."""
    after = account_line_set()
    new_lines = after - before
    n = 0
    for line in new_lines:
        info = parse_line(line)
        ok, _ = enqueue_cpa_convert(
            email=info.get("email") or "",
            sso=info.get("sso") or "",
            password=info.get("password") or "",
            source="register",
        )
        if ok:
            n += 1
    return n


def enqueue_missing_accounts(limit: int = 500) -> int:
    """Queue accounts that have SSO but no CPA file yet."""
    n = 0
    for acc in unique_accounts():
        if n >= limit:
            break
        ok, _ = enqueue_cpa_convert(
            email=acc.get("email") or "",
            sso=acc.get("sso") or "",
            password=acc.get("password") or "",
            source=acc.get("source") or "",
        )
        if ok:
            n += 1
    return n


def _cpa_worker_loop():
    log_line(
        f"[CPA] worker start · core={'ok' if _CPA_CORE_OK else 'FAIL'} · auto={AUTO_CPA} · dir={CPA_DIR}"
    )
    if not _CPA_CORE_OK:
        log_line(f"[CPA] core import error: {_CPA_CORE_ERR}")
    while True:
        item = _cpa_q.get()
        if item is None:
            break
        email = item.get("email") or ""
        sso = item.get("sso") or ""
        fp = item.get("fp") or sso_fingerprint(sso)
        with _cpa_lock:
            _cpa_state["running"] = True
            _cpa_state["pending"] = max(0, int(_cpa_state.get("pending") or 0) - 1)
        try:
            if convert_one is None:
                raise RuntimeError(f"core missing: {_CPA_CORE_ERR}")
            entry = convert_one(sso, email=email, proxy=PROXY_URL)
            # keep password if known (not required by CPA, useful for bookkeeping)
            if item.get("password") and not entry.get("password"):
                entry["password"] = item["password"]
            entry["_source"] = "grok-register-auto-cpa"
            entry["_source_file"] = item.get("source") or ""
            email_out = entry.get("email") or email or "unknown"
            fname = f"xai-{cpa_safe_filename(email_out)}.json"
            path = CPA_DIR / fname
            if path.exists():
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                    old_fp = sso_fingerprint(normalize_sso(old.get("sso") or ""))
                except Exception:
                    old_fp = ""
                if old_fp and old_fp != fp:
                    fname = f"xai-{cpa_safe_filename(email_out)}-{fp[:8]}.json"
                    path = CPA_DIR / fname
            path.write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            save_cpa_index_item(
                fp,
                {
                    "email": email_out,
                    "file": fname,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "auth_kind": entry.get("auth_kind"),
                },
            )
            with _cpa_lock:
                _cpa_done.add(fp)
                _cpa_inflight.discard(fp)
                _cpa_state["ok"] = int(_cpa_state.get("ok") or 0) + 1
                _cpa_state["last_ok_email"] = email_out
                _cpa_state["last_error"] = ""
            log_line(f"[CPA] OK {email_out} -> {fname}")
        except Exception as e:
            err = str(e)
            with _cpa_lock:
                _cpa_inflight.discard(fp)
                _cpa_state["fail"] = int(_cpa_state.get("fail") or 0) + 1
                _cpa_state["last_error"] = err
            try:
                with open(CPA_FAILED_PATH, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "at": datetime.now().isoformat(timespec="seconds"),
                                "email": email,
                                "fp": fp,
                                "error": err,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:
                pass
            log_line(f"[CPA] FAIL {email or fp[:12]}: {err}")
        finally:
            with _cpa_lock:
                _cpa_state["running"] = not _cpa_q.empty()
            if CPA_DELAY > 0:
                time.sleep(CPA_DELAY)
            _cpa_q.task_done()


def start_cpa_worker() -> None:
    load_cpa_index()
    th = threading.Thread(target=_cpa_worker_loop, name="cpa-worker", daemon=True)
    th.start()


def to_grok2api_pool(accounts: List[dict]) -> dict:
    """grok2api-style local token pool using web SSO tokens."""
    tokens = []
    for acc in accounts:
        sso = (acc.get("sso") or "").strip()
        if not sso:
            continue
        tokens.append(
            {
                "token": sso,
                "email": acc.get("email") or "",
                "status": "active",
            }
        )
    return {
        "ssoBasic": tokens,
        "ssoSuper": [],
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def email_config_public(cfg: Optional[dict] = None) -> dict:
    """Email settings for panel UI (custom maps to cloudflare_* backend)."""
    c = cfg if isinstance(cfg, dict) else load_config()
    provider = str(c.get("email_provider") or "tempmailer").strip().lower()
    # inboxkitten.com 已被 xAI 拒绝，旧配置自动映射为 tempmailer
    if provider in ("inboxkitten", "inbox_kitten"):
        provider = "tempmailer"
    if provider == "cloudflare":
        ui_provider = "custom"
    elif provider == "tempmailer":
        ui_provider = "tempmailer"
    else:
        # unknown / unpaid providers -> show as custom if base set, else tempmailer
        ui_provider = "custom" if c.get("cloudflare_api_base") else "tempmailer"
    return {
        "provider": ui_provider,
        "email_failover": bool(c.get("email_failover", True)),
        "tempmailer_domain": str(c.get("tempmailer_domain") or c.get("defaultDomains") or "").strip(),
        "custom_api_base": str(c.get("cloudflare_api_base") or "").strip(),
        "custom_api_key": str(c.get("cloudflare_api_key") or "").strip(),
        "custom_auth_mode": (
            "bearer"
            if str(c.get("cloudflare_auth_mode") or "").strip().lower()
            in ("auth", "bearer", "authorization")
            else str(c.get("cloudflare_auth_mode") or "x-admin-auth").strip()
        ),
        "custom_domain": str(c.get("defaultDomains") or "").strip(),
        "custom_path_accounts": str(
            c.get("cloudflare_path_accounts") or "/admin/new_address"
        ).strip(),
        "custom_path_messages": str(
            c.get("cloudflare_path_messages") or "/api/mails"
        ).strip(),
        "custom_path_token": str(c.get("cloudflare_path_token") or "/api/token").strip(),
        "hint": (
            "内置仅 Tempmailer（bluenode.cc 等）。"
            "InboxKitten 域名已被 xAI 拒绝，已从选项中移除。"
            "自定义：对接自建临时邮箱 API（兼容 cloudflare_temp_email）。"
        ),
    }


def apply_email_config_from_ui(data: dict) -> dict:
    """Merge panel email form into config.json and return public view."""
    cfg = load_config()
    provider = str(data.get("provider") or "tempmailer").strip().lower()
    if provider in ("inboxkitten", "inbox_kitten"):
        provider = "tempmailer"
    if provider not in ("tempmailer", "custom"):
        raise ValueError("provider 必须是 tempmailer / custom")

    cfg["email_failover"] = bool(data.get("email_failover", True))

    if provider == "tempmailer":
        cfg["email_provider"] = "tempmailer"
        domain = str(data.get("tempmailer_domain") or cfg.get("tempmailer_domain") or "bluenode.cc").strip()
        cfg["tempmailer_domain"] = domain
        cfg["tempmailer_domains"] = [domain] if domain else cfg.get("tempmailer_domains") or []
        cfg["defaultDomains"] = domain or cfg.get("defaultDomains") or ""
        chain = ["tempmailer"]
        if str(cfg.get("cloudflare_api_base") or "").strip():
            chain.append("cloudflare")
        cfg["email_providers"] = chain
    else:
        # custom -> cloudflare backend channel
        api_base = str(data.get("custom_api_base") or "").strip().rstrip("/")
        if not api_base:
            raise ValueError("自定义模式必须填写 API 地址 cloudflare_api_base")
        cfg["email_provider"] = "cloudflare"
        cfg["cloudflare_api_base"] = api_base
        cfg["cloudflare_api_key"] = str(data.get("custom_api_key") or "").strip()
        mode = str(data.get("custom_auth_mode") or "x-admin-auth").strip().lower()
        if mode not in ("none", "bearer", "x-api-key", "x-admin-auth", "query-key"):
            mode = "x-admin-auth"
        # register: x-api-key / x-admin-auth / query-key / none; anything else + key => Authorization Bearer
        if mode == "bearer":
            cfg["cloudflare_auth_mode"] = "auth"
        else:
            cfg["cloudflare_auth_mode"] = mode
        domain = str(data.get("custom_domain") or "").strip()
        cfg["defaultDomains"] = domain
        cfg["cloudflare_path_accounts"] = str(
            data.get("custom_path_accounts") or "/admin/new_address"
        ).strip() or "/admin/new_address"
        cfg["cloudflare_path_messages"] = str(
            data.get("custom_path_messages") or "/api/mails"
        ).strip() or "/api/mails"
        cfg["cloudflare_path_token"] = str(
            data.get("custom_path_token") or "/api/token"
        ).strip() or "/api/token"
        if cfg.get("email_failover"):
            cfg["email_providers"] = ["cloudflare", "tempmailer"]
        else:
            cfg["email_providers"] = ["cloudflare"]

    save_config(cfg)
    return email_config_public(cfg)



def resolve_proxy_url() -> str:
    """Prefer config.json proxy; auto-probe common Clash ports if dead."""
    import socket
    from urllib.parse import urlparse

    def open_port(host: str, port: int, timeout: float = 0.35) -> bool:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    preferred = ""
    try:
        cfg = load_config()
        preferred = str(cfg.get("proxy") or "").strip()
    except Exception:
        preferred = ""
    preferred = preferred or os.environ.get("GROK_PROXY", "").strip() or PROXY_URL

    def ok(url: str) -> bool:
        u = urlparse(url if "://" in url else "http://" + url)
        return open_port(u.hostname or "127.0.0.1", u.port or 7890)

    if preferred and ok(preferred):
        return preferred
    for port in (7897, 7890, 7891, 7892, 10809, 20171, 1080, 2080, 8888):
        url = f"http://127.0.0.1:{port}"
        if ok(url):
            return url
    return preferred or "http://127.0.0.1:7890"


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------- Clash helpers (optional external controller) ---------------
def clash_request(method: str, path: str, data=None, timeout=15):
    if not ENABLE_CLASH_UI:
        raise RuntimeError("clash ui disabled")
    url = CLASH_API + path
    body = None if data is None else json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if CLASH_SECRET:
        headers["Authorization"] = f"Bearer {CLASH_SECRET}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode())


def clash_list_nodes() -> dict:
    """Return usable non-HK leaf nodes + selectors + current."""
    try:
        prox = clash_request("GET", "/proxies")["proxies"]
        cfg = clash_request("GET", "/configs") or {}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "nodes": [],
            "selectors": {},
            "hint": "未检测到本机 Clash API。请在自己的 Clash 里选节点；本工具默认走 http://127.0.0.1:7890",
        }

    leaves = []
    for name, v in prox.items():
        t = v.get("type") or ""
        if t in (
            "Selector",
            "URLTest",
            "Fallback",
            "LoadBalance",
            "Relay",
            "Direct",
            "Reject",
            "Compatible",
            "Pass",
            "Dns",
        ):
            continue
        if name in ("PASS-RULE", "REJECT-DROP"):
            continue
        if HK_RE.search(name):
            continue
        leaves.append({"name": name, "type": t})

    # sort by region preference
    pref = ["US", "JP", "SG", "TW", "MY", "TH", "UK"]

    def key(n):
        name = n["name"].upper()
        for i, p in enumerate(pref):
            if name.startswith(p):
                return (i, name)
        return (99, name)

    leaves.sort(key=key)

    selectors = {}
    for name, v in prox.items():
        if v.get("type") == "Selector":
            selectors[name] = {"now": v.get("now"), "all": v.get("all") or []}

    return {
        "ok": True,
        "mode": cfg.get("mode"),
        "nodes": leaves,
        "selectors": selectors,
        "global_now": (selectors.get("GLOBAL") or {}).get("now"),
        "main_now": (selectors.get("🚀 使用节点") or {}).get("now"),
    }


def clash_set_node(node: str) -> Tuple[bool, str]:
    if not node:
        return True, "未指定节点（使用外部 Clash 当前节点）"
    if not ENABLE_CLASH_UI:
        return True, "Clash UI 关闭：请在本机 Clash 客户端切换节点"
    try:
        # ensure global mode so browser always uses proxy
        try:
            clash_request("PATCH", "/configs", {"mode": "global"})
        except Exception:
            pass
        prox = clash_request("GET", "/proxies")["proxies"]
        set_count = 0
        for name, v in prox.items():
            if v.get("type") != "Selector":
                continue
            alln = v.get("all") or []
            if node not in alln:
                continue
            try:
                clash_request(
                    "PUT",
                    "/proxies/" + urllib.parse.quote(name, safe=""),
                    {"name": node},
                )
                set_count += 1
            except Exception as e:
                log_line(f"[Clash] set {name} fail: {e}")
        if set_count == 0:
            return False, f"节点 {node} 不在任何选择器中（也可直接在 Clash 客户端切换）"
        return True, f"已切换到 {node}（{set_count} 个选择器）"
    except Exception as e:
        # soft-fail: external Clash without API is OK
        return True, f"Clash API 不可用，跳过切换（{e}）；请在客户端自选节点"


def clash_exit_ip() -> str:
    try:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": PROXY_URL, "https": PROXY_URL}
        )
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(
            "http://ip-api.com/json/?fields=country,city,query,isp", timeout=12
        ) as resp:
            d = json.loads(resp.read().decode())
            return f"{d.get('query')} {d.get('country')}/{d.get('city')} ({d.get('isp')})"
    except Exception as e:
        return f"unknown ({e})"


# --------------- job runner ---------------
def _update_stats_from_log(line: str):
    if "注册成功" in line or "[+] 注册成功" in line:
        with _job_lock:
            _job["success"] = int(_job.get("success") or 0) + 1
    if "注册失败" in line or "[-] 注册失败" in line:
        with _job_lock:
            _job["fail"] = int(_job.get("fail") or 0) + 1


def _run_one_round(round_no: int, total: int) -> bool:
    """Run register_count=1 once. Return True if success detected."""
    global _proc
    cfg = load_config()
    cfg["register_count"] = 1
    cfg["proxy"] = resolve_proxy_url()
    global PROXY_URL
    PROXY_URL = cfg["proxy"]
    os.environ["GROK_PROXY"] = PROXY_URL
    cfg.setdefault("email_provider", "tempmailer")
    save_config(cfg)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Windows / local: use system Chrome/Edge; allow override
    if os.name == "nt":
        if not env.get("BROWSER_PATH"):
            for cand in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ):
                if Path(cand).exists():
                    env["BROWSER_PATH"] = cand
                    break
    else:
        env["DISPLAY"] = env.get("DISPLAY") or ":0"
        env.setdefault("BROWSER_PATH", env.get("BROWSER_PATH") or "")

    log_line(f"=== 第 {round_no}/{total} 轮开始 · 节点 {_job.get('current_node') or '外部Clash'} ===")
    log_line(f"[*] proxy={PROXY_URL} python={VENV_PYTHON}")
    cmd = [
        VENV_PYTHON,
        "-u",
        str(MAIN_SCRIPT),
        "cli",
    ]
    try:
        _proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        log_line(f"[!] 启动失败: {e}")
        return False

    with _job_lock:
        _job["pid"] = _proc.pid

    # send start
    try:
        assert _proc.stdin is not None
        _proc.stdin.write("start\n")
        _proc.stdin.flush()
    except Exception as e:
        log_line(f"[!] 写入 start 失败: {e}")

    success = False
    failed = False
    assert _proc.stdout is not None
    for line in _proc.stdout:
        if _job.get("stop"):
            log_line("[!] 收到停止指令，终止当前轮")
            try:
                _proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                _proc.kill()
            except Exception:
                pass
            break
        line = line.rstrip("\n")
        if not line:
            continue
        log_line(line)
        if "注册成功" in line or "[+] 注册成功" in line:
            success = True
        if "注册失败" in line or "[-] 注册失败" in line:
            failed = True
        if "任务结束" in line and ("成功" in line or "失败" in line):
            # final summary line often has both
            pass

    try:
        _proc.wait(timeout=30)
    except Exception:
        try:
            _proc.kill()
        except Exception:
            pass
    with _job_lock:
        _job["pid"] = None
    _proc = None

    # best-effort cleanup of temp browser profiles (Windows/Linux)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/FI", "WINDOWTITLE eq *autoPortData*", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "chromium.*autoPortData"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except Exception:
        pass

    if success and not failed:
        return True
    if success:
        return True
    return False


def _next_node(nodes: List[str], index: int) -> Tuple[str, int]:
    if not nodes:
        return "", 0
    index = (index + 1) % len(nodes)
    return nodes[index], index


def job_worker(count: int, node: str = "", node_mode: str = "fixed", node_list: Optional[List[str]] = None):
    """Run register rounds. Node switching is intentionally not managed here —
    user selects nodes in their own Clash client."""
    global _job
    try:
        with _job_lock:
            _job["running"] = True
            _job["stop"] = False
            _job["status"] = "running"
            _job["count"] = count
            _job["success"] = 0
            _job["fail"] = 0
            _job["current_round"] = 0
            _job["node_mode"] = "external"
            _job["node_list"] = []
            _job["current_node"] = "external-clash"
            _job["started_at"] = datetime.now().isoformat(timespec="seconds")
            _job["finished_at"] = None
            _job["last_error"] = ""

        proxy_now = resolve_proxy_url()
        global PROXY_URL
        PROXY_URL = proxy_now
        os.environ["GROK_PROXY"] = proxy_now
        try:
            cfg0 = load_config(); cfg0["proxy"] = proxy_now; save_config(cfg0)
        except Exception:
            pass
        log_line(f"[*] 使用外部 Clash 代理: {proxy_now}（节点请在 Clash 客户端选择）")
        log_line(f"[*] 出口探测: {clash_exit_ip()}")

        for i in range(1, count + 1):
            if _job.get("stop"):
                log_line("[!] 用户停止，结束任务")
                break

            with _job_lock:
                _job["current_round"] = i

            before_lines = account_line_set()
            ok = _run_one_round(i, count)
            if ok:
                with _job_lock:
                    _job["success"] = int(_job.get("success") or 0) + 1
                log_line(f"[+] 第 {i} 轮成功（累计成功 {_job['success']}）")
                if AUTO_CPA:
                    time.sleep(0.8)
                    queued = enqueue_new_accounts(before_lines)
                    if queued:
                        log_line(f"[CPA] 本轮新账号入队转换: {queued}")
                    else:
                        queued2 = enqueue_missing_accounts(limit=3)
                        if queued2:
                            log_line(f"[CPA] 未匹配到新行，补队最近未转换: {queued2}")
                        else:
                            log_line("[CPA] 本轮未发现可转换的新 SSO（可能文件未写出）")
            else:
                with _job_lock:
                    _job["fail"] = int(_job.get("fail") or 0) + 1
                log_line(f"[-] 第 {i} 轮失败（累计失败 {_job['fail']}），继续下一轮")

            time.sleep(1)

        log_line(
            f"[*] 全部结束：成功 {_job.get('success')} | 失败 {_job.get('fail')} / 目标 {count}"
        )
    except Exception as e:
        log_line(f"[!] 任务异常: {e}")
        log_line(traceback.format_exc())
        with _job_lock:
            _job["last_error"] = str(e)
    finally:
        with _job_lock:
            _job["running"] = False
            _job["status"] = "idle"
            _job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _job["pid"] = None


def start_job(count: int, node: str = "", node_mode: str = "fixed") -> Tuple[bool, str]:
    with _job_lock:
        if _job.get("running"):
            return False, "已有任务在运行"
    if count < 1 or count > 500:
        return False, "轮数范围 1-500"

    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with _job_lock:
        _job["log_path"] = str(log_path)
    _logs.clear()
    log_line(f"任务创建：轮数={count} proxy={PROXY_URL}（节点由本机 Clash 管理）")

    th = threading.Thread(
        target=job_worker,
        args=(count,),
        daemon=True,
    )
    th.start()
    return True, "已启动"


def stop_job() -> Tuple[bool, str]:
    global _proc
    with _job_lock:
        if not _job.get("running"):
            return False, "当前没有运行中的任务"
        _job["stop"] = True
    log_line("[!] 正在停止…")
    p = _proc
    if p and p.poll() is None:
        try:
            p.send_signal(signal.SIGINT)
        except Exception:
            pass
        try:
            time.sleep(1)
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    try:
        if os.name == "nt":
            # kill register child if still around
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/FI", "MEMUSAGE gt 1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "grok_register_ttk.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except Exception:
        pass
    return True, "已发送停止"


# --------------- HTML ---------------
LOGIN_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>登录 · Grok Register</title>
  <style>
    :root{--bg:#0f1115;--card:#1a1f2b;--fg:#e8ecf4;--muted:#9aa6bf;--line:#2a3344}
    body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif;background:radial-gradient(1200px 600px at 20% -10%,#1d2a44,transparent),var(--bg);color:var(--fg)}
    .card{width:min(420px,92vw);background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px}
    h1{margin:0 0 8px;font-size:22px} p{margin:0 0 18px;color:var(--muted);font-size:14px}
    input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#121722;color:var(--fg)}
    button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:10px;background:linear-gradient(135deg,#4f8cff,#6ea8fe);color:#fff;font-weight:600;cursor:pointer}
    .err{color:#ff8f8f;margin-top:10px;font-size:13px}
  </style>
</head>
<body>
<form class="card" method="post">
  <h1>Grok Register</h1>
  <p>账号面板 · 启动注册 · 外置 Clash 代理</p>
  <input type="password" name="password" placeholder="面板密码" autofocus required/>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <button type="submit">进入</button>
</form>
</body></html>
"""

INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Grok Register 面板</title>
  <style>
    :root{
      --bg:#0f1115;--card:#171c27;--fg:#eef2fb;--muted:#9aa6bf;--accent:#6ea8fe;
      --ok:#3dd68c;--bad:#ff7b7b;--line:#2a3344;--chip:#222a3a;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:radial-gradient(1000px 500px at 10% -20%,#1b2740,transparent),var(--bg);color:var(--fg)}
    .wrap{max-width:1180px;margin:0 auto;padding:20px 14px 48px}
    header{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center;margin-bottom:14px}
    h1{margin:0;font-size:22px} .sub{color:var(--muted);font-size:12px;margin-top:4px}
    .actions{display:flex;flex-wrap:wrap;gap:8px}
    a.btn,button.btn{border:1px solid var(--line);background:var(--chip);color:var(--fg);padding:9px 12px;border-radius:10px;text-decoration:none;font-size:13px;cursor:pointer}
    a.btn.primary,button.btn.primary{background:linear-gradient(135deg,#4f8cff,#6ea8fe);border-color:transparent;color:#fff;font-weight:600}
    a.btn.ok,button.btn.ok{background:linear-gradient(135deg,#1f9d63,#3dd68c);border:0;color:#042}
    a.btn.danger,button.btn.danger{background:#2a1717;border-color:#5a2b2b;color:#ffb4b4}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
    .stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px}
    .stat .k{color:var(--muted);font-size:12px} .stat .v{font-size:20px;font-weight:700;margin-top:4px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:14px}
    .card h2{margin:0 0 12px;font-size:15px}
    .row{display:flex;flex-wrap:wrap;gap:10px;align-items:end}
    label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}
    input,select{background:#121722;border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:10px 12px;min-width:140px;font-size:13px}
    table{width:100%;border-collapse:collapse}
    th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
    th{color:var(--muted);background:#121722}
    .mono{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
    .muted{color:var(--muted)} .tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#1c2536;color:var(--muted);font-size:12px}
    #logbox{height:320px;overflow:auto;background:#0c0f16;border:1px solid var(--line);border-radius:12px;padding:12px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.45;white-space:pre-wrap}
    .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:#666}
    .dot.run{background:var(--ok);box-shadow:0 0 8px var(--ok)}
    .toast{position:fixed;right:16px;bottom:16px;background:#1d2433;border:1px solid var(--line);padding:10px 14px;border-radius:10px;display:none;z-index:9}
    @media(max-width:800px){ th:nth-child(3),td:nth-child(3){display:none} }
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Grok Register · Win 本地版</h1>
      <div class="sub">{{ base_dir }} · 代理请用本机 Clash（默认 7890）</div>
    </div>
    <div class="actions">
      <a class="btn primary" href="/download/sso.txt" title="email----password----sso">下载 SSO (TXT)</a>
      <a class="btn ok" href="/download/cpa.zip" title="真 CPA OAuth JSON（CLIProxyAPI 可用）">下载 CPA (JSON)</a>
      <a class="btn danger" href="/logout">退出</a>
    </div>
  </header>

  <div class="grid">
    <div class="stat"><div class="k">文件数</div><div class="v" id="st_files">{{ file_count }}</div></div>
    <div class="stat"><div class="k">SSO 账号</div><div class="v" id="st_accounts">{{ account_count }}</div></div>
    <div class="stat"><div class="k">CPA 已转换</div><div class="v" id="st_cpa_ok">{{ cpa_files }}</div></div>
    <div class="stat"><div class="k">CPA 队列</div><div class="v" style="font-size:16px" id="st_cpa_q">0 / 0 / 0</div></div>
    <div class="stat"><div class="k">任务状态</div><div class="v" style="font-size:16px"><span class="dot" id="st_dot"></span><span id="st_status">idle</span></div></div>
    <div class="stat"><div class="k">注册 成功/失败</div><div class="v" style="font-size:16px"><span id="st_sf">0 / 0</span></div></div>
  </div>

  <div class="card">
    <h2>启动注册</h2>
    <div class="row">
      <label>轮数
        <input type="number" id="count" min="1" max="500" value="1"/>
      </label>
      <button class="btn ok" id="btn_start" onclick="startJob()">▶ 开始注册</button>
      <button class="btn danger" id="btn_stop" onclick="stopJob()">■ 停止</button>
      <button class="btn" onclick="backfillCpa()" title="把尚未转成 CPA 的历史 SSO 入队">补转未转换 CPA</button>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px" id="cpa_hint">
      代理走本机 Clash（config.json 的 proxy，常见 7897）。节点在 Clash 里选。注册成功后自动转 CPA。
    </div>
  </div>

  <div class="card">
    <h2>邮箱服务</h2>
    <div class="row">
      <label>邮箱源
        <select id="email_provider" onchange="onEmailProviderChange()">
          <option value="tempmailer">Tempmailer（内置免 key）</option>
          <option value="custom">自定义（自建临时邮 API）</option>
        </select>
      </label>
      <label style="min-width:auto;flex-direction:row;align-items:center;gap:8px;padding-bottom:10px">
        <input type="checkbox" id="email_failover" style="width:auto;min-width:0"/> 失败时自动换源
      </label>
      <button class="btn primary" onclick="saveEmailConfig()">保存邮箱设置</button>
    </div>
    <div class="row" id="email_builtin_extra" style="margin-top:8px">
      <label id="lbl_temp_domain">Tempmailer 域名
        <input type="text" id="tempmailer_domain" placeholder="bluenode.cc"/>
      </label>
    </div>
    <div id="email_custom_box" style="display:none;margin-top:10px">
      <div class="muted" style="font-size:12px;margin-bottom:8px;line-height:1.5">
        自定义对接自建临时邮箱（兼容 <b>cloudflare_temp_email</b> 一类）：程序调用「创建地址」拿到邮箱+token，再轮询「收信」提取 xAI 验证码。<br/>
        常见管理员创建路径：<code>/admin/new_address</code>，鉴权头：<code>x-admin-auth</code>。
      </div>
      <div class="row">
        <label style="flex:2">API 根地址
          <input type="text" id="custom_api_base" placeholder="https://mail.example.com"/>
        </label>
        <label>API Key / 管理密码
          <input type="password" id="custom_api_key" placeholder="可选，视你的服务而定"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>鉴权方式
          <select id="custom_auth_mode">
            <option value="x-admin-auth">x-admin-auth（推荐，cf-temp-email 管理）</option>
            <option value="bearer">Authorization Bearer</option>
            <option value="x-api-key">X-API-Key</option>
            <option value="query-key">URL ?key=</option>
            <option value="none">无鉴权</option>
          </select>
        </label>
        <label>邮箱域名（可空则服务端默认）
          <input type="text" id="custom_domain" placeholder="mail.example.com"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>创建地址路径
          <input type="text" id="custom_path_accounts" placeholder="/admin/new_address"/>
        </label>
        <label>收信路径
          <input type="text" id="custom_path_messages" placeholder="/api/mails"/>
        </label>
        <label>Token 路径
          <input type="text" id="custom_path_token" placeholder="/api/token"/>
        </label>
      </div>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px" id="email_hint">加载邮箱配置…</div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div id="logbox">等待任务…</div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:14px 14px 0;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between">
      <h2 style="margin:0">账号文件</h2>
      <div class="actions" style="margin:0">
        <button class="btn" type="button" onclick="toggleSelectAllFiles(true)">全选</button>
        <button class="btn" type="button" onclick="toggleSelectAllFiles(false)">取消全选</button>
        <button class="btn danger" type="button" onclick="deleteSelectedFiles()">删除选中</button>
      </div>
    </div>
    <div class="muted" style="padding:8px 14px 0;font-size:12px">勾选已下载/不需要的 accounts_*.txt，删除后不会再出现在「下载 SSO」合并结果里。</div>
    {% if files %}
    <table>
      <thead>
        <tr>
          <th style="width:44px"><input type="checkbox" id="chk_all_files" onclick="toggleSelectAllFiles(this.checked)" title="全选"/></th>
          <th>文件</th><th>数量</th><th>时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
      {% for f in files %}
        <tr>
          <td><input type="checkbox" class="chk-file" value="{{ f.name }}"/></td>
          <td class="mono">{{ f.name }}</td>
          <td><span class="tag">{{ f.count }}</span></td>
          <td class="muted">{{ f.mtime }}</td>
          <td>
            <a class="btn" href="/preview/{{ f.name }}">预览</a>
            <a class="btn primary" href="/download/{{ f.name }}">下载</a>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="padding:24px;color:var(--muted);text-align:center">暂无 accounts_*.txt</div>
    {% endif %}
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2200)}
async function api(url, opt){
  const r = await fetch(url, Object.assign({credentials:'same-origin'}, opt||{}));
  const j = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.statusText||'request failed');
  return j;
}
function onEmailProviderChange(){
  const p=document.getElementById('email_provider').value;
  const custom=document.getElementById('email_custom_box');
  const builtin=document.getElementById('email_builtin_extra');
  if(p==='custom'){
    custom.style.display='block';
    builtin.style.display='none';
  }else{
    custom.style.display='none';
    builtin.style.display='flex';
  }
}
async function loadEmailConfig(){
  try{
    const j=await api('/api/config/email');
    const e=j.email||{};
    let prov=e.provider||'tempmailer';
    if(prov==='inboxkitten') prov='tempmailer';
    document.getElementById('email_provider').value=prov;
    document.getElementById('email_failover').checked=!!e.email_failover;
    document.getElementById('tempmailer_domain').value=e.tempmailer_domain||'';
    document.getElementById('custom_api_base').value=e.custom_api_base||'';
    document.getElementById('custom_api_key').value=e.custom_api_key||'';
    document.getElementById('custom_auth_mode').value=e.custom_auth_mode||'x-admin-auth';
    document.getElementById('custom_domain').value=e.custom_domain||'';
    document.getElementById('custom_path_accounts').value=e.custom_path_accounts||'/admin/new_address';
    document.getElementById('custom_path_messages').value=e.custom_path_messages||'/api/mails';
    document.getElementById('custom_path_token').value=e.custom_path_token||'/api/token';
    document.getElementById('email_hint').textContent=e.hint||'';
    onEmailProviderChange();
  }catch(err){
    document.getElementById('email_hint').textContent='加载邮箱配置失败: '+err.message;
  }
}
async function saveEmailConfig(){
  const body={
    provider: document.getElementById('email_provider').value,
    email_failover: document.getElementById('email_failover').checked,
    tempmailer_domain: document.getElementById('tempmailer_domain').value.trim(),
    custom_api_base: document.getElementById('custom_api_base').value.trim(),
    custom_api_key: document.getElementById('custom_api_key').value,
    custom_auth_mode: document.getElementById('custom_auth_mode').value,
    custom_domain: document.getElementById('custom_domain').value.trim(),
    custom_path_accounts: document.getElementById('custom_path_accounts').value.trim(),
    custom_path_messages: document.getElementById('custom_path_messages').value.trim(),
    custom_path_token: document.getElementById('custom_path_token').value.trim(),
  };
  try{
    const j=await api('/api/config/email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(j.message||'邮箱设置已保存');
    if(j.email){
      document.getElementById('email_hint').textContent='已保存 · 当前: '+(j.email.provider||'')+(j.email.custom_api_base?(' · '+j.email.custom_api_base):'');
    }
  }catch(e){toast('保存失败: '+e.message)}
}
function toggleSelectAllFiles(on){
  const boxes=document.querySelectorAll('.chk-file');
  boxes.forEach(b=>{ b.checked=!!on; });
  const all=document.getElementById('chk_all_files');
  if(all) all.checked=!!on;
}
async function deleteSelectedFiles(){
  const files=[...document.querySelectorAll('.chk-file:checked')].map(b=>b.value);
  if(!files.length){
    toast('请先勾选要删除的账号文件');
    return;
  }
  if(!confirm('确认删除选中的 '+files.length+' 个账号文件？\n删除后无法恢复，下载 SSO 时也不会再包含它们。')){
    return;
  }
  try{
    const j=await api('/api/accounts/delete',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({files})
    });
    toast(j.message||('已删除 '+((j.deleted||[]).length)+' 个文件'));
    setTimeout(()=>location.reload(), 500);
  }catch(e){toast('删除失败: '+e.message)}
}
async function startJob(){
  const count=parseInt(document.getElementById('count').value||'1',10);
  try{
    // auto-save email settings before start
    try{ await saveEmailConfig(); }catch(e){}
    const j=await api('/api/job/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({count})});
    toast(j.message||'已启动');
    poll();
  }catch(e){toast('启动失败: '+e.message)}
}
async function stopJob(){
  try{
    const j=await api('/api/job/stop',{method:'POST'});
    toast(j.message||'已停止');
  }catch(e){toast('停止失败: '+e.message)}
}
async function backfillCpa(){
  try{
    const j=await api('/api/cpa/backfill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:200})});
    toast(j.message||('已入队 '+j.queued));
    poll();
  }catch(e){toast('补转失败: '+e.message)}
}
let lastLogLen=0;
async function poll(){
  try{
    const j=await api('/api/job/status');
    const st=j.job||{};
    const cpa=j.cpa||{};
    document.getElementById('st_status').textContent=st.status||'idle';
    document.getElementById('st_dot').className='dot'+(st.running?' run':'');
    document.getElementById('st_sf').textContent=`${st.success||0} / ${st.fail||0}`;
    document.getElementById('btn_start').disabled=!!st.running;
    if(document.getElementById('st_cpa_ok')){
      document.getElementById('st_cpa_ok').textContent=String(cpa.files||0);
    }
    if(document.getElementById('st_cpa_q')){
      document.getElementById('st_cpa_q').textContent=
        `${cpa.pending||0}待 / ${cpa.ok||0}成 / ${cpa.fail||0}败`;
    }
    if(document.getElementById('cpa_hint')){
      const core = cpa.core_ok ? 'core就绪' : ('core失败: '+(cpa.core_error||''));
      const last = cpa.last_ok_email ? (' · 最近OK: '+cpa.last_ok_email) : '';
      const err = cpa.last_error ? (' · 最近错: '+cpa.last_error) : '';
      document.getElementById('cpa_hint').textContent =
        `代理走本机 Clash · 自动CPA: ${cpa.enabled?'开':'关'} · ${core} · 文件 ${cpa.files||0}${last}${err}`;
    }
    const box=document.getElementById('logbox');
    const logs=j.logs||[];
    if(logs.length!==lastLogLen){
      box.textContent=logs.join('\n');
      box.scrollTop=box.scrollHeight;
      lastLogLen=logs.length;
    }
  }catch(e){}
}
loadEmailConfig();
poll();
setInterval(poll, 2000);
</script>
</body></html>
"""

PREVIEW_HTML = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>预览 {{ name }}</title>
<style>body{margin:0;font-family:system-ui;background:#0f1115;color:#eef2fb}.wrap{max-width:1000px;margin:0 auto;padding:20px}
a{color:#6ea8fe}pre{background:#171c27;border:1px solid #2a3344;border-radius:12px;padding:16px;overflow:auto;white-space:pre-wrap;word-break:break-all}</style>
</head><body><div class="wrap">
<p><a href="/">← 返回</a> · <a href="/download/{{ name }}">下载</a></p>
<h1 style="font-size:18px">{{ name }}</h1>
<pre>{{ content }}</pre>
</div></body></html>
"""


# --------------- routes ---------------
@app.get("/login")
def login():
    if session.get("ok"):
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=None)


@app.post("/login")
def login_post():
    if request.form.get("password") == PANEL_PASSWORD:
        session["ok"] = True
        return redirect(request.args.get("next") or url_for("index"))
    return render_template_string(LOGIN_HTML, error="密码错误"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    need = require_login()
    if need:
        return need
    files_meta = []
    total = 0
    for p in list_account_files():
        lines = read_account_lines(p)
        total += len(lines)
        files_meta.append(
            {
                "name": p.name,
                "count": len(lines),
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return render_template_string(
        INDEX_HTML,
        base_dir=str(BASE_DIR),
        files=files_meta,
        file_count=len(files_meta),
        account_count=total,
        cpa_files=len(list_cpa_files()),
    )


def safe_name(name: str) -> Optional[Path]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not re.fullmatch(r"accounts_[\w.-]+\.txt", name):
        return None
    path = (BASE_DIR / name).resolve()
    if path.parent != BASE_DIR or not path.exists():
        return None
    return path


@app.get("/preview/<name>")
def preview_file(name: str):
    need = require_login()
    if need:
        return need
    path = safe_name(name)
    if not path:
        return "文件不存在", 404
    return render_template_string(
        PREVIEW_HTML,
        name=path.name,
        content=path.read_text(encoding="utf-8", errors="replace"),
    )


@app.get("/download/<name>")
def download_file(name: str):
    need = require_login()
    if need:
        return need
    path = safe_name(name)
    if not path:
        return "文件不存在", 404
    return send_file(
        path,
        as_attachment=True,
        download_name=path.name,
        mimetype="text/plain; charset=utf-8",
    )


def _merged_sso_txt() -> str:
    seen = set()
    lines = []
    for _, line in collect_all_accounts():
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


@app.get("/download/sso.txt")
def download_sso_txt():
    """主接口 1：全部 SSO，格式 email----password----sso"""
    need = require_login()
    if need:
        return need
    body = _merged_sso_txt()
    fname = f"sso_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/merged.txt")
def download_merged():
    """兼容旧链接 → 同 SSO txt"""
    return download_sso_txt()


@app.get("/download/all.zip")
def download_zip():
    need = require_login()
    if need:
        return need
    buf = io.BytesIO()
    files = list_account_files()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not files:
            zf.writestr("README.txt", "暂无 accounts_*.txt\n")
        for p in files:
            zf.write(p, arcname=p.name)
        seen = set()
        merged = []
        for _, line in collect_all_accounts():
            if line not in seen:
                seen.add(line)
                merged.append(line)
        zf.writestr(
            "accounts_merged_all.txt",
            "\n".join(merged) + ("\n" if merged else ""),
        )
    buf.seek(0)
    fname = f"accounts_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/accounts.json")
def download_accounts_json():
    """All accounts as one JSON array (email/password/sso)."""
    need = require_login()
    if need:
        return need
    accounts = unique_accounts()
    body = json.dumps(accounts, ensure_ascii=False, indent=2) + "\n"
    fname = f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/cpa.zip")
def download_cpa_zip():
    """主接口 2：已自动 OAuth 转换的真 CPA JSON（auth_kind=oauth）。"""
    need = require_login()
    if need:
        return need
    files = list_cpa_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → 真 CPA (CLIProxyAPI) JSON\n"
            "====================================\n\n"
            "1) 每个 xai-*.json 是 OAuth 凭证（access_token + refresh_token）。\n"
            "2) auth_kind=oauth，可直接放进 CLIProxyAPI auth-dir。\n"
            "3) 由注册成功后的 web SSO 自动换票生成。\n"
            "4) all.json 为全部账号数组；failed.jsonl 为转换失败记录（若有）。\n"
            "5) 若 zip 为空：先注册，或点「补转未转换 CPA」。\n"
        )
        zf.writestr("README.txt", readme)
        all_entries = []
        for i, p in enumerate(files, 1):
            try:
                raw = p.read_text(encoding="utf-8")
                obj = json.loads(raw)
                all_entries.append(obj)
                # keep original filename
                zf.writestr(p.name, raw if raw.endswith("\n") else raw + "\n")
            except Exception as e:
                zf.writestr(f"BAD-{p.name}.txt", str(e))
        zf.writestr(
            "all.json",
            json.dumps(all_entries, ensure_ascii=False, indent=2) + "\n",
        )
        if CPA_FAILED_PATH.exists():
            try:
                zf.write(CPA_FAILED_PATH, arcname="failed.jsonl")
            except Exception:
                pass
        if not files:
            zf.writestr(
                "EMPTY.txt",
                "暂无已转换的 CPA 文件。注册成功后会自动转换，或点击面板「补转未转换 CPA」。\n",
            )
    buf.seek(0)
    fname = f"cpa_oauth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/grok2api.json")
def download_grok2api_json():
    need = require_login()
    if need:
        return need
    body = (
        json.dumps(to_grok2api_pool(unique_accounts()), ensure_ascii=False, indent=2)
        + "\n"
    )
    fname = f"grok2api_pool_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/accounts")
def api_accounts():
    need = require_login()
    if need:
        return need
    data = []
    for source, line in collect_all_accounts():
        info = parse_line(line)
        info["source"] = source
        data.append(info)
    return jsonify(
        {
            "count": len(data),
            "files": [p.name for p in list_account_files()],
            "accounts": data,
        }
    )


@app.get("/api/nodes")
def api_nodes():
    need = require_login()
    if need:
        return need
    return jsonify(clash_list_nodes())


@app.post("/api/nodes/select")
def api_nodes_select():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    node = str(data.get("node") or "").strip()
    if not node:
        return jsonify({"ok": False, "error": "node required"}), 400
    ok, msg = clash_set_node(node)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg, "exit": clash_exit_ip()})


@app.post("/api/accounts/delete")
def api_accounts_delete():
    """Delete selected accounts_*.txt files (after user downloaded them)."""
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    names = data.get("files") or data.get("names") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names:
        return jsonify({"ok": False, "error": "files required"}), 400

    deleted = []
    missing = []
    errors = []
    for name in names:
        name = str(name or "").strip()
        path = safe_name(name)
        if not path:
            missing.append(name)
            continue
        try:
            path.unlink()
            deleted.append(path.name)
            log_line(f"[*] 已删除账号文件: {path.name}")
        except Exception as e:
            errors.append(f"{name}: {e}")

    if not deleted and errors:
        return jsonify({"ok": False, "error": "; ".join(errors)}), 400
    return jsonify(
        {
            "ok": True,
            "deleted": deleted,
            "missing": missing,
            "errors": errors,
            "message": f"已删除 {len(deleted)} 个文件"
            + (f"，跳过 {len(missing)}" if missing else ""),
        }
    )


@app.get("/api/config/email")
def api_get_email_config():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "email": email_config_public()})


@app.post("/api/config/email")
def api_set_email_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        email = apply_email_config_from_ui(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "message": "邮箱设置已保存", "email": email})


@app.get("/api/job/status")
def api_job_status():
    need = require_login()
    if need:
        return need
    with _job_lock:
        job = dict(_job)
    return jsonify({"ok": True, "job": job, "logs": list(_logs), "cpa": cpa_stats()})


@app.get("/api/cpa/status")
def api_cpa_status():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "cpa": cpa_stats()})


@app.post("/api/cpa/backfill")
def api_cpa_backfill():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        limit = int(data.get("limit") or 200)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))
    if not _CPA_CORE_OK:
        return jsonify({"ok": False, "error": f"core unavailable: {_CPA_CORE_ERR}"}), 500
    n = enqueue_missing_accounts(limit=limit)
    log_line(f"[CPA] 手动补转入队: {n}")
    return jsonify({"ok": True, "queued": n, "message": f"已入队 {n} 个待转换 SSO"})


@app.post("/api/job/start")
def api_job_start():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        count = int(data.get("count") or 1)
    except Exception:
        count = 1
    ok, msg = start_job(count)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


@app.post("/api/job/stop")
def api_job_stop():
    need = require_login()
    if need:
        return need
    ok, msg = stop_job()
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "base_dir": str(BASE_DIR),
            "files": len(list_account_files()),
            "running": bool(_job.get("running")),
            "cpa": cpa_stats(),
        }
    )


# start background CPA worker when module loads (systemd imports/runs this file)
start_cpa_worker()


if __name__ == "__main__":
    print(f"Grok Register Panel -> http://0.0.0.0:{PORT}")
    print(f"CPA auto-convert dir -> {CPA_DIR} core={_CPA_CORE_OK}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
