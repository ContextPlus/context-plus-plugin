---
name: call-agent
description: Invoke a dynamic agent by name. Loads its profile from the V2 dynamic_agents table, mints a fresh agent_session_id, then launches the dynamic-agent subagent with everything pre-assembled. Use this whenever you need to delegate work to a specialized agent. Example - /call-agent feature-implementation-planner Plan how to add user authentication
user-invocable: true
allowed-tools: Agent, Bash
---

You are the agent launcher for V2 dynamic agents. Your job is to orchestrate four steps in exact order, then return the agent's output to the user. Do not do the task yourself.

## Project context (from config)

The MCP base URL, API key, project_id, and timeout are read at runtime from
the shared Context+ config loader, which merges the plugin's non-secret
`config/cms.plugin.json` with the project's `.claude/cms.config.json`
(project values win). This skill reaches the dynamic-agents surface over HTTP
— a direct `urllib` POST to the MCP `/dynamic_agents/*` routes with the user
PAT — mirroring the inject hook and the `/context-plus:request-context` skill.

## Helper 1 — the MCP read POST (list / get)

Runs via Bash for `/dynamic_agents/list` and `/dynamic_agents/get`. It takes the
route path and (optionally) ONE raw scalar key/value as separate shell
arguments and **builds the JSON body in Python** — JSON is never assembled in,
or passed through, the shell (raw scalars only, like `/context-plus:request-context`).
It loads config via the shared merged loader, auto-injects `project_id`, and
prints the raw JSON response on success. On failure it prints a single-line
envelope (two distinct shapes) and never raises a traceback:

- HTTP error (route reachable, non-2xx): `{"error": true, "status": <code>, "body": <parsed-json-or-null>}`
- transport / config error (no HTTP response): `{"error": true, "status": null, "detail": "<text>"}`

```bash
python3 -c '
import importlib.util, json, sys, urllib.error, urllib.request

_TIMEOUT_FALLBACK_S = 30.0

_spec = importlib.util.spec_from_file_location("cms_config", "${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py")
_m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
_cfg, _err = _m.load_config()
if _cfg is None:
    print(json.dumps({"error": True, "status": None, "detail": "config not found at .claude/cms.config.json"}))
    sys.exit(0)
try:
    _base = str(_cfg["mcp_base_url"]).rstrip("/")
    _key = _cfg["api_key"]
    _pid = _cfg["project_id"]
    _timeout = float(_cfg.get("timeouts", {}).get("call_agent_tds", _TIMEOUT_FALLBACK_S))
    if not (_base and _key and _pid):
        raise KeyError("missing required config value")
except Exception as exc:
    print(json.dumps({"error": True, "status": None, "detail": f"config unreadable: {exc}"}))
    sys.exit(0)

# Build the body in Python from raw scalar argv. argv[1]=path; optional
# argv[2]/argv[3]=one key/value (e.g. "agent_id" "<id>"). No JSON crosses the shell.
_path = sys.argv[1]
_payload = {"project_id": _pid}
if len(sys.argv) >= 3 and sys.argv[2]:
    _k = sys.argv[2]
    _v = sys.argv[3] if len(sys.argv) >= 4 else ""
    if not _v:
        print(json.dumps({"error": True, "status": None, "detail": f"missing value for key {_k!r}"}))
        sys.exit(0)
    _payload[_k] = _v

_req = urllib.request.Request(
    _base + _path,
    data=json.dumps(_payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key}", "X-CMS-Project-Id": _pid},
    method="POST",
)
try:
    with urllib.request.urlopen(_req, timeout=_timeout) as r:
        sys.stdout.write(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    try:
        _b = json.loads(e.read())
    except Exception:
        _b = None
    print(json.dumps({"error": True, "status": e.code, "body": _b}))
except Exception as e:
    print(json.dumps({"error": True, "status": None, "detail": f"{type(e).__name__}: {e}"}))
' "<path>" "<key>" "<value>"
```

(This read helper is intentionally kept near-identical to the one in the
`workflow` skill; keep them mirrored.)

## Helper 2 — the MCP upsert POST (model-preference writeback)

Used ONLY by Step 1b when a model preference must be persisted. It reads the
FULL saved profile from a FILE (the verbatim Step-1 get response) and overrides
only `model_preference` from a raw scalar arg, then PUT-upserts. The unbounded
`instruction` (and `description`/`tool_preference`) are carried straight from
the file to the HTTP body in Python — they NEVER cross the shell, so they
round-trip byte-for-byte regardless of quotes, backticks, `$`, or newlines.

