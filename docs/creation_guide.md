# KiCad Creation and PCB Authoring Guide

This guide covers the MCP tools for creating KiCad projects, resolving libraries, and adding conservative PCB layout primitives.

Most tools in this guide are hidden from the default `KICAD_MCP_TOOL_PROFILE=agent` surface. Schematic generation is handled by `schematic_start_design_intent_job` plus status/result polling; alternate schematic builders and low-level connection tools are not part of the installed MCP surface.

## Project creation

- `create_kicad_project(project_dir, project_name, create_schematic=True, create_pcb=True, paper="A4")`
- `create_schematic_file(project_path, overwrite=False, paper="A4")`
- `create_pcb_file(project_path, overwrite=False, board_width_mm=100, board_height_mm=80)`

Creation tools refuse to overwrite existing files unless the tool exposes an explicit `overwrite=True` argument.

## Library discovery

- `list_symbol_libraries(query=None)`
- `list_footprint_libraries(query=None)`
- `resolve_symbol(lib_id)`
- `resolve_footprint(footprint_id)`

Symbols and footprints are resolved from installed KiCad libraries. The resolver checks environment overrides first:

- `KICAD_SYMBOL_DIR`
- `KICAD_SYMBOL_PATHS`
- `KICAD_FOOTPRINT_DIR`
- `KICAD_FOOTPRINT_PATHS`

On Windows it also searches versioned KiCad installations such as `C:\Program Files\KiCad\10.0\share\kicad`.

## PCB authoring

- `pcb_add_footprint(...)`
- `pcb_move_footprint(...)`
- `pcb_create_board_outline(...)`
- `pcb_add_track(...)`
- `pcb_add_via(...)`
- `pcb_generate_basic_layout(...)`

PCB tooling is conservative. The default agent workflow uses `pcb_preview_layout_intent`, `pcb_start_layout_job`, `pcb_get_layout_job_status`, `pcb_get_layout_job_result`, `pcb_validate_layout`, and `pcb_export_fabrication_package`. The job syncs footprints from the schematic, assigns pad nets, creates the board outline, applies functional placement, and reports ratsnest/quality status. It does not autoroute.

Manual tools such as `pcb_generate_basic_layout(...)`, `pcb_add_track(...)`, and `pcb_add_via(...)` remain debug/advanced tools. Copper is only written when explicit routing tools are called.

## KiCad CLI

KiCad CLI is used when available for schematic validation/export and existing DRC/export tools. Set `KICAD_CLI_PATH` if auto-detection does not find your installation:

```powershell
$env:KICAD_CLI_PATH = "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
```
