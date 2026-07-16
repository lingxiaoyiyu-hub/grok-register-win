#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch Playwright coreBundle.js to fix Firefox pageError.location crash.

Firefox/Camoufox may emit pageError events with location=undefined, which
causes the Node.js driver to crash with:
  TypeError: Cannot read properties of undefined (reading 'url')
or:
  ValidationError: location.url: expected string, got undefined

This patch adds nullish coalescing so missing fields default to "" / 0.
Run patch_playwright() at startup (after dependencies are installed).
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_corebundle() -> Path | None:
    """Locate coreBundle.js inside the active venv or site-packages."""
    # 1) project-local venv (Windows + Linux)
    here = Path(__file__).resolve().parent.parent
    candidates = [
        here / ".venv" / "Lib" / "site-packages",
        here / ".venv" / "lib",
    ]
    for base in candidates:
        if base.exists():
            for p in base.rglob("playwright/driver/package/lib/coreBundle.js"):
                return p
    # 2) global site-packages (best-effort; may not exist in venv/embeddable)
    try:
        import site
        sps = []
        try:
            sps.extend(site.getsitepackages())
        except Exception:
            pass
        try:
            sps.append(site.getusersitepackages())
        except Exception:
            pass
        for sp in sps:
            p = Path(sp) / "playwright" / "driver" / "package" / "lib" / "coreBundle.js"
            if p.exists():
                return p
    except Exception:
        pass
    return None


# The exact substrings we replace (old -> new)
_REPLACEMENTS = [
    # Dispatcher event (runtime)
    (
        """            location: {
              url: pageError.location.url,
              line: pageError.location.lineNumber,
              column: pageError.location.columnNumber
            }""",
        """            location: {
              url: pageError.location?.url ?? "",
              line: pageError.location?.lineNumber ?? 0,
              column: pageError.location?.columnNumber ?? 0
            }""",
    ),
    # Trace event (trace recorder)
    (
        """            location: {
              url: pageError.location?.url,
              line: pageError.location?.lineNumber,
              column: pageError.location?.columnNumber
            }""",
        """            location: {
              url: pageError.location?.url ?? "",
              line: pageError.location?.lineNumber ?? 0,
              column: pageError.location?.columnNumber ?? 0
            }""",
    ),
]


def patch_playwright(log_callback=None) -> bool:
    """Apply patches to coreBundle.js. Returns True if file is already patched
    or was patched successfully."""
    cb = _find_corebundle()
    if cb is None:
        if log_callback:
            log_callback("[patch] coreBundle.js not found; skipping Playwright patch")
        return False

    try:
        text = cb.read_text(encoding="utf-8")
    except Exception as e:
        if log_callback:
            log_callback(f"[patch] cannot read {cb}: {e}")
        return False

    import re

    patched = text
    applied = 0
    for old, new in _REPLACEMENTS:
        if old in patched:
            patched = patched.replace(old, new, 1)
            applied += 1

    # Harden ALL remaining pageError.location(.|?.)field forms (Playwright 1.60+ has multiple)
    before = patched
    patched = re.sub(
        r"pageError\.location(?:\?\.|\.)url(?!\s*\?\?)",
        'pageError.location?.url ?? ""',
        patched,
    )
    patched = re.sub(
        r"pageError\.location(?:\?\.|\.)lineNumber(?!\s*\?\?)",
        "pageError.location?.lineNumber ?? 0",
        patched,
    )
    patched = re.sub(
        r"pageError\.location(?:\?\.|\.)columnNumber(?!\s*\?\?)",
        "pageError.location?.columnNumber ?? 0",
        patched,
    )
    if patched != before:
        applied += 1

    # Already fully hardened?
    if applied == 0 and "pageError.location?.url ?? \"\"" in patched and "pageError.location.url" not in patched:
        return True

    if applied == 0:
        if log_callback:
            log_callback(f"[patch] no matching patterns in {cb.name}; may already be patched or version changed")
        return "pageError.location?.url" in patched  # consider partial patch ok

    try:
        cb.write_text(patched, encoding="utf-8")
        if log_callback:
            log_callback(f"[patch] Playwright coreBundle.js patched ({applied} replacement(s))")
        return True
    except Exception as e:
        if log_callback:
            log_callback(f"[patch] failed to write {cb}: {e}")
        return False


if __name__ == "__main__":
    # Non-fatal: always exit 0 so start.bat doesn't treat missing patch as failure
    patch_playwright(log_callback=print)
    sys.exit(0)
