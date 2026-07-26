"""Regression tests for CPA batch isolation, incremental logs, mailbox factory."""

from __future__ import annotations

import pytest

import app as panel
import base_mailbox
import sso2cpa_core as core


# --------------------------------------------------------------------------
# Uploading a new SSO batch must not let the previous batch's conversion
# results land in the fresh workspace (they used to race the archive step).
# --------------------------------------------------------------------------
def test_generation_bump_invalidates_queued_jobs():
    start = panel.bump_cpa_generation()
    assert panel.bump_cpa_generation() == start + 1


def test_enqueued_job_carries_current_generation(monkeypatch):
    panel._cpa_done.clear()
    panel._cpa_inflight.clear()
    while not panel._cpa_q.empty():
        panel._cpa_q.get_nowait()

    monkeypatch.setattr(panel, "convert_one", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(panel, "_CPA_CORE_OK", True, raising=False)

    gen = panel.bump_cpa_generation()
    ok, _ = panel.enqueue_cpa_convert(email="a@b.c", sso="tok-1", force=True)
    assert ok
    item = panel._cpa_q.get_nowait()
    assert item["generation"] == gen

    # 换批后，这份任务的代次就落后了 -> worker 会丢弃它
    new_gen = panel.bump_cpa_generation()
    assert item["generation"] != new_gen


# --------------------------------------------------------------------------
# Log paging: the panel polled every 2s and re-sent the whole 2000-line buffer.
# --------------------------------------------------------------------------
def test_logs_since_returns_only_new_lines():
    panel.reset_logs()
    panel.log_line("first")
    panel.log_line("second")

    lines, cursor, reset = panel.logs_since(0)
    assert reset is True and cursor == 2 and len(lines) == 2

    lines, cursor, reset = panel.logs_since(cursor)
    assert lines == [] and reset is False and cursor == 2

    panel.log_line("third")
    lines, cursor, reset = panel.logs_since(cursor)
    assert len(lines) == 1 and "third" in lines[0] and reset is False and cursor == 3


def test_logs_since_resets_when_client_is_ahead():
    """Job restart resets the counter; a stale client must resync, not crash."""
    panel.reset_logs()
    panel.log_line("only")
    lines, cursor, reset = panel.logs_since(999)
    assert reset is True and cursor == 1 and len(lines) == 1


def test_logs_since_resets_when_client_fell_too_far_behind():
    panel.reset_logs()
    for i in range(panel._logs.maxlen + 50):
        panel.log_line(f"line-{i}")
    lines, cursor, reset = panel.logs_since(0)
    assert reset is True
    assert len(lines) == panel._logs.maxlen
    panel.reset_logs()


# --------------------------------------------------------------------------
# Unknown mailbox provider used to silently fall through to Laoudo
# --------------------------------------------------------------------------
def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="未知邮箱源"):
        base_mailbox.create_mailbox("cfwoker")  # 少一个 r


def test_known_provider_still_builds():
    box = base_mailbox.create_mailbox("moemail", extra={"moemail_api_url": "https://x"})
    assert isinstance(box, base_mailbox.MoeMailMailbox)


def test_outlook_provider_removed():
    """The Outlook class was copied from another project and could not run."""
    assert not hasattr(base_mailbox, "OutlookMailbox")
    with pytest.raises(ValueError):
        base_mailbox.create_mailbox("outlook")


def test_no_hardcoded_third_party_bearer():
    from pathlib import Path

    src = Path(base_mailbox.__file__).read_text(encoding="utf-8")
    assert "kevin273945" not in src


# --------------------------------------------------------------------------
# Parsing helpers used by the upload / download paths
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line,email,password,sso",
    [
        ("a@b.c----pw----tok", "a@b.c", "pw", "tok"),
        ("a@b.c----pw----x----y", "a@b.c", "pw", "x----y"),
    ],
)
def test_parse_line(line, email, password, sso):
    got = panel.parse_line(line)
    assert (got["email"], got["password"], got["sso"]) == (email, password, sso)


def test_parse_uploaded_records_dedupes_and_strips_prefix():
    raw = b"a@b.c----pw----tok1\nsso=tok1\ntok2\n# comment\n"
    recs = core.parse_uploaded_records(raw)
    assert [r["sso"] for r in recs] == ["tok1", "tok2"]


def test_normalize_sso_strips_cookie_prefix():
    assert core.normalize_sso("  sso=abc  ") == "abc"
    assert core.normalize_sso("abc") == "abc"


def test_safe_name_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "BASE_DIR", tmp_path, raising=False)
    for bad in ("../etc/passwd", "accounts_../x.txt", "/etc/passwd", "notaccounts.txt"):
        assert panel.safe_name(bad) is None
