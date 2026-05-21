# KiCad Creation and PCB Authoring Guide

This guide covers the MCP tools for creating KiCad projects, authoring schematics, and adding conservative PCB layout primitives.

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

## Schematic authoring

- `schematic_add_symbol(...)`
- `schematic_add_wire(...)`
- `schematic_add_label(...)`
- `schematic_connect_points(...)`
- `schematic_delete_item(...)`

Schematic writes use the same transactional pattern as the cleanup tools: backup, parse, mutate, validate, optional KiCad CLI SVG export, and rollback on failure.

## PCB authoring

- `pcb_add_footprint(...)`
- `pcb_move_footprint(...)`
- `pcb_create_board_outline(...)`
- `pcb_add_track(...)`
- `pcb_add_via(...)`
- `pcb_generate_basic_layout(...)`

PCB tooling is conservative. `pcb_generate_basic_layout(...)` creates an outline and places footprints from schematic footprint properties. It does not autoroute. Copper is only written when `pcb_add_track(...)` or `pcb_add_via(...)` is called explicitly.

## KiCad CLI

KiCad CLI is used when available for schematic validation/export and existing DRC/export tools. Set `KICAD_CLI_PATH` if auto-detection does not find your installation:

```powershell
$env:KICAD_CLI_PATH = "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
```
