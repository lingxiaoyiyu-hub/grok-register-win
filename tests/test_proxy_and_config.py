"""Regression tests for proxy handling and credential-safety fixes."""

from __future__ import annotations

import socket
import threading

import pytest

import grok_register_ttk as reg
import launcher
import app as panel


# --------------------------------------------------------------------------
# allow_proxy_fallback must actually be honoured (previously ignored entirely)
# --------------------------------------------------------------------------
class _BoomProxyError(Exception):
    pass


@pytest.fixture()
def proxied(monkeypatch):
    monkeypatch.setitem(reg.config, "proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(reg, "is_proxy_connection_error", lambda exc: True)
    monkeypatch.setattr(reg, "_is_tls_or_ca_error", lambda exc: False)
    monkeypatch.setattr(reg, "get_ca_bundle_path", lambda: True)


def _always_fails(*a, **kw):
    raise _BoomProxyError("proxy connect failed")


@pytest.mark.parametrize("method", ["get", "post"])
def test_no_direct_fallback_when_disabled(proxied, monkeypatch, method):
    """Config says 'never go direct' -> a proxy failure must surface, not leak the real IP."""
    monkeypatch.setitem(reg.config, "allow_proxy_fallback", False)
    monkeypatch.setattr(reg.requests, method, _always_fails)
    fn = reg.http_get if method == "get" else reg.http_post
    with pytest.raises(RuntimeError, match="allow_proxy_fallback"):
        fn("https://example.com")


@pytest.mark.parametrize("method", ["get", "post"])
def test_direct_fallback_when_explicitly_enabled(proxied, monkeypatch, method):
    monkeypatch.setitem(reg.config, "allow_proxy_fallback", True)
    calls = []

    def flaky(url, **kwargs):
        calls.append(kwargs.get("proxies"))
        if len(calls) == 1:
            raise _BoomProxyError("proxy connect failed")
        return "direct-response"

    monkeypatch.setattr(reg.requests, method, flaky)
    fn = reg.http_get if method == "get" else reg.http_post
    assert fn("https://example.com") == "direct-response"
    assert calls[0]  # first attempt went through the proxy
    assert not calls[1]  # retry went direct


# --------------------------------------------------------------------------
# GROK_PROXY is documented as a proxy override but was never read
# --------------------------------------------------------------------------
def test_env_proxy_overrides_config(monkeypatch):
    monkeypatch.setitem(reg.config, "proxy", "http://127.0.0.1:1111")
    monkeypatch.setenv("GROK_PROXY", "http://127.0.0.1:2222")
    assert reg.get_configured_proxy() == "http://127.0.0.1:2222"


def test_config_proxy_used_when_env_absent(monkeypatch):
    monkeypatch.setitem(reg.config, "proxy", "http://127.0.0.1:1111")
    monkeypatch.delenv("GROK_PROXY", raising=False)
    assert reg.get_configured_proxy() == "http://127.0.0.1:1111"


# --------------------------------------------------------------------------
# Port probing must confirm it found a real HTTP proxy, not just any listener
# --------------------------------------------------------------------------
def _serve_once(reply: bytes) -> int:
    """Start a one-shot TCP server, return its port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                if reply:
                    conn.sendall(reply)
        except Exception:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


@pytest.mark.parametrize("probe", [launcher.looks_like_http_proxy, panel._looks_like_http_proxy])
def test_probe_accepts_real_proxy(probe):
    port = _serve_once(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert probe("127.0.0.1", port) is True


@pytest.mark.parametrize("probe", [launcher.looks_like_http_proxy, panel._looks_like_http_proxy])
def test_probe_accepts_proxy_that_refuses_target(probe):
    """407/403 still proves it IS a proxy."""
    port = _serve_once(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
    assert probe("127.0.0.1", port) is True


@pytest.mark.parametrize("probe", [launcher.looks_like_http_proxy, panel._looks_like_http_proxy])
def test_probe_rejects_unrelated_listener(probe):
    """A random local service must not be mistaken for the proxy."""
    port = _serve_once(b"* OK IMAP4rev1 ready\r\n")
    assert probe("127.0.0.1", port) is False


@pytest.mark.parametrize("probe", [launcher.looks_like_http_proxy, panel._looks_like_http_proxy])
def test_probe_rejects_silent_listener(probe):
    port = _serve_once(b"")
    assert probe("127.0.0.1", port) is False


@pytest.mark.parametrize("probe", [launcher.looks_like_http_proxy, panel._looks_like_http_proxy])
def test_probe_rejects_closed_port(probe):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe("127.0.0.1", port) is False


def test_probing_does_not_rewrite_config(tmp_path, monkeypatch):
    """Probing used to overwrite the user's hand-typed proxy in config.json."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"proxy": "http://127.0.0.1:9999"}\n', encoding="utf-8")
    monkeypatch.setattr(panel, "CONFIG_PATH", cfg, raising=False)
    before = cfg.read_text(encoding="utf-8")
    panel.resolve_proxy_url()
    assert cfg.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# Mailbox credentials must not be written to logs verbatim
# --------------------------------------------------------------------------
def test_mask_secret_hides_the_token():
    token = "eyJhbGciOiJIUzI1NiJ9.super-secret-payload.signature"
    masked = reg.mask_secret(token)
    assert token not in masked
    assert "super-secret-payload" not in masked
    assert masked.startswith(token[:6])


def test_mask_secret_edge_cases():
    assert reg.mask_secret("") == "(空)"
    assert reg.mask_secret(None) == "(空)"
    assert "abc" not in reg.mask_secret("abc")
