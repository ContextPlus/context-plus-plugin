#!/usr/bin/env python3
"""SessionStart hook — clear this session's memory de-dup tracking.

Restores V1's behavior: when a Claude Code session compacts or clears,
the session's ``runtime.session_retrievals`` de-dup tracking should be
wiped so previously-injected memory_items become re-injectable on the
next UserPromptSubmit turn.

THIN adapter (mirrors ``inject_memory_context_v2.py``): reads project
config from ``.claude/cms.config.json`` via the shared ``cms_config``
loader, then POSTs ``{project_id, session_id, source}`` to ONE MCP route
(``{mcp_base_url}/inject/reset_session``) with ``Authorization: Bearer
{api_key}``. The hook NEVER touches TDS / the DB directly, and does NOT
filter on source — the policy (act only on {compact, clear}) lives
SERVER-SIDE in the MCP route (D2). The hook simply forwards whatever
``source`` the SessionStart harness supplies.

FAIL SOFT — ALWAYS exit 0, never emit blocking stdout. Missing /
malformed config, missing session_id, a bad timeout value, a timeout, a
connection error, or a non-200 response are ALL swallowed so a
SessionStart can never be blocked. The ONE thing the hook skips
proactively is the POST when there is no ``session_id`` at all (nothing
to scope a reset to).

SILENT by default (D5). Diagnostics print to STDERR only when the
``RESET_HOOK_DEBUG`` env var is set. During bring-up, when
RESET_HOOK_DEBUG is set we also log the RAW stdin payload so the real
SessionStart field names (``source`` / ``session_id`` vs. some other
key like ``reason``) can be confirmed against the live harness — the
documented contract is ``session_id`` / ``source`` and that is what we
implement against.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request

# Shared stdlib loader for .claude/cms.config.json. Importing this module
# never raises (see cms_config.load_config's never-raise contract).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cms_config import load_config  # noqa: E402

# Default reset_session timeout used when ``timeouts.reset_session`` is missing
# or the ``timeouts`` block is not a dict (consistency with post_turn_triage).
_DEFAULT_RESET_TIMEOUT = 10.0


def _debug(msg: str) -> None:
    """Print a diagnostic to stderr ONLY when RESET_HOOK_DEBUG is set."""
    if os.environ.get("RESET_HOOK_DEBUG"):
        print(f"[reset-session] {msg}", file=sys.stderr)


def main() -> int:
    # Everything below degrades to exit 0. Missing / malformed config is
    # swallowed silently (D5) — unlike the inject hook, the SessionStart
    # reset is best-effort housekeeping, so there is no loud warning.
    config, _err = load_config()
    if config is None:
        _debug(f"config missing/malformed: {_err}")
        return 0

    try:
        # Read the raw SessionStart stdin payload first so the debug log
        # can surface the real field names during bring-up.
        raw_stdin = sys.stdin.read()
        _debug(f"raw stdin payload: {raw_stdin!r}")

        mcp_base_url = str(config["mcp_base_url"]).rstrip("/")
        mcp_url = f"{mcp_base_url}/inject/reset_session"
        api_key = config["api_key"]
        project_id = config["project_id"]
        # Timeout resolved defensively (consistency with post_turn_triage):
        # a missing key or non-dict ``timeouts`` block falls back to the
        # module default rather than raising. Still inside the
        # swallow-everything try, so a non-numeric value also degrades softly.
        _timeouts = config.get("timeouts") if isinstance(config.get("timeouts"), dict) else {}
        timeout_s = float(_timeouts.get("reset_session", _DEFAULT_RESET_TIMEOUT))
        if not (mcp_base_url and api_key and project_id):
            _debug("missing required config value")
            return 0

        event = json.loads(raw_stdin) if raw_stdin.strip() else {}
        session_id = event.get("session_id", "")
        source = event.get("source", "")

        # Skip the POST when there is no session_id — nothing to scope a
        # reset to. (Source filtering is SERVER-SIDE; we forward source
        # verbatim and let the MCP route decide whether to act.)
        if not session_id:
            _debug("no session_id in payload; skipping POST")
            return 0

        body = json.dumps(
            {
                "project_id": project_id,
                "session_id": session_id,
                "source": source,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            mcp_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "X-CMS-Project-Id": project_id,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            # Drain the response so the connection closes cleanly. The
            # hook emits NOTHING on stdout regardless of the outcome.
            _ = resp.read()
            _debug(f"POST {mcp_url} -> {resp.status}")
    except Exception:
        # Fail-soft swallow (D5): any error — bad config value, missing
        # session_id key, timeout, connection error, non-200 (urllib
        # raises HTTPError on >=400) — never blocks the session. The
        # RESET_HOOK_DEBUG escape hatch surfaces the traceback on stderr
        # without violating the default-silent contract.
        if os.environ.get("RESET_HOOK_DEBUG"):
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
