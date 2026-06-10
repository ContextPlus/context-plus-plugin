#!/usr/bin/env python3
"""Post-turn hook → POST the parsed last turn to V2 MCP /post_turn_triage.

Fires on ``Stop`` and ``SubagentStop``. Reads the Claude Code transcript,
extracts the last user prompt + concatenated assistant response, and
ships the result to the V2 MCP service for triage + fan-out detection.

Configuration:
  * All project-specific values — ``project_id``, ``api_key``, the MCP base
    URL, the request timeout, and the ``SubagentStop`` toggle — are read from
    ``.claude/cms.config.json`` via the shared ``cms_config`` loader. Nothing
    is hardcoded here and there are NO env-var overrides (the prior
    ``MCP_BASE_URL`` / ``MCP_PROJECT_ID`` / ``MCP_REQUEST_TIMEOUT`` /
    ``MCP_API_KEY`` reads have been removed). The POST timeout is a cap so the
    hook never blocks the user for more than that on a slow network leg.

Errors are swallowed (printed to stderr only) so a misbehaving MCP service
NEVER fails the user's local turn. Hook exit code is always 0. A
missing/malformed config likewise causes the hook to skip the POST and exit
0 without surfacing anything (this is the Stop hook — degrade silently).

Implementation notes
====================

* stdlib only — no httpx / requests dependency, no venv setup required.
* Walks the transcript backwards to find the LAST real user prompt (skips
  tool_result messages, which are also role="user"). Concatenates all
  assistant text blocks emitted AFTER that user prompt for ``llm_response``.
* ``tool_activity`` / ``subagent_activity`` are left empty — the V2 ParsedTurn
  schema accepts empty arrays. A richer parser (matching V1's behavior) is
  out of scope for this first version.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Shared stdlib loader for .claude/cms.config.json. Importing this module
# never raises (see cms_config.load_config's never-raise contract).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cms_config import load_config  # noqa: E402


# Fallback used only when the config omits the post_turn_triage timeout.
_DEFAULT_TIMEOUT = 5.0

# The master toggle for whether the hook fires on ``SubagentStop`` events
# now lives in .claude/cms.config.json under
# ``post_turn_triage.fire_on_subagent_stop`` (default False). When False,
# subagent stops are skipped so the cms_logs S3 partition isn't flooded with
# one session_id per subagent invocation (workflow planner/reviewer/
# implementer/etc., Explore agents, context-plus dynamic-agent runs all share
# the parent's intent but get their own UUID from Claude Code, which pollutes
# the partition). Flip it to True in the config to capture every subagent
# stop too. Requires session restart or /reload-plugins to take effect
# (Claude Code caches the hook command at session startup).


def _extract_text(content: object) -> str:
    """Return the text portion of a Claude Code message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _is_real_user_message(content: object) -> bool:
    """True if this user message is a real prompt, NOT a tool_result block."""
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        # A real user prompt has at least one "text" block. A tool_result
        # message has only "tool_result" blocks.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return True
        return False
    return False


def _summarize_input(inp: object) -> str | None:
    """Compact JSON repr of a tool input dict, capped at 300 chars."""
    if inp is None:
        return None
    try:
        s = json.dumps(inp, separators=(",", ":"))
    except (TypeError, ValueError):
        s = repr(inp)
    return s[:300] if s else None


