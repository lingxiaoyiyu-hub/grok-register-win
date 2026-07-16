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

# Ensure stdout/stderr use UTF-8 on Windows (default is GBK/CP936)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
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
# 面板默认不设登录密码（本机 127.0.0.1）。若需开启：PANEL_AUTH=1 且 PANEL_PASSWORD=xxx
PANEL_AUTH = os.environ.get("PANEL_AUTH", "0").strip() not in ("0", "false", "False", "no", "")
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
# Hard wall-clock per register round (one account). Stuck process is killed, next round starts.
DEFAULT_ROUND_TIMEOUT_SEC = 300
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
        build_sub2_payload,
        cpa_to_sub2_account,
        convert_one,
        normalize_sso,
        safe_filename as cpa_safe_filename,
        sso_fingerprint,
    )

    _CPA_CORE_OK = True
    _CPA_CORE_ERR = ""
except Exception as _e:  # pragma: no cover
    convert_one = None  # type: ignore
    build_sub2_payload = None  # type: ignore
    cpa_to_sub2_account = None  # type: ignore
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


# 日志过滤：只保留关键信息，屏蔽第三方库噪音
_LOG_NOISE_PATTERNS = re.compile(
    r"(?i)"
    r"(<html|<!doctype|<div|<script|<svg|<path\b)"          # HTML 片段
    r"|(playwright|drissionpage|camoufox|selenium|urllib3)"  # 第三方库调试
    r"|(connection\.(reusable|pool)|starting new (http|https))"  # urllib3 连接日志
    r"|(\bDEBUG\b|\bTRACE\b)"                                # 调试级别
    r"|(node:|child_process|events\.js|node_modules)"        # Node.js 内部
    r"|(pip\s|Downloading\s|Installing collected)"           # pip 安装
)
_LOG_KEY_PREFIXES = ("[*]", "[+]", "[-]", "[!]", "[Debug]", "[i]", "[OK]", "[ERR]")
_LOG_KEY_KEYWORDS = (
    "注册成功", "注册失败", "任务结束", "任务异常", "浏览器已启动", "开始注册",
    "验证码", "邮箱", "NSFW", "CPA", "SSO", "OAuth", "账号", "停止", "清理",
    "成功账号", "当前统计", "保存", "失败", "成功", "启动", "结束",
)
# 噪音行模式（即使是 [*] 前缀也过滤）：Cloudflare 轮询、GC 回收、网络模式重复
_LOG_NOISE_LINES = re.compile(
    r"(?i)"
    r"(等待\s*Cloudflare\s*人机验证)"           # Cloudflare 轮询刷屏
    r"|(Cloudflare\s*token\s*为空.*继续检测)"    # Cloudflare token 空轮询
    r"|(Python\s*GC\s*已回收)"                  # GC 回收细节
    r"|(浏览器网络模式)"                        # 每轮重复的网络模式
    r"|(浏览器已启动)(?!.*\b第\b)"              # 第 N 轮以外的「浏览器已启动」重复
    r"|(邮箱源\s*\w+\s*创建成功)"               # 与「已创建邮箱」重复
    r"|(已创建邮箱.*源=)"                       # 与「已创建 tempmailer 邮箱」重复
    r"|(资料已填:)"                             # 与「已填写注册资料并提交」重复
    r"|(Turnstile\s*二次复用完成)"              # 调试细节
    r"|(提交前仍卡住.*复用\s*Turnstile)"        # 调试细节
)


def _strip_inner_timestamp(line: str) -> str:
    """去掉子进程日志自带的时间戳，避免与 panel 的 log_line 时间戳重复。
    子进程原始行形如 "[02:30:39] [*] CLI 已加载配置" → 去掉前导时间戳 → "[*] CLI 已加载配置"
    这样 log_line 再加时间戳就只有一层 "[02:30:39] [*] CLI 已加载配置"。
    """
    # 标准形式：[HH:MM:SS] 后跟内容
    m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s+(.*)$", line)
    if m:
        return m.group(1)
    # 带 > 前缀形式：> [HH:MM:SS] [*] xxx
    m = re.match(r"^>\s*\[\d{2}:\d{2}:\d{2}\]\s+(.*)$", line)
    if m:
        return m.group(1)
    return line


