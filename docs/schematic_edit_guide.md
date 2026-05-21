# Safe Schematic Editing Guide

This guide describes the schematic-safe editing workflow in the KiCad MCP Server.

## Scope

This workflow is focused on **safe schematic read/edit/validate/preview flows**:

- Read a schematic into a structured S-expression model
- Inspect symbols, labels, wires, sheet bounds, and obvious overlaps
- Apply layout-safe edits such as moving symbols, labels, and symbol properties
- Create backups before writes
- Validate syntax after writes
- Attempt KiCad CLI SVG export validation when KiCad CLI is available
- Roll back automatically if a transactional edit fails validation

Electrical connectivity edits and PCB routing changes are intentionally out of scope for this phase.

## Safety guarantees

Every transactional schematic write follows this sequence:

1. Validate the target path and file type
2. Read the schematic
3. Create a timestamped backup
4. Parse and validate balanced KiCad S-expressions
5. Apply the edit through the structured schematic model
6. Re-serialize and re-validate the schematic
7. Optionally validate with KiCad CLI by attempting SVG export
8. Return changed objects, backup path, validation results, and diff output
9. Restore the backup automatically if validation fails

## Safety tools

- `validate_schematic_syntax(schematic_path)`
- `backup_project(project_path)`
- `restore_backup(backup_path)`
- `get_file_diff(file_path, backup_path)`

## Inspection tools

- `schematic_list_symbols(schematic_path)`
- `schematic_list_labels(schematic_path)`
- `schematic_list_wires(schematic_path)`
- `schematic_get_symbol(schematic_path, reference)`
- `schematic_find_overlaps(schematic_path)`
- `schematic_get_sheet_bounds(schematic_path)`

## Layout-safe editing tools

- `schematic_move_symbol(schematic_path, reference, x, y, angle=None)`
- `schematic_move_label(schematic_path, label_uuid, x, y, angle=None)`
- `schematic_move_symbol_property(schematic_path, reference, property_name, x, y, angle=None)`
- `schematic_set_property(schematic_path, reference, property_name, value)`
- `schematic_auto_arrange_symbol_properties(schematic_path, reference)`
- `schematic_auto_arrange_labels(schematic_path)`

These tools only change placement and text/property data. They do not create or delete circuit connectivity.

## Preview tools

- `export_schematic_svg(schematic_path, output_path=None)`
- `export_schematic_preview(project_path)`

These tools use `kicad-cli` through the repository’s secure subprocess wrapper and return an SVG path plus preview content when export succeeds.
