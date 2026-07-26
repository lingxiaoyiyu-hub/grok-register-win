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
import shutil
import signal
import socket
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
from datetime import datetime, timezone
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
    render_template_string,
    request,
    send_file,
)

# Project root = parent of panel/ (Windows / portable layout)
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("GROK_REGISTER_DIR", str(_DEFAULT_ROOT))).resolve()
# 面板是纯本机工具，只监听回环地址，不提供登录功能。
# （旧版的 PANEL_AUTH/PANEL_PASSWORD/PANEL_SECRET 是假锁：签名密钥硬编码在源码里，
#  任何人都能伪造 session 直接进入，已移除。安全性改由「只绑回环 + 同源校验」保证。）
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8787"))
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
ARCHIVE_DIR = Path(
    os.environ.get("ARCHIVE_DIR", str(BASE_DIR / "data" / "archive"))
).resolve()
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
SSO2CPA_PATH = Path(
    os.environ.get("SSO2CPA_PATH", str(BASE_DIR / "lib"))
).resolve()
AUTO_CPA = os.environ.get("AUTO_CPA", "1").strip() not in ("0", "false", "False", "no")
CPA_DELAY = float(os.environ.get("CPA_DELAY", "1.0"))
# workspace mode: register (本机注册) | upload (上传 SSO 整批替换)
_workspace_mode = "register"
_workspace_batch = 0
_workspace_meta: Dict = {
    "mode": "register",
    "batch": 0,
    "source": "",
    "total": 0,
    "queued": 0,
    "updated_at": "",
}
# Hard wall-clock per register round (one account). Stuck process is killed, next round starts.
DEFAULT_ROUND_TIMEOUT_SEC = 300

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
        parse_uploaded_records,
        safe_filename as cpa_safe_filename,
        sso_fingerprint,
    )

    _CPA_CORE_OK = True
    _CPA_CORE_ERR = ""
except Exception as _e:  # pragma: no cover
    convert_one = None  # type: ignore
    build_sub2_payload = None  # type: ignore
    cpa_to_sub2_account = None  # type: ignore
    parse_uploaded_records = None  # type: ignore
    normalize_sso = lambda t: (t or "").strip()  # type: ignore
    cpa_safe_filename = lambda s: re.sub(r"[^\w.@+-]+", "_", s or "unknown")[:100]  # type: ignore
    sso_fingerprint = lambda s: hashlib.sha256((s or "").encode()).hexdigest()  # type: ignore
    _CPA_CORE_OK = False
    _CPA_CORE_ERR = str(_e)

app = Flask(__name__)


# --------------- 同源 / DNS-rebinding 防护 ---------------
# 面板监听在本机，但浏览器里的**任意网页**都能向 127.0.0.1 发请求。
# 没有防护时，随便逛一个网页就能删掉账号文件、启动任务、替换工作区
# （multipart 与 text/plain 都属于 CORS「简单请求」，不触发预检）。
# 两道闸：
#   1. Host 头白名单 —— 挡住 DNS rebinding（把恶意域名解析到 127.0.0.1）
#   2. 改状态的请求必须带同源 Origin/Referer —— 挡住跨站伪造请求
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _build_allowed_hosts() -> Set[str]:
    names = {"127.0.0.1", "localhost", "::1", "[::1]"}
    host = (HOST or "").strip()
    if host and host not in ("0.0.0.0", "::"):
        names.add(host)
    for extra in os.environ.get("PANEL_ALLOWED_HOSTS", "").split(","):
        extra = extra.strip()
        if extra:
            names.add(extra)
    out: Set[str] = set()
    for name in names:
        out.add(name.lower())
        out.add(f"{name}:{PORT}".lower())
    return out


ALLOWED_HOSTS = _build_allowed_hosts()


def _netloc_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.strip().lower()
    except Exception:
        return ""


@app.before_request
def _same_origin_guard():
    host = (request.host or "").strip().lower()
    if host not in ALLOWED_HOSTS:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"拒绝的 Host: {request.host}。面板只接受本机访问；"
                    f"如确需其它主机名，设置 PANEL_ALLOWED_HOSTS=<host> 后重启。",
                }
            ),
            403,
        )
    if request.method in _SAFE_METHODS:
        return None
    for header in ("Origin", "Referer"):
        raw = request.headers.get(header, "")
        if not raw:
            continue
        if _netloc_of(raw) == host:
            return None
        return (
            jsonify({"ok": False, "error": f"跨站请求被拒绝（{header}: {raw}）"}),
            403,
        )
    return (
        jsonify(
            {
                "ok": False,
                "error": "缺少 Origin/Referer，已按跨站请求拒绝。请在面板页面内操作。",
            }
        ),
        403,
    )


