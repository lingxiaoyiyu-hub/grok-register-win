"""Regression tests for the panel's same-origin / DNS-rebinding guard.

Every case here corresponds to an attack that actually succeeded before v2.0.1:
a page the user merely visits could drive the local panel, because
  - the panel had no auth by default, and
  - POST bodies were parsed with get_json(force=True), so a CORS "simple
    request" (text/plain) needed no preflight, and
  - multipart upload is a simple request too, and
  - no Host header check meant DNS rebinding could also read responses.
"""

from __future__ import annotations

import io

import pytest

import app as panel  # panel/app.py


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "BASE_DIR", tmp_path, raising=False)
    panel.app.config.update(TESTING=True)
    return panel.app.test_client()


ORIGIN = "http://127.0.0.1:8787"


def test_plain_text_post_is_rejected(client):
    """The exact request that used to start a register job from any website."""
    r = client.post(
        "/api/job/start",
        data='{"count":1}',
        content_type="text/plain",
        headers={"Host": "127.0.0.1:8787"},
    )
    assert r.status_code == 403


def test_cross_site_origin_is_rejected(client):
    r = client.post(
        "/api/accounts/delete",
        json={"files": ["accounts_x.txt"]},
        headers={"Host": "127.0.0.1:8787", "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_cross_site_referer_is_rejected(client):
    r = client.post(
        "/api/accounts/delete",
        json={"files": ["accounts_x.txt"]},
        headers={"Host": "127.0.0.1:8787", "Referer": "https://evil.example.com/x"},
    )
    assert r.status_code == 403


def test_cross_site_multipart_upload_is_rejected(client):
    """multipart/form-data is a CORS simple request — Origin check must catch it."""
    r = client.post(
        "/api/sso/upload-convert",
        data={"file": (io.BytesIO(b"a----b----c.d.e"), "sso.txt")},
        content_type="multipart/form-data",
        headers={"Host": "127.0.0.1:8787", "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_state_changing_request_without_origin_is_rejected(client):
    r = client.post(
        "/api/job/stop",
        headers={"Host": "127.0.0.1:8787"},
    )
    assert r.status_code == 403


def test_dns_rebinding_host_is_rejected(client):
    """Attacker domain resolved to 127.0.0.1 would otherwise read every secret."""
    r = client.get("/api/config/email", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "127.0.0.1"])
def test_local_hosts_allowed(client, host):
    r = client.get("/health", headers={"Host": host})
    assert r.status_code == 200


def test_same_origin_post_still_works(client):
    """The guard must not break the panel itself."""
    r = client.post(
        "/api/config/browser",
        json={"browser_engine": "chromium"},
        headers={"Host": "127.0.0.1:8787", "Origin": ORIGIN},
    )
    assert r.status_code == 200
    assert r.get_json()["browser_engine"] == "chromium"


def test_safe_get_needs_no_origin(client):
    r = client.get("/api/job/status", headers={"Host": "127.0.0.1:8787"})
    assert r.status_code == 200


def test_login_routes_are_gone(client):
    """PANEL_AUTH was a fake lock (hardcoded signing key) and was removed."""
    for path in ("/login", "/logout"):
        r = client.get(path, headers={"Host": "127.0.0.1:8787"})
        assert r.status_code == 404


def test_no_hardcoded_session_secret():
    assert not hasattr(panel, "SECRET")
    assert not hasattr(panel, "PANEL_PASSWORD")
    assert panel.app.secret_key is None


def test_json_body_ignores_non_json_content_type(client):
    with panel.app.test_request_context(
        "/api/job/start", data="{\"count\": 9}", content_type="text/plain"
    ):
        assert panel.json_body() == {}
    with panel.app.test_request_context("/api/job/start", json={"count": 9}):
        assert panel.json_body() == {"count": 9}
