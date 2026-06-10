#!/usr/bin/env python3
"""V2 UserPromptSubmit injection adapter — tool-agnostic; ALL logic server-side.

This file is a thin tool-agnostic adapter. URL, API key, project_id and the
request timeout are now externalized into the merged plugin/project config and
read via the shared ``cms_config`` loader — nothing project-specific is
hardcoded here anymore.

DRY RUN — DO NOT emit on stdout. Slice 1's MCP route does not return an
``additional_context`` field, so the ``.get("additional_context", "")`` shape
below is silent in Slice 1 and becomes the only Claude-Code-specific line
the day a future slice adds the field.

The hook never logs anything itself (the only exception is the FIXED loud
config-broken warning below, which is intentionally surfaced). All other
observability lives server-side in cms_logs (see
``services/mcp/app/routes/inject_query_planner.py``).

Broken/missing config degrades NON-BLOCKING + LOUD: the loud warning string
is a fixed Python literal (never LLM-composed), printed to stdout, and the
POST is skipped. The hook still exits 0 so the prompt proceeds.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request

# Shared stdlib loader for the merged plugin/project config. Importing this
# module never raises (see cms_config.load_config's never-raise contract).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cms_config import load_config  # noqa: E402

# Default inject timeout used when ``timeouts.inject`` is missing or the
# ``timeouts`` block is not a dict. A missing timeout must NOT be conflated
# with a not-connected project (see main()'s defensive resolution).
_DEFAULT_INJECT_TIMEOUT = 240.0

# Fixed loud-warning literal (M-1): NEVER LLM-composed. Surfaced on stdout
# when the config is missing/malformed so the broken state is visible to the
# user rather than silently swallowed.
_CONFIG_BROKEN_WARNING = (
    "[memory-inject] this project is not connected — run "
    "/context-plus:cms-connect — skipping memory context injection for this "
    "turn. The prompt will proceed without injected project memory."
)


def main() -> int:
    # M-1: load config + handle the broken-config branch BEFORE entering the
    # urllib try/except, and check the result explicitly. A config failure
    # here must NOT fall into the silent ``except Exception`` below (that
    # would downgrade the promised loud warning to a silent swallow) and must
    # NOT raise (that would crash the turn). load_config() never raises.
    config, _err = load_config()
    if config is None:
        print(_CONFIG_BROKEN_WARNING)
        return 0

    # Resolve the connection triple via .get() so a MISSING timeouts.inject
    # key can never masquerade as a not-connected project. ONLY a missing
    # api_key / project_id / mcp_base_url triggers the loud not-connected
    # warning; a missing / non-dict timeouts block falls back to the default.
    mcp_base_url = str(config.get("mcp_base_url") or "").rstrip("/")
    mcp_url = f"{mcp_base_url}/inject/query_planner"
    api_key = config.get("api_key")
    project_id = config.get("project_id")
    # Partial/missing connection values are treated as not-connected →
    # loud-warning degradation, same as a wholly missing config. This is the
    # SOLE not-connected trigger.
    if not (mcp_base_url and api_key and project_id):
        print(_CONFIG_BROKEN_WARNING)
        return 0

    # Timeout resolved defensively and INDEPENDENTLY of the not-connected
    # check: a missing key or non-dict ``timeouts`` block falls back to the
    # module default rather than reporting "not connected". A hand-edited
    # non-numeric value falls back too (float() raises TypeError/ValueError).
    try:
        _timeouts = config.get("timeouts") if isinstance(config.get("timeouts"), dict) else {}
        timeout_s = float(_timeouts.get("inject", _DEFAULT_INJECT_TIMEOUT))
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_INJECT_TIMEOUT

    try:
        # Mirror the triage hook's stdin hardening: empty / non-dict stdin
        # degrades identically across hooks (default to {} → empty fields).
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            event = {}
        body = json.dumps(
            {
                "project_id": project_id,
                "session_id": event.get("session_id", ""),
                "prompt": event.get("prompt", ""),
                # Slice 4 (V2 dynamic agents + workflows + source-aware
                # planner): tags the planner invocation as UserPromptSubmit
                # so V21-refreshed memory-query-planner agent keeps its
                # current triage-skip judgment. The /request-context skill
                # uses ``source="agent_request"`` instead.
                "source": "user_prompt",
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
            data = json.loads(resp.read())
        # H1 (hook-runtime-adversary Pass 2): guard against non-string
        # additional_context values. Bare ``print(ctx)`` would coerce
        # an int / list / dict via repr() and inject ``[1, 2, 3]`` or
        # ``{'k': 'v'}`` as text — silently violating the
        # tool-agnostic-adapter contract.
        ctx = data.get("additional_context", "")
        if ctx and isinstance(ctx, str):
            print(ctx)
        elif ctx is not None and not isinstance(ctx, str):
            # m1 (hook-runtime-adversary fold-in): the H1 guard above
            # silently drops non-string additional_context values to
            # preserve the tool-agnostic contract. That silence is
            # correct in production but hides a server-side regression
            # in dev. Mirror the INJECT_HOOK_DEBUG escape hatch already
            # used for the outer except block so an opt-in caller can
            # see "I got a list/dict, not a string" on stderr.
            if os.environ.get("INJECT_HOOK_DEBUG"):
                print(
                    f"[inject_hook] non-string additional_context: "
                    f"{type(ctx).__name__}",
                    file=sys.stderr,
                )
    except Exception:
        # Per PR review #7 (pr-review-architect Pass 1): the silent
        # swallow is intentional (silent tool-agnostic adapter), but a
        # future maintainer's mistake (NameError, AttributeError, etc.)
        # would be utterly invisible. Provide an opt-in escape hatch via
        # env var — `INJECT_HOOK_DEBUG=1 claude` surfaces the traceback on
        # stderr without violating the default-silent contract. NOTE: a
        # broken/missing config is handled loudly BEFORE this try (M-1) and
        # never reaches this swallow.
        if os.environ.get("INJECT_HOOK_DEBUG"):
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