def _extract_result_text(content: object) -> str:
    """Extract text from a tool_result.content (str | list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def parse_last_turn(transcript_path: str) -> tuple[str, str, list[dict], list[dict]]:
    """Parse the most recent turn from a Claude Code transcript.

    Returns ``(user_prompt, llm_response, tool_activity, subagent_activity)``
    where the activity lists conform to the V2 ``ParsedTurn`` schema (see
    ``services/mcp/app/schemas/parsed_turn.py``):

      * ``tool_activity[i]`` = ``{tool_name, phase, input_summary?,
        output_summary?, error_summary?}``. Phase is ``"success"`` |
        ``"failure"`` | ``"attempt"``.
      * ``subagent_activity[i]`` = ``{agent_name, phase, purpose?,
        output_summary?, error_summary?}``. ``Task`` tool calls are
        classified as subagent activity (NOT tool activity) because the
        V1 spec treats them separately.

    Algorithm:
      1. Walk backwards to find the last real user-role message.
      2. Walk forward from there to end-of-file, collecting:
         - assistant ``text`` blocks → ``llm_response`` (concatenated)
         - assistant ``tool_use`` blocks → pending entries keyed on ``id``
         - user ``tool_result`` blocks → match to pending by ``tool_use_id``
      3. Pending tools with no matching result are emitted with
         ``phase="attempt"`` (the user stopped before the tool resolved).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return "", "", [], []

    try:
        with open(transcript_path, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"post_turn_triage: transcript read failed: {exc}", file=sys.stderr)
        return "", "", [], []

    # Step 1: locate the last real user prompt.
    last_user_idx: int | None = None
    user_prompt = ""
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and _is_real_user_message(content):
            user_prompt = _extract_text(content)
            last_user_idx = i
            break

    if last_user_idx is None:
        return user_prompt, "", [], []

    # Step 2: walk forward, collecting assistant text + tool_use/tool_result pairs.
    assistant_chunks: list[str] = []
    pending: dict[str, dict] = {}  # tool_use_id → partial activity dict
    tool_activity: list[dict] = []
    subagent_activity: list[dict] = []

    for entry in entries[last_user_idx + 1:]:
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            # The assistant rarely has string content (mostly via the
            # historical contract); be defensive and skip.
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if role == "assistant":
                if btype == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        assistant_chunks.append(text)
                elif btype == "tool_use":
                    use_id = block.get("id")
                    if not isinstance(use_id, str):
                        continue
                    name = block.get("name") or "<unknown>"
                    inp = block.get("input")
                    if name == "Task":
                        # Subagent invocation. Pull agent name + purpose
                        # from the Task input shape.
                        agent_name = "general-purpose"
                        purpose: str | None = None
                        if isinstance(inp, dict):
                            agent_name = (
                                inp.get("subagent_type")
                                or inp.get("agent_type")
                                or agent_name
                            )
                            # ``description`` is the short purpose;
                            # ``prompt`` is the full brief. Prefer
                            # description for purpose since it's short.
                            purpose = (
                                inp.get("description")
                                or inp.get("prompt")
                            )
                        pending[use_id] = {
                            "kind": "subagent",
                            "agent_name": str(agent_name)[:128],
                            "purpose": (
                                str(purpose)[:300] if purpose else None
                            ),
                        }
                    else:
                        pending[use_id] = {
                            "kind": "tool",
                            "tool_name": str(name)[:128],
                            "input_summary": _summarize_input(inp),
                        }
            elif role == "user":
                if btype == "tool_result":
                    use_id = block.get("tool_use_id")
                    if not isinstance(use_id, str):
                        continue
                    is_error = bool(block.get("is_error"))
                    result_text = _extract_result_text(block.get("content"))

                    p = pending.pop(use_id, None)
                    if p is None:
                        continue

                    phase = "failure" if is_error else "success"
                    if p["kind"] == "subagent":
                        out: dict = {
                            "agent_name": p["agent_name"],
                            "phase": phase,
                        }
                        if p.get("purpose"):
                            out["purpose"] = p["purpose"]
                        if result_text:
                            key = "error_summary" if is_error else "output_summary"
                            out[key] = result_text[:500]
                        subagent_activity.append(out)
                    else:
                        out = {
                            "tool_name": p["tool_name"],
                            "phase": phase,
                        }
                        if p.get("input_summary"):
                            out["input_summary"] = p["input_summary"]
                        if result_text:
                            key = "error_summary" if is_error else "output_summary"
                            out[key] = result_text[:500]
                        tool_activity.append(out)

    # Any pending tools that never got a tool_result → "attempt" phase.
    for p in pending.values():
        if p["kind"] == "subagent":
            out = {"agent_name": p["agent_name"], "phase": "attempt"}
            if p.get("purpose"):
                out["purpose"] = p["purpose"]
            subagent_activity.append(out)
        else:
            out = {"tool_name": p["tool_name"], "phase": "attempt"}
            if p.get("input_summary"):
                out["input_summary"] = p["input_summary"]
            tool_activity.append(out)

    llm_response = "\n".join(assistant_chunks)
    return user_prompt, llm_response, tool_activity, subagent_activity


