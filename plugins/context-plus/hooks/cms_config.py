#!/usr/bin/env python3
"""Shared stdlib loader for the Context+ TWO-TIER plugin/project config.

This loader assembles the effective Context+ configuration by merging two
tiers:

* PLUGIN tier (non-secret defaults shipped with the plugin):
  ``<plugin-root>/config/cms.plugin.json`` — ``mcp_base_url``,
  ``tds_base_url``, ``timeouts.*`` and the
  ``post_turn_triage.fire_on_subagent_stop`` toggle. The plugin root is
  resolved from ``$CLAUDE_PLUGIN_ROOT`` with a ``__file__``-relative
  fallback (``parent.parent / "config" / "cms.plugin.json"``).

* PROJECT tier (per-project, includes secrets the plugin must NEVER ship):
  ``<project>/.claude/cms.config.json`` — ``project_id``, ``api_key`` and
  any per-project overrides. The project root is resolved from
  ``$CLAUDE_PROJECT_DIR`` with a cwd-upward search fallback. The project
  tier is NEVER resolved via ``__file__`` — under a plugin install
  ``__file__`` points into the plugin cache, not the user's project.

Merge semantics: start from ``{}``, merge the plugin dict, then merge the
project dict OVER it so project keys win per top-level key. The nested
``timeouts`` and ``post_turn_triage`` blocks are shallow-merged ONLY when
BOTH tiers supply a dict for that key (isinstance guard on BOTH sides);
otherwise the overriding tier replaces the value wholesale, and if only one
tier supplies the key it is used as-is.

NEVER-RAISE CONTRACT (M-1, binding): this module must not raise on a
missing/malformed config in either tier, on a failed ``$CLAUDE_PLUGIN_ROOT``
/ ``$CLAUDE_PROJECT_DIR`` / ``__file__`` resolution, or on the merge itself.
Every file read, every environment/``__file__`` resolution, and the merge are
individually wrapped in try/except, and an OUTERMOST try/except around the
whole two-tier assembly returns ``(None, reason)`` on any unexpected error.
A missing/unreadable EITHER file is NOT fatal on its own — only an empty
merged result ``{}`` yields ``(None, reason)``; otherwise ``(merged, None)``.

``load_config()`` returns a ``(config_dict_or_None, error_reason_or_None)``
tuple. On success the second element is ``None``; on failure the first is
``None`` and the second is a short human-readable reason. Callers decide how
to degrade — this module never makes that decision and never crashes the
caller's turn.

stdlib only — no third-party dependency.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _read_json_file(path: Path) -> tuple[dict | None, str | None]:
    """Read a JSON object file. NEVER raises.

    Returns ``(dict, None)`` on success, or ``(None, reason)`` when the file
    is missing/unreadable/not-an-object. A ``(None, reason)`` result is not
    fatal on its own — the caller treats a missing tier as an empty tier.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, f"config not found at {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"config unreadable at {path}: {exc}"
    except Exception as exc:  # noqa: BLE001 — never-raise contract (M-1).
        return None, f"config load failed at {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"config at {path} is not a JSON object"
    return data, None


def _resolve_plugin_config_path() -> Path | None:
    """Resolve the plugin-tier config path. NEVER raises.

    ``$CLAUDE_PLUGIN_ROOT`` -> ``<root>/config/cms.plugin.json``, with a
    ``__file__``-relative fallback (``parent.parent/config/cms.plugin.json``).
    """
    try:
        root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if root:
            return Path(root) / "config" / "cms.plugin.json"
    except Exception:  # noqa: BLE001 — never-raise contract (M-1).
        pass
    try:
        return Path(__file__).resolve().parent.parent / "config" / "cms.plugin.json"
    except Exception:  # noqa: BLE001 — never-raise contract (M-1).
        return None


def _resolve_project_config_path() -> Path | None:
    """Resolve the project-tier config path. NEVER raises.

    ``$CLAUDE_PROJECT_DIR`` -> ``<dir>/.claude/cms.config.json``, with a
    cwd-upward search fallback. NEVER uses ``__file__`` — under a plugin
    install that points into the plugin cache, not the user's project.
    """
    try:
        proj = os.environ.get("CLAUDE_PROJECT_DIR")
        if proj:
            return Path(proj) / ".claude" / "cms.config.json"
    except Exception:  # noqa: BLE001 — never-raise contract (M-1).
        pass
    try:
        d = Path(os.getcwd()).resolve()
        while True:
            candidate = d / ".claude" / "cms.config.json"
            if candidate.is_file():
                return candidate
            if d.parent == d:
                return None
            d = d.parent
    except Exception:  # noqa: BLE001 — never-raise contract (M-1).
        return None


def _merge(plugin: dict, project: dict) -> dict:
    """Merge project OVER plugin per top-level key. NEVER raises.

    The nested ``timeouts`` and ``post_turn_triage`` blocks are
    shallow-merged ONLY when BOTH tiers supply a dict for that key;
    otherwise the project value replaces the plugin value wholesale, and if
    only one tier supplies the key it is used as-is.
    """
    merged: dict = {}
    merged.update(plugin)
    for key, proj_val in project.items():
        if key in ("timeouts", "post_turn_triage"):
            plugin_val = merged.get(key)
            if isinstance(plugin_val, dict) and isinstance(proj_val, dict):
                shallow = {}
                shallow.update(plugin_val)
                shallow.update(proj_val)
                merged[key] = shallow
                continue
        merged[key] = proj_val
    return merged


def load_config() -> tuple[dict | None, str | None]:
    """Load and merge the plugin + project config tiers.

    Returns ``(merged, None)`` on success or ``(None, reason)`` when the
    merged result is empty ``{}`` (or on any unexpected failure). NEVER
    raises — see the module docstring's never-raise contract (M-1).
    """
    try:
        plugin_cfg: dict = {}
        project_cfg: dict = {}
        reasons: list[str] = []

        plugin_path = _resolve_plugin_config_path()
        if plugin_path is not None:
            data, reason = _read_json_file(plugin_path)
            if data is not None:
                plugin_cfg = data
            elif reason:
                reasons.append(reason)
        else:
            reasons.append("could not resolve plugin config path")

        project_path = _resolve_project_config_path()
        if project_path is not None:
            data, reason = _read_json_file(project_path)
            if data is not None:
                project_cfg = data
            elif reason:
                reasons.append(reason)
        else:
            reasons.append("could not resolve project config path")

        merged = _merge(plugin_cfg, project_cfg)

        if not merged:
            reason = "; ".join(reasons) if reasons else "merged config is empty"
            return None, reason
        return merged, None
    except Exception as exc:  # noqa: BLE001 — never-raise contract (M-1).
        return None, f"config assembly failed: {exc}"
