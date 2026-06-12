---
name: cms-connect
description: Connect this project to ContextPlus by validating a project_id + API key against the MCP service and, on success, writing the per-project .claude/cms.config.json. Invocation - /context-plus:cms-connect <project_id> <api_key>
allowed-tools: Bash
---

You are the ContextPlus connection skill. You validate a `project_id` + `api_key`
pair against the MCP service and, ONLY on a clean 200, write the per-project
secret config (`<project>/.claude/cms.config.json`). The plugin ships the
non-secret `mcp_base_url` etc. in its own config tier; this skill adds the
project-scoped secrets the plugin must never ship.

## Inputs

- `project_id` (REQUIRED) — `$0`
- `api_key` (REQUIRED) — `$1`

If either argument is missing, the helper itself emits the `❌ NOT connected —
missing arguments (need <project_id> <api_key>). No config was written.`
verdict line and stops — there is no separate soft usage message.

## Project context (from config)

Only the non-secret `mcp_base_url` is needed here, and it comes from the shared
merged loader (`${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py`). The merged loader
does NOT require the project config to exist — the plugin tier alone supplies
`mcp_base_url` — so this skill works before the project is connected.

## Behavior

ONE-VERDICT CONTRACT: the helper ALWAYS terminates with EXACTLY ONE verdict
line and nothing else load-bearing follows it. The line is one of:

- success: `✅ Connected — wrote <abs-path>. Run /reload-plugins.` (a single
  line — there is no separate second success line).
- any failure: `❌ NOT connected — <reason>. No config was written.`

The orchestrator MUST surface this verdict line to the user VERBATIM. There is
no soft/ambiguous outcome — every branch resolves to exactly one ✅ or ❌ line.

Run the one-shot helper below via Bash. It:

1. Validates that BOTH `project_id` and `api_key` were supplied. Missing/empty
   either → `❌ NOT connected — missing arguments (need <project_id>
   <api_key>). No config was written.` (this is the hard failure form — there
   is no soft usage line).
2. Loads `mcp_base_url` via the shared merged loader (plugin tier alone is
   enough; no project config required). A loader/config failure resolves to
   the ❌ form with a distinct reason.
3. Builds the request body `{"project_id": <project_id>}` in Python (raw
   scalars only across the shell — JSON is never assembled in the shell) and
   POSTs it to `{mcp_base_url}/workflows/list` (the auth probe — this does NOT
   repoint to /status) with headers `Authorization: Bearer <api_key>` and
   `X-CMS-Project-Id: <project_id>`.
4. Branches on the response, mapping EVERY failure to the ❌ form with a
   DISTINCT reason:
   - **200** → require the body to be JSON carrying a `workflows` key. A
     malformed 200 → `❌ NOT connected — unexpected response shape from
     /workflows/list. No config was written.` On a well-formed 200, write
     `$CLAUDE_PROJECT_DIR/.claude/cms.config.json` (creating `.claude/` if
     missing) containing EXACTLY `{"project_id": ..., "api_key": ...}`,
     atomically (temp file `chmod 0600` then `os.replace`, which preserves the
     temp's mode; the temp is removed on any write failure so no secret-bearing
     `.tmp` is orphaned), then WRITE-THEN-VERIFY (below) before printing the ✅
     line.
   - **401** → reason `API key rejected (invalid key)`.
   - **403** → reason `key not authorized for project <project_id>`.
   - **404** → reason `project <project_id> not found`.
   - **503** → reason `ContextPlus service unavailable, try again`.
   - other non-2xx → reason `unexpected status <code>`.
   - transport / no HTTP response → reason `could not reach MCP at
     <mcp_base_url>`.

WRITE-THEN-VERIFY: after `os.replace(_tmp, _dest)`, the helper re-opens
`_dest`, `json.load`s it, and confirms BOTH `project_id` and `api_key` are
present AND non-empty. ONLY then does it print the ✅ line. If the re-read /
parse fails or a field is missing/empty, it best-effort `os.remove(_dest)`s the
partial file and prints `❌ NOT connected — wrote config but verification
failed (removed partial file). No config was written.`

