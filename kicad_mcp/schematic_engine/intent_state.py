"""Design-intent state persistence and merge support."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

CONTROL_KEYS = {"action", "mode"}
MERGE_ACTIONS = {"merge", "add", "update", "patch"}
REPLACE_ACTIONS = {"replace", "create", ""}


def prepare_intent_for_action(project_path: str, intent: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return the effective intent for replace or merge-style actions."""
    raw_action = str(intent.get("action") or intent.get("mode") or "replace").strip().lower()
    action = "merge" if raw_action in MERGE_ACTIONS else "replace"
    if raw_action not in MERGE_ACTIONS and raw_action not in REPLACE_ACTIONS:
        raise ValueError(f"Unsupported design intent action '{raw_action}'")

    patch = _strip_control_keys(intent)
    if action == "replace":
        return patch, action

    base = load_saved_intent(project_path) or {}
    return merge_intents(base, patch), action


def load_saved_intent(project_path: str) -> dict[str, Any] | None:
    """Load the last successfully committed design intent for a project."""
    path = intent_state_path(project_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    intent = data.get("intent") if isinstance(data, dict) else None
    return intent if isinstance(intent, dict) else None


def save_committed_intent(project_path: str, intent: dict[str, Any], *, action: str) -> str:
    """Persist the effective intent after a successful schematic commit."""
    path = intent_state_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "kicad_mcp.design_intent_state.v1",
        "action": action,
        "intent": _strip_control_keys(intent),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)
    return str(path)


def intent_state_path(project_path: str) -> Path:
    project_dir = Path(project_path).resolve().parent
    return project_dir / ".kicad_mcp" / "design_intent.current.json"


def merge_intents(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch intent into a previously committed full intent."""
    merged = deepcopy(base)
    patch_clean = _strip_control_keys(patch)

    for key, value in patch_clean.items():
        if key == "parts":
            merged[key] = _merge_by_key(merged.get(key, []), value, "ref")
        elif key in {"pin_rules", "bulk_connections", "no_connect_rules", "interfaces"}:
            merged[key] = _append_unique(merged.get(key, []), value)
        elif key == "support_circuits":
            merged[key] = _merge_grouped_or_list(merged.get(key, []), value)
        elif key == "rails":
            merged[key] = _merge_rails(merged.get(key, []), value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _strip_control_keys(intent: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in intent.items() if key not in CONTROL_KEYS}


def _merge_by_key(base: Any, patch: Any, key: str) -> list[Any]:
    base_items = base if isinstance(base, list) else []
    patch_items = patch if isinstance(patch, list) else []
    merged: list[Any] = []
    indexes: dict[str, int] = {}
    for item in base_items:
        if not isinstance(item, dict) or key not in item:
            merged.append(deepcopy(item))
            continue
        indexes[str(item[key])] = len(merged)
        merged.append(deepcopy(item))
    for item in patch_items:
        if not isinstance(item, dict) or key not in item:
            merged.append(deepcopy(item))
            continue
        item_key = str(item[key])
        if item_key in indexes and isinstance(merged[indexes[item_key]], dict):
            merged[indexes[item_key]] = _deep_merge_dicts(merged[indexes[item_key]], item)
        else:
            indexes[item_key] = len(merged)
            merged.append(deepcopy(item))
    return merged


def _append_unique(base: Any, patch: Any) -> list[Any]:
    items = []
    seen: set[str] = set()
    for source in (base, patch):
        if not isinstance(source, list):
            continue
        for item in source:
            marker = _stable_marker(item)
            if marker in seen:
                continue
            seen.add(marker)
            items.append(deepcopy(item))
    return items


def _merge_grouped_or_list(base: Any, patch: Any) -> Any:
    return _append_unique(_flatten_grouped(base), _flatten_grouped(patch))


def _merge_rails(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) or isinstance(patch, dict):
        result: dict[str, Any] = deepcopy(base) if isinstance(base, dict) else _rails_list_to_dict(base)
        patch_dict = deepcopy(patch) if isinstance(patch, dict) else _rails_list_to_dict(patch)
        for rail_name, rail_spec in patch_dict.items():
            if rail_name in result and isinstance(result[rail_name], dict) and isinstance(rail_spec, dict):
                result[rail_name] = _deep_merge_dicts(result[rail_name], rail_spec)
            else:
                result[rail_name] = deepcopy(rail_spec)
        return result
    return _append_unique(base, patch)


def _rails_list_to_dict(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("net") or item.get("name")
        if name:
            result[str(name)] = {k: deepcopy(v) for k, v in item.items() if k not in {"net", "name"}}
    return result


def _flatten_grouped(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    result = []
    for group, group_value in value.items():
        items = group_value if isinstance(group_value, list) else [group_value]
        for item in items:
            if isinstance(item, dict):
                item_copy = deepcopy(item)
                item_copy.setdefault("type", group)
                result.append(item_copy)
            else:
                result.append(item)
    return result


def _deep_merge_dicts(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _append_unique(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _stable_marker(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return repr(value)