```bash
python3 -c '
import importlib.util, json, sys, urllib.error, urllib.request

_TIMEOUT_FALLBACK_S = 30.0

_spec = importlib.util.spec_from_file_location("cms_config", "${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py")
_m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
_cfg, _err = _m.load_config()
if _cfg is None:
    print(json.dumps({"error": True, "status": None, "detail": "config not found at .claude/cms.config.json"}))
    sys.exit(0)
try:
    _base = str(_cfg["mcp_base_url"]).rstrip("/")
    _key = _cfg["api_key"]
    _pid = _cfg["project_id"]
    _timeout = float(_cfg.get("timeouts", {}).get("call_agent_tds", _TIMEOUT_FALLBACK_S))
    if not (_base and _key and _pid):
        raise KeyError("missing required config value")
except Exception as exc:
    print(json.dumps({"error": True, "status": None, "detail": f"config unreadable: {exc}"}))
    sys.exit(0)

# argv[1]=path to the saved profile JSON file; argv[2]=new model_preference.
try:
    _prof = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(json.dumps({"error": True, "status": None, "detail": f"profile file unreadable: {exc}"}))
    sys.exit(0)
if _prof.get("error") or not _prof.get("agent_id"):
    print(json.dumps({"error": True, "status": None, "detail": "saved profile is not a valid agent object"}))
    sys.exit(0)

# Full-profile PUT: every field carried verbatim from the file; only
# model_preference is overridden. Omitting any field would null it out.
_payload = {
    "project_id": _pid,
    "agent_id": _prof["agent_id"],
    "name": _prof["name"],
    "description": _prof.get("description"),
    "instruction": _prof["instruction"],
    "model_preference": sys.argv[2],
    "tool_preference": _prof.get("tool_preference"),
}

_req = urllib.request.Request(
    _base + "/dynamic_agents/upsert",
    data=json.dumps(_payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key}", "X-CMS-Project-Id": _pid},
    method="POST",
)
try:
    with urllib.request.urlopen(_req, timeout=_timeout) as r:
        sys.stdout.write(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    try:
        _b = json.loads(e.read())
    except Exception:
        _b = None
    print(json.dumps({"error": True, "status": e.code, "body": _b}))
except Exception as e:
    print(json.dumps({"error": True, "status": None, "detail": f"{type(e).__name__}: {e}"}))
' "<profile_file_path>" "<new_model_preference>"
```

## Inputs

- Agent ID: `$0`
- Task description: Everything after the agent ID in the arguments

If no agent ID is provided, run Helper 1 with path `/dynamic_agents/list`
(response shape `{"agents": [...]}`) to show available agents and ask the user
which one to use.

## Step 1: Load the agent profile

First create a UNIQUE per-invocation temp file so concurrent `/call-agent`
runs in the same checkout can never clobber each other's profile (a fixed path
could make Step 1b upsert one agent's model onto another agent's row):

```bash
mktemp /tmp/cms_agent_profile.XXXXXX.json
```

Capture the printed path and use it as `<profile_file>` for the rest of THIS
invocation. Then run Helper 1 as: path `/dynamic_agents/get`, key `agent_id`,
value `$0`, redirecting stdout to that file so the full profile (with its
unbounded `instruction`) is reused verbatim by Step 1b without crossing the
shell:

```bash
python3 -c '<Helper 1>' "/dynamic_agents/get" "agent_id" "$0" > "<profile_file>"
```

Read `<profile_file>` and FIRST check whether it is an error envelope (a JSON
object with `"error": true`). If so, branch on its shape BEFORE treating it as
a profile, and do NOT proceed to Step 1b:
- `{"error": true, "status": 404, ...}` AND `body` present: the token is at
  `body.detail.error` (e.g. `dynamic_agent_not_found`). Run Helper 1 with path
  `/dynamic_agents/list` and tell the user which agents exist.
- `{"error": true, "status": null, "detail": "..."}` (transport/config failure,
  NO `body`): report the `detail` and stop. Do NOT treat it as a 404.
- Any other `{"error": true}`: report `status`/`body` and stop.

Otherwise the file IS the V2 profile object with these fields (the structured
V1 splits `role_instructions / output_expectations / what_it_handles /
when_not_to_use` are folded into the single `instruction` blob):

- `project_id`
- `agent_id`
- `name`
- `description`
- `instruction` — paste this entire text into the agent's prompt verbatim
- `model_preference` — sentinel-aware (see Step 1b)
- `tool_preference` — sentinel-aware (see Step 1b)
- `created_at`, `updated_at`

## Step 1b: Resolve model and tool preferences

**Sentinel string (locked verbatim):**

```
ask the user next time this agent is invoked, then update this field with their answer
```

**Model preference:**

