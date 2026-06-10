---
name: workflow
description: Load an orchestrator workflow from the V2 workflows table. Use when you need to follow a defined process for a task (e.g. /workflow large-changes). Without arguments, lists available workflows.
allowed-tools: Bash
---

You are loading an orchestrator workflow from the V2 `memory_core.workflows` table.

## Project context (from config)

The MCP base URL, API key, project_id, and timeout are read at runtime from
the shared Context+ config loader, which merges the plugin's non-secret
`config/cms.plugin.json` with the project's `.claude/cms.config.json`
(project values win). This skill reaches the workflows surface over HTTP — a
direct `urllib` POST to the MCP `/workflows/*` routes with the user PAT —
mirroring the inject hook and the `/context-plus:request-context` skill.

## The MCP POST helper

Every workflows call below runs this one-shot helper via Bash. It takes the
route path and (optionally) ONE raw scalar key/value as separate shell
arguments, then **builds the JSON request body in Python** — JSON is never
assembled in, or passed through, the shell (raw scalars only, exactly like the
`/context-plus:request-context` skill passes `agent_session_id`/`request`). It
loads config via the shared merged loader, auto-injects `project_id`, POSTs,
and prints the raw JSON response on success. On failure it prints a
single-line envelope and never raises a traceback. Two distinct failure shapes:

- HTTP error (route reachable, non-2xx): `{"error": true, "status": <code>, "body": <parsed-json-or-null>}`
- transport / config error (no HTTP response): `{"error": true, "status": null, "detail": "<text>"}`

```bash
python3 -c '
import importlib.util, json, os, sys, urllib.error, urllib.request

_TIMEOUT_FALLBACK_S = 30.0

try:
    # Env-var-first path resolution: read CLAUDE_PLUGIN_ROOT from the
    # environment, falling back to the harness-substituted literal. Works
    # whether the harness exports the env var OR inline-substitutes the
    # ${CLAUDE_PLUGIN_ROOT} token in this SKILL.md content. A load failure
    # is caught and printed as the config-error envelope (no traceback).
    _root = os.environ.get("CLAUDE_PLUGIN_ROOT") or "${CLAUDE_PLUGIN_ROOT}"
    _spec = importlib.util.spec_from_file_location("cms_config", os.path.join(_root, "hooks", "cms_config.py"))
    _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
except Exception as exc:
    print(json.dumps({"error": True, "status": None, "detail": f"config loader unavailable: {exc}"}))
    sys.exit(0)
_cfg, _err = _m.load_config()
if _cfg is None:
    print(json.dumps({"error": True, "status": None, "detail": "config not found at .claude/cms.config.json"}))
    sys.exit(0)
try:
    _base = str(_cfg["mcp_base_url"]).rstrip("/")
    _key = _cfg["api_key"]
    _pid = _cfg["project_id"]
    _timeout = float(_cfg.get("timeouts", {}).get("workflow", _TIMEOUT_FALLBACK_S))
    if not (_base and _key and _pid):
        raise KeyError("missing required config value")
except Exception as exc:
    print(json.dumps({"error": True, "status": None, "detail": f"config unreadable: {exc}"}))
    sys.exit(0)

# Build the body in Python from raw scalar argv. argv[1]=path; optional
# argv[2]/argv[3]=one key/value (e.g. "workflow_key" "<slug>"). No JSON
# crosses the shell.
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

Pass `<path>` alone for list calls, or `<path> "<key>" "<value>"` for a keyed
get. The values are raw scalars — Bash quoting (single quotes) is sufficient;
there is no embedded JSON to escape. (This helper is intentionally kept
near-identical to the one in the `call-agent` skill; keep them mirrored.)

## If no arguments provided

Run the helper with path `/workflows/list` (no key/value). The response is
`{"workflows": [...]}`. Present each workflow's `name`, `description`, and
`trigger_hint`, then ask which workflow the user wants to load.

## If a workflow key is provided

Argument: `$0`

1. Run the helper as: path `/workflows/get`, key `workflow_key`, value `$0`.
2. On success the response IS the workflow object (fields `workflow_key`,
   `name`, `description`, `trigger_hint`, `content`, ...). Output the workflow
   content in full, prefixed with:

```
WORKFLOW LOADED: <name>

The following workflow is now active. Follow it for the current task.
Every response must be prefixed with: WORKFLOW: <name> - Step <N>
When not following a workflow, prefix with: NO WORKFLOW

---
```

3. Then output the full `content` field VERBATIM.

4. Error handling — branch on the envelope shape:
   - `{"error": true, "status": 404, ...}` AND `body` is present: read the
     token at `body.detail.error`. If `workflow_not_found`, run the helper
     with path `/workflows/list` and show the available options. If
     `project_not_found`, surface that clearly.
   - `{"error": true, "status": <other code>}` (e.g. 503 `tds_unavailable` at
     `body.detail.error`): report it and stop.
   - `{"error": true, "status": null, "detail": "..."}` (transport/config
     failure — there is NO `body`): report the `detail` and stop. Do NOT treat
     this as a 404.

## Hard rules

- Reach the workflows surface over HTTP via the MCP POST helper.
- Build request JSON in Python from raw scalar args — never assemble or pass
  JSON through the shell.
- All config values come from the merged plugin/project config. Do NOT prompt
  the user for project_id, base URL, or API key.
- Output the `content` field verbatim — never paraphrase, summarize, or filter.
- The WORKFLOW LOADED banner is required ONLY when a workflow_key argument is
  provided AND the get succeeded.
