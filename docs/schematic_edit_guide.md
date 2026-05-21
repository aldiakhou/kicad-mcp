# Safe Schematic Editing Guide

This guide describes the schematic-safe editing workflow in the KiCad MCP Server.

## Scope

This workflow is focused on **safe schematic read/edit/validate/preview flows**:

- Read a schematic into a structured S-expression model
- Inspect symbols, labels, wires, sheet bounds, and obvious overlaps
- Apply layout-safe edits such as moving symbol properties and editing property text
- Create backups before writes
- Validate syntax after writes
- Attempt KiCad CLI SVG export validation when KiCad CLI is available
- Roll back automatically if a transactional edit fails validation

Electrical connectivity edits and PCB routing changes are intentionally out of scope on this branch.

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

- `schematic_move_symbol_property(schematic_path, reference, property_name, x, y, angle=None)`
- `schematic_set_property(schematic_path, reference, property_name, value)`
- `schematic_auto_arrange_symbol_properties(schematic_path, reference)`
- `schematic_cleanup_report(schematic_path, layout_style="left_to_right", spacing_x=35.0, spacing_y=25.0, arrange_properties=True, preserve_connectivity=True)`
- `schematic_preview_cleanup(schematic_path, layout_style="left_to_right", spacing_x=35.0, spacing_y=25.0, arrange_properties=True, preserve_connectivity=True)`
- `schematic_apply_cleanup(schematic_path, layout_style="left_to_right", spacing_x=35.0, spacing_y=25.0, arrange_properties=True, preserve_connectivity=True, output_path=None)`

These are the recommended schematic cleanup tools for this branch. They keep writes conservative by limiting automatic edits to safe block translations plus symbol property arrangement.

## Cleanup workflow

The high-level cleanup flow is:

1. `schematic_cleanup_report(...)`
2. `schematic_preview_cleanup(...)`
3. `schematic_apply_cleanup(...)`

The workflow is intentionally conservative:

- block detection is heuristic
- only connectivity-preserving block moves are allowed
- labels are not rearranged on their own
- labels only move when they are part of a safe block move
- symbol properties can be re-arranged across the whole schematic

`schematic_preview_cleanup(...)` is read-only and returns the proposed block moves, property moves, overlaps, refusals, and label limitations. If any block move is unsafe, the preview returns `success: false`.

`schematic_apply_cleanup(...)` performs backup, validation, safe cleanup, post-write connectivity checks for moved blocks, SVG export, and diff generation in one transactional flow.

## Guarded connectivity-affecting tools

- `schematic_move_symbol(schematic_path, reference, x, y, angle=None, allow_connectivity_change=False)`
- `schematic_move_label(schematic_path, label_uuid, x, y, angle=None, allow_connectivity_change=False)`
- `schematic_auto_arrange_labels(schematic_path, allow_connectivity_change=False)`

These tools are **not** guaranteed to preserve connectivity yet. Moving a symbol or electrical label can disconnect wires or pins even when the schematic remains syntactically valid and still exports to SVG.

For example, moving a resistor symbol away from a wire endpoint without also moving the attached wire leaves the schematic readable and exportable, but the pin is no longer electrically connected to that wire. The same applies to moving a net label off the wire or pin it was naming.

By default, the tools refuse edits when they detect connectivity risk. Callers must explicitly opt in with `allow_connectivity_change=True` until a future connectivity-preserving move flow exists.

## Preview tools

- `export_schematic_svg(schematic_path, output_path=None)`
- `export_schematic_preview(project_path)`

These tools use `kicad-cli` through the repository’s secure subprocess wrapper and return an SVG path plus preview content when export succeeds.

## Current limitations

- No PCB routing changes
- No automatic symbol rotation with attached wires
- No complex multi-segment boundary wire movement
- No mid-segment label movement
- Block detection is geometric and heuristic, not ERC/netlist-aware