The write target directory is resolved from `$CLAUDE_PROJECT_DIR` (fallback:
cwd) so the config lands in the user's actual project, never in the plugin
cache.

```bash
python3 -c '
import importlib.util, json, os, sys, urllib.error, urllib.request

# ONE-VERDICT CONTRACT: every exit path below prints EXACTLY ONE line —
# either the single ✅ success line or one ❌ failure line of the form
# "❌ NOT connected — <reason>. No config was written." Length-guard argv so a
# missing arg maps to the ❌ missing-arguments verdict (the literal root-cause
# bug fix) instead of raising IndexError before the guard can fire.
_project_id = sys.argv[1] if len(sys.argv) > 1 else ""
_api_key    = sys.argv[2] if len(sys.argv) > 2 else ""
if not (_project_id and _api_key):
    print("❌ NOT connected — missing arguments (need <project_id> <api_key>). No config was written.")
    sys.exit(0)

# Resolve mcp_base_url from the plugin tier (no project config required).
# Env-var-first path resolution: read CLAUDE_PLUGIN_ROOT from the environment,
# falling back to the harness-substituted literal. Works whether the harness
# exports the env var OR inline-substitutes the ${CLAUDE_PLUGIN_ROOT} token in
# this SKILL.md content. A load failure degrades to a ❌ verdict.
try:
    _root = os.environ.get("CLAUDE_PLUGIN_ROOT") or "${CLAUDE_PLUGIN_ROOT}"
    _spec = importlib.util.spec_from_file_location("cms_config", os.path.join(_root, "hooks", "cms_config.py"))
    _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
except Exception as exc:
    print(f"❌ NOT connected — could not load plugin config ({exc}). No config was written.")
    sys.exit(0)
_cfg, _err = _m.load_config()
if _cfg is None:
    print(f"❌ NOT connected — could not load plugin config ({_err}). No config was written.")
    sys.exit(0)
try:
    _base = str(_cfg["mcp_base_url"]).rstrip("/")
    if not _base:
        raise KeyError("mcp_base_url")
except Exception:
    print("❌ NOT connected — mcp_base_url missing from plugin config. No config was written.")
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
_resp_body = b""
try:
    with urllib.request.urlopen(_req, timeout=30) as r:
        _status = r.getcode()
        _resp_body = r.read()
except urllib.error.HTTPError as e:
    _status = e.code
except Exception:
    print(f"❌ NOT connected — could not reach MCP at {_base}. No config was written.")
    sys.exit(0)

if _status == 200:
    # FIX 7 — a bare HTTP 200 from /workflows/list is sufficient auth proof
    # (the MCP auth middleware returns 403 project_not_granted for a key not
    # scoped to the project). As cheap insurance, require the 200 body to be
    # JSON carrying a "workflows" key before writing. Lenient: only an
    # explicitly malformed 200 blocks the write.
    try:
        _parsed = json.loads(_resp_body) if _resp_body else None
    except Exception:
        _parsed = None
    if not (isinstance(_parsed, dict) and "workflows" in _parsed):
        print("❌ NOT connected — unexpected response shape from /workflows/list. No config was written.")
        sys.exit(0)
    _proj_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    _claude_dir = os.path.join(_proj_dir, ".claude")
    _dest = os.path.join(_claude_dir, "cms.config.json")
    _tmp = _dest + ".tmp"
    try:
        os.makedirs(_claude_dir, exist_ok=True)
        _payload = json.dumps({"project_id": _project_id, "api_key": _api_key}, indent=2) + "\n"
        with open(_tmp, "w", encoding="utf-8") as f:
            f.write(_payload)
        # chmod the secret-bearing temp BEFORE the replace. os.replace
        # preserves the temp inode + mode, so no post-replace chmod is
        # needed. On ANY failure, remove the temp so no 0600 .tmp holding
        # the api_key is left orphaned.
        os.chmod(_tmp, 0o600)
        os.replace(_tmp, _dest)
    except Exception as exc:
        try:
            os.remove(_tmp)
        except OSError:
            pass
        print(f"❌ NOT connected — failed to write {_dest} ({exc}). No config was written.")
        sys.exit(0)
    # WRITE-THEN-VERIFY: re-open the destination we just wrote, parse it, and
    # confirm BOTH secret fields are present AND non-empty before declaring
    # success. A truncated write, a filesystem that silently dropped bytes, or
    # any corruption would otherwise leave a broken config that every later
    # hook reads as "connected" while the api_key is unusable. On any
    # verify failure, best-effort remove the partial file so no half-written
    # secret config is left in place, and emit the ❌ verdict.
    try:
        with open(_dest, encoding="utf-8") as f:
            _verify = json.load(f)
        if not (isinstance(_verify, dict)
                and _verify.get("project_id") and _verify.get("api_key")):
            raise ValueError("missing or empty project_id/api_key")
    except Exception:
        try:
            os.remove(_dest)
        except OSError:
            pass
        print("❌ NOT connected — wrote config but verification failed (removed partial file). No config was written.")
        sys.exit(0)
    print(f"✅ Connected — wrote {_dest}. Run /reload-plugins.")
elif _status == 401:
    print("❌ NOT connected — API key rejected (invalid key). No config was written.")
elif _status == 403:
    print(f"❌ NOT connected — key not authorized for project {_project_id}. No config was written.")
elif _status == 404:
    print(f"❌ NOT connected — project {_project_id} not found. No config was written.")
elif _status == 503:
    print("❌ NOT connected — ContextPlus service unavailable, try again. No config was written.")
else:
    print(f"❌ NOT connected — unexpected status {_status}. No config was written.")
' "$0" "$1"
```

