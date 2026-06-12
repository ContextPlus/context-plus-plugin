---
name: request-context
description: Mid-task context retrieval for a working dynamic agent. Posts a precise need-statement to the MCP planner route under the agent's launcher-minted agent_session_id and returns the assembled additional_context block. NEVER mint a new agent_session_id and NEVER reuse one — the launcher (/call-agent) is the SOLE minter; this skill receives the id verbatim from the agent's launch prompt.
allowed-tools: Bash
---

You are the mid-task context retrieval skill for V2 dynamic agents. A working dynamic agent calls you when it needs additional memory context to finish its task. You POST a request to the MCP `/inject/query_planner` route using the agent's `agent_session_id` and return whatever `additional_context` block the route compiles.

## Project context (from config)

The MCP base URL, API key, project_id, and request timeout are read at
runtime from the shared ContextPlus config loader, which merges the plugin's
non-secret `config/cms.plugin.json` with the project's
`.claude/cms.config.json` (project values win). Nothing project-specific is
hardcoded in this skill.

## Inputs

- `agent_session_id` (REQUIRED, UUIDv4 string) — minted by the `/call-agent` launcher and passed to the agent in its launch prompt. The agent passes this value VERBATIM here. **NEVER mint a new id and NEVER reuse one from a previous launch.**
- `request` (REQUIRED, free-form text) — a precise natural-language description of what the agent needs. Treated by the planner as a precise need-statement (source="agent_request"), not a vague user prompt.

If either argument is missing, return a clear error to the calling agent and stop.

## Implementation — direct urllib POST (NOT an MCP-tool round trip)

The `/inject/query_planner` route's auth gate requires `principal_type=="user"`; an MCP-tool round trip from the MCP service principal would be rejected with `service_principal_not_allowed` (403). So this skill posts directly with the user PAT, mirroring the inject hook's urllib pattern.

The MCP base URL, API key, project_id, and timeout are loaded at runtime via the shared merged loader (`${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py`). If the config can't be assembled (project not connected), the snippet writes a structured `request-context error: config not found` line to stdout and exits 0 — never blocking the calling agent.

Run a one-shot `python3 -c '...'` via Bash:

```bash
python3 -c '
import importlib.util, json, os, sys, urllib.error, urllib.request, uuid

_TIMEOUT_FALLBACK_S = 180

try:
    # Env-var-first path resolution: read CLAUDE_PLUGIN_ROOT from the
    # environment, falling back to the harness-substituted literal. Works
    # whether the harness exports the env var OR inline-substitutes the
    # ${CLAUDE_PLUGIN_ROOT} token in this SKILL.md content. A load failure
    # is caught and degrades to the skill config-error line (no traceback).
    _root = os.environ.get("CLAUDE_PLUGIN_ROOT") or "${CLAUDE_PLUGIN_ROOT}"
    _spec = importlib.util.spec_from_file_location("cms_config", os.path.join(_root, "hooks", "cms_config.py"))
    _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
except Exception:
    sys.stdout.write("request-context error: config not found at .claude/cms.config.json")
    sys.exit(0)
_cfg, _err = _m.load_config()
if _cfg is None:
    sys.stdout.write("request-context error: config not found at .claude/cms.config.json")
    sys.exit(0)
try:
    _MCP_URL = str(_cfg["mcp_base_url"]).rstrip("/") + "/inject/query_planner"
    _API_KEY = _cfg["api_key"]
    _PROJECT_ID = _cfg["project_id"]
    _TIMEOUT_S = float(_cfg.get("timeouts", {}).get("request_context", _TIMEOUT_FALLBACK_S))
    if not (_MCP_URL and _API_KEY and _PROJECT_ID):
        raise KeyError("missing required config value")
except Exception:
    sys.stdout.write("request-context error: config not found at .claude/cms.config.json")
    sys.exit(0)

# Length-guard argv so a missing arg degrades to the "both required" error
# path below instead of raising IndexError/traceback before the guard fires.
agent_session_id = sys.argv[1] if len(sys.argv) > 1 else ""
request_text = sys.argv[2] if len(sys.argv) > 2 else ""

# m3 (hook-runtime-adversary): refuse an empty request rather than
# round-tripping to the planner with a blank prompt. Soft-exit so the
# calling agent reads the error string verbatim from stdout.
if not request_text.strip():
    sys.stdout.write("request-context error: request must be non-empty")
    sys.exit(0)

# M3 (hook-runtime-adversary): validate agent_session_id is UUID-shaped
# BEFORE the POST. A typo would pass Pydantic on the route (session_id
# is a free-form string) and silently degrade session-retrieval dedup
# for the entire agent invocation.
try:
    uuid.UUID(agent_session_id)
except ValueError:
    sys.stdout.write("request-context error: agent_session_id is not a valid UUID")
    sys.exit(0)

body = json.dumps({
    "project_id": _PROJECT_ID,
    "session_id": agent_session_id,
    "prompt": request_text,
    "source": "agent_request",
}).encode("utf-8")

req = urllib.request.Request(
    _MCP_URL,
    data=body,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_API_KEY}",
        "X-CMS-Project-Id": _PROJECT_ID,
    },
)
# M1 (hook-runtime-adversary): wrap urlopen + json.loads in a try/except
# so HTTPError / URLError / socket.timeout / JSONDecodeError surface as
# a structured "request-context error: ..." line on stdout instead of a
# Python traceback on stderr (which the calling agent never sees).
#
# PR-review fold-in #4 — refine the error catch. The planner route
# returns a TYPED ENVELOPE on a non-2xx for many failure paths
# (project_not_authorized 403, agent_not_configured 503, etc.) with the
# shape ``{"detail": {"status": "...", "error_category": "...",
# "error_detail": "...", ...}}``. urllib raises HTTPError on non-2xx —
# parse its body to surface the typed envelope category + detail to
# the calling agent. Fall through to the generic Exception catch only
# for transport / decode failures that do not carry that shape.
try:
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = json.loads(resp.read())
except urllib.error.HTTPError as http_exc:
    typed_category = None
    typed_detail = None
    try:
        body_bytes = http_exc.read()
        parsed = json.loads(body_bytes) if body_bytes else None
        if isinstance(parsed, dict):
            detail = parsed.get("detail")
            if isinstance(detail, dict):
                typed_category = detail.get("error_category")
                typed_detail = detail.get("error_detail")
    except Exception:
        # Body unreadable / non-JSON / unexpected shape — fall through
        # to the generic HTTPError surface with status+reason only.
        pass
    if typed_category or typed_detail:
        sys.stdout.write(
            f"request-context error: HTTPError "
            f"status={http_exc.code} "
            f"error_category={typed_category!r} "
            f"error_detail={typed_detail!r}"
        )
    else:
        sys.stdout.write(
            f"request-context error: HTTPError "
            f"status={http_exc.code} reason={http_exc.reason!r}"
        )
    sys.exit(0)
except Exception as exc:
    sys.stdout.write(f"request-context error: {type(exc).__name__}: {exc}")
    sys.exit(0)

ctx = data.get("additional_context", "")
if isinstance(ctx, str):
    sys.stdout.write(ctx)
' "<agent_session_id>" "<request>"
```

Replace `<agent_session_id>` and `<request>` with the literal values the calling agent supplied. Bash will quote them safely (use proper shell escaping for embedded quotes).

## Return value

Return the `additional_context` string from the response body VERBATIM to the calling agent. If the field is empty (the planner emitted `triage_skip` or no candidates passed the selector), return the empty string — that is a valid response meaning "no new context relevant to that request".

If the POST fails (non-200, transport error, JSON parse error), return a short error string starting with `request-context error: ` so the calling agent can decide how to proceed. Do NOT block the agent — context retrieval is a top-up, not a blocking dependency.

## Hard rules

- `agent_session_id` MUST come from the calling agent's launch prompt VERBATIM. The launcher is the SOLE minter. Inventing or reusing an id silently breaks session-retrieval de-duplication for the entire agent invocation.
- Use the urllib + user PAT pattern. Do NOT call any MCP tool to make this round trip — the MCP service-principal would be rejected by the route's `principal_type=="user"` gate.
- `source` MUST be the literal string `"agent_request"` so the V21-refreshed planner agent suppresses its triage-skip judgment and treats `prompt` as a precise need-statement (the V22-refreshed selector also branches on this).
- Return the `additional_context` field verbatim — do not summarize, filter, or wrap it.
- On error, return a short `request-context error: <reason>` string; do not raise into the caller.
