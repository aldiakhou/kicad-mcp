# Agent Workflow

This server is intended to be agent-first: declare electrical intent, let the MCP layer resolve KiCad geometry, then verify with KiCad-native checks.

Recommended order:

1. `project_design_state`
2. `find_symbols` / `find_footprints` for unknown library IDs
3. `schematic_preview_design_intent`, when you want to inspect expansion first
4. `schematic_apply_expanded_spec`, when a preview already produced an expanded spec artifact
5. `schematic_apply_design_intent`
6. `export_schematic_preview` / `export_schematic_svg`, when visual feedback is needed
7. `schematic_quality_report`
8. `project_design_state`
9. `schematic_build_from_spec_v2`, when you have explicit parts/nets or a full design-intent payload
10. `schematic_apply_functional_layout`, when an existing schematic needs readable placement
11. `schematic_apply_connection_plan` or the simple `schematic_connect_*` wrappers, only for incremental edits

For large schematics, `schematic_apply_design_intent` now chooses a staged internal apply when the expanded design is above the direct-apply threshold. It places parts first, applies connection batches, then runs requested validation. Very large direct requests can return a background `job_id`; poll with `schematic_get_job_status` and finish with `schematic_get_job_result`.

For a direct but faster apply, use `schematic_apply_design_intent` with `quick_apply=true`, or set `include_preview=false`, `run_quality_report=false`, and `run_native_validation=false`. Run `export_schematic_preview` and `schematic_quality_report` as follow-up tools when needed. Transactional KiCad CLI validation stays enabled by default; disable it only for agent-controlled staging with `unsafe_fast_apply=true`, which sets `run_cli_validation=false`. Use `schematic_start_design_intent_job` / `schematic_get_job_status` / `schematic_get_job_result` when a single long operation is still required and the client may time out.

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
    {"ref": "U1", "match": {"name": "VDD"}, "net": "+3V3"},
    {"ref": "U1", "match": {"name_regex": "^(VSS|VSSA|GND)$"}, "net": "GND", "allow_hidden_power": true}
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
  "layout_hints": {
    "paper_strategy": "auto",
    "max_paper": "A1",
    "visual_gate": "strict"
  },
  "no_connect_rules": [
    {"ref": "U1", "match": {"name_regex": "PA[0-9]+|PB[0-9]+"}, "except": ["PB6", "PB7"], "action": "mark_no_connect"}
  ]
}
```

Pin selectors support exact `name` and `number` aliases. Regex selectors use substring matching, so anchor regexes when you need exact matches, for example `{"name_regex": "^VDD$"}`. Hidden power pins on power or ground nets are auto-authorized during design-intent compile; for intentional nonstandard hidden power wiring, pass `allow_hidden_power=true` on the pin rule or at the intent top level.

`bulk_connections` in design intent should normally use `{"net": "...", "pins": [[ref, pin], ...]}`. The compiler also accepts `type=pin_to_net` and `type=pin_to_pin` as forgiving aliases, but `schematic_apply_connection_plan` is the preferred tool for incremental connection-plan entries.

Support-circuit aliases are accepted for common agent output: `crystal` can use `xin`/`xout`, `ferrite_filter` can use `rail`/`supply_rail`, and `pullup` can use `target` or `ref` plus `pin` to connect the target pin before adding the resistor.

For readable incremental wiring, `schematic_apply_connection_plan` accepts `connection_style` per connection: `label`, `wire`, or `auto`. `auto` uses power symbols for known rails, routes simple two-endpoint signal nets as wires, and falls back to labels for multi-drop or unsafe routes. For fragile edits, it also accepts `verify=false`, `verify_native_netlist=false`, `run_erc=false`, and `rollback_on_failure=false`; run `schematic_quality_report` after layout is stable.

For incremental support circuitry on an existing schematic, use `schematic_add_support_circuits` with the same `support_circuits` entries as design intent, or the convenience tools `schematic_add_decoupling_capacitor`, `schematic_add_pullup_resistor`, and `schematic_add_passive`. Use `schematic_apply_no_connect_rules` to apply the same regex-based `no_connect_rules` format without rebuilding a full intent.

`no_connect_rules` can exclude pins either inside the selector or at the rule level:

```json
{"ref": "U1", "match": {"name_regex": "PA[0-9]+|PB[0-9]+", "exclude": {"names": ["PA13", "PA14"]}}}
{"ref": "U1", "match": {"name_regex": "PA[0-9]+|PB[0-9]+"}, "except": ["PA13", "PA14"]}
```

`schematic_apply_design_intent` compiles this into v2 `parts`, generated passives/connectors, expanded net memberships, and no-connect markers. `schematic_build_from_spec_v2` also accepts this full intent shape and compiles it before building, so support circuits and no-connect rules are not silently dropped. Both paths report exact `symbol_errors`, `footprint_errors`, and `normalization_errors` when preflight fails, and save artifacts under `.kicad_mcp/`.

By default it also applies a generic visual layout pass before writing the schematic. The visual pass assigns explicit symbol positions using estimated symbol bounds, groups generated support parts near their targets, uses short external stubs for signal labels, and keeps known power rails on power-symbol/pin-anchor behavior for native-netlist reliability. The compact tool response includes a `visual_layout` summary.

Set `visual_layout=false` to skip both the high-level visual pass and the lower-level default v2 visual layout. The expanded spec is marked with `layout_hints.visual_layout.enabled=false` so later build calls do not silently re-enable layout.

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

By default, `KICAD_MCP_TOOL_PROFILE=agent` exposes the design-intent workflow tools, schematic preview/export, functional layout, safe delete/grid helpers, simple pin connection wrappers, footprint assignment/report tools, ERC explanation/fix planning tools, symbol/footprint search and resolve tools, and `project_design_state`. Raw coordinate tools, v1 builders, compatibility aliases, full library listing, PCB primitives, broad export helpers, and analysis tools are hidden from the normal LLM tool list.

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
