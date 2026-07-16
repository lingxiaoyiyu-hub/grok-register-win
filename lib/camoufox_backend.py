#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Camoufox backend with a DrissionPage-like surface.

Playwright Sync API is driven on a dedicated worker thread so it still works
when the caller thread has a running asyncio loop (e.g. after geoip downloads).
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlparse


class CamoufoxNotAvailable(Exception):
    pass


# ---------------------------------------------------------------------------
# Selector / proxy helpers
# ---------------------------------------------------------------------------


def _dp_selector_to_css(selector: str) -> str:
    raw = str(selector or "").strip()
    if not raw:
        return "*"
    if raw.startswith("tag:"):
        return raw[4:].strip() or "*"
    if raw.startswith("@"):
        body = raw[1:]
        if "=" in body:
            key, val = body.split("=", 1)
            key = key.strip()
            val = val.strip().strip("\"'")
            return f'[{key}="{val}"]'
        return f"[{body}]"
    if raw.startswith("css:"):
        return raw[4:].strip()
    if raw.startswith("text:"):
        text = raw[5:].strip()
        return f'text="{text}"'
    return raw


def _proxy_to_camoufox(proxy: str) -> Optional[dict]:
    raw = str(proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    scheme = parsed.scheme or "http"
    cfg: dict = {"server": f"{scheme}://{parsed.hostname}:{port}"}
    if parsed.username:
        cfg["username"] = unquote(parsed.username)
    if parsed.password:
        cfg["password"] = unquote(parsed.password)
    return cfg


def _looks_like_missing_binary(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    # geoip/mmdb path issues must NOT trigger browser re-download
    if any(x in msg for x in ("geoip", "geolite", "mmdb", "asyncio loop", "sync api")):
        return False
    if "camoufoxnotinstalled" in msg or "camoufox is not installed" in msg:
        return True
    if "browser is not installed" in msg or "executable doesn't exist" in msg:
        return True
    if "camoufox.exe" in msg and ("not found" in msg or "no such file" in msg):
        return True
    return False


def _path_has_non_ascii(path: str) -> bool:
    try:
        str(path or "").encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def _prepare_ascii_mmdb(log_callback=None) -> Optional[str]:
    """Ensure GeoLite2 mmdb is on an ASCII path (camoufox.locales / package data)."""
    try:
        import shutil
        from pathlib import Path

        # camoufox>=0.5 uses camoufox.locales (plural); older used camoufox.locale
        locale_mod = None
        for mod_name in ("camoufox.locales", "camoufox.locale"):
            try:
                locale_mod = __import__(mod_name, fromlist=["*"])
                break
            except Exception:
                continue

        candidates = []
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            candidates.append(Path(local) / "camoufox" / "GeoLite2-City.mmdb")
            candidates.append(Path(local) / "camoufox" / "camoufox" / "GeoLite2-City.mmdb")
            candidates.append(Path(local) / "grok-register-GeoLite2-City.mmdb")
            # camoufox fetch may store mmdb under Cache
            cache_root = Path(local) / "camoufox" / "camoufox" / "Cache"
            if cache_root.exists():
                try:
                    for p in cache_root.rglob("*.mmdb"):
                        candidates.append(p)
                except Exception:
                    pass

        if locale_mod is not None:
            for attr in ("MMDB_FILE", "GEOIP_PATH", "LOCAL_DATA"):
                val = getattr(locale_mod, attr, None)
                if not val:
                    continue
                try:
                    p = Path(val)
                    if p.is_dir():
                        for name in ("GeoLite2-City.mmdb", "GeoLite2-City-ipv4.mmdb"):
                            candidates.append(p / name)
                    else:
                        candidates.append(p)
                except Exception:
                    pass

        # package data next to camoufox install
        try:
            import camoufox as cf_mod

            pkg = Path(cf_mod.__file__).resolve().parent
            for name in ("GeoLite2-City.mmdb", "GeoIP2-City.mmdb"):
                candidates.append(pkg / name)
                candidates.append(pkg / "data" / name)
        except Exception:
            pass

        ascii_path = None
        for c in candidates:
            try:
                if c.exists() and c.is_file() and not _path_has_non_ascii(str(c)):
                    ascii_path = c
                    break
            except Exception:
                continue

        if ascii_path is None:
            src_existing = next((c for c in candidates if c.exists() and c.is_file()), None)
            if src_existing is None:
                return None
            base = Path(local or os.environ.get("TEMP") or ".") / "camoufox"
            base.mkdir(parents=True, exist_ok=True)
            ascii_path = base / "GeoLite2-City.mmdb"
            if _path_has_non_ascii(str(ascii_path)):
                return None
            if (not ascii_path.exists()) or (
                src_existing.stat().st_mtime > ascii_path.stat().st_mtime
            ):
                shutil.copy2(src_existing, ascii_path)

        if locale_mod is not None:
            for attr in ("MMDB_FILE", "GEOIP_PATH"):
                if hasattr(locale_mod, attr):
                    try:
                        setattr(locale_mod, attr, Path(ascii_path))
                    except Exception:
                        pass
        if log_callback:
            log_callback(f"[*] Camoufox GeoIP DB: {ascii_path}")
        return str(ascii_path)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 准备 GeoIP DB 失败: {exc}")
        return None


def _geoip_safe_to_enable(log_callback=None) -> bool:
    """True when we can point camoufox at an ASCII-path GeoLite mmdb."""
    return bool(_prepare_ascii_mmdb(log_callback=log_callback))


def _resolve_launch_exe() -> Optional[str]:
    """Prefer newest installed camoufox.exe (152.x over stale 135.x defaults)."""
    name = "camoufox.exe" if sys.platform.startswith("win") else "camoufox"
    found: list[Path] = []

    # Scan common caches first so we can pick the newest binary
    roots = []
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        roots.append(Path(local) / "camoufox")
    try:
        from camoufox import pkgman

        for attr in ("INSTALL_DIR",):
            d = getattr(pkgman, attr, None)
            if d:
                roots.append(Path(d))
        try:
            cache = pkgman.camoufox_path(download_if_missing=False)
            if cache:
                roots.append(Path(cache))
        except Exception:
            pass
        try:
            path = pkgman.launch_path()
            if path:
                found.append(Path(path))
        except Exception:
            pass
    except Exception:
        pass

    for root in roots:
        try:
            if not root.exists():
                continue
            if root.is_file() and root.name.lower() == name.lower():
                found.append(root)
                continue
            for p in root.rglob(name):
                if p.is_file():
                    found.append(p)
        except Exception:
            continue

    # unique preserve
    uniq = []
    seen = set()
    for p in found:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists():
            uniq.append(p)

    if not uniq:
        return None

    def _ver_key(p: Path):
        # path like .../152.0.4-beta.26-xxx/camoufox.exe
        s = str(p)
        import re as _re

        m = _re.search(r"(\d+)\.(\d+)\.(\d+)", s)
        if m:
            return tuple(int(x) for x in m.groups())
        try:
            return (0, 0, int(p.stat().st_mtime))
        except Exception:
            return (0, 0, 0)

    best = max(uniq, key=_ver_key)
    return str(best)


def ensure_camoufox_ready(log_callback=None) -> str:
    """Ensure package importable and browser binary present. Returns exe path."""
    try:
        from camoufox.sync_api import Camoufox  # noqa: F401
    except ImportError as exc:
        raise CamoufoxNotAvailable(
            '未安装 camoufox。请执行: pip install "camoufox[geoip]" 后重试'
        ) from exc

    exe = _resolve_launch_exe()
    if exe:
        if log_callback:
            log_callback(f"[*] Camoufox 浏览器已就绪: {exe}")
        return exe

    _fetch_camoufox_binary(log_callback=log_callback)
    exe = _resolve_launch_exe()
    if not exe:
        raise CamoufoxNotAvailable(
            "camoufox 浏览器二进制仍未找到。请手动执行: python -m camoufox fetch"
        )
    if log_callback:
        log_callback(f"[*] Camoufox 浏览器已就绪: {exe}")
    return exe


def _fetch_camoufox_binary(log_callback=None) -> None:
    if log_callback:
        log_callback("[*] 首次使用 Camoufox，正在下载浏览器二进制（可能较久，请耐心等待）...")
    cmd = [sys.executable, "-m", "camoufox", "fetch"]
    try:
        # 实时输出进度，避免“假死”观感
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise CamoufoxNotAvailable("无法执行 camoufox fetch") from exc

    deadline = time.time() + 60 * 30
    last_ping = 0.0
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        while True:
            if time.time() > deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise CamoufoxNotAvailable("camoufox fetch 超时（>30 分钟）")
            line = proc.stdout.readline()
            if line:
                text = line.rstrip()
                if text:
                    lines.append(text)
                    if log_callback and (
                        "download" in text.lower()
                        or "fetch" in text.lower()
                        or "%" in text
                        or "MB" in text
                        or "完成" in text
                        or "error" in text.lower()
                    ):
                        log_callback(f"[*] Camoufox: {text[:180]}")
            elif proc.poll() is not None:
                break
            else:
                now = time.time()
                if log_callback and now - last_ping >= 15:
                    log_callback("[*] Camoufox 仍在下载浏览器，请稍候…")
                    last_ping = now
                time.sleep(0.5)
        code = proc.wait(timeout=10)
    except CamoufoxNotAvailable:
        raise
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        raise CamoufoxNotAvailable(f"camoufox fetch 异常: {exc}") from exc

    if code != 0:
        err = "\n".join(lines[-20:]).strip()
        raise CamoufoxNotAvailable(
            f"camoufox fetch 失败 (code={code}): {err[:500]}"
        )
    if log_callback:
        log_callback("[+] Camoufox 浏览器下载完成")


def _read_turnstile_token(page) -> str:
    try:
        token = page.evaluate(
            """() => {
  try {
    const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
    if (byInput) return byInput;
    // hidden textareas sometimes used
    const ta = document.querySelector('textarea[name="cf-turnstile-response"]');
    if (ta && ta.value) return String(ta.value || '').trim();
    if (window.turnstile && typeof turnstile.getResponse === 'function') {
      return String(turnstile.getResponse() || '').trim();
    }
    return '';
  } catch (e) { return ''; }
}"""
        )
        return str(token or "").strip()
    except Exception:
        return ""


def _click_turnstile_on_page(page) -> dict:
    """Best-effort click Cloudflare Turnstile checkbox via Playwright frames."""
    import time as _time

    detail = {
        "clicked": False,
        "frames": 0,
        "token_len": 0,
        "method": "",
        "error": "",
        "frame_urls": [],
    }
    token = _read_turnstile_token(page)
    detail["token_len"] = len(token)
    if detail["token_len"] >= 80:
        detail["method"] = "already-solved"
        return detail

    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    detail["frames"] = len(frames)
    frame_meta = []
    for frame in frames:
        try:
            frame_meta.append(
                {
                    "frame": frame,
                    "url": (frame.url or ""),
                    "name": getattr(frame, "name", "") or "",
                }
            )
        except Exception:
            continue
    detail["frame_urls"] = [m["url"][:120] for m in frame_meta if m.get("url")]

    # Sort: turnstile/challenge frames first
    def score(meta):
        u = (meta.get("url") or "").lower()
        n = (meta.get("name") or "").lower()
        s = 0
        if "turnstile" in u or "turnstile" in n:
            s += 100
        if "challenges.cloudflare" in u or "cdn-cgi" in u or "cf-chl" in u:
            s += 80
        if "cloudflare" in u:
            s += 40
        if u in ("", "about:blank"):
            s -= 5
        return -s

    frame_meta.sort(key=score)

    # Prefer real Turnstile challenge frames first (ignore about:blank / main app page)
    def is_turnstile_frame(meta):
        u = (meta.get("url") or "").lower()
        return any(
            x in u
            for x in (
                "challenges.cloudflare.com",
                "turnstile",
                "cdn-cgi/challenge-platform",
                "cf-chl",
            )
        )

    turnstile_frames = [m for m in frame_meta if is_turnstile_frame(m)]
    other_frames = [m for m in frame_meta if not is_turnstile_frame(m)]
    ordered = turnstile_frames + other_frames

    click_selectors = [
        "input[type='checkbox']",
        "label.ctp-checkbox-label",
        ".ctp-checkbox-label",
        "#challenge-stage input",
        "#challenge-stage",
        "[role='checkbox']",
        "label",
        # body last — can steal focus without solving
        "body",
    ]

    for meta in ordered:
        frame = meta["frame"]
        url = meta.get("url") or ""
        prefer = is_turnstile_frame(meta)
        if not prefer and turnstile_frames:
            # If we already know a turnstile frame exists, don't spam-click main page frames
            continue

        # 1) Coordinate click on the iframe element (checkbox is left side)
        try:
            frame_el = frame.frame_element()
            box = frame_el.bounding_box()
            if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
                # Cloudflare checkbox is typically ~left 25-35px, vertically centered
                points = [
                    (box["x"] + 28, box["y"] + box["height"] * 0.5),
                    (box["x"] + 22, box["y"] + box["height"] * 0.48),
                    (box["x"] + box["width"] * 0.12, box["y"] + box["height"] * 0.5),
                ]
                for x, y in points:
                    try:
                        page.mouse.move(x, y, steps=8)
                        _time.sleep(0.08)
                        page.mouse.click(x, y, delay=40)
                        detail["clicked"] = True
                        detail["method"] = "mouse-checkbox-coords"
                        # short wait for token after precise click
                        for _ in range(6):
                            _time.sleep(0.4)
                            token = _read_turnstile_token(page)
                            detail["token_len"] = len(token)
                            if detail["token_len"] >= 80:
                                detail["method"] = "mouse-checkbox-coords+solved"
                                return detail
                        break
                    except Exception:
                        continue
        except Exception:
            pass

        for sel in click_selectors:
            if sel == "body" and prefer:
                # clicking body of turnstile frame is weak; only after other selectors
                pass
            try:
                loc = frame.locator(sel).first
                try:
                    cnt = loc.count()
                except Exception:
                    cnt = 1
                if cnt == 0:
                    continue
                try:
                    visible = loc.is_visible(timeout=700)
                except Exception:
                    visible = True
                if not visible and sel != "body":
                    continue
                try:
                    loc.hover(timeout=1000)
                except Exception:
                    pass
                loc.click(timeout=2500, force=True)
                detail["clicked"] = True
                detail["method"] = f"frame-click:{sel}:{'prefer' if prefer else 'any'}"
                break
            except Exception:
                continue
        if detail["clicked"] and detail.get("token_len", 0) < 80:
            # wait a bit after locator click
            for _ in range(6):
                _time.sleep(0.4)
                token = _read_turnstile_token(page)
                detail["token_len"] = len(token)
                if detail["token_len"] >= 80:
                    detail["method"] = (detail.get("method") or "click") + "+solved"
                    return detail
        if detail["clicked"]:
            break

    if not detail["clicked"]:
        try:
            # Playwright frame_locator shortcuts
            for sel in (
                "iframe[src*='turnstile']",
                "iframe[src*='challenges.cloudflare']",
                "iframe[src*='cdn-cgi']",
            ):
                try:
                    fl = page.frame_locator(sel)
                    for inner in (
                        "input[type='checkbox']",
                        "label",
                        ".ctp-checkbox-label",
                        "body",
                    ):
                        try:
                            fl.locator(inner).first.click(timeout=1500, force=True)
                            detail["clicked"] = True
                            detail["method"] = f"frame_locator:{sel}:{inner}"
                            break
                        except Exception:
                            continue
                    if detail["clicked"]:
                        break
                except Exception:
                    continue
        except Exception as exc:
            detail["error"] = f"frame_locator:{exc}"

    if not detail["clicked"]:
        try:
            clicked = page.evaluate(
                """() => {
  const nodes = Array.from(document.querySelectorAll('iframe, div, span, label, input'));
  const hits = nodes.filter((n) => {
    const txt = [
      n.className, n.id, n.getAttribute && n.getAttribute('src'),
      n.getAttribute && n.getAttribute('name'), n.getAttribute && n.getAttribute('title'),
    ].filter(Boolean).join(' ').toLowerCase();
    return txt.includes('turnstile') || txt.includes('cf-chl') || txt.includes('challenge') || txt.includes('cloudflare');
  });
  for (const n of hits) {
    try {
      const r = n.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) { n.click(); return true; }
    } catch (e) {}
  }
  return false;
}"""
            )
            if clicked:
                detail["clicked"] = True
                detail["method"] = "dom-click-container"
        except Exception as exc:
            detail["error"] = f"dom-click:{exc}"

    # Wait a bit for managed/auto solve after interaction
    for _ in range(8):
        _time.sleep(0.5)
        token = _read_turnstile_token(page)
        detail["token_len"] = len(token)
        if detail["token_len"] >= 80:
            detail["method"] = (detail.get("method") or "wait") + "+solved"
            break
    return detail


def _fetch_exit_ip(proxy: str, timeout: float = 8.0) -> Optional[str]:
    """Resolve public IP via proxy for Camoufox geoip=<ip> (avoids True + bad paths)."""
    raw = str(proxy or "").strip()
    if not raw:
        return None
    try:
        import urllib.request

        handler = urllib.request.ProxyHandler({"http": raw, "https": raw})
        opener = urllib.request.build_opener(handler)
        with opener.open("https://api.ipify.org", timeout=timeout) as resp:
            ip = (resp.read() or b"").decode("utf-8", errors="ignore").strip()
        # basic IPv4/IPv6 sanity
        if ip and all(c.isdigit() or c in ".:abcdefABCDEF" for c in ip) and 3 <= len(ip) <= 45:
            return ip
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Worker-thread Playwright session
# ---------------------------------------------------------------------------


class _BrowserWorker:
    """Runs Camoufox / Playwright Sync API on a dedicated thread."""

    def __init__(self, proxy: str = "", headless: bool = True, log_callback=None):
        self.proxy = proxy or ""
        self.headless = bool(headless)
        self.log = log_callback
        self._cmd_q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._dead = False
        self._dead_reason = ""
        self._thread = threading.Thread(
            target=self._run, name="camoufox-worker", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=180):
            raise CamoufoxNotAvailable("Camoufox 工作线程启动超时")
        if self._start_error is not None:
            raise CamoufoxNotAvailable(f"Camoufox 启动失败: {self._start_error}")

    def call(self, op: str, *args, timeout: float = 120, **kwargs):
        if self._dead:
            raise RuntimeError(f"Camoufox browser dead: {self._dead_reason}")
        if not self._thread.is_alive():
            raise RuntimeError("Camoufox worker thread is dead")
        # Short default for eval so cookie/consent helpers fail soft instead of hanging 60s+
        if op == "page_eval" and timeout == 120:
            timeout = 12
        resp: queue.Queue = queue.Queue(maxsize=1)
        self._cmd_q.put((op, args, kwargs, resp))
        try:
            ok, payload = resp.get(timeout=timeout)
        except queue.Empty as exc:
            # Mark dead only for hard ops; page_eval timeouts often mean navigation mid-script
            if op not in ("page_eval", "page_query", "turnstile_token", "turnstile_click"):
                self._dead = True
                self._dead_reason = f"timeout:{op}"
            raise TimeoutError(f"Camoufox op timeout: {op}") from exc
        if ok:
            return payload
        raise payload

    def close(self):
        try:
            self.call("shutdown", timeout=30)
        except Exception:
            pass
        self._thread.join(timeout=5)

    def _run(self):
        # Playwright Sync API must not see a *running* asyncio loop on this
        # thread. Clear any inherited loop; do NOT create a new one here.
        import asyncio
        import warnings

        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass

        browser = None
        cm = None
        pages: dict = {}  # page_id -> playwright page
        page_seq = 0
        contexts = []

        def new_page_id(raw):
            nonlocal page_seq
            page_seq += 1
            pid = page_seq
            pages[pid] = raw
            return pid

        try:
            ensure_camoufox_ready(log_callback=self.log)
            from camoufox.sync_api import Camoufox

            proxy_cfg = _proxy_to_camoufox(self.proxy)
            # geoip needs GeoLite mmdb under site-packages. On paths with Chinese
            # characters (e.g. D:\下载\...) it fails and can leave a running
            # asyncio loop that breaks Playwright Sync API. Only enable when safe.
            launch_kwargs = {
                "headless": self.headless,
                "humanize": True,
                "geoip": False,
                # Keep UI language English so signup button/text matchers work.
                "locale": "en-US",
            }
            if sys.platform.startswith("win"):
                launch_kwargs["os"] = "windows"
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
                if _geoip_safe_to_enable(log_callback=self.log):
                    exit_ip = _fetch_exit_ip(self.proxy)
                    if exit_ip:
                        launch_kwargs["geoip"] = exit_ip
                        if self.log:
                            self.log(f"[*] Camoufox geoip 对齐出口 IP: {exit_ip}")
                    else:
                        launch_kwargs["i_know_what_im_doing"] = True
                else:
                    launch_kwargs["i_know_what_im_doing"] = True
                    if self.log:
                        self.log("[*] Camoufox geoip 已关闭（无法提供 ASCII 路径 GeoIP DB）")

            last_exc = None
            fetched_binary = False
            for attempt in range(3):
                try:
                    # Ensure no running loop on worker thread before Sync launch
                    try:
                        asyncio.set_event_loop(None)
                    except Exception:
                        pass
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", message=".*geoip.*")
                        warnings.filterwarnings("ignore", category=UserWarning)
                        cm = Camoufox(**launch_kwargs)
                        browser = cm.__enter__()
                    last_exc = None
                    break
                except TypeError as exc:
                    last_exc = exc
                    launch_kwargs.pop("i_know_what_im_doing", None)
                    launch_kwargs.pop("os", None)
                    if launch_kwargs.get("geoip") not in (False, None):
                        launch_kwargs["geoip"] = False
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc or "").lower()
                    # Always disable geoip after any geoip-related failure
                    if any(x in msg for x in ("geoip", "mmdb", "geolite", "asyncio")):
                        launch_kwargs["geoip"] = False
                        launch_kwargs["i_know_what_im_doing"] = True
                    launch_kwargs.pop("os", None)
                    try:
                        asyncio.set_event_loop(None)
                    except Exception:
                        pass
                    if (not fetched_binary) and _looks_like_missing_binary(exc):
                        _fetch_camoufox_binary(log_callback=self.log)
                        fetched_binary = True
                        continue
            if browser is None:
                raise RuntimeError(last_exc)

            # Initial blank page. Camoufox/Firefox rejects default viewport
            # isMobile in some playwright versions — always use no_viewport.
            try:
                raw = browser.new_page(no_viewport=True)
            except TypeError:
                try:
                    ctx = browser.new_context(no_viewport=True)
                    contexts.append(ctx)
                    raw = ctx.new_page()
                except Exception:
                    raw = browser.new_page()
            new_page_id(raw)
            self._start_error = None
            self._ready.set()
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return

        while True:
            op, args, kwargs, resp = self._cmd_q.get()
            try:
                if op == "shutdown":
                    resp.put((True, None))
                    break
                if op == "list_pages":
                    # refresh from browser if possible
                    live = []
                    try:
                        if hasattr(browser, "contexts"):
                            for ctx in browser.contexts:
                                for p in ctx.pages:
                                    pid = next((i for i, r in pages.items() if r is p), None)
                                    if pid is None:
                                        pid = new_page_id(p)
                                    live.append(pid)
                        elif hasattr(browser, "pages"):
                            for p in browser.pages:
                                pid = next((i for i, r in pages.items() if r is p), None)
                                if pid is None:
                                    pid = new_page_id(p)
                                live.append(pid)
                    except Exception:
                        live = list(pages.keys())
                    if not live:
                        live = list(pages.keys())
                    resp.put((True, live))
                    continue
                if op == "new_page":
                    url = kwargs.get("url") or (args[0] if args else "")
                    try:
                        raw = browser.new_page(no_viewport=True)
                    except TypeError:
                        ctx = browser.new_context(no_viewport=True)
                        contexts.append(ctx)
                        raw = ctx.new_page()
                    except Exception:
                        # Browser may already be a context
                        raw = browser.new_page()
                    pid = new_page_id(raw)
                    if url:
                        raw.goto(url, wait_until="domcontentloaded", timeout=60000)
                    resp.put((True, pid))
                    continue
                if op == "page_url":
                    pid = args[0]
                    resp.put((True, pages[pid].url or ""))
                    continue
                if op == "page_html":
                    pid = args[0]
                    resp.put((True, pages[pid].content() or ""))
                    continue
                if op == "page_goto":
                    pid, url = args[0], args[1]
                    page_timeout = float(kwargs.get("page_timeout", 60) or 60)
                    pages[pid].goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(page_timeout * 1000),
                    )
                    resp.put((True, None))
                    continue
                if op == "page_wait_load":
                    pid = args[0]
                    page_timeout = float(kwargs.get("page_timeout", 30) or 30)
                    ms = int(page_timeout * 1000)
                    try:
                        pages[pid].wait_for_load_state("domcontentloaded", timeout=ms)
                    except Exception:
                        try:
                            pages[pid].wait_for_load_state("load", timeout=ms)
                        except Exception:
                            pass
                    resp.put((True, None))
                    continue
                if op == "page_eval":
                    pid, script = args[0], args[1]
                    js_args = list(args[2:])
                    page = pages[pid]
                    src = str(script or "")
                    last = None
                    # DrissionPage run_js(script, *args) exposes args as JS `arguments`.
                    # Playwright evaluate has no free `arguments`; wrap classic function + apply.
                    if js_args:
                        wrapped = (
                            "(argv) => {\n"
                            "  return (function () {\n"
                            f"{src}\n"
                            "  }).apply(null, argv);\n"
                            "}"
                        )
                        try:
                            resp.put((True, page.evaluate(wrapped, js_args)))
                            continue
                        except Exception as exc:
                            last = exc
                            # Fallback: single-arg form for simple scripts
                            try:
                                if len(js_args) == 1:
                                    wrapped1 = (
                                        "(arg0) => {\n"
                                        "  return (function () {\n"
                                        f"{src}\n"
                                        "  }).call(null, arg0);\n"
                                        "}"
                                    )
                                    resp.put((True, page.evaluate(wrapped1, js_args[0])))
                                    continue
                            except Exception as exc2:
                                last = exc2
                            raise last
                    candidates = [
                        f"() => {{ {src}\n}}",
                        f"(function(){{ {src}\n}})()",
                        src,
                    ]
                    for code in candidates:
                        try:
                            resp.put((True, page.evaluate(code)))
                            break
                        except Exception as exc:
                            last = exc
                    else:
                        raise last or RuntimeError("evaluate failed")
                    continue
                if op == "page_query":
                    pid, selector = args[0], args[1]
                    css = _dp_selector_to_css(selector)
                    handle = pages[pid].query_selector(css)
                    resp.put((True, bool(handle)))
                    continue
                if op == "page_cookies":
                    pid = args[0]
                    page = pages[pid]
                    try:
                        items = page.context.cookies()
                    except Exception:
                        items = []
                    result = []
                    for item in items or []:
                        if isinstance(item, dict):
                            result.append(
                                {
                                    "name": item.get("name", ""),
                                    "value": item.get("value", ""),
                                    "domain": item.get("domain", ""),
                                    "path": item.get("path", ""),
                                    "expires": item.get("expires", -1),
                                    "httpOnly": item.get("httpOnly", False),
                                    "secure": item.get("secure", False),
                                    "sameSite": item.get("sameSite", ""),
                                }
                            )
                    resp.put((True, result))
                    continue
                if op == "page_close":
                    pid = args[0]
                    try:
                        pages[pid].close()
                    except Exception:
                        pass
                    pages.pop(pid, None)
                    resp.put((True, None))
                    continue
                if op == "turnstile_click":
                    pid = args[0]
                    page = pages[pid]
                    detail = _click_turnstile_on_page(page)
                    resp.put((True, detail))
                    continue
                if op == "turnstile_token":
                    pid = args[0]
                    page = pages[pid]
                    token = page.evaluate(
                        """() => {
  try {
    const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
    if (byInput) return byInput;
    if (window.turnstile && typeof turnstile.getResponse === 'function') {
      return String(turnstile.getResponse() || '').trim();
    }
    return '';
  } catch (e) { return ''; }
}"""
                    )
                    resp.put((True, str(token or "")))
                    continue
                raise RuntimeError(f"unknown op: {op}")
            except BaseException as exc:
                msg = str(exc or "").lower()
                fatal = any(x in msg for x in (
                    "connection closed", "browser has been closed",
                    "browser closed", "target closed", "pipe closed",
                    "protocol error", "transport", "eof",
                ))
                if fatal:
                    self._dead = True
                    self._dead_reason = str(exc)
                    if self.log:
                        self.log(f"[!] Camoufox 浏览器已崩溃: {exc}")
                try:
                    resp.put((False, type(exc)(f"{exc}")))
                except Exception:
                    resp.put((False, RuntimeError(str(exc))))

        # cleanup
        try:
            for p in list(pages.values()):
                try:
                    p.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if cm is not None:
                cm.__exit__(None, None, None)
        except Exception:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DrissionPage-like wrappers