def json_body() -> dict:
    """只接受 Content-Type: application/json 的请求体。

    旧代码用 get_json(force=True)，等于连 text/plain 也照单全收 —— 那种请求
    在浏览器里属于「简单请求」，可以被任意网站跨站发出。去掉 force 后，
    跨站 JSON 请求必须先过 CORS 预检，而面板不返回任何 CORS 头。
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


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
_log_lock = threading.Lock()
_log_seq = 0  # 累计产生过的日志行数，用于前端增量拉取
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
# 工作区代次：上传换票 / 开始注册时 +1。
# 转换任务会带上入队时的代次，写盘前比对；代次对不上说明工作区已经被整批替换过，
# 这份结果属于上一批，直接丢弃。否则「归档清空目录」和「worker 正在写文件」
# 撞在一起时，旧批次的 CPA 会落进新工作区，混进下载包。
_cpa_generation = 0


def log_line(msg: str):
    global _log_seq
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        _logs.append(line)
        _log_seq += 1
    path = _job.get("log_path")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def reset_logs() -> None:
    global _log_seq
    with _log_lock:
        _logs.clear()
        _log_seq = 0


def logs_since(since: int) -> Tuple[List[str], int, bool]:
    """Return (new_lines, next_cursor, reset).

    /api/job/status is polled every 2s; returning the whole 2000-line buffer
    each time was ~400KB per poll and forced a full re-render of the log box.
    `reset=True` means the client must replace its buffer (job restarted, or
    it fell so far behind that the missing lines already rolled out).
    """
    with _log_lock:
        total = _log_seq
        buf = list(_logs)
    first = total - len(buf)  # absolute index of buf[0]
    # since<=0 也算 reset：此时客户端要么刚打开页面（日志框里还是占位文案），
    # 要么刚被重置，直接整块替换才不会把占位文本拼进日志。
    if since <= 0 or since < first or since > total:
        return buf, total, True
    return buf[since - first :], total, False


# 日志过滤：只保留关键信息，屏蔽第三方库噪音
# 注意：不要把业务日志里的 Camoufox/Playwright 字样当噪音误杀
_LOG_NOISE_PATTERNS = re.compile(
    r"(?i)"
    r"(<html|<!doctype|<div|<script|<svg|<path\b)"          # HTML 片段
    r"|(?:^|\s)(?:playwright|drissionpage|selenium|urllib3)[\s:.]"  # 库调试，不含业务 Camoufox
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
    "浏览器", "Camoufox", "Chromium", "硬超时", "下载", "就绪",
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
    # 即使带 [*] 前缀的噪音行也过滤（Cloudflare 轮询、GC、网络模式重复）
    if _LOG_NOISE_LINES.search(stripped):
        return False
    # 业务前缀优先保留（避免 “Camoufox/Playwright” 字样被整行误杀）
    for prefix in _LOG_KEY_PREFIXES:
        if prefix in stripped:
            return True
    # 噪音模式（无业务前缀时）
    if _LOG_NOISE_PATTERNS.search(stripped):
        return False
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


def active_identity_sets() -> Tuple[Set[str], Set[str]]:
    """Emails + SSO fingerprints currently visible on the panel."""
    emails: Set[str] = set()
    fps: Set[str] = set()
    for acc in unique_accounts():
        email = str(acc.get("email") or "").strip().lower()
        if email and email not in ("unknown",):
            emails.add(email)
        sso = normalize_sso(acc.get("sso") or "")
        if sso:
            fps.add(sso_fingerprint(sso))
    return emails, fps


def cpa_matches_active(obj: dict, emails: Set[str], fps: Set[str]) -> bool:
    if not isinstance(obj, dict):
        return False
    sso = normalize_sso(obj.get("sso") or "")
    if sso and sso_fingerprint(sso) in fps:
        return True
    email = str(obj.get("email") or "").strip().lower()
    if email and email in emails:
        return True
    return False


def list_active_cpa_files() -> List[Path]:
    """CPA files that belong to current panel accounts only."""
    emails, fps = active_identity_sets()
    if not emails and not fps:
        return []
    out: List[Path] = []
    for p in list_cpa_files():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if cpa_matches_active(obj, emails, fps):
            out.append(p)
    return out


def prune_orphan_cpa() -> int:
    """Delete CPA JSON not matching any current panel account."""
    emails, fps = active_identity_sets()
    removed = 0
    removed_fps: Set[str] = set()
    for p in list(list_cpa_files()):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
            continue
        keep = bool(emails or fps) and cpa_matches_active(obj, emails, fps)
        if keep:
            continue
        try:
            sso = normalize_sso((obj or {}).get("sso") or "")
            if sso:
                removed_fps.add(sso_fingerprint(sso))
            p.unlink()
            removed += 1
        except Exception:
            pass
    if removed_fps or removed:
        # drop index entries for removed fingerprints; rebuild lightly
        try:
            items: Dict[str, dict] = {}
            if CPA_INDEX_PATH.exists():
                data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("items"), dict):
                    items = {
                        k: v
                        for k, v in data["items"].items()
                        if k not in removed_fps
                    }
            # also drop index rows whose file is gone
            alive_files = {p.name for p in list_cpa_files()}
            items = {
                k: v
                for k, v in items.items()
                if not isinstance(v, dict)
                or not v.get("file")
                or v.get("file") in alive_files
            }
            CPA_INDEX_PATH.write_text(
                json.dumps(
                    {
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "items": items,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        with _cpa_lock:
            if removed_fps:
                _cpa_done.difference_update(removed_fps)
            # 只把被删掉的那部分从「累计成功数」里扣除。
            # 原来这里直接赋成当前文件数，把「累计转换成功」和「现存文件数」
            # 两个语义混成一个字段，面板上的「x成」会莫名跳变。
            _cpa_state["ok"] = max(0, int(_cpa_state.get("ok") or 0) - removed)
    return removed


def cpa_stats() -> dict:
    with _cpa_lock:
        st = dict(_cpa_state)
        done_n = len(_cpa_done)
    # UI / download count = current panel batch only
    files = list_active_cpa_files()
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
        generation = _cpa_generation
    _cpa_q.put(
        {
            "email": email or "",
            "sso": sso,
            "password": password or "",
            "source": source or "",
            "fp": fp,
            "force": force,
            "generation": generation,
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
        item_gen = item.get("generation", 0)
        with _cpa_lock:
            _cpa_state["running"] = True
            _cpa_state["pending"] = max(0, int(_cpa_state.get("pending") or 0) - 1)
            stale = item_gen != _cpa_generation
            if stale:
                _cpa_inflight.discard(fp)
        try:
            if stale:
                # 工作区在本任务排队之后被整批替换过，这份结果属于上一批，丢弃。
                log_line(f"[CPA] 跳过过期任务（工作区已替换）: {email or fp[:12]}")
                continue
            if convert_one is None:
                raise RuntimeError(f"core missing: {_CPA_CORE_ERR}")
            entry = convert_one(sso, email=email, proxy=PROXY_URL)
            # keep password if known (not required by CPA, useful for bookkeeping)
            if item.get("password") and not entry.get("password"):
                entry["password"] = item["password"]
            entry["_source"] = "grok-register-auto-cpa"
            entry["_source_file"] = item.get("source") or ""
            # convert_one 是一次网络往返，期间工作区可能已被整批替换。写盘前再确认一次。
            with _cpa_lock:
                stale = item_gen != _cpa_generation
                if stale:
                    _cpa_inflight.discard(fp)
            if stale:
                log_line(f"[CPA] 转换完成但工作区已替换，丢弃结果: {email or fp[:12]}")
                continue
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


def _drain_cpa_queue() -> int:
    """Drop pending convert jobs without processing them."""
    n = 0
    while True:
        try:
            item = _cpa_q.get_nowait()
        except queue.Empty:
            break
        if item is None:
            break
        n += 1
        try:
            _cpa_q.task_done()
        except Exception:
            pass
    with _cpa_lock:
        _cpa_inflight.clear()
        _cpa_state["pending"] = 0
        _cpa_state["running"] = False
    return n


def _archive_current_workspace(reason: str = "replace") -> Optional[Path]:
    """Move current accounts_*.txt + data/cpa into data/archive/<ts>/."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / f"{ts}_{reason}"
    moved_any = False
    try:
        dest.mkdir(parents=True, exist_ok=True)
        acc_dir = dest / "accounts"
        cpa_dir = dest / "cpa"
        acc_dir.mkdir(exist_ok=True)
        cpa_dir.mkdir(exist_ok=True)
        for p in list_account_files():
            try:
                shutil.move(str(p), str(acc_dir / p.name))
                moved_any = True
            except Exception as e:
                log_line(f"[WORKSPACE] archive account fail {p.name}: {e}")
        if CPA_DIR.exists():
            for p in CPA_DIR.iterdir():
                if not p.is_file():
                    continue
                try:
                    shutil.move(str(p), str(cpa_dir / p.name))
                    moved_any = True
                except Exception as e:
                    log_line(f"[WORKSPACE] archive cpa fail {p.name}: {e}")
        if moved_any:
            log_line(f"[WORKSPACE] 已归档旧数据 → {dest}")
            return dest
        # empty archive folder: remove
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
    except Exception as e:
        log_line(f"[WORKSPACE] archive error: {e}")
    return None


def bump_cpa_generation() -> int:
    """Invalidate every queued/in-flight convert job. Call before wiping the workspace."""
    global _cpa_generation
    with _cpa_lock:
        _cpa_generation += 1
        return _cpa_generation


def reset_workspace_for_upload(source_name: str, total: int) -> dict:
    """Whole-batch replace: archive old data, clear CPA state, enter upload mode."""
    global _workspace_mode, _workspace_batch, _workspace_meta
    # 先作废在途任务，再动文件；否则 worker 可能在归档之后把旧批次结果写回来
    bump_cpa_generation()
    drained = _drain_cpa_queue()
    archived = _archive_current_workspace("upload")
    with _cpa_lock:
        _cpa_done.clear()
        _cpa_inflight.clear()
        _cpa_state["pending"] = 0
        _cpa_state["ok"] = 0
        _cpa_state["fail"] = 0
        _cpa_state["last_error"] = ""
        _cpa_state["last_ok_email"] = ""
        _cpa_state["running"] = False
    # ensure empty cpa dir
    CPA_DIR.mkdir(parents=True, exist_ok=True)
    for leftover in list(CPA_DIR.glob("*")):
        if leftover.is_file():
            try:
                leftover.unlink()
            except Exception:
                pass
    _workspace_mode = "upload"
    _workspace_batch += 1
    _workspace_meta = {
        "mode": "upload",
        "batch": _workspace_batch,
        "source": source_name or "upload.txt",
        "total": int(total or 0),
        "queued": 0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "archived": str(archived) if archived else "",
        "drained": drained,
    }
    return dict(_workspace_meta)


