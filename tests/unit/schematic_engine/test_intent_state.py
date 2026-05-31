from pathlib import Path

from kicad_mcp.schematic_engine.intent_state import (
    load_saved_intent,
    merge_intents,
    prepare_intent_for_action,
    save_committed_intent,
)


def test_merge_intents_updates_parts_and_appends_rules():
    base = {
        "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "old"}],
        "bulk_connections": [{"net": "A", "pins": [["U1", "1"]]}],
    }
    patch = {
        "parts": [
            {"ref": "U1", "value": "new"},
            {"ref": "C1", "lib_id": "Device:C", "value": "100n"},
        ],
        "bulk_connections": [{"net": "B", "pins": [["C1", "1"]]}],
    }

    merged = merge_intents(base, patch)

    assert merged["parts"][0]["ref"] == "U1"
    assert merged["parts"][0]["value"] == "new"
    assert merged["parts"][1]["ref"] == "C1"
    assert len(merged["bulk_connections"]) == 2


def test_prepare_merge_uses_saved_committed_intent(tmp_path: Path):
    project_path = tmp_path / "demo.kicad_pro"
    project_path.write_text("{}", encoding="utf-8")
    save_committed_intent(
        str(project_path),
        {"parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}]},
        action="replace",
    )

    effective, action = prepare_intent_for_action(
        str(project_path),
        {
            "action": "merge",
            "parts": [{"ref": "C1", "lib_id": "Device:C", "value": "100n"}],
        },
    )

    assert action == "merge"
    assert [part["ref"] for part in effective["parts"]] == ["U1", "C1"]
    assert load_saved_intent(str(project_path))["parts"][0]["ref"] == "U1"