# ---------------------------------------------------------------------------


class _WaitHelper:
    def __init__(self, page: "CamoufoxPage"):
        self._page = page

    def doc_loaded(self, timeout: float = 30):
        self._page._worker.call(
            "page_wait_load",
            self._page._pid,
            page_timeout=timeout,
            timeout=float(timeout) + 5,
        )


class _ElementProxy:
    """Best-effort element stub (Turnstile path). Most registration uses run_js."""

    def __init__(self, page: "CamoufoxPage", found: bool):
        self._page = page
        self._found = found

    def parent(self):
        return self

    @property
    def shadow_root(self):
        return self

    def ele(self, selector: str):
        return _ElementProxy(self._page, False)

    def click(self):
        return None

    def run_js(self, script: str):
        return None

    def __bool__(self):
        return bool(self._found)


class CamoufoxPage:
    def __init__(self, browser: "CamoufoxBrowser", pid: int):
        self._browser = browser
        self._worker = browser._worker
        self._pid = pid
        self.wait = _WaitHelper(self)

    def _ensure_alive(self):
        if self._pid is None:
            raise RuntimeError("Camoufox page closed")

    @property
    def url(self) -> str:
        self._ensure_alive()
        try:
            return self._worker.call("page_url", self._pid) or ""
        except Exception:
            return ""

    @property
    def html(self) -> str:
        self._ensure_alive()
        try:
            return self._worker.call("page_html", self._pid) or ""
        except Exception:
            return ""

    def get(self, url: str, timeout: float = 60):
        self._ensure_alive()
        self._worker.call(
            "page_goto",
            self._pid,
            url,
            page_timeout=timeout,
            timeout=float(timeout) + 15,
        )
        return self

    def run_js(self, script: str, *args):
        self._ensure_alive()
        # Keep cookie/consent JS short; long hangs usually mean page navigated away
        return self._worker.call("page_eval", self._pid, script, *args, timeout=12)

    def ele(self, selector: str):
        self._ensure_alive()
        try:
            found = bool(self._worker.call("page_query", self._pid, selector))
        except Exception:
            found = False
        return _ElementProxy(self, found) if found else None

    def cookies(self, all_domains: bool = True, all_info: bool = True):
        self._ensure_alive()
        try:
            return self._worker.call("page_cookies", self._pid) or []
        except Exception:
            return []

    def click_turnstile(self) -> dict:
        """Best-effort Cloudflare Turnstile interaction for headless Camoufox."""
        self._ensure_alive()
        try:
            return self._worker.call("turnstile_click", self._pid, timeout=30) or {}
        except Exception as exc:
            return {"clicked": False, "error": str(exc), "token_len": 0}

    def get_turnstile_token(self) -> str:
        self._ensure_alive()
        try:
            return str(self._worker.call("turnstile_token", self._pid, timeout=15) or "")
        except Exception:
            return ""


