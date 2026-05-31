# Schematic Generation Guide

Manual schematic edit and cleanup tools are not part of the installed MCP server surface. Schematic creation now uses one netlist-first path:

```text
design intent
  -> SKiDL canonical circuit / expected netlist
  -> KiUtils / kicad-skip schematic writer
  -> KiCad CLI netlist + ERC verification
  -> commit or rollback
```

Use these tools for generated schematics:

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

Use these tools for symbol and footprint discovery:

- `find_symbols`
- `resolve_symbol`
- `resolve_symbols`
- `find_footprints`
- `resolve_footprint`
- `resolve_footprints`

There is no legacy schematic builder, unsafe apply mode, partial write mode, blocking apply path, or fallback engine in the public MCP workflow. Agents start an async apply job, poll status, then fetch the final result.
