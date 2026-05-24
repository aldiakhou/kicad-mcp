# Agent Workflow

This server is intended to be agent-first: declare electrical intent, let the MCP layer resolve KiCad geometry, then verify with KiCad-native checks.

Recommended order:

1. `project_design_state`
2. `find_symbols` / `find_footprints` for unknown library IDs
3. `schematic_preview_design_intent`, when you want to inspect expansion first
4. `schematic_apply_design_intent`
5. `schematic_build_from_spec_v2`, only when you already have explicit parts/nets
6. `schematic_apply_connection_plan`, only for incremental edits
7. `schematic_quality_report`
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

Use this bulk design-intent shape for normal complete circuits:

```json
{
  "parts": [
    {
      "ref": "U1",
      "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx",
      "value": "STM32F103C8T6",
      "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    }
  ],
  "pin_rules": [
    {"ref": "U1", "match": {"name_regex": "VDD|VDDA|VBAT"}, "net": "+3V3"},
    {"ref": "U1", "match": {"name_regex": "VSS|VSSA|GND"}, "net": "GND"}
  ],
  "interfaces": [
    {
      "type": "i2c",
      "name": "SENSOR_I2C",
      "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
      "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
      "pullups": {"rail": "+3V3", "value": "4.7k"}
    }
  ],
  "support_circuits": [
    {"type": "decoupling", "target": "U1", "rail": "+3V3", "ground": "GND", "capacitors": ["100n", "4.7u"]}
  ],
  "no_connect_rules": [
    {"ref": "U1", "match": {"name_regex": "PA[0-9]+|PB[0-9]+"}, "except": ["PB6", "PB7"], "action": "mark_no_connect"}
  ]
}
```

`schematic_apply_design_intent` compiles this into v2 `parts`, generated passives/connectors, expanded net memberships, and no-connect markers. It always saves the normalized intent, expanded spec, and report under `.kicad_mcp/`.

Use this lower-level v2 build shape when every connection is already explicit:

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
    "GND": [{"ref": "U1", "pin": "GND"}]
  },
  "no_connects": [["U1", "CV"]]
}
```

In v2 specs, `symbol` and `lib_id` are both accepted for parts, but the value must be a full KiCad library ID such as `Device:R`, not a symbol-unit name such as `R_1_1`. Net endpoints should use `["U1", "1"]` or `{"ref": "U1", "pin": "1"}`. Legacy string shorthand like `U1_1` is accepted with `rsplit("_", 1)` but is not preferred.

Missing library parts can be declared as `custom_parts` with explicit pins:

```json
{
  "custom_parts": [
    {
      "ref": "U6",
      "value": "DPS310",
      "footprint": "Package_LGA:LGA-8_2.0x2.5mm_P0.65mm",
      "pins": [
        {"number": "1", "name": "SCL", "type": "bidirectional"},
        {"number": "2", "name": "SDA", "type": "bidirectional"},
        {"number": "3", "name": "GND", "type": "power_in"}
      ]
    }
  ]
}
```

The MCP layer resolves symbols, resolves pins, snaps generated geometry to the schematic grid, writes KiCad S-expressions, exports the native netlist, runs ERC when requested, and rolls back failed connection transactions.

## Tool Profiles

By default, `KICAD_MCP_TOOL_PROFILE=agent` exposes only `schematic_apply_design_intent`, `schematic_preview_design_intent`, `schematic_build_from_spec_v2`, `schematic_apply_connection_plan`, `schematic_quality_report`, `find_symbols`, `find_footprints`, and `project_design_state`. Low-level coordinate tools, v1 builders, compatibility aliases, full library listing, PCB primitives, export helpers, and analysis tools are hidden from the normal LLM tool list.

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