class CamoufoxBrowser:
    """DrissionPage Chromium-like wrapper around threaded Camoufox."""

    def __init__(self, proxy: str = "", headless: bool = True, log_callback=None):
        self.user_data_path = ""
        self._log = log_callback
        self._worker = _BrowserWorker(
            proxy=proxy or "", headless=headless, log_callback=log_callback
        )
        self._pages: List[CamoufoxPage] = []
        # Sync initial page list
        try:
            for pid in self._worker.call("list_pages") or []:
                self._pages.append(CamoufoxPage(self, pid))
        except Exception:
            pass

    def get_tabs(self) -> List[CamoufoxPage]:
        try:
            pids = self._worker.call("list_pages") or []
            mapped = []
            for pid in pids:
                existing = next((p for p in self._pages if p._pid == pid), None)
                mapped.append(existing or CamoufoxPage(self, pid))
            self._pages = mapped
        except Exception:
            pass
        return list(self._pages)

    def get_tab(self, index: int = 0) -> CamoufoxPage:
        tabs = self.get_tabs()
        if not tabs:
            return self.new_tab()
        if index < 0:
            index = len(tabs) + index
        if index < 0 or index >= len(tabs):
            index = 0
        return tabs[index]

    def new_tab(self, url: str = ""):
        pid = self._worker.call("new_page", url=url or "", timeout=90)
        page = CamoufoxPage(self, pid)
        self._pages.append(page)
        return page

    def quit(self, del_data: bool = True):
        try:
            self._worker.close()
        except Exception:
            pass
        self._pages.clear()


def start_camoufox_browser(browser_proxy: str = "", log_callback=None) -> CamoufoxBrowser:
    if log_callback:
        mode = "代理" if browser_proxy else "直连"
        log_callback(f"[*] 启动 Camoufox 无头浏览器（{mode}）...")
    return CamoufoxBrowser(
        proxy=browser_proxy or "",
        headless=True,
        log_callback=log_callback,
    )
