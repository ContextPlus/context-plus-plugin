---
name: status
description: Doctor for the Context-Plus connection — checks local project config, live authentication against the MCP /status endpoint, and whether the installed plugin version is up to date. Invocation - /context-plus:status
allowed-tools: Bash
---

You are the Context-Plus status (doctor) skill. You run a read-only,
side-effect-free health check and print a 3-section verdict. You NEVER write
any config and NEVER mutate anything — this is purely diagnostic.

## Project context (from config)

`mcp_base_url` comes from the shared merged loader
(`${CLAUDE_PLUGIN_ROOT}/hooks/cms_config.py`); the project-tier secrets
(`project_id`, `api_key`) come from the same merged dict. Unlike cms-connect,
this skill DOES need the project tier — the connection check can't run without
the project's `project_id` + `api_key`. When the project config is missing or
invalid, Section 1 reports that and Sections 2 & 3 are SKIPPED.

## Behavior

Run the one-shot helper below via Bash. It prints exactly three labelled
sections and ALWAYS exits 0 — every branch prints a line, never a traceback.

- **SECTION 1 — config**: load the merged config. Report whether the project
  config is present, parses, and carries non-empty `project_id` + `api_key`. If
  the config is missing/invalid (or the loader fails, or `mcp_base_url` is
  absent), print the config-invalid line and mark sections 2 & 3 as
  `skipped — fix config first`.
- **SECTION 2 — connection**: POST `{mcp_base_url}/status` with body
  `{"project_id": <pid>}` and headers `Authorization: Bearer <api_key>` +
  `X-CMS-Project-Id: <pid>`. Branch with DISTINCT lines: 200 → `authenticated +
  project authorized`; 401 → `API key rejected`; 403 → `key not authorized for
  project`; 404 → `project not found`; 503 → `service unavailable`; transport /
  no response → `could not reach MCP at <base>`.
- **SECTION 3 — version**: read the INSTALLED version from
  `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` (`version` field) and
  compare to the 200 body's `latest_plugin_version`. If section 2 was not 200,
  or the body lacks the field, or `latest_plugin_version == "0.0.0"` (the
  unconfigured sentinel) → `version check unavailable`. Else if installed ==
  latest → `up to date (v<X>)`. Else → `behind — installed v<X>, latest v<Y>.
  Update: claude plugin update context-plus@context-plus (restart required).`

The orchestrator should surface all three section lines to the user.

```bash
python3 -c '
import importlib.util, json, os, sys, urllib.error, urllib.request

def _load_loader():
    _root = os.environ.get("CLAUDE_PLUGIN_ROOT") or "${CLAUDE_PLUGIN_ROOT}"
    _spec = importlib.util.spec_from_file_location(
        "cms_config", os.path.join(_root, "hooks", "cms_config.py")
    )
    _m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    return _m

# ----- SECTION 1: config -------------------------------------------------
_cfg = None
_base = ""
_pid = ""
_key = ""
_section1_ok = False
try:
    _m = _load_loader()
    _cfg, _err = _m.load_config()
except Exception as exc:
    _cfg, _err = None, str(exc)

if _cfg is None:
    print(f"[1/3] config: INVALID — could not load config ({_err}).")
else:
    _base = str(_cfg.get("mcp_base_url") or "").rstrip("/")
    _pid = str(_cfg.get("project_id") or "")
    _key = str(_cfg.get("api_key") or "")
    if not _base:
        print("[1/3] config: INVALID — mcp_base_url missing from plugin config.")
    elif not (_pid and _key):
        print("[1/3] config: NOT CONNECTED — project config missing or empty "
              "project_id/api_key (run /context-plus:cms-connect).")
    else:
        _section1_ok = True
        print(f"[1/3] config: OK — present, parses, project_id + api_key non-empty (mcp_base_url={_base}).")

if not _section1_ok:
    print("[2/3] connection: skipped — fix config first.")
    print("[3/3] version: skipped — fix config first.")
    sys.exit(0)

# ----- SECTION 2: connection --------------------------------------------
_url = _base + "/status"
_body = json.dumps({"project_id": _pid}).encode("utf-8")
_req = urllib.request.Request(
    _url,
    data=_body,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_key}",
        "X-CMS-Project-Id": _pid,
    },
    method="POST",
)
_status = None
_resp_body = b""
_transport_err = False
try:
    with urllib.request.urlopen(_req, timeout=30) as r:
        _status = r.getcode()
        _resp_body = r.read()
except urllib.error.HTTPError as e:
    _status = e.code
    try:
        _resp_body = e.read()
    except Exception:
        _resp_body = b""
except Exception:
    _transport_err = True

if _transport_err:
    print(f"[2/3] connection: FAIL — could not reach MCP at {_base}.")
elif _status == 200:
    print("[2/3] connection: OK — authenticated + project authorized.")
elif _status == 401:
    print("[2/3] connection: FAIL — API key rejected.")
elif _status == 403:
    print("[2/3] connection: FAIL — key not authorized for project.")
elif _status == 404:
    print("[2/3] connection: FAIL — project not found.")
elif _status == 503:
    print("[2/3] connection: FAIL — service unavailable.")
else:
    print(f"[2/3] connection: FAIL — unexpected status {_status}.")

# ----- SECTION 3: version ------------------------------------------------
_installed = None
try:
    _proot = os.environ.get("CLAUDE_PLUGIN_ROOT") or "${CLAUDE_PLUGIN_ROOT}"
    with open(os.path.join(_proot, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        _installed = json.load(f).get("version")
except Exception:
    _installed = None

_latest = None
if _status == 200:
    try:
        _parsed = json.loads(_resp_body) if _resp_body else None
        if isinstance(_parsed, dict):
            _latest = _parsed.get("latest_plugin_version")
    except Exception:
        _latest = None

# 0.0.0 is the server-side UNCONFIGURED sentinel — treat as "no advertised
# version" so we never claim the user is behind/ahead against a placeholder.
if _status != 200 or not _latest or _latest == "0.0.0" or not _installed:
    print("[3/3] version: version check unavailable.")
elif _installed == _latest:
    print(f"[3/3] version: up to date (v{_installed}).")
else:
    print(f"[3/3] version: behind — installed v{_installed}, latest v{_latest}. "
          "Update: claude plugin update context-plus@context-plus (restart required).")

sys.exit(0)
'
```

## Hard rules

- READ-ONLY: this skill NEVER writes config or mutates anything. It is purely
  diagnostic.
- ALWAYS print all three labelled sections and ALWAYS exit 0 — every branch
  prints a line, never a traceback.
- Section 1 gates sections 2 & 3: if the project config is missing/invalid (no
  non-empty `project_id` + `api_key`, or `mcp_base_url` absent), print the
  config line and mark sections 2 & 3 `skipped — fix config first` (no network
  call).
- Build the request JSON in Python (raw scalars across the shell). POST to
  `{mcp_base_url}/status` with `Authorization: Bearer <api_key>` +
  `X-CMS-Project-Id: <project_id>`.
- Read the INSTALLED version from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/
  plugin.json`. A `latest_plugin_version` of `0.0.0` (unconfigured sentinel),
  a non-200 section 2, a missing field, or a missing installed version all
  resolve to `version check unavailable`.
