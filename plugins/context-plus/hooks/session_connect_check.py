#!/usr/bin/env python3
"""SessionStart hook — once-per-session "not connected" notice.

When a Claude Code session starts and this project has NOT been connected to
Context-Plus (no project-tier config supplying ``project_id`` + ``api_key``),
emit a SINGLE non-blocking systemMessage telling the user to run
``/context-plus:cms-connect``. SessionStart fires once per session, so this
fires at most once per session with no throttle file needed.

FAIL SOFT (mirrors ``reset_session_tracking.py``): ANY internal error exits 0
emitting nothing. The hook NEVER blocks a session and NEVER raises.

Connected predicate (integrity review M4): a project is connected iff the
merged config carries BOTH a non-empty ``project_id`` AND a non-empty
``api_key``. ``project_id`` / ``api_key`` are PROJECT-tier-EXCLUSIVE keys — the
plugin tier supplies only ``mcp_base_url`` / ``tds_base_url`` / ``timeouts`` /
``post_turn_triage`` — so checking these two keys on the MERGED dict is a
correct project-tier presence check. A merged-non-None result alone does NOT
mean connected (the plugin tier alone produces a non-None merged dict).
"""
from __future__ import annotations

import json
import os
import sys

# Shared stdlib loader for .claude/cms.config.json. The sys.path shim matches
# the other hooks so ``cms_config`` resolves next to this file. The import is
# wrapped so that even a missing/broken ``cms_config`` module fails soft (the
# hook's contract is NEVER raise / always exit 0) — a bare module-level import
# would otherwise raise before ``main()``'s try/except can catch it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cms_config import load_config  # noqa: E402
except Exception:  # pragma: no cover - defensive; loader ships next to hook
    load_config = None  # type: ignore[assignment]

_NOTICE = (
    "context-plus: not connected (no project config) — run "
    "/context-plus:cms-connect <project_id> <api_key>"
)


def main() -> int:
    try:
        if load_config is None:
            # Loader unavailable (import failed) — we cannot determine the
            # connection state, so fail soft and emit nothing.
            return 0
        cfg, _err = load_config()
        # project_id / api_key are PROJECT-tier-exclusive keys, so checking
        # them on the merged dict is a correct project-tier presence check.
        # ``.strip()`` so a whitespace-only value counts as not-connected.
        pid = cfg.get("project_id") if isinstance(cfg, dict) else None
        key = cfg.get("api_key") if isinstance(cfg, dict) else None
        connected = bool(
            isinstance(pid, str) and pid.strip()
            and isinstance(key, str) and key.strip()
        )
        if connected:
            return 0
        print(
            json.dumps(
                {"systemMessage": _NOTICE, "suppressOutput": False}
            )
        )
    except Exception:
        # Fail-soft: any unexpected error exits 0 emitting nothing so a
        # SessionStart can never be blocked.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