def main() -> int:
    # All project-specific values come from .claude/cms.config.json — there
    # are no env-var overrides. A missing/malformed config (or a missing
    # required value) degrades silently: this is the Stop hook, so we skip
    # the POST and exit 0 without surfacing anything to the user.
    config, _err = load_config()
    if config is None:
        return 0

    try:
        api_key = config["api_key"]
        base_url = str(config["mcp_base_url"]).rstrip("/")
        project_id = config["project_id"]
        if not (api_key and base_url and project_id):
            return 0
    except (KeyError, TypeError):
        return 0

    # The SubagentStop toggle has a documented default of False, so — unlike
    # the three required connection values above — a config that omits the
    # optional ``post_turn_triage`` block must NOT be treated as fatal.
    # Coupling it into the required-key try would make a missing toggle raise
    # KeyError and disable triage entirely, even on a normal ``Stop`` event.
    # Resolve it defensively with the documented default instead.
    pt = config.get("post_turn_triage")
    fire_on_subagent_stop = (
        bool(pt.get("fire_on_subagent_stop", False))
        if isinstance(pt, dict)
        else False
    )

    # A non-dict ``timeouts`` value (list/str/null) would make ``.get`` raise
    # ``AttributeError`` — which is NOT a (TypeError, ValueError) and would
    # crash the Stop hook (exit 1 + traceback), violating the silent-degrade
    # contract. Guard the shape first, then fall back to the default on a
    # missing key or a non-numeric value.
    try:
        timeouts = config.get("timeouts")
        if not isinstance(timeouts, dict):
            timeouts = {}
        timeout = float(timeouts.get("post_turn_triage", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            event = {}
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"post_turn_triage: stdin parse failed: {exc}", file=sys.stderr)
        return 0

    hook_event_name = event.get("hook_event_name") or "Stop"
    session_id = event.get("session_id") or ""
    transcript_path = event.get("transcript_path") or ""

    # SubagentStop master gate — driven by the config-sourced
    # ``fire_on_subagent_stop`` toggle (see the comment near the top). When
    # False (default), exit silently before doing any work so the cms_logs
    # partition isn't flooded with one new session_id per subagent
    # invocation. The Stop hook on the parent session still fires normally
    # and captures the outer turn.
    if hook_event_name == "SubagentStop" and not fire_on_subagent_stop:
        return 0
    # For SubagentStop, Claude Code surfaces the subagent identifier.
    # Tested-against-V1 contract: agent_type is the subagent name on
    # SubagentStop and "main" on Stop.
    if hook_event_name == "SubagentStop":
        agent_type = (
            event.get("subagent_name")
            or event.get("agent_type")
            or "subagent"
        )
    else:
        agent_type = "main"

    user_prompt, llm_response, tool_activity, subagent_activity = parse_last_turn(
        transcript_path
    )

    body = {
        "project_id": project_id,
        "session_id": session_id,
        "hook_event_name": hook_event_name,
        "agent_type": agent_type,
        "turn_transcript": {
            "user_prompt": user_prompt,
            "llm_response": llm_response,
            "tool_activity": tool_activity,
            "subagent_activity": subagent_activity,
        },
    }

    url = f"{base_url}/post_turn_triage"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-CMS-Project-Id": project_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Drain but ignore the body — the parent route's response is
            # informational only.
            resp.read()
    except urllib.error.HTTPError as exc:
        # Read up to 1 KiB of the upstream body for diagnosis.
        try:
            snippet = exc.read()[:1024].decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            snippet = ""
        print(
            f"post_turn_triage: upstream {exc.code} {exc.reason}: {snippet}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — never break the user's turn.
        print(f"post_turn_triage: post failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