def set_workspace_register_mode(reason: str = "register") -> None:
    """Switch UI mode marker back to register (does not delete upload batch)."""
    global _workspace_mode, _workspace_meta
    _workspace_mode = "register"
    _workspace_meta = {
        "mode": "register",
        "batch": _workspace_batch,
        "source": reason,
        "total": 0,
        "queued": 0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def workspace_stats() -> dict:
    meta = dict(_workspace_meta)
    meta["mode"] = _workspace_mode
    meta["account_count"] = len(unique_accounts())
    meta["cpa_files"] = len(list_cpa_files())
    return meta


def write_upload_accounts_file(records: List[dict], source_name: str = "") -> Path:
    """Write current batch as single accounts_upload_*.txt (email----password----sso)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BASE_DIR / f"accounts_upload_{ts}.txt"
    lines = []
    for rec in records:
        email = str(rec.get("email") or "").strip() or "unknown"
        password = str(rec.get("password") or "").strip()
        sso = normalize_sso(rec.get("sso") or "")
        if not sso:
            continue
        lines.append(f"{email}----{password}----{sso}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def parse_upload_file_bytes(raw: bytes, filename: str = "") -> List[dict]:
    """Parse uploaded SSO txt/json into {email,password,sso} records."""
    if parse_uploaded_records is None:
        raise RuntimeError(f"sso2cpa core unavailable: {_CPA_CORE_ERR}")
    base = parse_uploaded_records(raw, filename=filename or "")
    # recover password when line is email----password----sso
    text = raw.decode("utf-8", "replace")
    pass_map: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "----" in line:
            parts = line.split("----")
            if len(parts) >= 3:
                email = parts[0].strip()
                password = parts[1].strip()
                sso = normalize_sso(parts[-1])
                if sso:
                    pass_map[sso] = password
                    if email:
                        pass_map[f"e:{email.lower()}"] = password
    out = []
    for rec in base:
        sso = normalize_sso(rec.get("sso") or "")
        if not sso:
            continue
        email = str(rec.get("email") or "").strip()
        password = (
            str(rec.get("password") or "").strip()
            or pass_map.get(sso, "")
            or pass_map.get(f"e:{email.lower()}", "")
        )
        out.append({"email": email, "password": password, "sso": sso})
    return out


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
    """Email settings for panel UI (multi-provider dropdown)."""
    c = cfg if isinstance(cfg, dict) else load_config()
    provider = str(c.get("email_provider") or "cfworker").strip().lower()
    # cloudflare/custom/tempmailer are all aliases of cfworker (same protocol)
    # tempmail_lol/duckmail/gptmail: removed public providers (xAI 拒收), fall back to cfworker
    if provider in ("tempmailer", "inboxkitten", "inbox_kitten", "custom", "cloudflare",
                    "tempmail_lol", "duckmail", "gptmail"):
        provider = "cfworker"
    # alias yyds -> maliapi for UI
    if provider == "yyds":
        provider = "maliapi"

    choices = [
        {"id": "cfworker", "label": "CF Worker", "group": "自建"},
        {"id": "moemail", "label": "MoeMail", "group": "自建"},
        {"id": "freemail", "label": "Freemail", "group": "自建"},
        {"id": "opentrashmail", "label": "OpenTrashMail", "group": "自建"},
        {"id": "maliapi", "label": "MaliAPI", "group": "付费"},
        {"id": "luckmail", "label": "LuckMail", "group": "付费"},
        {"id": "skymail", "label": "SkyMail", "group": "付费"},
        {"id": "cloudmail", "label": "CloudMail", "group": "付费"},
        {"id": "laoudo", "label": "Laoudo", "group": "付费"},
    ]
    valid = {x["id"] for x in choices}
    if provider not in valid:
        provider = "cfworker"

    return {
        "provider": provider,
        "choices": choices,
        "email_failover": bool(c.get("email_failover", True)),
        # cfworker (cloudflare_temp_email protocol; cloudflare_*/defaultDomains kept as legacy aliases)
        "cfworker_api_url": str(c.get("cfworker_api_url") or c.get("cloudflare_api_base") or "").strip(),
        "cfworker_admin_token": str(c.get("cfworker_admin_token") or c.get("cloudflare_api_key") or "").strip(),
        "cfworker_domain": str(c.get("cfworker_domain") or c.get("defaultDomains") or "").strip(),
        "cfworker_custom_auth": str(c.get("cfworker_custom_auth") or "").strip(),
        "cfworker_subdomain": str(c.get("cfworker_subdomain") or "").strip(),
        # providers
        "moemail_api_url": str(c.get("moemail_api_url") or "https://sall.cc").strip(),
        "moemail_api_key": str(c.get("moemail_api_key") or "").strip(),
        "maliapi_base_url": str(c.get("maliapi_base_url") or "https://maliapi.215.im/v1").strip(),
        "maliapi_api_key": str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip(),
        "maliapi_domain": str(c.get("maliapi_domain") or "").strip(),
        "luckmail_base_url": str(c.get("luckmail_base_url") or "https://mails.luckyous.com/").strip(),
        "luckmail_api_key": str(c.get("luckmail_api_key") or "").strip(),
        "luckmail_project_code": str(c.get("luckmail_project_code") or "grok").strip(),
        "luckmail_domain": str(c.get("luckmail_domain") or "").strip(),
        "skymail_api_base": str(c.get("skymail_api_base") or "https://api.skymail.ink").strip(),
        "skymail_token": str(c.get("skymail_token") or "").strip(),
        "skymail_domain": str(c.get("skymail_domain") or "").strip(),
        "cloudmail_api_base": str(c.get("cloudmail_api_base") or "").strip(),
        "cloudmail_admin_email": str(c.get("cloudmail_admin_email") or "").strip(),
        "cloudmail_admin_password": str(c.get("cloudmail_admin_password") or "").strip(),
        "cloudmail_domain": str(c.get("cloudmail_domain") or "").strip(),
        "freemail_api_url": str(c.get("freemail_api_url") or "").strip(),
        "freemail_admin_token": str(c.get("freemail_admin_token") or "").strip(),
        "freemail_domain": str(c.get("freemail_domain") or "").strip(),
        "opentrashmail_api_url": str(c.get("opentrashmail_api_url") or "").strip(),
        "opentrashmail_domain": str(c.get("opentrashmail_domain") or "").strip(),
        "opentrashmail_password": str(c.get("opentrashmail_password") or "").strip(),
        "laoudo_auth": str(c.get("laoudo_auth") or "").strip(),
        "laoudo_email": str(c.get("laoudo_email") or "").strip(),
        "laoudo_account_id": str(c.get("laoudo_account_id") or "").strip(),
    }


def apply_email_config_from_ui(data: dict) -> dict:
    """Merge panel email form into config.json and return public view."""
    cfg = load_config()
    provider = str(data.get("provider") or "cfworker").strip().lower()
    # cloudflare/custom/tempmailer are all aliases of cfworker (same protocol)
    # tempmail_lol/duckmail/gptmail: removed public providers (xAI 拒收), fall back to cfworker
    if provider in ("tempmailer", "inboxkitten", "inbox_kitten", "custom", "cloudflare",
                    "tempmail_lol", "duckmail", "gptmail"):
        provider = "cfworker"
    if provider == "yyds":
        provider = "maliapi"

    valid = {
        "cfworker", "moemail",
        "maliapi", "luckmail", "skymail", "cloudmail", "freemail", "opentrashmail", "laoudo",
    }
    if provider not in valid:
        raise ValueError(f"不支持的邮箱源: {provider}")

    cfg["email_failover"] = bool(data.get("email_failover", True))
    cfg["email_provider"] = provider
    cfg["email_providers"] = [provider]

    def g(key, default=""):
        return str(data.get(key, cfg.get(key, default)) or default).strip()

    # cfworker fields (also sync to cloudflare_*/defaultDomains legacy keys for back-compat)
    cfg["cfworker_api_url"] = g("cfworker_api_url")
    cfg["cfworker_admin_token"] = g("cfworker_admin_token")
    cfg["cfworker_domain"] = g("cfworker_domain")
    cfg["cfworker_custom_auth"] = g("cfworker_custom_auth")
    cfg["cfworker_subdomain"] = g("cfworker_subdomain")
    cfg["cloudflare_api_base"] = cfg["cfworker_api_url"]
    cfg["cloudflare_api_key"] = cfg["cfworker_admin_token"]
    cfg["defaultDomains"] = cfg["cfworker_domain"]

    for key in (
        "moemail_api_url", "moemail_api_key",
        "maliapi_base_url", "maliapi_api_key", "maliapi_domain",
        "luckmail_base_url", "luckmail_api_key", "luckmail_project_code", "luckmail_domain",
        "skymail_api_base", "skymail_token", "skymail_domain",
        "cloudmail_api_base", "cloudmail_admin_email", "cloudmail_admin_password", "cloudmail_domain",
        "freemail_api_url", "freemail_admin_token", "freemail_domain",
        "opentrashmail_api_url", "opentrashmail_domain", "opentrashmail_password",
        "laoudo_auth", "laoudo_email", "laoudo_account_id",
    ):
        if key in data or key in cfg:
            cfg[key] = g(key, cfg.get(key, ""))

    # sync yyds keys for legacy
    if cfg.get("maliapi_api_key") and not cfg.get("yyds_api_key"):
        cfg["yyds_api_key"] = cfg["maliapi_api_key"]

    # required fields soft-check for selected provider
    need = {
        "cfworker": ["cfworker_api_url"],
        "luckmail": ["luckmail_api_key"],
        "skymail": ["skymail_token"],
        "cloudmail": ["cloudmail_api_base"],
        "freemail": ["freemail_api_url"],
        "opentrashmail": ["opentrashmail_api_url"],
        "laoudo": ["laoudo_email"],
        "maliapi": ["maliapi_api_key"],
    }
    for field in need.get(provider, []):
        if not str(cfg.get(field) or "").strip():
            raise ValueError(f"邮箱源 {provider} 需要配置: {field}")

    cfg.pop("tempmailer_api_base", None)
    cfg.pop("tempmailer_domain", None)
    cfg.pop("tempmailer_domains", None)
    save_config(cfg)
    return email_config_public(cfg)


def _open_port(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _looks_like_http_proxy(host: str, port: int, timeout: float = 1.2) -> bool:
    """Confirm the port really speaks HTTP-proxy before routing traffic through it.

    A bare TCP connect only proves *something* listens there. Treating an
    unrelated local service as the proxy silently sends registration traffic
    (SSO cookies included) to the wrong process.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(
                b"CONNECT api.x.ai:443 HTTP/1.1\r\n"
                b"Host: api.x.ai:443\r\n"
                b"Proxy-Connection: keep-alive\r\n\r\n"
            )
            head = s.recv(64)
    except Exception:
        return False
    if not head.startswith(b"HTTP/"):
        return False
    try:
        code = int(head.split(b" ", 2)[1])
    except Exception:
        return False
    return code in (200, 403, 405, 407, 502)


def resolve_proxy_url() -> str:
    """Proxy to use for this run. Never written back to config.json.

    Probing used to overwrite the user's configured `proxy`, so a momentarily
    dead Clash could permanently replace a hand-typed value.
    """
    from urllib.parse import urlparse

    preferred = ""
    try:
        preferred = str(load_config().get("proxy") or "").strip()
    except Exception:
        preferred = ""
    preferred = preferred or os.environ.get("GROK_PROXY", "").strip() or PROXY_URL

    if preferred:
        u = urlparse(preferred if "://" in preferred else "http://" + preferred)
        if _open_port(u.hostname or "127.0.0.1", u.port or 7890):
            return preferred
    for port in (7897, 7890, 7891, 7892, 10809, 20171, 1080, 2080, 8888):
        if _looks_like_http_proxy("127.0.0.1", port):
            return f"http://127.0.0.1:{port}"
    return preferred or "http://127.0.0.1:7890"


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # config.json 里有邮箱源的管理员令牌/密码，收紧权限（Windows 上是 no-op）
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# --------------- 出口 IP 探测 ---------------
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
    cfg_run.setdefault("email_provider", "cfworker")
    engine = str(cfg_run.get("browser_engine") or "chromium").strip().lower()
    if engine in ("camoufox", "firefox", "headless", "cfox"):
        engine = "camoufox"
    else:
        engine = "chromium"
    cfg_run["browser_engine"] = engine
    # 探测到的代理只在本轮生效，不写回 config.json（以前会覆盖用户手填的值）。
    # 引擎是用户在面板上主动选的，由 /api/config/browser 负责持久化，这里不重复写盘。
    cfg = cfg_run

    round_timeout = resolve_round_timeout_sec(cfg)

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

    # 注册前检查邮箱源是否可用
    try:
        mail_cfg = load_config()
        mail_prov = str(mail_cfg.get("email_provider") or "cfworker").strip().lower()
        # cloudflare/custom/tempmailer are all aliases of cfworker
        # tempmail_lol/duckmail/gptmail: removed public providers (xAI 拒收), fall back to cfworker
        if mail_prov in ("tempmailer", "inboxkitten", "inbox_kitten", "custom", "cloudflare",
                         "tempmail_lol", "duckmail", "gptmail"):
            mail_prov = "cfworker"
        if mail_prov == "yyds":
            mail_prov = "maliapi"
        # no-key providers
        free_ok = mail_prov in ("moemail",)
        has_cf = bool(str(mail_cfg.get("cfworker_api_url") or mail_cfg.get("cloudflare_api_base") or "").strip())
        has_luck = bool(str(mail_cfg.get("luckmail_api_key") or "").strip())
        has_mali = bool(str(mail_cfg.get("maliapi_api_key") or mail_cfg.get("yyds_api_key") or "").strip())
        has_sky = bool(str(mail_cfg.get("skymail_token") or "").strip())
        has_cloud = bool(str(mail_cfg.get("cloudmail_api_base") or "").strip())
        has_free = bool(str(mail_cfg.get("freemail_api_url") or "").strip())
        has_otm = bool(str(mail_cfg.get("opentrashmail_api_url") or "").strip())
        has_lao = bool(str(mail_cfg.get("laoudo_email") or "").strip())
        ok = free_ok
        if mail_prov == "cfworker":
            ok = has_cf
        elif mail_prov == "luckmail":
            ok = has_luck
        elif mail_prov == "maliapi":
            ok = has_mali
        elif mail_prov == "skymail":
            ok = has_sky
        elif mail_prov == "cloudmail":
            ok = has_cloud
        elif mail_prov == "freemail":
            ok = has_free
        elif mail_prov == "opentrashmail":
            ok = has_otm
        elif mail_prov == "laoudo":
            ok = has_lao
        if not ok:
            log_line(f"[!] 邮箱源 {mail_prov} 尚未配置完整，请到面板「邮箱服务」填写后保存")
            return False
        log_line(f"[*] 邮箱源: {mail_prov}")
    except Exception as e:
        log_line(f"[!] 检查邮箱配置失败: {e}")
        return False

    # Camoufox 首次要下载浏览器二进制，不计入 5 分钟注册超时
    if engine == "camoufox":
        try:
            lib_dir = str(BASE_DIR / "lib")
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            from camoufox_backend import ensure_camoufox_ready  # type: ignore

            log_line("[*] 检查 Camoufox 浏览器（首次会下载，可能几分钟）...")
            exe = ensure_camoufox_ready(log_callback=log_line)
            log_line(f"[*] Camoufox 就绪: {exe}")
        except Exception as e:
            log_line(f"[!] Camoufox 准备失败: {e}")
            log_line("[!] 可改用 Chromium 有头引擎，或手动执行: .venv\\Scripts\\python.exe -m camoufox fetch")
            return False

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


def job_worker(count: int):
    """Run register rounds. Node switching is intentionally not managed here —
    user selects nodes in their own Clash client.

    `running` is already set by start_job() under the lock, so two concurrent
    /api/job/start calls cannot both get through.
    """
    try:
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


def start_job(count: int) -> Tuple[bool, str]:
    if count < 1 or count > 500:
        return False, "轮数范围 1-500"

    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    # 「检查是否在跑」和「置为在跑」必须在同一个临界区里完成。
    # 分开写会让并发的两次 /api/job/start 都通过检查，起两个 worker
    # 争抢同一个 _proc，stop 只能停掉其中一个。
    with _job_lock:
        if _job.get("running"):
            return False, "已有任务在运行"
        _job["running"] = True
        _job["stop"] = False
        _job["status"] = "running"
        _job["count"] = count
        _job["success"] = 0
        _job["fail"] = 0
        _job["current_round"] = 0
        _job["current_node"] = "external-clash"
        _job["started_at"] = datetime.now().isoformat(timespec="seconds")
        _job["finished_at"] = None
        _job["last_error"] = ""
        _job["log_path"] = str(log_path)
    reset_logs()
    log_line(f"任务创建：轮数={count} proxy={PROXY_URL}（节点由本机 Clash 管理）")

    th = threading.Thread(target=job_worker, args=(count,), daemon=True)
    try:
        th.start()
    except Exception as e:
        # 线程没起来就把 running 放回去，否则面板会永远卡在「已有任务在运行」
        with _job_lock:
            _job["running"] = False
            _job["status"] = "idle"
            _job["last_error"] = str(e)
        log_line(f"[!] 启动任务线程失败: {e}")
        return False, f"启动任务线程失败: {e}"
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
INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Grok 注册控制台</title>
  <!-- 字体全部走系统栈：面板是本机工具，外链字体在离线/内网下会阻塞渲染 -->
  <style>
    :root{
      --paper:#f7f4ed;--paper-2:#efebe0;--ink:#1a1a18;--ink-2:#2a2a26;
      --steel:#6b685f;--steel-2:#9a968a;--line:#d8d3c4;
      --rust:#c8351a;--amber:#d98324;--ok:#2f6b2f;
      --log-bg:#1a1a18;--log-fg:#d8d3c4;--log-dim:#9a968a;
      --font-display:"Segoe UI Variable Display","Segoe UI","Microsoft YaHei UI","PingFang SC","Noto Sans SC",system-ui,sans-serif;
      --font-body:"Segoe UI","Microsoft YaHei UI","PingFang SC","Noto Sans SC",system-ui,-apple-system,sans-serif;
      --font-mono:"Cascadia Mono","Consolas","SF Mono","DejaVu Sans Mono",ui-monospace,monospace;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:var(--font-body);background:var(--paper);color:var(--ink);min-height:100vh;-webkit-font-smoothing:antialiased}
    .wrap{max-width:1240px;margin:0 auto;padding:0 20px 56px}

    header{display:flex;align-items:stretch;flex-wrap:wrap;border:1px solid var(--ink);background:var(--ink);color:var(--paper)}
    .brand{display:flex;align-items:center;gap:14px;padding:14px 18px;flex:1}
    .logo{width:38px;height:38px;border:1px solid var(--paper);display:grid;place-items:center;font-family:var(--font-display);font-weight:800;font-size:20px;background:var(--rust);color:var(--paper);letter-spacing:-1px;flex-shrink:0}
    h1{margin:0;font-family:var(--font-display);font-weight:800;font-size:24px;letter-spacing:-.5px;line-height:1}
    h1 .slash{color:var(--rust)}
    .sub{font-family:var(--font-mono);font-size:11px;color:#9a968a;margin-top:4px;letter-spacing:.5px}
    .actions{display:flex;flex-wrap:wrap;align-items:stretch}
    .actions .btn{border:0;border-right:1px solid var(--paper);background:transparent;color:var(--paper);padding:14px 18px;border-radius:0;text-decoration:none;font-family:var(--font-body);font-size:12.5px;font-weight:500;cursor:pointer;transition:background .12s;display:inline-flex;align-items:center;gap:6px}
    .actions .btn:last-child{border-right:0}
    .actions .btn:hover{background:var(--rust)}

    a.btn,button.btn{border:1px solid var(--ink);background:var(--paper);color:var(--ink);padding:10px 16px;border-radius:2px;text-decoration:none;font-size:12.5px;font-family:var(--font-body);font-weight:500;cursor:pointer;transition:all .12s;display:inline-flex;align-items:center;gap:6px}
    a.btn:hover,button.btn:hover{background:var(--ink);color:var(--paper)}
    a.btn:active,button.btn:active{transform:translateY(1px)}
    a.btn.primary,button.btn.primary{background:var(--rust);color:var(--paper);border-color:var(--ink)}
    a.btn.primary:hover,button.btn.primary:hover{background:var(--ink)}
    a.btn.ok,button.btn.ok{background:var(--ok);color:#fff;border-color:var(--ink)}
    a.btn.ok:hover,button.btn.ok:hover{background:var(--ink)}
    a.btn.sub2,button.btn.sub2{background:var(--amber);color:var(--ink);border-color:var(--ink)}
    a.btn.sub2:hover,button.btn.sub2:hover{background:var(--ink);color:var(--paper)}
    a.btn.danger,button.btn.danger{background:var(--ink);color:var(--paper);border-color:var(--ink)}
    a.btn.danger:hover,button.btn.danger:hover{background:var(--rust)}
    button.btn:disabled{opacity:.4;cursor:not-allowed}

    .grid{display:grid;grid-template-columns:repeat(6,1fr);border:1px solid var(--ink);border-top:0;background:var(--paper)}
    .stat{border-right:1px solid var(--ink);padding:16px;background:var(--paper)}
    .stat:last-child{border-right:0}
    .stat .k{font-family:var(--font-body);font-size:11px;color:var(--steel);letter-spacing:.3px}
    .stat .v{font-family:var(--font-display);font-weight:700;font-size:30px;line-height:1;margin-top:10px;letter-spacing:-1px;color:var(--ink)}
    .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;background:var(--steel-2);vertical-align:middle}
    .dot.run{background:var(--ok);box-shadow:0 0 0 2px rgba(47,107,47,.15);animation:pulse 1.4s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

    .card{border:1px solid var(--ink);border-top:0;background:var(--paper);position:relative}
    .card h2{margin:0;padding:12px 16px;border-bottom:1px solid var(--line);font-family:var(--font-display);font-weight:600;font-size:16px;display:flex;align-items:center;gap:10px}
    .card h2::before{content:"";width:3px;height:14px;background:var(--rust);border-radius:1px}
    .card-body{padding:20px 16px}

    .row{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
    label{display:flex;flex-direction:column;gap:6px;font-family:var(--font-body);font-size:11.5px;color:var(--steel)}
    input,select{background:var(--paper);border:1px solid var(--line);color:var(--ink);border-radius:2px;padding:10px 12px;min-width:150px;font-size:13px;font-family:var(--font-mono);appearance:none;outline:none}
    input:focus,select:focus{border-color:var(--ink)}
    select{background-image:linear-gradient(45deg,transparent 50%,var(--ink) 50%),linear-gradient(135deg,var(--ink) 50%,transparent 50%);background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px;background-repeat:no-repeat;padding-right:30px}
    input[type=number]{font-weight:500}
    /* 复选框自绘对号，不依赖浏览器原生控件。
       原来靠 appearance:auto + accent-color 交给系统画，在部分 Windows 浏览器 /
       系统主题下勾不显示（issue: 勾选后前面不打勾）。这里用 ::after 自己画，
       任何内核、任何主题下表现一致。 */
    input[type=checkbox]{
      -webkit-appearance:none;appearance:none;
      width:18px;height:18px;min-width:0;flex:none;margin:0;padding:0;
      border:1.5px solid var(--steel);border-radius:3px;background:var(--paper);
      cursor:pointer;vertical-align:middle;position:relative;display:inline-block;
      transition:background .12s,border-color .12s}
    input[type=checkbox]:hover{border-color:var(--ink)}
    input[type=checkbox]:checked{background:var(--rust);border-color:var(--rust)}
    input[type=checkbox]:checked::after{
      content:"";position:absolute;left:5px;top:1px;width:5px;height:10px;
      border:solid #fff;border-width:0 2.5px 2.5px 0;transform:rotate(45deg)}
    input[type=checkbox]:focus-visible{outline:2px solid var(--ink);outline-offset:1px}
    /* 整行可点选 + 选中高亮，避免只有 18px 的小方块能点 */
    tbody tr.selected{background:#f0e6e0}
    td.chk-cell{cursor:pointer;user-select:none}
    input[type=file]{min-width:200px}

    .mail-box{display:none;margin-top:14px;border:1px solid var(--line);padding:14px;background:var(--paper)}

    .muted{color:var(--steel);font-size:12px}
    .hint-box{font-family:var(--font-body);font-size:12px;color:var(--steel);border-left:2px solid var(--ink);background:transparent;padding:6px 14px;margin:12px 0;line-height:1.7}
    code{background:var(--paper-2);padding:1px 5px;border-radius:2px;font-family:var(--font-mono);font-size:11px;color:var(--ink)}

    #logbox{height:340px;overflow:auto;background:var(--log-bg);color:var(--log-fg);border:1px solid var(--ink);border-top:0;border-radius:0;padding:16px 18px;font-family:var(--font-mono);font-size:12px;line-height:1.7;white-space:pre-wrap}
    #logbox::-webkit-scrollbar{width:10px}
    #logbox::-webkit-scrollbar-track{background:var(--ink-2)}
    #logbox::-webkit-scrollbar-thumb{background:var(--steel-2)}

    table{width:100%;border-collapse:collapse}
    th,td{padding:11px 14px;border-bottom:1px solid var(--line);text-align:left;font-size:12.5px;vertical-align:middle}
    th{font-family:var(--font-body);font-size:11px;font-weight:500;color:var(--steel);background:var(--paper);border-bottom:1px solid var(--ink)}
    tbody tr:hover{background:var(--paper-2)}
    .mono{font-family:var(--font-mono);word-break:break-all}
    .tag{display:inline-block;padding:2px 8px;border:1px solid var(--line);border-radius:2px;font-family:var(--font-mono);font-size:11px;font-weight:500;color:var(--ink)}

    .toast{position:fixed;right:24px;bottom:24px;background:var(--ink);color:var(--paper);border:1px solid var(--ink);padding:12px 18px;border-radius:0;font-family:var(--font-body);font-size:12px;display:none;z-index:50}

    @media(max-width:900px){
      .grid{grid-template-columns:repeat(3,1fr)}
      .stat:nth-child(3){border-right:0}
      .row{flex-direction:column;align-items:stretch}
      input,select{min-width:0}
      .actions{width:100%}
      .actions .btn{flex:1;justify-content:center}
    }
  </style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="brand">
      <div class="logo">G</div>
      <div>
        <h1>GROK<span class="slash">//</span>注册控制台</h1>
        <div class="sub">{{ base_dir }} · 本机代理 Clash 7897</div>
      </div>
    </div>
    <div class="actions">
      <a class="btn primary" href="/download/sso.txt" title="email----password----sso">⬇ SSO 账号包</a>
      <a class="btn ok" href="/download/cpa.zip" title="CPA OAuth JSON（CLIProxyAPI 可用）">⬇ CPA 凭证</a>
      <a class="btn sub2" href="/download/sub2.zip" title="Sub2API 官方导入包">⬇ Sub2 导入包</a>
    </div>
  </header>

  <div class="grid">
    <div class="stat"><div class="k">文件数</div><div class="v" id="st_files">{{ file_count }}</div></div>
    <div class="stat"><div class="k">SSO 账号</div><div class="v" id="st_accounts">{{ account_count }}</div></div>
    <div class="stat"><div class="k">已转 CPA</div><div class="v" id="st_cpa_ok">{{ cpa_files }}</div></div>
    <div class="stat"><div class="k">CPA 队列</div><div class="v" style="font-size:16px" id="st_cpa_q">0 / 0 / 0</div></div>
    <div class="stat"><div class="k">任务状态</div><div class="v" style="font-size:16px"><span class="dot" id="st_dot"></span><span id="st_status">idle</span></div></div>
    <div class="stat"><div class="k">注册 成功/失败</div><div class="v" style="font-size:16px"><span id="st_sf">0 / 0</span></div></div>
  </div>

  <div class="card">
    <h2>注册引擎</h2>
    <div class="card-body">
      <div class="row">
        <label>轮数
          <input type="number" id="count" min="1" max="500" value="1"/>
        </label>
        <label>浏览器引擎
          <select id="browser_engine" onchange="saveBrowserEngine()">
            <option value="chromium">Chromium 有头（默认）</option>
            <option value="camoufox">Camoufox 无头（反检测）</option>
          </select>
        </label>
        <button class="btn ok" id="btn_start" onclick="startJob()">▶ 开始注册</button>
        <button class="btn danger" id="btn_stop" onclick="stopJob()">■ 停止</button>
        <button class="btn" onclick="backfillCpa()" title="把尚未转成 CPA 的历史 SSO 入队">⟳ 补转 CPA</button>
      </div>
      <div class="muted" style="margin-top:10px" id="cpa_hint">代理走本机 Clash · 注册成功后自动转 CPA</div>
    </div>
  </div>

  <div class="card">
    <h2>SSO 续期上传</h2>
    <div class="card-body">
      <div class="hint-box">支持 <code>email----password----sso</code> / <code>email----sso</code> / 纯 SSO。上传会整批替换当前工作区（旧数据归档到 <code>data/archive/</code>）。</div>
      <div class="row">
        <label style="flex:2">SSO 文件
          <input type="file" id="sso_upload_file" accept=".txt,.json,text/plain,application/json"/>
        </label>
        <button class="btn primary" id="btn_upload_convert" onclick="uploadSsoConvert()">⬆ 上传并转换</button>
      </div>
      <div class="muted" style="margin-top:10px" id="upload_hint">
        当前模式：注册 · 未上传批次
      </div>
    </div>
  </div>

  <div class="card">
    <h2>邮箱服务</h2>
    <div class="card-body">
      <div class="row">
        <label>邮箱源
          <select id="email_provider" onchange="onEmailProviderChange()"></select>
        </label>
        <label style="min-width:auto;flex-direction:row;align-items:center;gap:8px;padding-bottom:10px">
          <input type="checkbox" id="email_failover" style="width:auto;min-width:0"/> 失败时自动换源
        </label>
        <button class="btn primary" onclick="saveEmailConfig()">💾 保存邮箱设置</button>
      </div>

      <div id="box_cfworker" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API URL
            <input type="text" id="cfworker_api_url" placeholder="https://apimail.example.com"/>
          </label>
          <label>管理员令牌
            <input type="password" id="cfworker_admin_token" placeholder="管理员密钥"/>
          </label>
        </div>
        <div class="row" style="margin-top:8px">
          <label>域名
            <input type="text" id="cfworker_domain" placeholder="mail.example.com"/>
          </label>
          <label>站点密码
            <input type="password" id="cfworker_custom_auth" placeholder="可选"/>
          </label>
          <label>子域名
            <input type="text" id="cfworker_subdomain" placeholder="可选"/>
          </label>
        </div>
      </div>

      <div id="box_moemail" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API URL
            <input type="text" id="moemail_api_url" placeholder="https://sall.cc"/>
          </label>
          <label>API Key
            <input type="password" id="moemail_api_key" placeholder="可选"/>
          </label>
        </div>
      </div>

      <div id="box_maliapi" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API URL
            <input type="text" id="maliapi_base_url" placeholder="https://maliapi.215.im/v1"/>
          </label>
          <label>API Key
            <input type="password" id="maliapi_api_key"/>
          </label>
          <label>域名
            <input type="text" id="maliapi_domain" placeholder="可选"/>
          </label>
        </div>
      </div>

      <div id="box_luckmail" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">平台地址
            <input type="text" id="luckmail_base_url" placeholder="https://mails.luckyous.com"/>
          </label>
          <label>API Key
            <input type="password" id="luckmail_api_key"/>
          </label>
        </div>
        <div class="row" style="margin-top:8px">
          <label>项目代码
            <input type="text" id="luckmail_project_code" placeholder="grok"/>
          </label>
          <label>域名
            <input type="text" id="luckmail_domain" placeholder="可选"/>
          </label>
        </div>
      </div>

      <div id="box_skymail" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API 地址
            <input type="text" id="skymail_api_base" placeholder="https://api.skymail.ink"/>
          </label>
          <label>令牌
            <input type="password" id="skymail_token"/>
          </label>
          <label>域名
            <input type="text" id="skymail_domain"/>
          </label>
        </div>
      </div>

      <div id="box_cloudmail" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API 地址
            <input type="text" id="cloudmail_api_base"/>
          </label>
          <label>管理员邮箱
            <input type="text" id="cloudmail_admin_email"/>
          </label>
          <label>管理员密码
            <input type="password" id="cloudmail_admin_password"/>
          </label>
        </div>
        <div class="row" style="margin-top:8px">
          <label>域名
            <input type="text" id="cloudmail_domain"/>
          </label>
        </div>
      </div>

      <div id="box_freemail" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API URL
            <input type="text" id="freemail_api_url"/>
          </label>
          <label>管理员令牌
            <input type="password" id="freemail_admin_token"/>
          </label>
          <label>域名
            <input type="text" id="freemail_domain"/>
          </label>
        </div>
      </div>

      <div id="box_opentrashmail" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label style="flex:2">API URL
            <input type="text" id="opentrashmail_api_url"/>
          </label>
          <label>域名
            <input type="text" id="opentrashmail_domain"/>
          </label>
          <label>密码
            <input type="password" id="opentrashmail_password"/>
          </label>
        </div>
      </div>

      <div id="box_laoudo" class="mail-box" style="display:none;margin-top:10px">
        <div class="row">
          <label>Auth
            <input type="password" id="laoudo_auth"/>
          </label>
          <label>邮箱
            <input type="text" id="laoudo_email"/>
          </label>
          <label>账号 ID
            <input type="text" id="laoudo_account_id"/>
          </label>
        </div>
      </div>

      <div class="muted" style="margin-top:10px;display:none" id="email_hint"></div>
    </div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div id="logbox">等待任务…</div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-bottom:1px solid var(--line)">
      <h2 style="margin:0;border:0;padding:0">账号文件</h2>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn" type="button" onclick="toggleSelectAllFiles(true)">全选</button>
        <button class="btn" type="button" onclick="toggleSelectAllFiles(false)">取消全选</button>
        <button class="btn danger" type="button" id="btn_del_files" onclick="deleteSelectedFiles()" disabled>删除选中</button>
      </div>
    </div>
    {% if files %}
    <table>
      <thead>
        <tr>
          <th style="width:44px"><input type="checkbox" id="chk_all_files" onclick="toggleSelectAllFiles(this.checked)" title="全选"/></th>
          <th>文件名</th><th>数量</th><th>修改时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
      {% for f in files %}
        <tr>
          <td class="chk-cell" onclick="onChkCellClick(event, this)"><input type="checkbox" class="chk-file" value="{{ f.name }}" onclick="event.stopPropagation();syncFileSelection()"/></td>
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
    <div style="padding:24px;color:var(--steel);text-align:center">暂无 accounts_*.txt</div>
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
  const p=document.getElementById('email_provider').value||'cfworker';
  document.querySelectorAll('.mail-box').forEach(el=>{ el.style.display='none'; });
  const box=document.getElementById('box_'+p);
  if(box) box.style.display='block';
}
function _val(id){const el=document.getElementById(id); return el?el.value:'';}
function _set(id,v){const el=document.getElementById(id); if(el) el.value=v||'';}
function _check(id,v){const el=document.getElementById(id); if(el) el.checked=!!v;}
async function loadEmailConfig(){
  try{
    const j=await api('/api/config/email');
    const e=j.email||{};
    const sel=document.getElementById('email_provider');
    sel.innerHTML='';
    const _groups={};
    (e.choices||[]).forEach(c=>{
      const g=c.group||'';
      if(!_groups[g]) _groups[g]=[];
      _groups[g].push(c);
    });
    ['自建','付费',''].forEach(g=>{
      if(!_groups[g]) return;
      let parent=sel;
      if(g){
        const og=document.createElement('optgroup');
        og.label=g;
        sel.appendChild(og);
        parent=og;
      }
      _groups[g].forEach(c=>{
        const o=document.createElement('option');
        o.value=c.id; o.textContent=c.label;
        parent.appendChild(o);
      });
    });
    let prov=e.provider||'cfworker';
    if(![...sel.options].some(o=>o.value===prov)) prov='cfworker';
    sel.value=prov;
    _check('email_failover', e.email_failover);
    _set('cfworker_api_url', e.cfworker_api_url);
    _set('cfworker_admin_token', e.cfworker_admin_token);
    _set('cfworker_domain', e.cfworker_domain);
    _set('cfworker_custom_auth', e.cfworker_custom_auth);
    _set('cfworker_subdomain', e.cfworker_subdomain);
    _set('moemail_api_url', e.moemail_api_url||'https://sall.cc');
    _set('moemail_api_key', e.moemail_api_key);
    _set('maliapi_base_url', e.maliapi_base_url||'https://maliapi.215.im/v1');
    _set('maliapi_api_key', e.maliapi_api_key);
    _set('maliapi_domain', e.maliapi_domain);
    _set('luckmail_base_url', e.luckmail_base_url||'https://mails.luckyous.com');
    _set('luckmail_api_key', e.luckmail_api_key);
    _set('luckmail_project_code', e.luckmail_project_code||'grok');
    _set('luckmail_domain', e.luckmail_domain);
    _set('skymail_api_base', e.skymail_api_base||'https://api.skymail.ink');
    _set('skymail_token', e.skymail_token);
    _set('skymail_domain', e.skymail_domain);
    _set('cloudmail_api_base', e.cloudmail_api_base);
    _set('cloudmail_admin_email', e.cloudmail_admin_email);
    _set('cloudmail_admin_password', e.cloudmail_admin_password);
    _set('cloudmail_domain', e.cloudmail_domain);
    _set('freemail_api_url', e.freemail_api_url);
    _set('freemail_admin_token', e.freemail_admin_token);
    _set('freemail_domain', e.freemail_domain);
    _set('opentrashmail_api_url', e.opentrashmail_api_url);
    _set('opentrashmail_domain', e.opentrashmail_domain);
    _set('opentrashmail_password', e.opentrashmail_password);
    _set('laoudo_auth', e.laoudo_auth);
    _set('laoudo_email', e.laoudo_email);
    _set('laoudo_account_id', e.laoudo_account_id);
    setEmailHint(e.hint||'');
    onEmailProviderChange();
  }catch(err){
    setEmailHint('加载邮箱配置失败: '+err.message);
  }
}
async function saveEmailConfig(){
  const body={
    provider: (document.getElementById('email_provider').value||'cfworker'),
    email_failover: document.getElementById('email_failover').checked,
    cfworker_api_url: _val('cfworker_api_url'),
    cfworker_admin_token: _val('cfworker_admin_token'),
    cfworker_domain: _val('cfworker_domain'),
    cfworker_custom_auth: _val('cfworker_custom_auth'),
    cfworker_subdomain: _val('cfworker_subdomain'),
    moemail_api_url: _val('moemail_api_url'),
    moemail_api_key: _val('moemail_api_key'),
    maliapi_base_url: _val('maliapi_base_url'),
    maliapi_api_key: _val('maliapi_api_key'),
    maliapi_domain: _val('maliapi_domain'),
    luckmail_base_url: _val('luckmail_base_url'),
    luckmail_api_key: _val('luckmail_api_key'),
    luckmail_project_code: _val('luckmail_project_code'),
    luckmail_domain: _val('luckmail_domain'),
    skymail_api_base: _val('skymail_api_base'),
    skymail_token: _val('skymail_token'),
    skymail_domain: _val('skymail_domain'),
    cloudmail_api_base: _val('cloudmail_api_base'),
    cloudmail_admin_email: _val('cloudmail_admin_email'),
    cloudmail_admin_password: _val('cloudmail_admin_password'),
    cloudmail_domain: _val('cloudmail_domain'),
    freemail_api_url: _val('freemail_api_url'),
    freemail_admin_token: _val('freemail_admin_token'),
    freemail_domain: _val('freemail_domain'),
    opentrashmail_api_url: _val('opentrashmail_api_url'),
    opentrashmail_domain: _val('opentrashmail_domain'),
    opentrashmail_password: _val('opentrashmail_password'),
    laoudo_auth: _val('laoudo_auth'),
    laoudo_email: _val('laoudo_email'),
    laoudo_account_id: _val('laoudo_account_id'),
  };
  try{
    const j=await api('/api/config/email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(j.message||'邮箱设置已保存');
    if(j.email){
      setEmailHint('已保存 · 当前: '+(j.email.provider||''));
    }
  }catch(e){toast('保存失败: '+e.message)}
}
function setEmailHint(text){
  const el=document.getElementById('email_hint');
  if(!el) return;
  const t=String(text||'').trim();
  el.textContent=t;
  el.style.display=t ? '' : 'none';
}
function toggleSelectAllFiles(on){
  document.querySelectorAll('.chk-file').forEach(b=>{ b.checked=!!on; });
  syncFileSelection();
}
// 点击整个单元格（不只是 18px 的小方块）也能切换勾选
function onChkCellClick(ev, td){
  const box=td.querySelector('.chk-file');
  if(!box) return;
  box.checked=!box.checked;
  syncFileSelection();
}
// 勾选状态 -> 行高亮 + 表头全选框 + 「删除选中」按钮文案，
// 保证用户一定能看到「选中了几个」，不依赖原生对号是否渲染出来。
function syncFileSelection(){
  const boxes=[...document.querySelectorAll('.chk-file')];
  let n=0;
  boxes.forEach(b=>{
    const tr=b.closest('tr');
    if(tr) tr.classList.toggle('selected', b.checked);
    if(b.checked) n++;
  });
  const all=document.getElementById('chk_all_files');
  if(all){
    all.checked = boxes.length>0 && n===boxes.length;
    all.indeterminate = n>0 && n<boxes.length;
  }
  const btn=document.getElementById('btn_del_files');
  if(btn){
    btn.textContent = n ? ('删除选中 ('+n+')') : '删除选中';
    btn.disabled = n===0;
  }
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
async function uploadSsoConvert(){
  const input=document.getElementById('sso_upload_file');
  if(!input || !input.files || !input.files.length){
    toast('请先选择 SSO txt/json 文件');
    return;
  }
  if(!confirm('上传后会整批替换当前工作区（旧账号与 CPA 会归档）。确定继续？')){
    return;
  }
  const fd=new FormData();
  fd.append('file', input.files[0]);
  const btn=document.getElementById('btn_upload_convert');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/sso/upload-convert',{method:'POST',body:fd,credentials:'same-origin'});
    const j=await r.json();
    if(!r.ok || j.ok===false){
      throw new Error(j.error||('HTTP '+r.status));
    }
    toast(j.message||('已入队 '+j.queued+' 个'));
    if(input) input.value='';
    poll();
    setTimeout(()=>location.reload(), 800);
  }catch(e){
    toast('上传转换失败: '+e.message);
  }finally{
    if(btn) btn.disabled=false;
  }
}
let logCursor=0;
async function poll(){
  try{
    const j=await api('/api/job/status?since='+logCursor);
    const st=j.job||{};
    const cpa=j.cpa||{};
    const ws=j.workspace||{};
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
    if(document.getElementById('st_accounts')){
      document.getElementById('st_accounts').textContent=String(ws.account_count!=null?ws.account_count:(j.accounts||0));
    }
    if(document.getElementById('cpa_hint')){
      const core = cpa.core_ok ? 'core就绪' : ('core失败: '+(cpa.core_error||''));
      const last = cpa.last_ok_email ? (' · 最近OK: '+cpa.last_ok_email) : '';
      const err = cpa.last_error ? (' · 最近错: '+cpa.last_error) : '';
      document.getElementById('cpa_hint').textContent =
        `代理走本机 Clash · 自动CPA: ${cpa.enabled?'开':'关'} · ${core} · 文件 ${cpa.files||0}${last}${err}`;
    }
    if(document.getElementById('upload_hint')){
      const mode = ws.mode==='upload' ? '上传' : '注册';
      const src = ws.source ? (' · 来源 '+ws.source) : '';
      const tot = ws.total!=null ? (' · 本批 '+ws.total) : '';
      const q = ws.queued!=null ? (' · 已入队 '+ws.queued) : '';
      const okf = ' · CPA文件 '+(cpa.files||0);
      document.getElementById('upload_hint').textContent =
        '当前模式：'+mode+src+tot+q+okf;
    }
    if(document.getElementById('st_files')){
      document.getElementById('st_files').textContent=String(j.files!=null?j.files:0);
    }
    // 日志增量追加：只有新行才过来，不再每 2 秒重传整个缓冲区
    const box=document.getElementById('logbox');
    const logs=j.logs||[];
    if(j.log_reset){
      box.textContent=logs.join('\n');
    }else if(logs.length){
      box.textContent=(box.textContent?box.textContent+'\n':'')+logs.join('\n');
    }
    if(j.log_reset||logs.length){
      box.scrollTop=box.scrollHeight;
    }
    if(j.log_cursor!=null) logCursor=j.log_cursor;
  }catch(e){}
}
loadEmailConfig();
loadBrowserEngine();
syncFileSelection();
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
@app.get("/")
def index():
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
    """主接口 2：当前面板账号对应的 CPA JSON（auth_kind=oauth）。"""
    # 只打包面板当前账号对应的 CPA，删掉的不会再进包
    files = list_active_cpa_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → 真 CPA (CLIProxyAPI) JSON\n"
            "====================================\n\n"
            "1) 每个 xai-*.json 是 OAuth 凭证（access_token + refresh_token）。\n"
            "2) auth_kind=oauth，可直接放进 CLIProxyAPI auth-dir。\n"
            "3) 仅包含当前面板账号列表中的号（删除后不会再进此包）。\n"
            "4) all.json 为全部账号数组。\n"
            "5) 若 zip 为空：先注册/上传，或点「补转未转换 CPA」。\n"
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
        if not files:
            zf.writestr(
                "EMPTY.txt",
                "暂无当前面板账号的 CPA 文件。注册/上传成功后会自动转换，"
                "或点击面板「补转未转换 CPA」。\n",
            )
    buf.seek(0)
    fname = f"cpa_oauth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


def _load_cpa_entries_for_sub2() -> Tuple[List[dict], List[str]]:
    """Read CPA JSON for current panel accounts only. No re-OAuth."""
    entries: List[dict] = []
    name_hints: List[str] = []
    for p in list_active_cpa_files():
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
            expires_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            "4) 仅映射当前面板账号对应的 CPA，删除后不会再进此包。\n"
            "5) proxies 为空；导入后请在 Sub2API 里绑定分组/代理。\n"
            "6) 若 zip 为空：先注册/上传并等待 CPA 转换完成。\n"
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
                "暂无当前面板账号的 CPA。注册/上传成功并转换后，再点「下载 Sub2」。\n",
            )

    buf.seek(0)
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/sub2.json")
def download_sub2_json():
    """兼容旧链接：返回 all 合集 JSON（等同 zip 内 all.json）。"""
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


@app.post("/api/accounts/delete")
def api_accounts_delete():
    """Delete selected accounts_*.txt and prune matching CPA JSON."""
    data = json_body()
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

    pruned = 0
    if deleted:
        # 同步清掉已不在面板里的 CPA，保证下载数量 = 面板当前数量
        pruned = prune_orphan_cpa()
        if pruned:
            log_line(f"[*] 已清理无主账号的 CPA 文件: {pruned} 个")

    if not deleted and errors:
        return jsonify({"ok": False, "error": "; ".join(errors)}), 400
    return jsonify(
        {
            "ok": True,
            "deleted": deleted,
            "missing": missing,
            "errors": errors,
            "cpa_pruned": pruned,
            "message": f"已删除 {len(deleted)} 个账号文件"
            + (f"，清理 CPA {pruned}" if pruned else "")
            + (f"，跳过 {len(missing)}" if missing else ""),
        }
    )


@app.get("/api/config/email")
def api_get_email_config():
    return jsonify({"ok": True, "email": email_config_public()})


@app.post("/api/config/email")
def api_set_email_config():
    data = json_body()
    try:
        email = apply_email_config_from_ui(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "message": "邮箱设置已保存", "email": email})


@app.get("/api/job/status")
def api_job_status():
    with _job_lock:
        job = dict(_job)
    try:
        since = int(request.args.get("since", "0"))
    except Exception:
        since = 0
    new_logs, cursor, reset = logs_since(since)
    return jsonify(
        {
            "ok": True,
            "job": job,
            "logs": new_logs,
            "log_cursor": cursor,
            "log_reset": reset,
            "cpa": cpa_stats(),
            "workspace": workspace_stats(),
            "accounts": len(unique_accounts()),
            "files": len(list_account_files()),
        }
    )


@app.get("/api/cpa/status")
def api_cpa_status():
    return jsonify({"ok": True, "cpa": cpa_stats(), "workspace": workspace_stats()})


@app.post("/api/cpa/backfill")
def api_cpa_backfill():
    data = json_body()
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


@app.post("/api/sso/upload-convert")
def api_sso_upload_convert():
    """Upload SSO txt/json → whole-batch replace workspace → queue CPA convert.

    Download buttons keep using current workspace only.
    """
    if not _CPA_CORE_OK or convert_one is None or parse_uploaded_records is None:
        return jsonify({"ok": False, "error": f"core unavailable: {_CPA_CORE_ERR}"}), 500
    if bool(_job.get("running")):
        return jsonify({"ok": False, "error": "注册任务进行中，请先停止再上传"}), 400

    f = request.files.get("file") or request.files.get("sso")
    if f is None:
        return jsonify({"ok": False, "error": "请上传 file 字段（txt/json）"}), 400
    filename = (f.filename or "upload.txt").strip()
    raw = f.read()
    if not raw:
        return jsonify({"ok": False, "error": "文件为空"}), 400
    if len(raw) > 30 * 1024 * 1024:
        return jsonify({"ok": False, "error": "文件过大（>30MB）"}), 400

    try:
        records = parse_upload_file_bytes(raw, filename=filename)
    except Exception as e:
        return jsonify({"ok": False, "error": f"解析失败: {e}"}), 400
    if not records:
        return jsonify({"ok": False, "error": "未解析到有效 SSO"}), 400

    meta = reset_workspace_for_upload(source_name=filename, total=len(records))
    out_path = write_upload_accounts_file(records, source_name=filename)

    queued = 0
    for rec in records:
        ok, _ = enqueue_cpa_convert(
            email=rec.get("email") or "",
            sso=rec.get("sso") or "",
            password=rec.get("password") or "",
            source=out_path.name,
            force=True,
        )
        if ok:
            queued += 1

    _workspace_meta["queued"] = queued
    _workspace_meta["accounts_file"] = out_path.name
    log_line(
        f"[UPLOAD] {filename} → {len(records)} SSO，入队 {queued}，"
        f"工作区文件 {out_path.name}（旧数据已归档）"
    )
    return jsonify(
        {
            "ok": True,
            "message": f"已替换工作区：解析 {len(records)}，入队 {queued}。请等 CPA 队列转完后下载。",
            "parsed": len(records),
            "queued": queued,
            "accounts_file": out_path.name,
            "workspace": workspace_stats(),
            "archived": meta.get("archived") or "",
        }
    )


def _normalize_browser_engine(value: str) -> str:
    eng = str(value or "").strip().lower()
    if eng in ("camoufox", "firefox", "headless", "cfox"):
        return "camoufox"
    return "chromium"


@app.get("/api/config/browser")
def api_get_browser_config():
    cfg = load_config()
    return jsonify(
        {
            "ok": True,
            "browser_engine": _normalize_browser_engine(cfg.get("browser_engine") or "chromium"),
        }
    )


@app.post("/api/config/browser")
def api_set_browser_config():
    data = json_body()
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
    data = json_body()
    try:
        count = int(data.get("count") or 1)
    except Exception:
        count = 1
    if "browser_engine" in data:
        eng = _normalize_browser_engine(data.get("browser_engine"))
        cfg = load_config()
        cfg["browser_engine"] = eng
        save_config(cfg)
    # 从上传模式切到注册：整批归档清空，避免和注册号混在一起
    if _workspace_mode == "upload":
        bump_cpa_generation()
        _drain_cpa_queue()
        arch = _archive_current_workspace("register-start")
        with _cpa_lock:
            _cpa_done.clear()
            _cpa_inflight.clear()
            _cpa_state["pending"] = 0
            _cpa_state["ok"] = 0
            _cpa_state["fail"] = 0
            _cpa_state["last_error"] = ""
            _cpa_state["last_ok_email"] = ""
        set_workspace_register_mode("register-start")
        log_line(
            f"[WORKSPACE] 开始注册 → 已归档上传批次"
            + (f" ({arch})" if arch else "")
            + "，工作区清空后重新注册"
        )
    ok, msg = start_job(count)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg, "workspace": workspace_stats()})


@app.post("/api/job/stop")
def api_job_stop():
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
