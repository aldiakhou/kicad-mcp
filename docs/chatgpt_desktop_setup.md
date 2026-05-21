# ChatGPT Desktop MCP Setup

This repository can be used from ChatGPT Desktop through an MCP server entry.

## Prerequisites

- `uv` installed
- project dependencies installed with `make install`
- KiCad CLI available

## Transport modes

The server now supports configurable MCP transports through environment variables:

- `KICAD_MCP_TRANSPORT=stdio` keeps the historical local stdio behavior.
- `KICAD_MCP_TRANSPORT=sse` starts a local HTTP/SSE MCP endpoint for clients that require SSE.
- `KICAD_MCP_TRANSPORT=streamable-http` or `http` can be used with clients that support those FastMCP transports.

Optional HTTP/SSE variables:

- `KICAD_MCP_HOST`, default `127.0.0.1`
- `KICAD_MCP_PORT`, default `8000`
- `KICAD_MCP_PATH`, default `/sse` for SSE and `/mcp` for HTTP transports

## ChatGPT Desktop SSE setup

If your ChatGPT Desktop build expects an SSE MCP endpoint, start the server separately:

```bash
KICAD_MCP_TRANSPORT=sse \
KICAD_MCP_HOST=127.0.0.1 \
KICAD_MCP_PORT=8765 \
KICAD_CLI_PATH=/path/to/kicad-cli \
uv run python main.py
```

Then configure ChatGPT Desktop to connect to:

```text
http://127.0.0.1:8765/sse
```

Keep the terminal running while using the connector.

## Stdio setup

For clients that launch local stdio MCP servers directly, use this configuration:

```json
{
  "mcpServers": {
    "kicad-mcp": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "/path/to/kicad-mcp",
      "env": {
        "KICAD_CLI_PATH": "/path/to/kicad-cli",
        "KICAD_MCP_TRANSPORT": "stdio"
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
