#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows / local launcher: ensure config, check proxy, start panel, open browser."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

# Ensure stdout/stderr use UTF-8 on Windows (default is GBK/CP936)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
CONFIG_EXAMPLE = ROOT / "config.example.json"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = ROOT / ".venv" / "bin" / "python"

PANEL_HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8787"))


def log(msg: str) -> None:
    print(msg, flush=True)


def open_port(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def looks_like_http_proxy(host: str, port: int, timeout: float = 1.2) -> bool:
    """Confirm the port actually speaks HTTP-proxy, not just "something is listening".

    A bare TCP connect only proves *a* service is there. Picking a random local
    listener (a database, a dev server) as the proxy silently routes registration
    traffic — including SSO cookies — to the wrong process. Send a real CONNECT
    and require an HTTP status line back.
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
    # 200 = tunnel open; 403/407/502 = it IS a proxy, just refused this target.
    try:
        code = int(head.split(b" ", 2)[1])
    except Exception:
        return False
    return code in (200, 403, 405, 407, 502)


def detect_local_proxy(preferred: str = "") -> str:
    """Resolve the proxy to use for this run.

    The result is intentionally NOT written back to config.json: probing used to
    overwrite whatever the user had typed there.
    """
    pref = (preferred or "").strip()
    if pref:
        u = urlparse(pref if "://" in pref else "http://" + pref)
        host = u.hostname or "127.0.0.1"
        port = u.port or 7890
        if open_port(host, port):
            return pref if "://" in pref else f"http://{host}:{port}"
        log(f"[!] 配置里的代理 {pref} 端口不通，尝试探测本机常见 Clash 端口")
    for port in (7897, 7890, 7891, 7892, 10809, 20171, 1080, 2080, 8888):
        if looks_like_http_proxy("127.0.0.1", port):
            found = f"http://127.0.0.1:{port}"
            log(f"[*] 探测到可用 HTTP 代理: {found}（仅本次生效，未写入 config.json）")
            return found
    return pref or "http://127.0.0.1:7897"


def ensure_config() -> dict:
    if not CONFIG.exists():
        if not CONFIG_EXAMPLE.exists():
            raise SystemExit("missing config.example.json")
        shutil.copyfile(CONFIG_EXAMPLE, CONFIG)
        log(f"[*] created {CONFIG.name}")
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def save_config(cfg: dict) -> None:
    CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_dirs() -> None:
    for p in (ROOT / "data" / "logs", ROOT / "data" / "cpa"):
        p.mkdir(parents=True, exist_ok=True)


def _apply_playwright_patch() -> None:
    """Patch Playwright coreBundle.js to fix Firefox pageError.location crash."""
    patch_script = ROOT / "lib" / "patch_playwright.py"
    if not patch_script.exists():
        return
    try:
        import subprocess as _sp
        _sp.run(
            [python_bin(), str(patch_script)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        pass


def python_bin() -> str:
    if VENV_PY.exists():
        return str(VENV_PY)
    return sys.executable


def check_proxy(proxy: str) -> None:
    proxy = (proxy or "").strip()
    if not proxy:
        log("[!] config.json has no proxy; register/CPA may fail")
        return
    try:
        u = urlparse(proxy if "://" in proxy else "http://" + proxy)
        host = u.hostname or "127.0.0.1"
        port = u.port or 7897
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        log(f"[+] proxy port open: {host}:{port}")
    except Exception as e:
        log(f"[!] proxy port closed ({proxy}): {e}")
        log("    Start Clash Verge first; common mixed port is 7897 (not always 7890)")
        return
    try:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        with opener.open("https://api.ipify.org", timeout=10) as resp:
            ip = resp.read().decode("utf-8", "replace").strip()
        log(f"[+] proxy exit IP: {ip}")
    except Exception as e:
        log(f"[!] proxy cannot reach internet: {e}")
        log("    Switch to a working node in Clash, then retry")


def wait_health(timeout: float = 25.0) -> bool:
    url = f"http://{PANEL_HOST}:{PANEL_PORT}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        # sleep 必须在循环末尾：原来只在 except 分支里 sleep，
        # 面板返回非 200 时会变成不间断的忙轮询。
        time.sleep(0.3)
    return False


def main() -> int:
    try:
        return _main_impl()
    except SystemExit:
        raise
    except Exception as e:
        log(f"[FATAL] {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return 1


def _main_impl() -> int:
    os.chdir(ROOT)
    ensure_dirs()
    _apply_playwright_patch()
    cfg = ensure_config()
    configured_proxy = str(cfg.get("proxy") or "").strip()
    proxy = detect_local_proxy(configured_proxy)
    if proxy and configured_proxy and proxy != configured_proxy:
        log(f"[*] 本次使用探测到的代理 {proxy}（config.json 仍保留 {configured_proxy}）")
        log("    如需固定，请手动修改 config.json 的 proxy 字段")

    log("========== Grok Register Win ==========")
    log(f"Dir: {ROOT}")
    log(f"Python: {python_bin()}")
    log(f"Proxy: {proxy}")
    log(f"Panel: http://{PANEL_HOST}:{PANEL_PORT}  (local only)")
    log("Use your own Clash for subscription/nodes. This app does not embed Clash.")
    log("======================================")
    check_proxy(proxy)

    env = os.environ.copy()
    env["GROK_REGISTER_DIR"] = str(ROOT)
    env["GROK_PROXY"] = proxy
    env["PANEL_HOST"] = PANEL_HOST
    env["PANEL_PORT"] = str(PANEL_PORT)
    env["SSO2CPA_PATH"] = str(ROOT / "lib")
    env["CPA_DIR"] = str(ROOT / "data" / "cpa")
    env["PANEL_LOG_DIR"] = str(ROOT / "data" / "logs")
    env["GROK_PYTHON"] = python_bin()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("CLASH_API", "http://127.0.0.1:9090")
    env.setdefault("ENABLE_CLASH_UI", "1")

    panel_py = ROOT / "panel" / "app.py"
    if not panel_py.exists():
        log(f"[FATAL] missing {panel_py}")
        return 1

    cmd = [python_bin(), str(panel_py)]
    log(f"[*] start panel: {' '.join(cmd)}")
    panel_log = ROOT / "data" / "logs" / "panel_boot.log"
    try:
        boot_f = open(panel_log, "w", encoding="utf-8", errors="replace")
    except Exception:
        boot_f = subprocess.DEVNULL

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=boot_f,
        stderr=subprocess.STDOUT,
    )
    # 子进程已经继承了这个 fd，父进程这边可以关掉，避免句柄一直泄漏
    if boot_f is not subprocess.DEVNULL:
        try:
            boot_f.close()
        except Exception:
            pass

    if wait_health(25.0):
        url = f"http://{PANEL_HOST}:{PANEL_PORT}/"
        log(f"[+] panel ready: {url}")
        try:
            webbrowser.open(url)
        except Exception as e:
            log(f"[!] open browser failed: {e}")
    else:
        log("[!] panel health check timeout")
        log(f"[!] see {panel_log}")
        try:
            if panel_log.exists():
                log("--- panel_boot.log ---")
                log(panel_log.read_text(encoding="utf-8", errors="replace")[-3000:])
        except Exception:
            pass
        if proc.poll() is not None:
            log(f"[!] panel process exited early: code={proc.returncode}")
            return int(proc.returncode or 1)

    log("[*] keep this window open. Ctrl+C to stop.")
    try:
        return int(proc.wait() or 0)
    except KeyboardInterrupt:
        log("\n[*] stopping...")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
