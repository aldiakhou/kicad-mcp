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
- `schematic_start_design_intent_job`
- `schematic_get_job_status`
- `schematic_get_job_result`
- `schematic_cancel_job`
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

`schematic_start_design_intent_job` always uses the required SKiDL, KiUtils, kicad-skip, and KiCad CLI verification path. It does not expose engine mode, unsafe apply, partial write, or validation-level parameters. The old blocking apply tool is not exposed in the default agent profile; agents should always start a job and poll it.

## Recommended Order

1. `create_kicad_project`
2. `schematic_engine_status`
3. `find_symbols` / `resolve_symbols`
4. `find_footprints` / `resolve_footprints`
5. `schematic_preview_design_intent`
6. `schematic_start_design_intent_job`
7. `schematic_get_job_status` until `status` is `succeeded`, `failed`, or `cancelled`
8. `schematic_get_job_result`
9. `schematic_validate_generated_schematic`
10. `export_schematic_preview`

The apply job writes in a temporary worktree first. If KiCad CLI export, ERC, or netlist comparison fails, the live project is not changed. `schematic_cancel_job` is cooperative: it cancels queued work immediately and requests rollback at the next pipeline checkpoint for running work. If KiCad CLI is already running, that command may finish before the job rolls back.

## PCB Layout

After the schematic is committed and validated, use the PCB intent workflow. The default agent profile exposes only the high-level PCB tools:

- `pcb_design_intent_schema`
- `pcb_preview_layout_intent`
- `pcb_start_layout_job`
- `pcb_get_layout_job_status`
- `pcb_get_layout_job_result`
- `pcb_cancel_layout_job`
- `pcb_validate_layout`
- `pcb_export_fabrication_package`

Recommended PCB order:

1. `pcb_design_intent_schema`
2. `pcb_preview_layout_intent`
3. `pcb_start_layout_job`
4. `pcb_get_layout_job_status` until `status` is `succeeded`, `failed`, or `cancelled`
5. `pcb_get_layout_job_result`
6. `pcb_validate_layout`
7. `pcb_export_fabrication_package`

The PCB job syncs footprints and pad nets from the generated schematic, creates or updates the board outline, applies functional placement constraints, and returns ratsnest/quality status. It does not expose coordinate-level manual routing in the agent profile. Low-level footprint, track, via, and pad-routing tools remain debug-only.

PCB intent example:

```json
{
  "board": {"width_mm": 60, "height_mm": 40, "shape": "rectangular"},
  "placement": {
    "style": "functional",
    "preserve_existing_placement": true,
    "components": [
      {"ref": "J1", "x": 5, "y": 20, "angle": 90},
      {"ref": "U1", "x": 30, "y": 20, "angle": 0}
    ],
    "rules": {
      "roles": {
        "connector": {"x": 8, "y": 35, "angle": 0},
        "primary_controller": {"x": 30, "y": 20, "angle": 0}
      }
    }
  },
  "routing": {"mode": "report_only", "track_width_mm": 0.25},
  "validation": {"run_drc": false, "require_clean_drc": false},
  "fabrication": {"include_step": false, "include_ipc2581": false, "run_drc": true}
}
```

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
