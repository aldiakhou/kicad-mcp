# Agent Workflow

This server is agent-first: declare electrical intent, let the MCP layer build a canonical circuit, then verify the generated KiCad schematic before anything is committed.

## Schematic Generation

Use one schematic-generation path:

```text
design intent
  -> SKiDL canonical circuit / expected netlist
  -> KiUtils / kicad-skip schematic writer
  -> KiCad CLI netlist + ERC verification
  -> commit or rollback
```

The public schematic tools are:

- `schematic_engine_status`
- `schematic_design_intent_schema`
- `schematic_preview_design_intent`
- `schematic_apply_design_intent`
- `schematic_validate_generated_schematic`
- `export_schematic_preview`
- `export_schematic_svg`

Symbol and footprint discovery tools remain public:

- `find_symbols`
- `resolve_symbol`
- `resolve_symbols`
- `find_footprints`
- `resolve_footprint`
- `resolve_footprints`

`schematic_apply_design_intent` always uses the required SKiDL, KiUtils, kicad-skip, and KiCad CLI verification path. It does not expose engine mode, unsafe apply, partial write, or validation-level parameters.

## Recommended Order

1. `create_kicad_project`
2. `schematic_engine_status`
3. `find_symbols` / `resolve_symbols`
4. `find_footprints` / `resolve_footprints`
5. `schematic_preview_design_intent`
6. `schematic_apply_design_intent`
7. `schematic_validate_generated_schematic`
8. `export_schematic_preview`

The apply tool writes in a temporary worktree first. If KiCad CLI export, ERC, or netlist comparison fails, the live project is not changed.

## Design Intent Shape

Use full KiCad symbol IDs and explicit connection intent:

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
    {"ref": "U1", "match": {"name": "VDD"}, "net": "+3V3"},
    {"ref": "U1", "match": {"name_regex": "^(VSS|VSSA|GND)$"}, "net": "GND"}
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
    {
      "type": "decoupling",
      "target": "U1",
      "rail": "+3V3",
      "ground": "GND",
      "capacitors": ["100n", "4.7u"]
    }
  ],
  "no_connect_rules": [
    {
      "ref": "U1",
      "match": {"name_regex": "PA[0-9]+|PB[0-9]+"},
      "except": ["PB6", "PB7"],
      "action": "mark_no_connect"
    }
  ]
}
```

Pin selectors support exact `name`, exact `number`, and anchored regex selectors such as `{"name_regex": "^VDD$"}`. Support-circuit aliases are accepted for common intent output, including decoupling, crystal, ferrite, pullup, pulldown, reset button, and LED circuits.

Generated support parts use normal unique references such as `C1`, `C2`, `Y1`, `R1`, `FB1`, and `SW1`.

## Tool Profiles

The default `KICAD_MCP_TOOL_PROFILE=agent` exposes only the agent workflow tools, project discovery/state tools, validation, preview/export, and symbol/footprint lookup.

`advanced` adds project creation details, library listing, and PCB sync/placement/report tools.

`debug` adds read-only schematic inspection plus explicit PCB primitives. It does not expose alternate schematic generation engines.

`all` means the union of the curated agent, advanced, and debug profiles. It does not expose retired schematic generation or connection paths.
