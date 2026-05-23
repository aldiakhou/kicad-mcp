# Agent Workflow

This server is intended to be agent-first: declare electrical intent, let the MCP layer resolve KiCad geometry, then verify with KiCad-native checks.

Recommended order:

1. `project_design_state`
2. `create_kicad_project`, if needed
3. `schematic_preview_build_from_spec_v2`
4. `schematic_build_from_spec_v2`
5. `schematic_apply_connection_plan`, only for incremental edits
6. `schematic_quality_report`
7. `run_erc_check`
8. `project_design_state`

For normal design tasks, do not use `schematic_add_wire`, `schematic_connect_points`, or raw coordinate-based PCB routing unless the intent-based tools cannot represent the edit.

Use this schematic connection shape for incremental work:

```json
{
  "type": "pin_to_pin",
  "from": {"ref": "U1", "pin": "OUT"},
  "to": {"ref": "R3", "pin": "1"},
  "net": "LED_A"
}
```

Use this v2 build shape for complete circuits:

```json
{
  "name": "555_led_blinker",
  "paper": "A4",
  "parts": [
    {
      "ref": "U1",
      "symbol": "Timer:LM555xN",
      "value": "LM555",
      "footprint": "Package_DIP:DIP-8_W7.62mm"
    }
  ],
  "nets": {
    "+5V": [["U1", "VCC"]],
    "GND": [["U1", "GND"]]
  },
  "no_connects": [["U1", "CV"]]
}
```

The MCP layer resolves symbols, resolves pins, snaps generated geometry to the schematic grid, writes KiCad S-expressions, exports the native netlist, runs ERC when requested, and rolls back failed connection transactions.

## Tool Profiles

By default, `KICAD_MCP_TOOL_PROFILE=agent` exposes only the intent-first schematic/project tools needed for normal design work. Low-level coordinate tools, v1 builders, compatibility aliases, full library listing, PCB primitives, export helpers, and analysis tools are hidden from the normal LLM tool list.

Use this for manual schematic edits or library exploration:

```text
KICAD_MCP_TOOL_PROFILE=advanced
```

Use this for raw schematic geometry, v1 builder compatibility, and pin-map diagnostics:

```text
KICAD_MCP_TOOL_PROFILE=debug
```

Use this only for broad regression testing or non-agent clients that need every registered tool:

```text
KICAD_MCP_TOOL_PROFILE=all
```
