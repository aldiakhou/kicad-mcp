# ChatGPT Desktop MCP Setup

This repository can be used from ChatGPT Desktop through an MCP server entry.

## Prerequisites

- `uv` installed
- project dependencies installed with `make install`
- KiCad CLI available

## Example configuration

Update your ChatGPT Desktop MCP configuration to point at this repository:

```json
{
  "mcpServers": {
    "kicad-mcp": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "/path/to/kicad-mcp",
      "env": {
        "KICAD_CLI_PATH": "/path/to/kicad-cli"
      }
    }
  }
}
```

Replace the placeholder paths with your local repository path and KiCad CLI path.

## Recommended cleanup workflow

For safe schematic cleanup, use the high-level workflow in this order:

1. `schematic_cleanup_report(...)`
2. `schematic_preview_cleanup(...)`
3. `schematic_apply_cleanup(...)`

This lets ChatGPT Desktop inspect the schematic, preview conservative cleanup changes, apply only safe block/property edits, and export a final SVG preview.

## Current limitations

- No PCB routing yet.
- No automatic symbol rotation with attached wires.
- No complex multi-segment boundary wire movement yet.
- No mid-segment label movement yet.
- Block detection is geometric and heuristic, not full ERC/netlist-aware.
