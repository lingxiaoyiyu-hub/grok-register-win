#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import datetime
import time
import os
import sys
import gc
import queue
import secrets
import struct
import random
import re
import string
import json
import base64
import select
import socket

# Ensure stdout/stderr use UTF-8 on Windows (default is GBK/CP936)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import socketserver
import ssl
import urllib.parse
import uuid

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests


def is_page_disconnected_error(exc):
    """Chromium PageDisconnectedError or Camoufox/Playwright closed target."""
    if isinstance(exc, PageDisconnectedError):
        return True
    msg = str(exc or "").lower()
    return any(
        x in msg
        for x in (
            "target closed",
            "target page",
            "page closed",
            "browser has been closed",
            "browser closed",
            "connection closed",
            "context closed",
            "session closed",
            "protocol error",
            "execution context was destroyed",
            "camoufox browser dead",
            "pipe closed",
            "transport",
        )
    )


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
MEMORY_CLEANUP_INTERVAL = 5

UI_BG = "#242424"
UI_PANEL_BG = "#2b2b2b"
UI_FG = "#f2f2f2"
UI_MUTED_FG = "#b8b8b8"
UI_ENTRY_BG = "#333333"
UI_BUTTON_BG = "#3a3a3a"
UI_ACTIVE_BG = "#4a6078"

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "tempmailer_api_base": "https://tempmailer.cc/api",
    "tempmailer_domain": "",
    "tempmailer_domains": [],
    "email_provider": "tempmailer",
    # 邮箱故障自动切换：按 email_providers 顺序轮换（只使用已配置可用的源）
    # 注意：inboxkitten.com 已被 xAI 拒绝，不再作为内置/默认源
    "email_failover": True,
    "email_providers": ["tempmailer", "duckmail", "yyds", "cloudflare"],
    "proxy": "http://127.0.0.1:7890",
    # 代理失败时是否回退直连（本地版默认关闭，避免直连拿不到 grok SSO）
    "allow_proxy_fallback": False,
    "enable_nsfw": True,
    "register_count": 1,
    # 单账号整轮硬超时（秒）；超时后跳过该账号进入下一个（面板也会在同超时后杀进程）
    "round_timeout_sec": 300,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    # chromium = DrissionPage 有头; camoufox = Camoufox 无头（反检测 Firefox）
    "browser_engine": "chromium",
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_email_provider_index = 0
_tempmailer_domain_index = 0


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_configured_proxy():
    return str(config.get("proxy", "") or "").strip()


def get_proxies():
    proxy = get_configured_proxy()
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def _parse_proxy_url(proxy):
    raw = str(proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return urllib.parse.urlsplit(raw)
    except Exception:
        return None


def _safe_proxy_port(parsed):
    try:
        return parsed.port
    except Exception:
        return None


def _proxy_has_auth(proxy):
    parsed = _parse_proxy_url(proxy)
    return bool(parsed and parsed.hostname and (parsed.username is not None or parsed.password is not None))


def _strip_proxy_auth(proxy):
    raw = str(proxy or "").strip()
    parsed = _parse_proxy_url(raw)
    if not parsed or not parsed.hostname:
        return raw
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = _safe_proxy_port(parsed)
    netloc = f"{host}:{port}" if port else host
    stripped = urllib.parse.urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))
    if "://" not in raw:
        return stripped.split("://", 1)[1]
    return stripped


def _proxy_endpoint_terms(proxy=None):
    parsed = _parse_proxy_url(proxy or get_configured_proxy())
    if not parsed or not parsed.hostname:
        return []
    terms = [parsed.hostname]
    port = _safe_proxy_port(parsed)
    if port:
        terms.append(f"{parsed.hostname}:{port}")
        terms.append(f"port {port}")
    return [x.lower() for x in terms if x]


def is_proxy_connection_error(exc):
    if not get_configured_proxy():
        return False
    err = str(exc or "").lower()
    if not err:
        return False
    if any(x in err for x in ("proxy", "tunnel", "socks")):
        return True
    connect_markers = (
        "could not connect",
        "failed to connect",
        "connection refused",
        "connection reset",
        "connect error",
        "timed out",
        "timeout",
    )
    if any(x in err for x in connect_markers):
        terms = _proxy_endpoint_terms()
        if not terms or any(t in err for t in terms):
            return True
    return False


def page_has_proxy_error(page_obj):
    try:
        url = str(getattr(page_obj, "url", "") or "")
        title = str(page_obj.run_js("return document.title || ''") or "")
        body = str(page_obj.run_js("return document.body ? document.body.innerText.slice(0, 2000) : ''") or "")
    except Exception:
        return False
    text = f"{url}\n{title}\n{body}".lower()
    return any(
        marker in text
        for marker in (
            "err_proxy",
            "proxy connection failed",
            "proxy server",
            "proxy authentication",
            "tunnel connection failed",
            "无法连接到代理服务器",
            "代理服务器",
        )
    )


class _ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _proxy_recv_until_headers(sock, timeout=20, limit=65536):
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _proxy_relay(left, right, timeout=60):
    left.settimeout(timeout)
    right.settimeout(timeout)
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], timeout)
        if not readable:
            return
        for sock in readable:
            data = sock.recv(65536)
            if not data:
                return
            peer = right if sock is left else left
            peer.sendall(data)


class _LocalAuthProxyBridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        upstream = None
        try:
            initial = _proxy_recv_until_headers(self.request, timeout=bridge.timeout)
            if not initial:
                return
            first_line = initial.split(b"\r\n", 1)[0].decode("latin1", "ignore")
            if first_line.upper().startswith("CONNECT "):
                target = first_line.split()[1]
                upstream = bridge.open_upstream()
                req = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
                if bridge.auth_header:
                    req.append(f"Proxy-Authorization: Basic {bridge.auth_header}")
                upstream.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin1"))
                response = _proxy_recv_until_headers(upstream, timeout=bridge.timeout)
                if response:
                    self.request.sendall(response)
                status = response.split(b"\r\n", 1)[0]
                if b" 200 " not in status:
                    return
                _proxy_relay(self.request, upstream, timeout=bridge.relay_timeout)
            else:
                upstream = bridge.open_upstream()
                upstream.sendall(bridge.inject_proxy_auth(initial))
                _proxy_relay(self.request, upstream, timeout=bridge.relay_timeout)
        except Exception:
            return
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass


class LocalAuthProxyBridge:
    def __init__(self, proxy_url):
        parsed = _parse_proxy_url(proxy_url)
        if not parsed or not parsed.hostname:
            raise ValueError("认证代理地址格式无效")
        if (parsed.scheme or "http").lower() not in ("http", "https"):
            raise ValueError("Chromium 本地认证代理桥仅支持 http/https 上游代理")
        self.upstream_scheme = (parsed.scheme or "http").lower()
        self.upstream_host = parsed.hostname
        self.upstream_port = _safe_proxy_port(parsed) or (443 if self.upstream_scheme == "https" else 80)
        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
        raw_auth = f"{username}:{password}".encode("utf-8")
        self.auth_header = base64.b64encode(raw_auth).decode("ascii") if (username or password) else ""
        self.timeout = 20
        self.relay_timeout = 90
        self.server = None
        self.thread = None
        self.local_proxy = ""

    def open_upstream(self):
        sock = socket.create_connection((self.upstream_host, self.upstream_port), timeout=self.timeout)
        if self.upstream_scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=self.upstream_host)
        sock.settimeout(self.timeout)
        return sock

    def inject_proxy_auth(self, data):
        if not self.auth_header or b"\r\n\r\n" not in data:
            return data
        if b"\r\nproxy-authorization:" in data.lower():
            return data
        head, body = data.split(b"\r\n\r\n", 1)
        auth_line = f"Proxy-Authorization: Basic {self.auth_header}".encode("latin1")
        return head + b"\r\n" + auth_line + b"\r\n\r\n" + body

    def start(self):
        self.server = _ReusableThreadingTCPServer(("127.0.0.1", 0), _LocalAuthProxyBridgeHandler)
        self.server.bridge = self
        port = self.server.server_address[1]
        self.local_proxy = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.local_proxy

    def stop(self):
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None
        self.thread = None
        self.local_proxy = ""


def stop_browser_proxy_bridge():
    global browser_proxy_bridge
    if browser_proxy_bridge is not None:
        try:
            browser_proxy_bridge.stop()
        except Exception:
            pass
    browser_proxy_bridge = None


def prepare_browser_proxy(use_proxy=True, log_callback=None):
    proxy = get_configured_proxy()
    if not use_proxy or not proxy:
        return "", None
    if _proxy_has_auth(proxy):
        parsed = _parse_proxy_url(proxy)
        scheme = (parsed.scheme or "http").lower() if parsed else ""
        if scheme in ("http", "https"):
            bridge = LocalAuthProxyBridge(proxy)
            browser_proxy = bridge.start()
            if log_callback:
                log_callback(f"[*] 已为 Chromium 启动本地认证代理桥: {browser_proxy}")
            return browser_proxy, bridge
        stripped = _strip_proxy_auth(proxy)
        if log_callback:
            log_callback("[!] Chromium 暂不直接支持该认证代理协议，已使用去认证代理地址，失败将回退直连")
        return stripped, None
    return proxy, None


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    domain = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    return domain


