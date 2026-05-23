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
8. `pcb_sync_place_and_report`
9. `pcb_route_between_pads` or `pcb_route_ratsnest_connection`
10. `run_drc_check`
11. `project_design_state`

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

By default, `KICAD_MCP_TOOL_PROFILE=agent` exposes only the intent-first tools needed for normal design work. Low-level coordinate and raw KiCad geometry tools are hidden from the normal LLM tool list. The default profile still includes the high-level PCB workflow tools for sync/place/report and pad-based routing.

Use this only for manual recovery, debugging, or library exploration:

```text
KICAD_MCP_TOOL_PROFILE=advanced
```

`debug` is also accepted and exposes the same full tool surface. In advanced/debug mode, legacy builders, raw schematic geometry tools, pin-map diagnostics, full library listing, explicit PCB primitives, export helpers, and netlist-analysis tools are registered again.