## Hard rules

- ONE-VERDICT CONTRACT: the helper ALWAYS terminates with EXACTLY ONE verdict
  line — the single ✅ success line `✅ Connected — wrote <abs-path>. Run
  /reload-plugins.` OR one ❌ line `❌ NOT connected — <reason>. No config was
  written.` Every failure branch (missing args, 401, 403, 404, 503, other
  non-2xx, transport failure, malformed 200, write failure, verify failure)
  maps to a ❌ line with a DISTINCT reason. There is no soft / ambiguous
  outcome.
- The orchestrator MUST surface the verdict line to the user VERBATIM.
- Pass `project_id` and `api_key` to the helper as raw scalar shell arguments
  (`$0`, `$1`). NEVER assemble the request JSON in the shell — build it in
  Python.
- The auth probe STAYS on `/workflows/list` — do NOT repoint it to `/status`.
- Write the project config ONLY on a clean, well-formed 200 (body parses as
  JSON and carries a `workflows` key). Every other status (and any transport
  failure, and a malformed 200) emits its ❌ verdict and writes NOTHING.
- The write is atomic: write a temp file, `chmod 0600` the temp, then
  `os.replace` it onto the destination (the replace preserves the temp's mode,
  so no redundant post-replace chmod). On any write failure the temp is removed
  so no secret-bearing `.tmp` is left behind.
- WRITE-THEN-VERIFY: after `os.replace`, re-open + `json.load` the destination
  and confirm BOTH `project_id` and `api_key` are present and non-empty before
  printing ✅. On any verify failure, best-effort `os.remove` the partial file
  and emit the verify-failure ❌ verdict.
- Resolve the write location from `$CLAUDE_PROJECT_DIR` (fallback cwd). Never
  write into the plugin cache.
- `mcp_base_url` comes from the shared merged loader's plugin tier — this skill
  must work BEFORE the project config exists, so it must NOT require the
  project tier.