def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return os.path.join(os.path.dirname(__file__), "token.json")


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def get_grok2api_remote_api_bases(base):
    """生成 grok2api 管理 API 候选根路径。

    参数:
      - base str: 用户配置的 grok2api 远端地址

    返回:
      - list[str]: 依次尝试的管理 API 根路径
    """
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return []
    lower = normalized.lower()
    candidates = [normalized]
    if lower.endswith("/admin/api"):
        return candidates
    if lower.endswith("/admin"):
        candidates.append(f"{normalized}/api")
    else:
        candidates.append(f"{normalized}/admin/api")
    seen = set()
    unique = []
    for item in candidates:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    api_bases = get_grok2api_remote_api_bases(base)
    add_errors = []
    # 优先使用 add 接口，避免全量覆盖远端池
    add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
    for api_base in api_bases:
        endpoint = f"{api_base}/tokens/add"
        try:
            resp_add = http_post(
                endpoint,
                headers=headers,
                params=query,
                json=add_payload,
                timeout=30,
            )
            resp_add.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({endpoint})")
            return True
        except Exception as add_exc:
            add_errors.append(f"{endpoint}: {add_exc}")
    if log_callback:
        log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {'; '.join(add_errors)}")

    # 兜底：旧版全量保存接口
    current = {}
    fallback_base = api_bases[0] if api_bases else base
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20)
            if resp.status_code == 200:
                payload = resp.json()
                current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
                fallback_base = api_base
                break
        except Exception:
            continue
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    save_errors = []
    save_bases = []
    for item in [fallback_base, *(api_bases or [base])]:
        if item and item not in save_bases:
            save_bases.append(item)
    for api_base in save_bases:
        try:
            resp2 = http_post(f"{api_base}/tokens", headers=headers, params=query, json=current, timeout=30)
            resp2.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({api_base}/tokens)")
            return True
        except Exception as save_exc:
            save_errors.append(f"{api_base}/tokens: {save_exc}")
    raise RuntimeError(f"grok2api 远端 /tokens 全量模式写入失败: {'; '.join(save_errors)}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    if config.get("grok2api_auto_add_local", True):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def apply_browser_proxy_option(options, proxy):
    if not proxy:
        return
    if hasattr(options, "set_proxy"):
        try:
            options.set_proxy(proxy)
            return
        except Exception:
            pass
    if not hasattr(options, "set_argument"):
        raise AttributeError("当前 DrissionPage ChromiumOptions 不支持设置浏览器代理")
    try:
        options.set_argument(f"--proxy-server={proxy}")
    except TypeError:
        options.set_argument("--proxy-server", proxy)


def create_browser_options(browser_proxy=""):
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    # Server-friendly browser path / flags
    for _browser_path in (
        os.environ.get("BROWSER_PATH") or "",
        "/snap/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if _browser_path and os.path.exists(_browser_path):
            try:
                options.set_browser_path(_browser_path)
            except Exception:
                pass
            break
    for _arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"):
        try:
            options.set_argument(_arg)
        except Exception:
            pass
    apply_browser_proxy_option(options, browser_proxy)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    return options


def get_browser_engine():
    """chromium (headed DrissionPage) | camoufox (headless anti-detect Firefox)."""
    env = str(os.environ.get("GROK_BROWSER_ENGINE") or "").strip().lower()
    if env in ("chromium", "chrome", "drission", "headed"):
        return "chromium"
    if env in ("camoufox", "firefox", "headless", "cfox"):
        return "camoufox"
    engine = str(config.get("browser_engine") or "chromium").strip().lower()
    if engine in ("camoufox", "firefox", "headless", "cfox"):
        return "camoufox"
    return "chromium"


_CA_BUNDLE_CACHE = None


def _path_has_non_ascii(path):
    try:
        str(path or "").encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def get_ca_bundle_path():
    """Return a CA bundle path safe for curl_cffi (ASCII path on Windows).

    certifi under project dirs like D:\\下载\\... triggers curl error 77.
    """
    global _CA_BUNDLE_CACHE
    if _CA_BUNDLE_CACHE and os.path.exists(_CA_BUNDLE_CACHE):
        return _CA_BUNDLE_CACHE
    try:
        import certifi
        import shutil

        src = certifi.where()
        if src and os.path.exists(src) and not _path_has_non_ascii(src):
            _CA_BUNDLE_CACHE = src
            return _CA_BUNDLE_CACHE
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
        dst = os.path.join(base, "grok-register-cacert.pem")
        if src and os.path.exists(src):
            try:
                if (not os.path.exists(dst)) or (
                    os.path.getmtime(src) > os.path.getmtime(dst)
                ):
                    shutil.copy2(src, dst)
            except Exception:
                # last resort: still point to dst if previous copy exists
                pass
            if os.path.exists(dst):
                _CA_BUNDLE_CACHE = dst
                # Help child libs that read env vars
                os.environ.setdefault("SSL_CERT_FILE", dst)
                os.environ.setdefault("CURL_CA_BUNDLE", dst)
                os.environ.setdefault("REQUESTS_CA_BUNDLE", dst)
                return _CA_BUNDLE_CACHE
        if src and os.path.exists(src):
            _CA_BUNDLE_CACHE = src
            return _CA_BUNDLE_CACHE
    except Exception:
        pass
    return True


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    # curl_cffi defaults verify=True and uses certifi path; force ASCII CA when needed
    if "verify" not in request_kwargs:
        request_kwargs["verify"] = get_ca_bundle_path()
    return request_kwargs


def _requests_fallback(method, url, **request_kwargs):
    """Fall back to stdlib requests when curl_cffi TLS/CA fails on non-ASCII paths."""
    import requests as std_requests

    kwargs = dict(request_kwargs)
    # std requests uses verify path/bool similarly
    if method == "GET":
        return std_requests.get(url, **kwargs)
    return std_requests.post(url, **kwargs)


def _is_tls_or_ca_error(exc):
    err = str(exc or "").lower()
    return any(
        x in err
        for x in (
            "certificate verify locations",
            "curl: (77)",
            "curl: (35)",
            "ssl",
            "tls connect error",
            "openssl_internal",
        )
    )


def http_get(url, **kwargs):
    request_kwargs = _build_request_kwargs(**kwargs)
    try:
        return requests.get(url, **request_kwargs)
    except Exception as exc:
        err = str(exc or "")
        # CA path issue: rebuild bundle and retry once
        if "certificate verify locations" in err or "curl: (77)" in err:
            global _CA_BUNDLE_CACHE
            _CA_BUNDLE_CACHE = None
            request_kwargs["verify"] = get_ca_bundle_path()
            try:
                return requests.get(url, **request_kwargs)
            except Exception as exc2:
                exc = exc2
                err = str(exc2 or "")
        if _is_tls_or_ca_error(exc):
            try:
                return _requests_fallback("GET", url, **request_kwargs)
            except Exception:
                pass
        if request_kwargs.get("proxies") and is_proxy_connection_error(exc):
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    request_kwargs = _build_request_kwargs(**kwargs)
    try:
        return requests.post(url, **request_kwargs)
    except Exception as exc:
        err = str(exc or "")
        if "certificate verify locations" in err or "curl: (77)" in err:
            global _CA_BUNDLE_CACHE
            _CA_BUNDLE_CACHE = None
            request_kwargs["verify"] = get_ca_bundle_path()
            try:
                return requests.post(url, **request_kwargs)
            except Exception as exc2:
                exc = exc2
                err = str(exc2 or "")
        if _is_tls_or_ca_error(exc):
            try:
                return _requests_fallback("POST", url, **request_kwargs)
            except Exception:
                pass
        if request_kwargs.get("proxies") and is_proxy_connection_error(exc):
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def get_round_timeout_sec():
    """Per-account wall-clock timeout in seconds (default 300)."""
    for key in ("ROUND_TIMEOUT_SEC", "ROUND_TIMEOUT"):
        raw = str(os.environ.get(key, "") or "").strip()
        if not raw:
            continue
        try:
            return max(60, min(int(float(raw)), 3600))
        except Exception:
            pass
    try:
        raw = config.get("round_timeout_sec", 300)
        return max(60, min(int(float(raw)), 3600))
    except Exception:
        return 300


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鍒涘缓閭澶辫触: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 鑾峰彇token澶辫触: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鑾峰彇閭欢璇︽儏澶辫触: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("鑾峰彇 YYDS token 澶辫触")
    print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")



# ===== tempmailer.cc provider (unofficial internal API) =====
TEMPMAILER_API_BASE_DEFAULT = "https://tempmailer.cc/api"


def get_tempmailer_api_base():
    return str(config.get("tempmailer_api_base") or TEMPMAILER_API_BASE_DEFAULT).rstrip("/")


def tempmailer_new_session_id():
    return str(uuid.uuid4())


def tempmailer_headers(session_id, content_type=True):
    headers = {
        "User-Agent": get_user_agent(),
        "Accept": "application/json",
        "Origin": "https://tempmailer.cc",
        "Referer": "https://tempmailer.cc/zh/",
        "x-tempmailer-session": session_id or tempmailer_new_session_id(),
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def tempmailer_prewarm(session_id):
    base = get_tempmailer_api_base()
    try:
        http_get(f"{base}/prewarm", headers=tempmailer_headers(session_id, content_type=False), timeout=15)
    except Exception:
        pass


def get_tempmailer_domains():
    domains = []
    raw_list = config.get("tempmailer_domains") or []
    if isinstance(raw_list, str):
        raw_list = [x.strip() for x in raw_list.split(",") if x.strip()]
    for d in raw_list:
        d = str(d).strip()
        if d and d not in domains and d != "example.com":
            domains.append(d)
    single = str(config.get("tempmailer_domain") or config.get("defaultDomains") or "").strip()
    for d in single.split(","):
        d = d.strip()
        if d and d not in domains and d != "example.com":
            domains.append(d)
    return domains


def pick_tempmailer_domain(rotate=False):
    """Pick tempmailer domain; rotate=True advances to next configured domain."""
    global _tempmailer_domain_index
    domains = get_tempmailer_domains()
    if not domains:
        return ""
    if rotate:
        _tempmailer_domain_index = (_tempmailer_domain_index + 1) % len(domains)
    return domains[_tempmailer_domain_index % len(domains)]


def tempmailer_generate_email(session_id=None, domain=None):
    """Create a temp mailbox. Returns (email, session_id). session is required for inbox."""
    base = get_tempmailer_api_base()
    sid = session_id or tempmailer_new_session_id()
    tempmailer_prewarm(sid)
    headers = tempmailer_headers(sid, content_type=True)
    payload = {}
    use_domain = (domain or pick_tempmailer_domain(rotate=False) or "").strip()
    if use_domain and use_domain not in ("example.com",):
        payload["domain"] = use_domain
    resp = http_post(f"{base}/generate", json=payload, headers=headers, timeout=30)
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"tempmailer generate 返回非JSON: HTTP {resp.status_code} {resp.text[:300]}")
    email = (data or {}).get("email")
    if not email:
        raise Exception(f"tempmailer generate 失败: HTTP {resp.status_code} {data}")
    return email, sid


def tempmailer_get_email_and_token():
    domains = get_tempmailer_domains() or [""]
    errors = []
    # 多域名时逐个尝试
    for i, _ in enumerate(domains):
        domain = pick_tempmailer_domain(rotate=(i > 0))
        try:
            email, sid = tempmailer_generate_email(domain=domain or None)
            print(f"[*] 已创建 tempmailer 邮箱: {email}" + (f" (domain={domain})" if domain else ""))
            return email, sid
        except Exception as exc:
            errors.append(f"{domain or 'auto'}: {exc}")
            continue
    raise Exception("tempmailer 创建邮箱失败: " + "; ".join(errors))


def tempmailer_inbox_url(email, message_id=None):
    base = get_tempmailer_api_base()
    encoded = urllib.parse.quote(email, safe="")
    if message_id is None:
        return f"{base}/inbox/{encoded}"
    return f"{base}/inbox/{encoded}/{urllib.parse.quote(str(message_id), safe='')}"


def tempmailer_get_messages(email, session_id):
    resp = http_get(
        tempmailer_inbox_url(email),
        headers=tempmailer_headers(session_id, content_type=False),
        timeout=20,
    )
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"tempmailer inbox 返回非JSON: HTTP {resp.status_code} {resp.text[:300]}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "mails", "items", "data", "inbox"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        # single message object
        if data.get("id") or data.get("subject") or data.get("html_body") or data.get("text_body"):
            return [data]
    return []


def tempmailer_get_message_detail(email, message_id, session_id):
    resp = http_get(
        tempmailer_inbox_url(email, message_id),
        headers=tempmailer_headers(session_id, content_type=False),
        timeout=20,
    )
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"tempmailer mail detail 返回非JSON: HTTP {resp.status_code} {resp.text[:300]}")
    if isinstance(data, dict):
        return data
    return {}


def tempmailer_message_text(msg):
    parts = []
    if not isinstance(msg, dict):
        return ""
    for field in (
        "subject",
        "text_body",
        "html_body",
        "text",
        "html",
        "body",
        "content",
        "raw",
        "intro",
        "snippet",
        "preview",
    ):
        value = msg.get(field)
        if isinstance(value, str) and value.strip():
            if field in ("html_body", "html"):
                parts.append(re.sub(r"<[^>]+>", " ", value))
            else:
                parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    parts.append(re.sub(r"<[^>]+>", " ", item) if field in ("html_body", "html") else item)
    return "\n".join(parts)


def tempmailer_get_oai_code(
    session_id,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = tempmailer_get_messages(email, session_id)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] tempmailer 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] tempmailer 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = (
                msg.get("id")
                or msg.get("msgid")
                or msg.get("message_id")
                or msg.get("uid")
                or msg.get("mail_id")
            )
            # fallback key when id missing
            if not msg_id:
                msg_id = f"subj:{msg.get('subject','')}|from:{msg.get('from','')}|ts:{msg.get('date') or msg.get('created_at') or msg.get('timestamp') or ''}"
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 8:
                continue
            seen_attempts[msg_id] = attempt + 1
            combined = tempmailer_message_text(msg)
            subject = str(msg.get("subject", "") or "")
            # detail endpoint if available and content thin
            if msg_id and (not combined.strip() or len(combined) < 20):
                try:
                    detail = tempmailer_get_message_detail(email, msg_id, session_id)
                    combined = (combined + "\n" + tempmailer_message_text(detail)).strip()
                    if not subject:
                        subject = str(detail.get("subject", "") or "")
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] tempmailer detail失败，用列表内容: {exc}")
            if log_callback:
                log_callback(f"[Debug] tempmailer 收到邮件: {subject or '(no subject)'}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] tempmailer 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"tempmailer 在 {timeout}s 内未收到验证码邮件")


# ===== end tempmailer =====


def get_email_provider():
    p = str(config.get("email_provider") or "tempmailer").strip().lower()
    # inboxkitten.com 域名已被 xAI 拒绝，强制改用 tempmailer
    if p in ("inboxkitten", "inbox_kitten"):
        return "tempmailer"
    return p or "tempmailer"


def email_provider_ready(provider: str) -> bool:
    """判断邮箱源是否已配置到可尝试状态。"""
    p = (provider or "").strip().lower()
    # 已从内置移除：xAI 拒绝 inboxkitten.com 域名
    if p in ("inboxkitten", "inbox_kitten"):
        return False
    if p == "tempmailer":
        return bool(get_tempmailer_api_base())
    if p == "duckmail":
        return bool(str(config.get("duckmail_api_key") or "").strip())
    if p == "yyds":
        return bool(
            str(config.get("yyds_api_key") or "").strip()
            or str(config.get("yyds_jwt") or "").strip()
        )
    if p == "cloudflare":
        return bool(str(config.get("cloudflare_api_base") or "").strip())
    return False


def get_email_provider_chain():
    """可用邮箱源列表：优先 config.email_providers，过滤未配置项。"""
    raw = config.get("email_providers")
    if isinstance(raw, str):
        chain = [x.strip().lower() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list):
        chain = [str(x).strip().lower() for x in raw if str(x).strip()]
    else:
        chain = []
    # 过滤已废弃的 inboxkitten
    chain = [p for p in chain if p not in ("inboxkitten", "inbox_kitten")]
    primary = get_email_provider()
    if primary and primary not in chain:
        chain.insert(0, primary)
    if not chain:
        chain = [primary or "tempmailer"]
    # 去重保持顺序
    seen = set()
    ordered = []
    for p in chain:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    ready = [p for p in ordered if email_provider_ready(p)]
    # 若一个都没 ready，仍返回 primary 以免完全不可用
    return ready or ([primary] if primary else ["tempmailer"])


def rotate_email_provider(log_callback=None, reason=""):
    """切换到下一个可用邮箱源；返回新 provider。"""
    global _email_provider_index
    chain = get_email_provider_chain()
    if not chain:
        return get_email_provider()
    if len(chain) == 1:
        # 仅一个源：若是 tempmailer 则轮换域名
        if chain[0] == "tempmailer" and len(get_tempmailer_domains()) > 1:
            domain = pick_tempmailer_domain(rotate=True)
            if log_callback:
                log_callback(
                    f"[*] 邮箱源仅 tempmailer，切换域名: {domain}"
                    + (f" ({reason})" if reason else "")
                )
            return chain[0]
        if log_callback:
            log_callback(
                f"[!] 无其它可用邮箱源可切换，继续使用 {chain[0]}"
                + (f" ({reason})" if reason else "")
            )
        config["email_provider"] = chain[0]
        return chain[0]
    _email_provider_index = (_email_provider_index + 1) % len(chain)
    new_p = chain[_email_provider_index]
    config["email_provider"] = new_p
    if log_callback:
        log_callback(
            f"[*] 邮箱源已切换为: {new_p}（链: {', '.join(chain)}）"
            + (f" · 原因: {reason}" if reason else "")
        )
    return new_p


def is_mail_related_error(exc) -> bool:
    msg = str(exc or "").lower()
    keys = (
        "验证码",
        "邮箱",
        "邮件",
        "tempmailer",
        "inboxkitten",
        "duckmail",
        "yyds",
        "未收到",
        "generate",
        "创建邮箱",
        "inbox",
        "/api/mail",
        "new_address",
        "mail_api",
        "http 429",
        "rate limit",
        "限流",
        "所有邮箱源",
    )
    return any(k.lower() in msg for k in keys)


def _get_email_and_token_once(provider, api_key=None):
    provider = (provider or "").strip().lower()
    if provider in ("inboxkitten", "inbox_kitten"):
        raise Exception(
            "InboxKitten 已移除：xAI 拒绝域名 inboxkitten.com，请改用 tempmailer 或自定义邮箱"
        )
    if provider == "tempmailer":
        return tempmailer_get_email_and_token()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    # duckmail default
    key = api_key or get_duckmail_api_key()
    if not key:
        raise Exception("DuckMail API Key 未配置")
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def get_email_and_token(api_key=None, log_callback=None):
    """创建临时邮箱；失败时按 email_providers 自动切换备用源。"""
    global _email_provider_index
    chain = get_email_provider_chain()
    failover = bool(config.get("email_failover", True))
    # 对齐当前 provider 在链中的下标
    primary = get_email_provider()
    if primary in chain:
        _email_provider_index = chain.index(primary)
    errors = []
    attempts = len(chain) if failover else 1
    for i in range(attempts):
        provider = chain[(_email_provider_index + i) % len(chain)]
        config["email_provider"] = provider
        try:
            email, token = _get_email_and_token_once(provider, api_key=api_key)
            if log_callback:
                log_callback(f"[*] 邮箱源 {provider} 创建成功: {email}")
            # 记住当前成功源
            _email_provider_index = chain.index(provider) if provider in chain else _email_provider_index
            return email, token
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            if log_callback:
                log_callback(f"[!] 邮箱源 {provider} 创建失败: {exc}")
            if not failover:
                break
            # 尝试下一个
            continue
    raise Exception("所有邮箱源均创建失败: " + " | ".join(errors))


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider in ("inboxkitten", "inbox_kitten"):
        provider = "tempmailer"
    if provider == "tempmailer":
        return tempmailer_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    """从邮件主题/正文提取 xAI/Grok 验证码（对齐 grok-reg-tool 策略）。"""
    content = text or ""
    if subject:
        content = f"Subject: {subject}\n{content}"
    if not content:
        return None

    # Subject 行优先：6F2-CT8 xAI confirmation code
    match = re.search(
        r"(?:Subject:\s*)?([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI",
        content,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()

    # 独立 XXX-XXX（避免被邮箱/长串误伤）
    match = re.search(
        r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])",
        content,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()

    # 文案旁带标签
    match = re.search(
        r"(?:verification\s+code|验证码|your\s+code|confirmation\s+code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        content,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()

    # HTML 灰底块常见样式
    match = re.search(
        r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>",
        content,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()

    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1)

    # 6 位数字兜底（排除常见噪声）
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code != "177010":
            return code
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    try:
        text = str(res.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                return False, message
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

browser = None
page = None
browser_proxy_bridge = None
browser_started_with_proxy = False


def setup_light_theme(root):
    try:
        root.option_add("*Background", UI_BG)
        root.option_add("*Foreground", UI_FG)
        root.option_add("*selectBackground", UI_ACTIVE_BG)
        root.option_add("*selectForeground", UI_FG)
        root.option_add("*insertBackground", UI_FG)
        root.option_add("*Entry.Background", UI_ENTRY_BG)
        root.option_add("*Text.Background", UI_ENTRY_BG)
        root.option_add("*Menu.Background", UI_ENTRY_BG)
        root.option_add("*Menu.Foreground", UI_FG)
        style = ttk.Style(root)
        available = set(style.theme_names())
        if "clam" in available:
            style.theme_use("clam")
        elif "default" in available:
            style.theme_use("default")
        root.configure(bg=UI_BG)
        style.configure(".", background=UI_BG, foreground=UI_FG, fieldbackground=UI_ENTRY_BG)
        style.configure("TFrame", background=UI_BG)
        style.configure("TLabelframe", background=UI_BG, foreground=UI_FG)
        style.configure("TLabelframe.Label", background=UI_BG, foreground=UI_FG)
        style.configure("TLabel", background=UI_BG, foreground=UI_FG)
        style.configure("TCheckbutton", background=UI_BG, foreground=UI_FG)
        style.configure("TButton", background=UI_BUTTON_BG, foreground=UI_FG)
        style.configure("TEntry", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
        style.configure("TCombobox", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
        style.configure("TSpinbox", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
    except Exception:
        pass


def tk_label(parent, text="", **kwargs):
    return tk.Label(parent, text=text, bg=kwargs.pop("bg", UI_BG), fg=kwargs.pop("fg", UI_FG), **kwargs)


def tk_entry(parent, textvariable=None, width=30, **kwargs):
    return tk.Entry(
        parent,
        textvariable=textvariable,
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        insertbackground=UI_FG,
        disabledbackground="#2f2f2f",
        disabledforeground=UI_MUTED_FG,
        highlightthickness=1,
        highlightbackground="#555555",
        relief=tk.SOLID,
        **kwargs,
    )


def tk_button(parent, text="", command=None, state=tk.NORMAL, **kwargs):
    return tk.Button(
        parent,
        text=text,
        command=command,
        state=state,
        bg=UI_BUTTON_BG,
        fg=UI_FG,
        activebackground=UI_ACTIVE_BG,
        activeforeground=UI_FG,
        disabledforeground="#777777",
        relief=tk.RAISED,
        padx=10,
        pady=3,
        **kwargs,
    )


def tk_checkbutton(parent, text="", variable=None, **kwargs):
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        bg=UI_BG,
        fg=UI_FG,
        activebackground=UI_BG,
        activeforeground=UI_FG,
        selectcolor="#3d7be0",
        **kwargs,
    )


def tk_option_menu(parent, variable, values, width=12):
    menu = tk.OptionMenu(parent, variable, *values)
    menu.configure(
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        activebackground=UI_ACTIVE_BG,
        activeforeground=UI_FG,
        highlightthickness=1,
        highlightbackground="#555555",
        relief=tk.SOLID,
    )
    menu["menu"].configure(bg=UI_ENTRY_BG, fg=UI_FG, activebackground=UI_ACTIVE_BG, activeforeground=UI_FG)
    return menu


def start_browser(log_callback=None, use_proxy=True):
    global browser, page, browser_proxy_bridge, browser_started_with_proxy
    last_exc = None
    proxy_enabled = bool(use_proxy and get_configured_proxy())
    engine = get_browser_engine()
    if log_callback:
        engine_label = "Camoufox 无头" if engine == "camoufox" else "Chromium 有头"
        log_callback(f"[*] 浏览器引擎: {engine_label}")
    for attempt in range(1, 5):
        bridge = None
        try:
            browser_proxy, bridge = prepare_browser_proxy(use_proxy=use_proxy, log_callback=log_callback)
            if engine == "camoufox":
                # Camoufox supports authenticated proxies natively; prefer original URL
                configured = get_configured_proxy() if use_proxy else ""
                if configured and _proxy_has_auth(configured):
                    camoufox_proxy = configured
                else:
                    camoufox_proxy = browser_proxy or configured or ""
                lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
                if lib_dir not in sys.path:
                    sys.path.insert(0, lib_dir)
                from camoufox_backend import start_camoufox_browser

                browser = start_camoufox_browser(
                    browser_proxy=camoufox_proxy,
                    log_callback=log_callback,
                )
                browser_started_with_proxy = bool(camoufox_proxy)
                # Local auth bridge not needed when Camoufox uses auth proxy directly
                if bridge is not None and configured and _proxy_has_auth(configured):
                    try:
                        bridge.stop()
                    except Exception:
                        pass
                    bridge = None
            else:
                browser = Chromium(create_browser_options(browser_proxy=browser_proxy))
                browser_started_with_proxy = bool(browser_proxy)
            browser_proxy_bridge = bridge
            tabs = browser.get_tabs()
            page = tabs[-1] if tabs else browser.new_tab()
            if log_callback and getattr(browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser.user_data_path}")
            if log_callback and get_configured_proxy():
                mode = "代理" if browser_started_with_proxy else "直连"
                log_callback(f"[*] 浏览器网络模式: {mode}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser, page
        except Exception as exc:
            last_exc = exc
            if bridge is not None:
                try:
                    bridge.stop()
                except Exception:
                    pass
            if log_callback:
                mode = "代理" if proxy_enabled else "直连"
                log_callback(f"[Debug] 浏览器{mode}启动失败(第{attempt}/4次): {exc}")
            try:
                if browser is not None:
                    browser.quit(del_data=True)
            except Exception:
                pass
            browser = None
            page = None
            browser_proxy_bridge = None
            browser_started_with_proxy = False
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    global browser, page, browser_started_with_proxy
    if browser is not None:
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
    stop_browser_proxy_bridge()
    browser = None
    page = None
    browser_started_with_proxy = False


def restart_browser(log_callback=None, use_proxy=True):
    stop_browser()
    return start_browser(log_callback=log_callback, use_proxy=use_proxy)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    if log_callback:
        log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
    stop_browser()
    collected = gc.collect()
    if log_callback:
        log_callback(f"[*] Python GC 已回收对象数: {collected}")


def refresh_active_page():
    global browser, page
    if browser is None:
        restart_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    global page
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    // Spanish / other locales (geoip may switch UI language)
    if (lower.includes('correoelectr') || lower.includes('correo') && lower.includes('electr')) {
        if (lower.includes('regist') || lower.includes('contin') || lower.includes('usar') || lower.includes('con')) return 92;
    }
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with') || lower.includes('regist'))) return 80;
    if (lower.includes('e-mail') && (lower.includes('sign') || lower.includes('contin') || lower.includes('regist'))) return 80;
    if (lower === 'email' || lower.includes('邮箱') || lower === 'correo' || lower.includes('mail')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    global browser, page
    raise_if_cancelled(cancel_callback)
    if browser is None:
        start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")

    def _open_with_current_browser():
        global page
        try:
            page = browser.get_tab(0)
            page.get(SIGNUP_URL)
        except Exception as e:
            if log_callback:
                log_callback(f"[Debug] 打开URL异常: {e}")
            page = browser.new_tab(SIGNUP_URL)
        page.wait.doc_loaded()

    allow_fallback = bool(config.get("allow_proxy_fallback", False))

    try:
        _open_with_current_browser()
    except Exception as e:
        if browser_started_with_proxy and get_configured_proxy() and allow_fallback:
            if log_callback:
                log_callback(f"[!] 浏览器代理访问注册页失败，自动回退直连: {e}")
            restart_browser(log_callback=log_callback, use_proxy=False)
            _open_with_current_browser()
        else:
            if browser_started_with_proxy and get_configured_proxy() and not allow_fallback:
                raise Exception(
                    f"浏览器经代理打开注册页失败（已关闭直连回退，请检查 Clash 是否监听 7890）: {e}"
                ) from e
            raise

    if browser_started_with_proxy and page_has_proxy_error(page):
        if allow_fallback:
            if log_callback:
                log_callback("[!] 浏览器页面显示代理错误，自动回退直连")
            restart_browser(log_callback=log_callback, use_proxy=False)
            _open_with_current_browser()
        else:
            raise Exception(
                "浏览器页面显示代理错误（已关闭直连回退）。请先打开 Clash，确认 mixed/HTTP 端口为 7890，并选好可用节点。"
            )

    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token(log_callback=log_callback)
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}（源={get_email_provider()}）")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            r"""
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('correoelectr') || (lower.includes('correo') && lower.includes('electr'))) {
        if (lower.includes('regist') || lower.includes('contin') || lower.includes('usar') || lower.includes('con')) return 92;
    }
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with') || lower.includes('regist'))) return 80;
    if (lower === 'email' || lower.includes('邮箱') || lower.includes('mail')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", page.url if page else "") if isinstance(filled, dict) else (page.url if page else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", page.url if page else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None, allow_reset=False):
    global page
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    # Camoufox/headless: prefer real frame click helper (do not reset by default —
    # reset often clears a half-solved widget and keeps token length at 0).
    if allow_reset:
        try:
            page.run_js(
                "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
            )
        except Exception:
            pass

    for attempt in range(0, 24):
        raise_if_cancelled(cancel_callback)
        try:
            token = ""
            if hasattr(page, "get_turnstile_token"):
                try:
                    token = str(page.get_turnstile_token() or "").strip()
                except Exception:
                    token = ""
            if not token:
                token = page.run_js(
                    """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                    """
                )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            # 1) Camoufox dedicated click path (Playwright frames)
            if hasattr(page, "click_turnstile"):
                try:
                    detail = page.click_turnstile() or {}
                    if log_callback and (attempt == 0 or attempt % 4 == 0):
                        log_callback(
                            f"[Debug] Turnstile 点击: clicked={detail.get('clicked')} "
                            f"method={detail.get('method') or '-'} "
                            f"frames={detail.get('frames')} "
                            f"token_len={detail.get('token_len')}"
                        )
                    token2 = str(detail.get("token") or "").strip()
                    if not token2 and hasattr(page, "get_turnstile_token"):
                        token2 = str(page.get_turnstile_token() or "").strip()
                    if len(token2) >= 80:
                        if log_callback:
                            log_callback(f"[*] Turnstile 已通过，token长度={len(token2)}")
                        return token2
                except Exception as click_exc:
                    if log_callback and attempt == 0:
                        log_callback(f"[Debug] Turnstile click_turnstile 异常: {click_exc}")

            # 2) DrissionPage-style shadow DOM path (Chromium)
            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        sleep_with_cancel(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                if log_callback:
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                if token_len == "0":
                    now0 = time.time()
                    if wait_cf_since is None:
                        wait_cf_since = now0
                    # Managed Turnstile often auto-solves if we wait; avoid spam-clicking early.
                    waited = now0 - wait_cf_since
                    if waited >= 6 and hasattr(page, "click_turnstile"):
                        try:
                            detail = page.click_turnstile() or {}
                            if log_callback:
                                log_callback(
                                    f"[Debug] Turnstile 交互: clicked={detail.get('clicked')} "
                                    f"method={detail.get('method') or '-'} token_len={detail.get('token_len')}"
                                )
                        except Exception as early_exc:
                            if log_callback:
                                log_callback(f"[Debug] Turnstile 交互失败: {early_exc}")
                    pause_seconds = random.uniform(1.2, 2.5)
                    if log_callback:
                        log_callback(f"[*] Cloudflare token 为空，暂停 {pause_seconds:.1f}s 后继续检测")
                    sleep_with_cancel(pause_seconds, cancel_callback)
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 10 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            # After a grace period, periodically interact with Turnstile (Camoufox)
            if (
                hasattr(page, "click_turnstile")
                and now - wait_cf_since >= 8
                and now - last_cf_retry_at >= 6
            ):
                try:
                    page.click_turnstile()
                except Exception:
                    pass
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def _cookie_name_domain_value(item):
    if isinstance(item, dict):
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", "")).strip()
        domain = str(item.get("domain", "") or "").strip().lstrip(".")
    else:
        name = str(getattr(item, "name", "") or "").strip()
        value = str(getattr(item, "value", "") or "").strip()
        domain = str(getattr(item, "domain", "") or "").strip().lstrip(".")
    return name, domain, value


def _iter_cookie_sources():
    """优先 grok.com 标签页，再扫其它标签页与当前 page（对齐 grok-reg-tool）。"""
    tabs = []
    try:
        if browser is not None and hasattr(browser, "get_tabs"):
            for tab in browser.get_tabs() or []:
                try:
                    url = tab.url or ""
                except Exception:
                    url = ""
                tabs.append((url, tab))
    except Exception:
        pass

    # grok.com 标签优先
    ordered = []
    for url, tab in tabs:
        if "grok.com" in (url or ""):
            ordered.append(tab)
    for url, tab in tabs:
        if "grok.com" not in (url or ""):
            ordered.append(tab)
    if page is not None and page not in ordered:
        ordered.append(page)

    seen = set()
    for tab in ordered:
        try:
            key = id(tab)
            if key in seen:
                continue
            seen.add(key)
            cookies = tab.cookies(all_domains=True, all_info=True) or []
            yield tab, cookies
        except Exception:
            continue


def wait_for_grok_com_landing(timeout=90, log_callback=None, cancel_callback=None):
    """
    注册完成后等浏览器经 SSO 重定向落到 grok.com 登录态。
    grok.com 与 accounts.x.ai 不共享 cookie；过早取 accounts 域 sso 质量差。
    """
    global page
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            # 优先切到已打开的 grok.com 标签
            try:
                if browser is not None and hasattr(browser, "get_tabs"):
                    for tab in browser.get_tabs() or []:
                        try:
                            url = tab.url or ""
                        except Exception:
                            url = ""
                        if "grok.com" in url:
                            page = tab
                            break
            except Exception:
                pass

            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            current_url = ""
            try:
                current_url = page.url or ""
            except Exception:
                current_url = ""
            if current_url != last_url:
                if log_callback:
                    log_callback(f"[*] 等待重定向到 grok.com，当前: {current_url}")
                last_url = current_url

            if "grok.com" in current_url:
                logged_in = False
                try:
                    logged_in = bool(
                        page.run_js(
                            r"""
function isVisible(n) {
  if (!n) return false;
  const s = window.getComputedStyle(n);
  if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
  const r = n.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
if (/grok\.com\/(chat|c)\//.test(location.href)) return true;
const ta = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]'))
  .find(n => isVisible(n) && !n.disabled && !n.readOnly);
if (ta) return true;
// 有 grok.com 域 sso cookie 也视为基本登录
return document.cookie.split(';').some(c => c.trim().startsWith('sso='));
"""
                        )
                    )
                except Exception:
                    logged_in = "grok.com" in current_url
                if logged_in:
                    if log_callback:
                        log_callback(f"[*] 已落到 grok.com 并登录: {current_url}")
                    return True
        except RegistrationCancelled:
            raise
        except Exception as exc:
            if is_page_disconnected_error(exc):
                refresh_active_page()
        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        log_callback(f"[Warn] 等待 grok.com 登录超时，最后 URL: {last_url}")
    return False


def dismiss_cookie_and_consent_banners(log_callback=None):
    """Click OneTrust / cookie consent / continue buttons that block SSO landing."""
    global page
    if page is None:
        return ""
    try:
        result = page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
  return [
    node.innerText,
    node.textContent,
    node.getAttribute('value'),
    node.getAttribute('aria-label'),
    node.getAttribute('title'),
  ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
// Prefer known OneTrust / cookie SDK ids
const idCandidates = [
  'onetrust-accept-btn-handler',
  'accept-recommended-btn-handler',
  'onetrust-reject-all-handler',
];
for (const id of idCandidates) {
  const el = document.getElementById(id);
  if (el && isVisible(el) && id.includes('accept')) {
    el.click();
    return 'clicked-id:' + id;
  }
}
const needles = [
  '接受所有 cookie',
  '接受所有cookie',
  '接受全部 cookie',
  '接受全部cookie',
  '全部接受',
  '接受全部',
  '同意全部',
  '同意并继续',
  'allow all',
  'accept all',
  'accept all cookies',
  'accept cookies',
  'i agree',
  'got it',
  '继续',
  'continue',
];
const nodes = Array.from(document.querySelectorAll(
  'button, [role="button"], input[type="button"], input[type="submit"], a'
)).filter(isVisible);
for (const node of nodes) {
  const t = textOf(node).toLowerCase().replace(/\s+/g, '');
  if (!t) continue;
  for (const n of needles) {
    const nn = n.toLowerCase().replace(/\s+/g, '');
    if (t.includes(nn)) {
      // avoid rejecting all when accept is available
      if (nn.includes('拒绝') || nn.includes('reject') || nn.includes('deny')) continue;
      node.click();
      return 'clicked-text:' + textOf(node).slice(0, 40);
    }
  }
}
// Chinese exact-ish: 接受所有 Cookie (mixed case / spacing)
for (const node of nodes) {
  const raw = textOf(node);
  if (/接受\s*所有\s*Cookie/i.test(raw) || /Accept\s*All/i.test(raw)) {
    node.click();
    return 'clicked-regex:' + raw.slice(0, 40);
  }
}
return '';
"""
        )
        if result and log_callback:
            log_callback(f"[*] 已处理同意/Cookie 弹窗: {result}")
        return result or ""
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 处理 Cookie 弹窗失败: {e}")
        return ""


def wait_for_sso_cookie(
    timeout=120,
    log_callback=None,
    cancel_callback=None,
    prefer_domain="grok.com",
):
    """
    取网页 SSO cookie。
    优先 grok.com 域（grok2api/chat 同域）；accounts.x.ai 仅作回退。
    对齐 FengZi1221/grok-reg-tool 策略。
    """
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    last_consent_retry = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25
    fallback_sso = ""
    fallback_domain = ""
    grok_wait_done = False

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            now = time.time()
            # Cookie / “您正在登录” 同意墙：不点就永远没有 sso
            if now - last_consent_retry >= 1.5:
                dismiss_cookie_and_consent_banners(log_callback=log_callback)
                last_consent_retry = now

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"], a[href]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    if (t.includes('goback') || t.includes('返回')) return false;
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup')
        || t.includes('createaccount') || t.includes('continue') || t.includes('继续')
        || t.includes('signin') || t.includes('proceed') || t.includes('confirm')
        || t.includes('next') || t.includes('agree') || t.includes('accept')
        || t.includes('authorize') || t.includes('allow');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (
                    retried == "final-page-clicked-submit"
                    or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))
                ):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(
                                log_callback=log_callback, cancel_callback=cancel_callback
                            )
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(
                                        f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}"
                                    )
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            # 离开注册页后，只尝试一次等待落到 grok.com 登录态
            try:
                cur = (page.url or "") if page is not None else ""
            except Exception:
                cur = ""
            left_signup = bool(
                cur
                and ("sign-up" not in cur)
                and not cur.rstrip("/").endswith("accounts.x.ai")
            )
            if (not grok_wait_done) and (left_signup or "grok.com" in cur):
                remain = max(8, min(70, int(deadline - time.time()) - 5))
                if remain > 0:
                    wait_for_grok_com_landing(
                        timeout=remain,
                        log_callback=log_callback,
                        cancel_callback=cancel_callback,
                    )
                grok_wait_done = True

            preferred_hit = None
            for _tab, cookies in _iter_cookie_sources():
                for item in cookies:
                    name, domain, value = _cookie_name_domain_value(item)
                    if name:
                        last_seen_names.add(f"{name}@{domain}" if domain else name)
                    if name != "sso" or not value:
                        continue
                    # 优先 grok.com
                    if prefer_domain and prefer_domain in domain:
                        preferred_hit = (domain, value)
                        break
                    # 次优：非 accounts.x.ai 的 sso
                    if not fallback_sso:
                        fallback_sso = value
                        fallback_domain = domain or "?"
                    elif "accounts.x.ai" in (fallback_domain or "") and "accounts.x.ai" not in domain:
                        fallback_sso = value
                        fallback_domain = domain or "?"
                    # accounts.x.ai 仅记录，不优先
                    if "accounts.x.ai" in domain and not fallback_sso:
                        fallback_sso = value
                        fallback_domain = domain
                if preferred_hit:
                    break

            if preferred_hit:
                domain, value = preferred_hit
                if log_callback:
                    log_callback(f"[*] 已获取到 {domain} 域的 sso cookie（优先 grok.com）")
                return value

        except AccountRetryNeeded:
            raise
        except RegistrationCancelled:
            raise
        except Exception as exc:
            if is_page_disconnected_error(exc):
                refresh_active_page()

        sleep_with_cancel(1, cancel_callback)

    # 超时前再扫一轮；允许用非 prefer 域回退
    try:
        for _tab, cookies in _iter_cookie_sources():
            for item in cookies:
                name, domain, value = _cookie_name_domain_value(item)
                if name == "sso" and value:
                    if prefer_domain and prefer_domain in domain:
                        if log_callback:
                            log_callback(f"[*] 已获取到 {domain} 域的 sso cookie")
                        return value
                    if not fallback_sso:
                        fallback_sso = value
                        fallback_domain = domain or "?"
    except Exception:
        pass

    if fallback_sso:
        if log_callback:
            log_callback(
                f"[Warn] 未拿到 {prefer_domain} 域 sso，回退使用 {fallback_domain} 域（质量可能较差）"
            )
        return fallback_sso

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("1120x900")
        self.root.minsize(960, 700)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.setup_ui()

    def setup_ui(self):
        load_config()
        main_frame = tk.Frame(self.root, bg=UI_BG, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(3, weight=1)

        config_frame = tk.LabelFrame(
            main_frame,
            text="配置",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=10,
            pady=10,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        config_frame.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        config_frame.grid_columnconfigure(1, weight=1, minsize=260)
        config_frame.grid_columnconfigure(3, weight=1, minsize=260)

        def add_label(row, column, text):
            tk_label(config_frame, text=text, bg=UI_PANEL_BG).grid(
                row=row,
                column=column,
                sticky=tk.W,
                padx=(0, 6),
                pady=3,
            )

        def add_field(widget, row, column, columnspan=1, sticky=tk.EW):
            widget.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                sticky=sticky,
                padx=(0, 14),
                pady=3,
            )

        add_label(0, 0, "邮箱服务商:")
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "duckmail"))
        self.email_provider_combo = tk_option_menu(
            config_frame,
            self.email_provider_var,
            ["tempmailer", "duckmail", "yyds", "cloudflare"],
            width=12,
        )
        add_field(self.email_provider_combo, 0, 1, sticky=tk.W)

        add_label(0, 2, "注册数量:")
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = tk.Spinbox(
            config_frame,
            from_=1,
            to=2500,
            width=8,
            textvariable=self.count_var,
            bg=UI_ENTRY_BG,
            fg=UI_FG,
            insertbackground=UI_FG,
            buttonbackground=UI_BUTTON_BG,
            disabledbackground="#2f2f2f",
            disabledforeground=UI_MUTED_FG,
            relief=tk.SOLID,
        )
        add_field(self.count_spinbox, 0, 3, sticky=tk.W)

        add_label(1, 0, "注册选项:")
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = tk_checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        add_field(self.nsfw_check, 1, 1, sticky=tk.W)

        add_label(1, 2, "代理（可选）:")
        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = tk_entry(config_frame, textvariable=self.proxy_var, width=34)
        add_field(self.proxy_entry, 1, 3)

        add_label(2, 0, "DuckMail API Key:")
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = tk_entry(config_frame, textvariable=self.api_key_var, width=34)
        add_field(self.api_key_entry, 2, 1)

        add_label(2, 2, "Cloudflare 鉴权模式:")
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "none"))
        self.cloudflare_auth_mode_combo = tk_option_menu(
            config_frame, self.cloudflare_auth_mode_var, ["query-key", "bearer", "x-api-key", "x-admin-auth", "none"], width=12
        )
        add_field(self.cloudflare_auth_mode_combo, 2, 3, sticky=tk.W)

        add_label(3, 0, "Cloudflare API Base:")
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = tk_entry(config_frame, textvariable=self.cloudflare_api_base_var, width=72)
        add_field(self.cloudflare_api_base_entry, 3, 1, columnspan=3)

        add_label(4, 0, "Cloudflare API Key:")
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = tk_entry(config_frame, textvariable=self.cloudflare_api_key_var, width=34)
        add_field(self.cloudflare_api_key_entry, 4, 1)

        add_label(4, 2, "CF 路径:")
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/api/domains"),
                    config.get("cloudflare_path_accounts", "/api/new_address"),
                    config.get("cloudflare_path_token", "/api/token"),
                    config.get("cloudflare_path_messages", "/api/mails"),
                ]
            )
        )
        self.cloudflare_paths_entry = tk_entry(config_frame, textvariable=self.cloudflare_paths_var, width=34)
        add_field(self.cloudflare_paths_entry, 4, 3)

        add_label(5, 0, "grok2api 本地入池:")
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_local", True)))
        self.grok2api_local_auto_check = tk_checkbutton(config_frame, variable=self.grok2api_local_auto_var)
        add_field(self.grok2api_local_auto_check, 5, 1, sticky=tk.W)

        add_label(5, 2, "grok2api 池名:")
        self.grok2api_pool_name_var = tk.StringVar(value=str(config.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = tk_option_menu(
            config_frame, self.grok2api_pool_name_var, ["ssoBasic", "ssoSuper"], width=12
        )
        add_field(self.grok2api_pool_name_combo, 5, 3, sticky=tk.W)

        add_label(6, 0, "本地 token.json:")
        self.grok2api_local_file_var = tk.StringVar(value=str(config.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = tk_entry(config_frame, textvariable=self.grok2api_local_file_var, width=72)
        add_field(self.grok2api_local_file_entry, 6, 1, columnspan=3)

        add_label(7, 0, "grok2api 远端入池:")
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = tk_checkbutton(config_frame, variable=self.grok2api_remote_auto_var)
        add_field(self.grok2api_remote_auto_check, 7, 1, sticky=tk.W)

        add_label(8, 0, "grok2api 远端 Base:")
        self.grok2api_remote_base_var = tk.StringVar(value=str(config.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = tk_entry(config_frame, textvariable=self.grok2api_remote_base_var, width=72)
        add_field(self.grok2api_remote_base_entry, 8, 1, columnspan=3)

        add_label(9, 0, "grok2api 远端 app_key:")
        self.grok2api_remote_key_var = tk.StringVar(value=str(config.get("grok2api_remote_app_key", "")))
        self.grok2api_remote_key_entry = tk_entry(config_frame, textvariable=self.grok2api_remote_key_var, width=72)
        add_field(self.grok2api_remote_key_entry, 9, 1, columnspan=3)

        btn_frame = tk.Frame(main_frame, bg=UI_BG)
        btn_frame.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        self.start_btn = tk_button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = tk_button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = tk_button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)

        status_frame = tk.Frame(main_frame, bg=UI_BG)
        status_frame.grid(row=2, column=0, sticky=tk.EW, pady=(0, 6))
        self.status_var = tk.StringVar(value="就绪")
        tk_label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = tk.Label(status_frame, textvariable=self.status_var, bg=UI_BG, fg="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        tk.Label(status_frame, textvariable=self.stats_var, bg=UI_BG, fg=UI_FG).pack(side=tk.RIGHT)
        log_frame = tk.LabelFrame(
            main_frame,
            text="日志",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=5,
            pady=5,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        log_frame.grid(row=3, column=0, sticky=tk.NSEW)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=18,
            width=60,
            bg="#111111",
            fg="#f5f5f5",
            insertbackground="#f5f5f5",
            selectbackground="#345a8a",
            selectforeground="#ffffff",
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#555555",
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        self.log("[*] GUI 已就绪，配置已加载")
        self.log(f"[*] 当前邮箱服务商: {self.email_provider_var.get()} | 注册数量: {self.count_var.get()}")

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        self.log_text.insert(tk.END, f"{line}\n")
        self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "duckmail"
        config["enable_nsfw"] = bool(self.nsfw_var.get())
        config["proxy"] = self.proxy_var.get().strip()
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "none"
        config["grok2api_auto_add_local"] = bool(self.grok2api_local_auto_var.get())
        config["grok2api_local_token_file"] = self.grok2api_local_file_var.get().strip()
        config["grok2api_pool_name"] = self.grok2api_pool_name_var.get().strip() or "ssoBasic"
        config["grok2api_auto_add_remote"] = bool(self.grok2api_remote_auto_var.get())
        config["grok2api_remote_base"] = self.grok2api_remote_base_var.get().strip()
        config["grok2api_remote_app_key"] = self.grok2api_remote_key_var.get().strip()
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        save_config()
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        config["register_count"] = count
        save_config()
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(
            os.path.dirname(__file__), f"accounts_{now}.txt"
        )
        self.update_stats()
        self._set_running_ui(True)
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}")
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count,),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def run_registration(self, count):
        try:
            start_browser(log_callback=self.log)
            self.log("[*] 浏览器已启动")
            i = 0
            retry_count_for_slot = 0
            max_slot_retry = 3
            round_timeout = get_round_timeout_sec()
            self.log(f"[*] 单账号硬超时: {round_timeout}s")
            while i < count:
                if self.should_stop():
                    break
                account_deadline = time.time() + round_timeout
                cancel_cb = _make_account_cancel(
                    self.should_stop, account_deadline, round_timeout
                )
                self.log(f"--- 开始第 {i + 1}/{count} 个账号 · 超时 {round_timeout}s ---")
                try:
                    email = ""
                    dev_token = ""
                    code = ""
                    mail_ok = False
                    max_mail_retry = 3
                    for mail_try in range(1, max_mail_retry + 1):
                        try:
                            self.log(
                                f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry}"
                                f" · 邮箱源={get_email_provider()})"
                            )
                            open_signup_page(
                                log_callback=self.log, cancel_callback=cancel_cb
                            )
                            self.log("[*] 2. 创建邮箱并提交")
                            email, dev_token = fill_email_and_submit(
                                log_callback=self.log, cancel_callback=cancel_cb
                            )
                            self.log(f"[*] 邮箱: {email}")
                            self.log(f"[Debug] 邮箱credential(jwt): {dev_token}")
                            try:
                                with open(
                                    os.path.join(
                                        os.path.dirname(__file__), "mail_credentials.txt"
                                    ),
                                    "a",
                                    encoding="utf-8",
                                ) as f:
                                    f.write(f"{email}\t{dev_token}\n")
                            except Exception:
                                pass
                            self.log("[*] 3. 拉取验证码")
                            code = fill_code_and_submit(
                                email,
                                dev_token,
                                log_callback=self.log,
                                cancel_callback=cancel_cb,
                            )
                            mail_ok = True
                            break
                        except Exception as mail_exc:
                            msg = str(mail_exc)
                            if "单账号超时" in msg:
                                raise
                            if is_mail_related_error(mail_exc) and mail_try < max_mail_retry:
                                self.log(
                                    f"[!] 邮箱/验证码失败，自动切换备用源并换邮箱重试: {msg}"
                                )
                                if config.get("email_failover", True):
                                    rotate_email_provider(
                                        log_callback=self.log, reason=msg[:120]
                                    )
                                restart_browser(log_callback=self.log)
                                sleep_with_cancel(1, cancel_cb)
                                continue
                            raise

                    if not mail_ok:
                        raise Exception("验证码阶段失败，已达到最大重试次数")
                    self.log(f"[*] 验证码: {code}")
                    self.log("[*] 4. 填写资料")
                    profile = fill_profile_and_submit(
                        log_callback=self.log, cancel_callback=cancel_cb
                    )
                    self.log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                    self.log("[*] 5. 等待落到 grok.com 并提取 sso cookie（优先 grok.com 域）")
                    sso = wait_for_sso_cookie(
                        log_callback=self.log, cancel_callback=cancel_cb
                    )
                    if config.get("enable_nsfw", True):
                        self.log("[*] 6. 开启 NSFW")
                        nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                            sso, log_callback=self.log
                        )
                        if nsfw_ok:
                            self.log(f"[+] NSFW 开启成功: {nsfw_msg}")
                        else:
                            self.log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
                    self.results.append({"email": email, "sso": sso, "profile": profile})
                    try:
                        line = f"{email}----{profile.get('password','')}----{sso}\n"
                        with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                            f.write(line)
                    except Exception as file_exc:
                        self.log(f"[Debug] 保存账号文件失败: {file_exc}")
                    add_token_to_grok2api_pools(sso, email=email, log_callback=self.log)
                    self.success_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    self.log(f"[+] 注册成功: {email}")
                    if (
                        self.success_count > 0
                        and self.success_count % MEMORY_CLEANUP_INTERVAL == 0
                        and i < count
                    ):
                        cleanup_runtime_memory(
                            log_callback=self.log,
                            reason=f"已成功 {self.success_count} 个账号，执行定期清理",
                        )
                except RegistrationCancelled:
                    self.log("[!] 注册被用户停止")
                    break
                except AccountRetryNeeded as exc:
                    retry_count_for_slot += 1
                    if retry_count_for_slot <= max_slot_retry:
                        self.log(
                            f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                        )
                    else:
                        self.fail_count += 1
                        self.log(
                            f"[-] 当前账号已达到最大重试次数，跳过: {exc}"
                        )
                        retry_count_for_slot = 0
                        i += 1
                except Exception as exc:
                    self.fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    self.log(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop():
                        break
                    if browser is None:
                        start_browser(log_callback=self.log)
                    else:
                        restart_browser(log_callback=self.log)
                    sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            self.log(f"[!] 任务异常: {exc}")
        finally:
            stop_browser()
            self._set_running_ui(False)
            self.log("[*] 任务结束")


class CliStopController:
    def __init__(self):
        self.stop_requested = False

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True


def cli_log(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{timestamp}] {message}", flush=True)
    except UnicodeEncodeError:
        print(f"[{timestamp}] {message}".encode("ascii", "replace").decode("ascii"), flush=True)


def _make_account_cancel(stop_callback, deadline, timeout_sec):
    """User stop → RegistrationCancelled; account wall-clock → Exception (skip to next)."""

    def cancel():
        if stop_callback and stop_callback():
            return True
        if deadline and time.time() >= deadline:
            raise Exception(f"单账号超时（{timeout_sec}s），跳过进入下一轮")
        return False

    return cancel


def run_registration_cli(count):
    controller = CliStopController()
    success_count = 0
    fail_count = 0
    retry_count_for_slot = 0
    max_slot_retry = 3
    round_timeout = get_round_timeout_sec()
    accounts_output_file = os.path.join(
        os.path.dirname(__file__),
        f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    cli_log(f"[*] 终端模式启动，目标数量: {count}")
    cli_log(f"[*] 单账号硬超时: {round_timeout}s")
    cli_log(f"[*] 成功账号将实时保存到: {accounts_output_file}")
    try:
        start_browser(log_callback=cli_log)
        cli_log("[*] 浏览器已启动")
        i = 0
        while i < count:
            if controller.should_stop():
                break
            account_deadline = time.time() + round_timeout
            cancel_cb = _make_account_cancel(
                controller.should_stop, account_deadline, round_timeout
            )
            cli_log(f"--- 开始第 {i + 1}/{count} 个账号 · 超时 {round_timeout}s ---")
            try:
                email = ""
                dev_token = ""
                code = ""
                mail_ok = False
                max_mail_retry = 3
                for mail_try in range(1, max_mail_retry + 1):
                    try:
                        cli_log(
                            f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry}"
                            f" · 邮箱源={get_email_provider()})"
                        )
                        open_signup_page(
                            log_callback=cli_log, cancel_callback=cancel_cb
                        )
                        cli_log("[*] 2. 创建邮箱并提交")
                        email, dev_token = fill_email_and_submit(
                            log_callback=cli_log, cancel_callback=cancel_cb
                        )
                        cli_log(f"[*] 邮箱: {email}")
                        cli_log(f"[Debug] 邮箱credential(jwt): {dev_token}")
                        try:
                            with open(
                                os.path.join(
                                    os.path.dirname(__file__), "mail_credentials.txt"
                                ),
                                "a",
                                encoding="utf-8",
                            ) as f:
                                f.write(f"{email}\t{dev_token}\n")
                        except Exception:
                            pass
                        cli_log("[*] 3. 拉取验证码")
                        code = fill_code_and_submit(
                            email,
                            dev_token,
                            log_callback=cli_log,
                            cancel_callback=cancel_cb,
                        )
                        mail_ok = True
                        break
                    except Exception as mail_exc:
                        msg = str(mail_exc)
                        if "单账号超时" in msg:
                            raise
                        if is_mail_related_error(mail_exc) and mail_try < max_mail_retry:
                            cli_log(
                                f"[!] 邮箱/验证码失败，自动切换备用源并换邮箱重试: {msg}"
                            )
                            if config.get("email_failover", True):
                                rotate_email_provider(
                                    log_callback=cli_log, reason=msg[:120]
                                )
                            restart_browser(log_callback=cli_log)
                            sleep_with_cancel(1, cancel_cb)
                            continue
                        raise

                if not mail_ok:
                    raise Exception("验证码阶段失败，已达到最大重试次数")
                cli_log(f"[*] 验证码: {code}")
                cli_log("[*] 4. 填写资料")
                profile = fill_profile_and_submit(
                    log_callback=cli_log, cancel_callback=cancel_cb
                )
                cli_log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                cli_log("[*] 5. 等待落到 grok.com 并提取 sso cookie（优先 grok.com 域）")
                sso = wait_for_sso_cookie(
                    log_callback=cli_log, cancel_callback=cancel_cb
                )
                if config.get("enable_nsfw", True):
                    cli_log("[*] 6. 开启 NSFW")
                    nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                        sso, log_callback=cli_log
                    )
                    if nsfw_ok:
                        cli_log(f"[+] NSFW 开启成功: {nsfw_msg}")
                    else:
                        cli_log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
                try:
                    line = f"{email}----{profile.get('password','')}----{sso}\n"
                    with open(accounts_output_file, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception as file_exc:
                    cli_log(f"[Debug] 保存账号文件失败: {file_exc}")
                add_token_to_grok2api_pools(sso, email=email, log_callback=cli_log)
                success_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[+] 注册成功: {email}")
                cli_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                if success_count > 0 and success_count % MEMORY_CLEANUP_INTERVAL == 0 and i < count:
                    cleanup_runtime_memory(
                        log_callback=cli_log,
                        reason=f"已成功 {success_count} 个账号，执行定期清理",
                    )
            except RegistrationCancelled:
                cli_log("[!] 注册被停止")
                break
            except AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    cli_log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
            except Exception as exc:
                fail_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[-] 注册失败: {exc}")
            finally:
                if controller.should_stop():
                    break
                if browser is None:
                    start_browser(log_callback=cli_log)
                else:
                    restart_browser(log_callback=cli_log)
                sleep_with_cancel(1, controller.should_stop)
    except KeyboardInterrupt:
        controller.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止并清理")
    except Exception as exc:
        cli_log(f"[!] 任务异常: {exc}")
    finally:
        cleanup_runtime_memory(log_callback=cli_log, reason="任务结束")
        cli_log(f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}")


def main_cli():
    load_config()
    # 面板通过 GROK_REGISTER_COUNT=1 强制单轮；否则用 config.register_count
    env_count = str(os.environ.get("GROK_REGISTER_COUNT", "") or "").strip()
    if env_count:
        try:
            count = max(1, int(env_count))
        except Exception:
            count = int(config.get("register_count", 1) or 1)
    else:
        count = int(config.get("register_count", 1) or 1)
    cli_log("[*] CLI 已加载配置")
    cli_log(f"[*] 当前邮箱服务商: {config.get('email_provider', 'duckmail')} | 注册数量: {count}")
    cli_log("[*] 输入 start 后开始；按 Ctrl+C 可强制停止")
    try:
        command = input("> ").strip().lower()
    except KeyboardInterrupt:
        cli_log("[!] 已取消")
        return
    if command != "start":
        cli_log("[!] 未输入 start，已退出")
        return
    run_registration_cli(count)


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("start", "cli", "--cli"):
        main_cli()
        return
    root = tk.Tk()
    setup_light_theme(root)
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