- If `model_preference` is `null` OR matches the sentinel string EXACTLY: ask the user `"Which model should I use for **<name>**? (sonnet / opus / haiku — you can change this anytime)"`. Wait for their answer. Normalize to one of: `sonnet`, `opus`, `haiku`. Then persist it: run **Helper 2** with the saved profile file (`<profile_file>` from Step 1) and the new model: `python3 -c '<Helper 2>' "<profile_file>" "<model>"`. Helper 2 carries `description`, `instruction`, and `tool_preference` straight from the file VERBATIM and overrides only `model_preference` — so the PUT-style (full-row overwrite by composite PK) upsert cannot null out or corrupt any other field. Announce: `"Invoking **<name>** with **<model>**"`.
- If `model_preference` contains a recognizable model keyword (sonnet, opus, haiku — case-insensitive): resolve to that model alias. Announce: `"Invoking **<name>** with **<model>**"`.
- If `model_preference` is non-null but ambiguous or unresolvable (e.g. `inherit`): treat as "no override" — do NOT pass a `model:` to the Agent tool in Step 3 (the subagent inherits the launcher's model). Do not write anything back.

Store the resolved model alias (if any) for use in Step 3.

**Tool preference:**

- If `tool_preference` is `null` OR matches the sentinel string EXACTLY: treat as no guidance (do not inject the tool guidance block in Step 3).
- Otherwise: store `tool_preference` for injection in Step 3.

## Step 2: Mint agent_session_id

Mint a fresh UUIDv4 for `agent_session_id`. Per invocation; never reuse, never cache, never re-mint mid-invocation. The launcher is the SOLE minter of this id. The dynamic agent receives it as a string in its launch prompt and passes it verbatim to `/context-plus:request-context`.

**UUID-mint failure fallback:** if the `python3` subprocess fails (non-zero exit, missing interpreter, empty stdout), STOP and report to the user: `/call-agent: unable to mint agent_session_id — Python3 unavailable`. Do NOT fall back to a manually-typed id, do NOT proceed with a placeholder, and do NOT continue to Step 3 — the launcher is the sole minter of `agent_session_id`, and a missing/invalid id silently breaks session-retrieval de-duplication for the entire agent invocation.

Invoke via Bash:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

Capture stdout as `agent_session_id` and thread it into Step 3.

## Step 3: Launch the context-plus:dynamic-agent subagent

Launch the `context-plus:dynamic-agent` subagent using the Agent tool. Pass it a prompt that contains ALL of the following — the agent should not need to fetch anything itself.

When a model was resolved in Step 1b, pass `model: <resolved_model>` to the Agent tool call. (If the preference was `inherit`/unresolved, omit `model:`.)

Only inject the tool guidance blocks when `tool_preference` is non-null and NOT the sentinel string.

**PSEUDOCODE — represents the Agent tool invocation the launcher LLM must perform; not a literal copy-paste.** The launcher LLM interprets the block below as a description of the actual Agent tool call (subagent_type, model, and prompt arguments) — it does not paste this text into a Bash command or into any file. The placeholders (`<resolved_model>`, `<UUID>`, `<tool_preference>`, the instruction text, the user's task description, the agent_session_id) are substituted into the Agent tool's `prompt` argument at the corresponding sites.

```
Agent(
  subagent_type: "context-plus:dynamic-agent",
  model: <resolved_model>,   ← omit this line if no model was resolved
  prompt: "## Your Identity

  <paste the full `instruction` text from Step 1 here verbatim>

  **Tool guidance:** <tool_preference>   ← omit this line if no tool guidance

  ## Your Task

  <paste the user's full task description>

  ## Mid-task context retrieval

  Your agent_session_id is <UUID>. To request additional context mid-task, invoke the /context-plus:request-context skill with:

    agent_session_id=<UUID>
    request=<natural-language description of what you need>

  The skill returns the assembled context block. NEVER mint a new agent_session_id and NEVER reuse one from a previous launch — the launcher is the sole minter, and this id is bound to your invocation alone.

  **Reminder — tool guidance:** <tool_preference>   ← omit this line if no tool guidance

  ## Hard Rules

  - You MUST complete the task as described above.
  - You MUST NOT exceed your defined scope.
  **You MUST** follow the tool guidance above.   ← omit this line if no tool guidance"
)
```

## Step 4: Return the result

Return the dynamic agent's output directly to the user. Do not summarize or filter it.

## Hard rules

- Execute Steps 1, 1b, 2, 3, 4 in order. Do not skip any step.
- Reach the dynamic-agents surface over HTTP via the MCP POST helpers. Build
  request JSON in Python — never assemble or pass JSON through the shell.
- The model-preference writeback (Step 1b) MUST use Helper 2 reading the saved
  profile FILE, so `instruction`/`description`/`tool_preference` round-trip
  verbatim. Never reconstruct the `instruction` blob by hand or pass it as a
  shell argument — the PUT-style upsert overwrites the whole row by composite
  PK, so a corrupted or omitted field would destroy the stored value.
- Do NOT do the task yourself. You are a launcher, not a worker.
- Step 2 MUST mint a fresh agent_session_id every single invocation. Reusing an id from a previous launch would cause `/context-plus:request-context` calls to silently filter against the wrong session's retrieval history.
- All config values come from the merged plugin/project config. Do NOT prompt the user for project_id, base URL, or API key.
- If any step fails fatally (network, agent not found, Python3 unavailable, etc.), report the failure clearly and stop.