def _truncate_line(line: str, max_len: int = 200) -> str:
    """超长行截断，保留前部关键信息。"""
    if len(line) <= max_len:
        return line
    return line[:max_len] + " …"


def _is_key_log(line: str) -> bool:
    """判断一行日志是否为关键信息，应保留显示。"""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    # 超长单行通常是 URL 或 HTML 片段
    if len(stripped) > 400:
        return False
    # 噪音模式直接过滤
    if _LOG_NOISE_PATTERNS.search(stripped):
        return False
    # 即使带 [*] 前缀的噪音行也过滤（Cloudflare 轮询、GC、网络模式重复）
    if _LOG_NOISE_LINES.search(stripped):
        return False
    # 注册脚本自己的业务日志（带 [*]/[+]/[-]/[!] 等前缀）
    for prefix in _LOG_KEY_PREFIXES:
        if prefix in stripped:
            return True
    # 关键业务关键词
    for kw in _LOG_KEY_KEYWORDS:
        if kw in stripped:
            return True
    # panel 自己写的 [!] 前缀日志（已带时间戳）
    if stripped.startswith("[") and "]" in stripped[:9]:
        rest = stripped[stripped.find("]") + 1 :].strip()
        if rest.startswith("[!]") or rest.startswith("[*]") or rest.startswith("[+]"):
            return True
    # 默认过滤（非关键噪音）
    return False


def require_login():
    """默认关闭鉴权；仅当 PANEL_AUTH=1 时校验 session。"""
    if not PANEL_AUTH:
        return None
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
        "hint": "",
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


