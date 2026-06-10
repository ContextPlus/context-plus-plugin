---
name: cms-connect
description: Connect this project to Context+ by validating a project_id + API key against the MCP service and, on success, writing the per-project .claude/cms.config.json. Invocation - /context-plus:cms-connect <project_id> <api_key>
allowed-tools: Bash
---

You are the Context+ connection skill. You validate a `project_id` + `api_key`
pair against the MCP service and, ONLY on a clean 200, write the per-project
secret config (`<project>/.claude/cms.config.json`). The plugin ships the
non-secret `mcp_base_url` etc. in its own config tier; this skill adds the
project-scoped secrets the plugin must never ship.

## Inputs

- `project_id` (REQUIRED) — `$0`
- `api_key` (REQUIRED) — `$1`

If either argument is missing, tell the user the invocation form
`/context-plus:cms-connect <project_id> <api_key>` and stop.

## Project context (from config)

Only the non-secret `mcp_base_url` is needed here, and it comes from the shared
merged loader (`${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py`). The merged loader
does NOT require the project config to exist — the plugin tier alone supplies
`mcp_base_url` — so this skill works before the project is connected.

## Behavior

Run the one-shot helper below via Bash. It:

1. Loads `mcp_base_url` via the shared merged loader (plugin tier alone is
   enough; no project config required).
2. Builds the request body `{"project_id": <project_id>}` in Python (raw
   scalars only across the shell — JSON is never assembled in the shell) and
   POSTs it to `{mcp_base_url}/workflows/list` with headers
   `Authorization: Bearer <api_key>` and `X-CMS-Project-Id: <project_id>`.
3. Branches on the response with DISTINCT messages:
   - **200** → write `$CLAUDE_PROJECT_DIR/.claude/cms.config.json` (creating
     `.claude/` if missing) containing EXACTLY
     `{"project_id": ..., "api_key": ...}`, atomically (temp file +
     `os.replace`), `chmod 0600`, then print success + a reminder to run
     `/reload-plugins` (or restart) to activate.
   - **401** → `API key rejected (invalid key)`, no write.
   - **403** → `key not authorized for project <project_id>`, no write.
   - **404** → `project <project_id> not found`, no write.
   - **503** → `Context+ service unavailable, try again`, no write.
   - other non-2xx → `unexpected status <code>`, no write.
   - transport / no HTTP response → `could not reach MCP at <mcp_base_url>`,
     no write.

The write target directory is resolved from `$CLAUDE_PROJECT_DIR` (fallback:
cwd) so the config lands in the user's actual project, never in the plugin
cache.

```bash
python3 -c '
import importlib.util, json, os, sys, urllib.error, urllib.request

_project_id = sys.argv[1]
_api_key = sys.argv[2]
if not (_project_id and _api_key):
    print("cms-connect error: usage /context-plus:cms-connect <project_id> <api_key>")
    sys.exit(0)

# Resolve mcp_base_url from the plugin tier (no project config required).
_spec = importlib.util.spec_from_file_location("cms_config", "${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py")
_m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
_cfg, _err = _m.load_config()
if _cfg is None:
    print(f"cms-connect error: could not load plugin config ({_err})")
    sys.exit(0)
try:
    _base = str(_cfg["mcp_base_url"]).rstrip("/")
    if not _base:
        raise KeyError("mcp_base_url")
except Exception:
    print("cms-connect error: mcp_base_url missing from plugin config")
    sys.exit(0)

_url = _base + "/workflows/list"
_body = json.dumps({"project_id": _project_id}).encode("utf-8")
_req = urllib.request.Request(
    _url,
    data=_body,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_api_key}",
        "X-CMS-Project-Id": _project_id,
    },
    method="POST",
)

_status = None
try:
    with urllib.request.urlopen(_req, timeout=30) as r:
        _status = r.getcode()
        r.read()
except urllib.error.HTTPError as e:
    _status = e.code
except Exception:
    print(f"could not reach MCP at {_base}")
    sys.exit(0)

if _status == 200:
    _proj_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    _claude_dir = os.path.join(_proj_dir, ".claude")
    _dest = os.path.join(_claude_dir, "cms.config.json")
    try:
        os.makedirs(_claude_dir, exist_ok=True)
        _payload = json.dumps({"project_id": _project_id, "api_key": _api_key}, indent=2) + "\n"
        _tmp = _dest + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            f.write(_payload)
        os.chmod(_tmp, 0o600)
        os.replace(_tmp, _dest)
        os.chmod(_dest, 0o600)
    except Exception as exc:
        print(f"cms-connect error: validated but failed to write {_dest}: {exc}")
        sys.exit(0)
    print(f"Connected to Context+ — wrote {_dest}")
    print("Run /reload-plugins (or restart Claude Code) to activate.")
elif _status == 401:
    print("API key rejected (invalid key)")
elif _status == 403:
    print(f"key not authorized for project {_project_id}")
elif _status == 404:
    print(f"project {_project_id} not found")
elif _status == 503:
    print("Context+ service unavailable, try again")
else:
    print(f"unexpected status {_status}")
' "$0" "$1"
```

## Hard rules

- Pass `project_id` and `api_key` to the helper as raw scalar shell arguments
  (`$0`, `$1`). NEVER assemble the request JSON in the shell — build it in
  Python.
- Write the project config ONLY on a clean 200. Every other status (and any
  transport failure) prints its distinct message and writes NOTHING.
- The write is atomic (temp file + `os.replace`) and the file is `chmod 0600`.
- Resolve the write location from `$CLAUDE_PROJECT_DIR` (fallback cwd). Never
  write into the plugin cache.
- `mcp_base_url` comes from the shared merged loader's plugin tier — this skill
  must work BEFORE the project config exists, so it must NOT require the
  project tier.