def resolve_round_timeout_sec(cfg: Optional[dict] = None) -> int:
    """Per-account wall-clock timeout (seconds). Default 300; clamp 60..3600."""
    for key in ("ROUND_TIMEOUT_SEC", "ROUND_TIMEOUT"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            return max(60, min(int(float(raw)), 3600))
        except Exception:
            pass
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
        raw_cfg = c.get("round_timeout_sec", DEFAULT_ROUND_TIMEOUT_SEC)
        return max(60, min(int(float(raw_cfg)), 3600))
    except Exception:
        return DEFAULT_ROUND_TIMEOUT_SEC


def _terminate_register_proc(proc: Optional[subprocess.Popen]) -> None:
    """Kill register CLI and its browser children (Windows process tree)."""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    try:
        if os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _cleanup_browser_leftovers() -> None:
    """Best-effort cleanup of temp browser profiles (Windows/Linux)."""
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


def _run_one_round(round_no: int, total: int) -> bool:
    """Run register_count=1 once. Return True if success detected.

    Enforces round_timeout_sec (default 300s): if the CLI hangs (Turnstile /
    proxy / browser dead), kill the process tree and let job_worker start the
    next account instead of blocking forever.
    """
    global _proc
    cfg = load_config()
    # 面板每轮强制 register_count=1（job_worker 自己控轮数），但不要永久改坏用户配置
    cfg_run = dict(cfg)
    cfg_run["register_count"] = 1
    cfg_run["proxy"] = resolve_proxy_url()
    global PROXY_URL
    PROXY_URL = cfg_run["proxy"]
    os.environ["GROK_PROXY"] = PROXY_URL
    cfg_run.setdefault("email_provider", "tempmailer")
    engine = str(cfg_run.get("browser_engine") or "chromium").strip().lower()
    if engine in ("camoufox", "firefox", "headless", "cfox"):
        engine = "camoufox"
    else:
        engine = "chromium"
    cfg_run["browser_engine"] = engine
    # 只把代理/引擎写回；register_count 保持用户原值
    try:
        cfg_save = load_config()
        cfg_save["proxy"] = cfg_run["proxy"]
        cfg_save["browser_engine"] = engine
        if "round_timeout_sec" not in cfg_save:
            cfg_save["round_timeout_sec"] = DEFAULT_ROUND_TIMEOUT_SEC
        save_config(cfg_save)
        cfg = cfg_save
    except Exception:
        cfg = cfg_run

    round_timeout = resolve_round_timeout_sec(cfg)
    # 子进程强制单账号；用环境变量覆盖，避免改坏 config.json 里的 register_count
    os.environ["GROK_REGISTER_COUNT"] = "1"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["GROK_BROWSER_ENGINE"] = engine
    env["ROUND_TIMEOUT_SEC"] = str(round_timeout)
    env["GROK_REGISTER_COUNT"] = "1"
    # Windows / local: use system Chrome/Edge; allow override (chromium engine only)
    if engine == "chromium":
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
    engine_label = "Camoufox 无头" if engine == "camoufox" else "Chromium 有头"
    log_line(
        f"[*] proxy={PROXY_URL} engine={engine_label} python={VENV_PYTHON} "
        f"round_timeout={round_timeout}s"
    )
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
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as e:
        log_line(f"[!] 启动失败: {e}")
        return False

    with _job_lock:
        _job["pid"] = _proc.pid
        _job["round_timeout_sec"] = round_timeout
        _job["round_deadline"] = time.time() + round_timeout

    # send start
    try:
        assert _proc.stdin is not None
        _proc.stdin.write("start\n")
        _proc.stdin.flush()
    except Exception as e:
        log_line(f"[!] 写入 start 失败: {e}")

    success = False
    failed = False
    timed_out = False
    stopped = False
    line_q: "queue.Queue[Optional[str]]" = queue.Queue()

    def _stdout_reader() -> None:
        try:
            assert _proc is not None and _proc.stdout is not None
            for raw in _proc.stdout:
                line_q.put(raw)
        except Exception:
            pass
        finally:
            line_q.put(None)

    reader = threading.Thread(target=_stdout_reader, name=f"round-{round_no}-stdout", daemon=True)
    reader.start()

    deadline = time.time() + round_timeout
    while True:
        if _job.get("stop"):
            stopped = True
            log_line("[!] 收到停止指令，终止当前轮")
            _terminate_register_proc(_proc)
            break

        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            log_line(
                f"[!] 第 {round_no} 轮超时（{round_timeout}s），终止进程并进入下一轮"
            )
            with _job_lock:
                _job["last_error"] = f"round {round_no} timeout after {round_timeout}s"
            _terminate_register_proc(_proc)
            break

        try:
            raw = line_q.get(timeout=min(1.0, max(0.05, remaining)))
        except queue.Empty:
            if _proc.poll() is not None:
                # process exited; drain residual lines briefly
                drain_deadline = time.time() + 1.0
                while time.time() < drain_deadline:
                    try:
                        raw = line_q.get(timeout=0.1)
                    except queue.Empty:
                        break
                    if raw is None:
                        break
                    line = raw.rstrip("\n")
                    if not line:
                        continue
                    if _is_key_log(line):
                        log_line(_truncate_line(_strip_inner_timestamp(line)))
                    if "注册成功" in line or "[+] 注册成功" in line:
                        success = True
                    if "注册失败" in line or "[-] 注册失败" in line:
                        failed = True
                break
            continue

        if raw is None:
            break

        line = raw.rstrip("\n")
        if not line:
            continue
        # 只有关键日志才写入面板显示，但状态检测仍基于原始内容
        if _is_key_log(line):
            log_line(_truncate_line(_strip_inner_timestamp(line)))
        if "注册成功" in line or "[+] 注册成功" in line:
            success = True
        if "注册失败" in line or "[-] 注册失败" in line:
            failed = True
        if "任务结束" in line and ("成功" in line or "失败" in line):
            # final summary line often has both
            pass

    if _proc is not None and _proc.poll() is None:
        _terminate_register_proc(_proc)
    try:
        if _proc is not None:
            _proc.wait(timeout=15)
    except Exception:
        _terminate_register_proc(_proc)

    with _job_lock:
        _job["pid"] = None
        _job.pop("round_deadline", None)
    _proc = None
    _cleanup_browser_leftovers()

    if stopped:
        return False
    # 已打出「注册成功」后若在 NSFW/关浏览器阶段卡住被硬超时杀掉，账号其实已可用
    if success:
        if timed_out:
            log_line(
                f"[!] 第 {round_no} 轮在成功后超时被终止，仍记为成功（账号文件可能已写入）"
            )
        return True
    if timed_out:
        log_line(f"[-] 第 {round_no} 轮因超时记为失败")
        return False
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

            # 无论本轮判定成功/失败，都扫一遍新账号文件：
            # 避免「已写入 accounts 但日志未刷出/成功后硬超时」漏掉 CPA 转换
            queued = 0
            if AUTO_CPA:
                time.sleep(0.8)
                queued = enqueue_new_accounts(before_lines)
                if queued:
                    log_line(f"[CPA] 本轮新账号入队转换: {queued}")
                elif ok:
                    queued2 = enqueue_missing_accounts(limit=3)
                    if queued2:
                        log_line(f"[CPA] 未匹配到新行，补队最近未转换: {queued2}")
                        queued = queued2
                    else:
                        log_line("[CPA] 本轮未发现可转换的新 SSO（可能文件未写出）")

            # 日志没成功但文件里多了账号 → 也算成功
            if not ok and queued > 0:
                ok = True
                log_line(f"[+] 第 {i} 轮日志未显示成功，但检测到 {queued} 个新账号，记为成功")

            if ok:
                with _job_lock:
                    _job["success"] = int(_job.get("success") or 0) + 1
                log_line(f"[+] 第 {i} 轮成功（累计成功 {_job['success']}）")
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
    _terminate_register_proc(p)
    _cleanup_browser_leftovers()
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
    :root{--bg:#0b0e14;--card:#141a26;--fg:#eef2fb;--muted:#8b97b0;--line:#222b3d;--accent:#6ea8fe;--accent2:#4f8cff}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
      background:radial-gradient(1200px 600px at 20% -10%,#1a2540 0%,transparent 55%),radial-gradient(900px 500px at 80% 100%,#1a1f3a 0%,transparent 50%),var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
    .card{width:min(420px,92vw);background:var(--card);border:1px solid var(--line);border-radius:18px;padding:32px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .brand{display:flex;align-items:center;gap:12px;margin-bottom:6px}
    .logo{width:40px;height:40px;border-radius:11px;background:#000;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;color:#fff;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.35);letter-spacing:-1px}
    h1{margin:0;font-size:22px;font-weight:700} p{margin:6px 0 22px;color:var(--muted);font-size:13.5px}
    input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0f131c;color:var(--fg);font-size:14px;font-family:inherit;transition:border-color .15s}
    input:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.15)}
    button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;font-weight:600;font-size:14px;cursor:pointer;transition:box-shadow .15s}
    button:hover{box-shadow:0 6px 18px rgba(79,140,255,.45)}
    .err{color:#ff8f8f;margin-top:10px;font-size:13px}
  </style>
</head>
<body>
<form class="card" method="post">
  <div class="brand"><div class="logo">G</div><h1>Grok Register</h1></div>
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
      --bg:#0b0e14;--bg2:#0f131c;--card:#141a26;--card2:#1a2130;--fg:#eef2fb;--muted:#8b97b0;--muted2:#6b7793;
      --accent:#6ea8fe;--accent2:#4f8cff;--ok:#3dd68c;--bad:#ff7b7b;--warn:#ffb454;
      --line:#222b3d;--line2:#2c3650;--chip:#1c2434;--chip2:#222c40;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:
      radial-gradient(1200px 600px at 12% -18%,#1a2540 0%,transparent 55%),
      radial-gradient(900px 500px at 92% 8%,#1a1f3a 0%,transparent 50%),
      var(--bg);color:var(--fg);min-height:100vh;-webkit-font-smoothing:antialiased}
    .wrap{max-width:1200px;margin:0 auto;padding:24px 16px 56px}
    header{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid var(--line)}
    .brand{display:flex;align-items:center;gap:14px}
    .logo{width:42px;height:42px;border-radius:12px;background:#000;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900;color:#fff;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.35);letter-spacing:-1px}
    h1{margin:0;font-size:22px;font-weight:700;letter-spacing:.3px} .sub{color:var(--muted);font-size:12.5px;margin-top:3px}
    .actions{display:flex;flex-wrap:wrap;gap:10px}
    a.btn,button.btn{border:1px solid var(--line2);background:var(--chip);color:var(--fg);padding:10px 14px;border-radius:10px;text-decoration:none;font-size:13px;cursor:pointer;transition:all .15s ease;display:inline-flex;align-items:center;gap:6px}
    a.btn:hover,button.btn:hover{background:var(--chip2);border-color:var(--accent);transform:translateY(-1px)}
    a.btn:active,button.btn:active{transform:translateY(0)}
    a.btn.primary,button.btn.primary{background:linear-gradient(135deg,var(--accent2),var(--accent));border-color:transparent;color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(79,140,255,.3)}
    a.btn.primary:hover,button.btn.primary:hover{box-shadow:0 6px 18px rgba(79,140,255,.45)}
    a.btn.ok,button.btn.ok{background:linear-gradient(135deg,#1f9d63,#3dd68c);border:0;color:#042;font-weight:600;box-shadow:0 4px 12px rgba(61,214,140,.25)}
    a.btn.ok:hover,button.btn.ok:hover{box-shadow:0 6px 18px rgba(61,214,140,.4)}
    a.btn.sub2,button.btn.sub2{background:linear-gradient(135deg,#6d28d9,#a78bfa);border:0;color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(167,139,250,.28)}
    a.btn.sub2:hover,button.btn.sub2:hover{box-shadow:0 6px 18px rgba(167,139,250,.45)}
    a.btn.danger,button.btn.danger{background:#2a1717;border-color:#5a2b2b;color:#ffb4b4}
    a.btn.danger:hover,button.btn.danger:hover{background:#381c1c}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0 20px}
    .stat{background:linear-gradient(180deg,var(--card) 0%,var(--card2) 100%);border:1px solid var(--line);border-radius:14px;padding:14px 16px;position:relative;overflow:hidden;transition:border-color .15s}
    .stat:hover{border-color:var(--accent)}
    .stat::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),transparent);opacity:.7}
    .stat .k{color:var(--muted2);font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
    .stat .v{font-size:22px;font-weight:700;margin-top:6px;color:var(--fg)}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 4px 16px rgba(0,0,0,.15)}
    .card h2{margin:0 0 14px;font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
    .card h2::before{content:"";width:3px;height:14px;background:linear-gradient(180deg,var(--accent),var(--accent2));border-radius:2px}
    .row{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
    label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}
    input,select{background:var(--bg2);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:10px 12px;min-width:150px;font-size:13px;transition:border-color .15s;font-family:inherit}
    input:focus,select:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.15)}
    table{width:100%;border-collapse:collapse}
    th,td{padding:11px 14px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
    th{color:var(--muted);background:var(--bg2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
    tbody tr{transition:background .12s}
    tbody tr:hover{background:rgba(110,168,254,.04)}
    .mono{font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;word-break:break-all;font-size:12.5px}
    .muted{color:var(--muted)} .tag{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--chip);color:var(--accent);font-size:12px;font-weight:500}
    #logbox{height:340px;overflow:auto;background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:14px;font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;font-size:12.5px;line-height:1.5;white-space:pre-wrap;color:var(--muted)}
    #logbox::-webkit-scrollbar{width:8px}
    #logbox::-webkit-scrollbar-thumb{background:var(--line2);border-radius:4px}
    .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;background:#555;vertical-align:middle}
    .dot.run{background:var(--ok);box-shadow:0 0 10px var(--ok);animation:pulse 1.5s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
    .toast{position:fixed;right:20px;bottom:20px;background:var(--card2);border:1px solid var(--line2);padding:12px 16px;border-radius:10px;display:none;z-index:9;box-shadow:0 8px 24px rgba(0,0,0,.4);font-size:13px}
    code{background:var(--chip);padding:2px 6px;border-radius:4px;font-size:12px;color:var(--accent)}
    @media(max-width:800px){ th:nth-child(3),td:nth-child(3){display:none} .row{flex-direction:column;align-items:stretch} input,select{min-width:0} }
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">G</div>
      <div>
        <h1>Grok Register</h1>
        <div class="sub">{{ base_dir }} · 代理走本机 Clash（Clash Verge 默认 7897）</div>
      </div>
    </div>
    <div class="actions">
      <a class="btn primary" href="/download/sso.txt" title="email----password----sso">⬇ 下载 SSO (TXT)</a>
      <a class="btn ok" href="/download/cpa.zip" title="CPA OAuth JSON（CLIProxyAPI 可用）">⬇ 下载 CPA (JSON)</a>
      <a class="btn sub2" href="/download/sub2.zip" title="Sub2API 官方导入包 type=sub2api-data：单账号 JSON + all 合集">⬇ 下载 Sub2 (JSON)</a>
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
      <label>浏览器引擎
        <select id="browser_engine" onchange="saveBrowserEngine()">
          <option value="chromium">Chromium 有头（默认）</option>
          <option value="camoufox">Camoufox 无头（反检测 Firefox）</option>
        </select>
      </label>
      <button class="btn ok" id="btn_start" onclick="startJob()">▶ 开始注册</button>
      <button class="btn danger" id="btn_stop" onclick="stopJob()">■ 停止</button>
      <button class="btn" onclick="backfillCpa()" title="把尚未转成 CPA 的历史 SSO 入队">补转未转换 CPA</button>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px" id="cpa_hint">
      代理走本机 Clash（config.json 的 proxy，常见 7897）。节点在 Clash 里选。注册成功后自动转 CPA。
      Camoufox 首次使用会自动下载浏览器二进制。
    </div>
    <div class="muted" style="margin-top:8px;font-size:12px;line-height:1.55">
      提示：绝大多数注册失败来自网络环境，而非脚本本身。实测机场节点里<strong style="color:var(--ok);font-weight:600">日本</strong>更稳；
      新加坡 / 美国 / 德国成功率偏低。失败时请先在 Clash 换日本节点再试。
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
    <div class="muted" style="margin-top:10px;font-size:12px;display:none" id="email_hint"></div>
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
    setEmailHint(e.hint||'');
    onEmailProviderChange();
  }catch(err){
    setEmailHint('加载邮箱配置失败: '+err.message);
  }
}
function setEmailHint(text){
  const el=document.getElementById('email_hint');
  if(!el) return;
  const t=String(text||'').trim();
  el.textContent=t;
  el.style.display=t ? '' : 'none';
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
      setEmailHint('已保存 · 当前: '+(j.email.provider||'')+(j.email.custom_api_base?(' · '+j.email.custom_api_base):''));
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
async function loadBrowserEngine(){
  try{
    const j=await api('/api/config/browser');
    const eng=(j.browser_engine||'chromium').toLowerCase();
    document.getElementById('browser_engine').value=(eng==='camoufox'?'camoufox':'chromium');
  }catch(e){}
}
async function saveBrowserEngine(){
  const browser_engine=document.getElementById('browser_engine').value;
  try{
    const j=await api('/api/config/browser',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({browser_engine})});
    toast(j.message||('浏览器引擎: '+(j.browser_engine||browser_engine)));
  }catch(e){toast('保存浏览器引擎失败: '+e.message)}
}
async function startJob(){
  const count=parseInt(document.getElementById('count').value||'1',10);
  try{
    // auto-save email settings before start
    try{ await saveEmailConfig(); }catch(e){}
    try{ await saveBrowserEngine(); }catch(e){}
    const j=await api('/api/job/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({count, browser_engine: document.getElementById('browser_engine').value})});
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
loadBrowserEngine();
poll();
setInterval(poll, 2000);
</script>
</body></html>
"""

PREVIEW_HTML = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>预览 {{ name }}</title>
<style>
:root{--bg:#0b0e14;--card:#141a26;--fg:#eef2fb;--muted:#8b97b0;--line:#222b3d;--accent:#6ea8fe;--accent2:#4f8cff;--bg2:#0f131c}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  background:radial-gradient(1000px 500px at 12% -18%,#1a2540 0%,transparent 55%),var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 56px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;padding-bottom:16px;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:10px}
.top a{color:var(--accent);text-decoration:none;font-size:13.5px;padding:8px 14px;border:1px solid var(--line);border-radius:8px;transition:all .15s}
.top a:hover{border-color:var(--accent);background:rgba(110,168,254,.06)}
.brand{display:flex;align-items:center;gap:10px}
.logo{width:32px;height:32px;border-radius:9px;background:#000;display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:900;color:#fff;letter-spacing:-1px}
h1{margin:0;font-size:18px;font-weight:700}
pre{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:18px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;font-size:13px;line-height:1.5;color:var(--muted)}
pre::-webkit-scrollbar{width:8px}
pre::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
</style>
</head><body><div class="wrap">
<div class="top">
  <div class="brand"><div class="logo">G</div><h1>{{ name }}</h1></div>
  <div><a href="/">← 返回</a> · <a href="/download/{{ name }}">下载</a></div>
</div>
<pre>{{ content }}</pre>
</div></body></html>
"""


# --------------- routes ---------------
@app.get("/login")
def login():
    # 默认无密码：直接进面板
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    if session.get("ok"):
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=None)


@app.post("/login")
def login_post():
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    if request.form.get("password") == PANEL_PASSWORD:
        session["ok"] = True
        return redirect(request.args.get("next") or url_for("index"))
    return render_template_string(LOGIN_HTML, error="密码错误"), 401


@app.get("/logout")
def logout():
    session.clear()
    if not PANEL_AUTH:
        return redirect(url_for("index"))
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


def _load_cpa_entries_for_sub2() -> Tuple[List[dict], List[str]]:
    """Read existing CPA JSON files for Sub2 export. No re-OAuth."""
    entries: List[dict] = []
    name_hints: List[str] = []
    for p in list_cpa_files():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                continue
            entries.append(obj)
            # xai-email.json → email hint; strip optional -fingerprint suffix
            stem = p.stem
            hint = stem[4:] if stem.lower().startswith("xai-") else stem
            name_hints.append(hint or "")
        except Exception:
            continue
    return entries, name_hints


def _fallback_sub2_payload(cpa_entries: List[dict], name_hints: List[str]) -> dict:
    """If sso2cpa_core import failed, still build a minimal sub2api-data package."""
    accounts: List[dict] = []
    for i, cpa in enumerate(cpa_entries):
        if not isinstance(cpa, dict):
            continue
        access = str(cpa.get("access_token") or "").strip()
        refresh = str(cpa.get("refresh_token") or "").strip()
        if not access and not refresh:
            continue
        email = str(cpa.get("email") or "").strip()
        sub = str(cpa.get("sub") or "").strip()
        hint = name_hints[i] if i < len(name_hints) else ""
        name = hint or email or sub or "grok-oauth"
        expires_at = str(cpa.get("expires_at") or cpa.get("expired") or "").strip()
        if not expires_at:
            expires_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        creds = {
            "access_token": access,
            "expires_at": expires_at,
            "base_url": str(cpa.get("base_url") or "https://cli-chat-proxy.grok.com/v1").strip(),
        }
        if refresh:
            creds["refresh_token"] = refresh
        token_type = str(cpa.get("token_type") or "Bearer").strip()
        if token_type:
            creds["token_type"] = token_type
        for k in ("id_token", "email", "sub", "client_id", "scope"):
            v = str(cpa.get(k) or "").strip()
            if v:
                creds[k] = v
        accounts.append(
            {
                "name": name,
                "platform": "grok",
                "type": "oauth",
                "credentials": creds,
                "concurrency": 1,
                "priority": 50,
            }
        )
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }


def _build_sub2_accounts(
    cpa_entries: List[dict], name_hints: List[str]
) -> List[dict]:
    """Map CPA entries → Sub2 DataAccount list (no re-OAuth)."""
    if build_sub2_payload is not None:
        payload = build_sub2_payload(cpa_entries, name_hints=name_hints)
        return list(payload.get("accounts") or [])
    payload = _fallback_sub2_payload(cpa_entries, name_hints)
    return list(payload.get("accounts") or [])


def _sub2_package(accounts: List[dict]) -> dict:
    """Official Sub2API import wrapper around account list."""
    if build_sub2_payload is not None:
        # reuse core helper for type/version/exported_at; pass empty CPA list
        # then inject accounts (avoids re-mapping)
        base = build_sub2_payload([])
        base["accounts"] = accounts
        return base
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }


def _sub2_safe_arcname(name: str, used: Set[str]) -> str:
    """Unique zip member name: grok-{name}.json"""
    base = cpa_safe_filename(name or "grok-oauth")
    fname = f"grok-{base}.json"
    if fname not in used:
        used.add(fname)
        return fname
    i = 2
    while True:
        alt = f"grok-{base}-{i}.json"
        if alt not in used:
            used.add(alt)
            return alt
        i += 1


@app.get("/download/sub2.zip")
def download_sub2_zip():
    """主接口 3：Sub2API 官方导入包 ZIP（对齐 CPA zip 结构）。

    从已转换的 CPA JSON 现场映射，不重新注册/换票。

    zip 内容：
      README.txt
      grok-*.json     — 每个账号一份完整 sub2api-data（可单独导入）
      all.json        — 全部账号合集（推荐一键导入）
      EMPTY.txt       — 无账号时的说明
    """
    need = require_login()
    if need:
        return need
    cpa_entries, name_hints = _load_cpa_entries_for_sub2()
    accounts = _build_sub2_accounts(cpa_entries, name_hints)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → Sub2API 官方导入包 (sub2api-data)\n"
            "================================================\n\n"
            "1) all.json：全部账号合集，推荐直接导入 Sub2API。\n"
            "   管理后台 → 账号 → 导入数据 → 上传 all.json\n"
            "2) grok-*.json：每个账号一份完整 sub2api-data（也可单独导入）。\n"
            "3) type=sub2api-data / version=1 / platform=grok / type=oauth\n"
            "4) 由已转换的 CPA OAuth 凭证现场映射，不重新注册/换票。\n"
            "5) proxies 为空；导入后请在 Sub2API 里绑定分组/代理。\n"
            "6) 若 zip 为空：先注册，或点面板「补转未转换 CPA」。\n"
        )
        zf.writestr("README.txt", readme)

        used_names: Set[str] = set()
        for acc in accounts:
            try:
                single = _sub2_package([acc])
                raw = json.dumps(single, ensure_ascii=False, indent=2) + "\n"
                arc = _sub2_safe_arcname(str(acc.get("name") or ""), used_names)
                zf.writestr(arc, raw)
            except Exception as e:
                bad = _sub2_safe_arcname(
                    f"BAD-{acc.get('name') or 'unknown'}", used_names
                )
                zf.writestr(bad.replace(".json", ".txt"), str(e))

        all_pkg = _sub2_package(accounts)
        zf.writestr(
            "all.json",
            json.dumps(all_pkg, ensure_ascii=False, indent=2) + "\n",
        )

        if not accounts:
            zf.writestr(
                "EMPTY.txt",
                "暂无已转换账号。注册成功后会自动转 CPA，再点「下载 Sub2」；"
                "或先点面板「补转未转换 CPA」。\n",
            )

    buf.seek(0)
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/sub2.json")
def download_sub2_json():
    """兼容旧链接：返回 all 合集 JSON（等同 zip 内 all.json）。"""
    need = require_login()
    if need:
        return need
    cpa_entries, name_hints = _load_cpa_entries_for_sub2()
    accounts = _build_sub2_accounts(cpa_entries, name_hints)
    payload = _sub2_package(accounts)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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


def _normalize_browser_engine(value: str) -> str:
    eng = str(value or "").strip().lower()
    if eng in ("camoufox", "firefox", "headless", "cfox"):
        return "camoufox"
    return "chromium"


@app.get("/api/config/browser")
def api_get_browser_config():
    need = require_login()
    if need:
        return need
    cfg = load_config()
    return jsonify(
        {
            "ok": True,
            "browser_engine": _normalize_browser_engine(cfg.get("browser_engine") or "chromium"),
        }
    )


@app.post("/api/config/browser")
def api_set_browser_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    eng = _normalize_browser_engine(data.get("browser_engine") or "chromium")
    cfg = load_config()
    cfg["browser_engine"] = eng
    save_config(cfg)
    label = "Camoufox 无头" if eng == "camoufox" else "Chromium 有头"
    return jsonify(
        {
            "ok": True,
            "browser_engine": eng,
            "message": f"浏览器引擎已保存: {label}",
        }
    )


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
    if "browser_engine" in data:
        eng = _normalize_browser_engine(data.get("browser_engine"))
        cfg = load_config()
        cfg["browser_engine"] = eng
        save_config(cfg)
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
