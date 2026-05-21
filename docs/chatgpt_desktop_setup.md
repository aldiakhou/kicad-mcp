# ChatGPT MCP Setup

This repository can be used from ChatGPT as a remote MCP server, or from other MCP clients that launch local stdio servers directly.

## Important ChatGPT URL requirement

ChatGPT apps/connectors expect a remote MCP server URL that ChatGPT can reach over the network. Do not enter a local URL such as:

```text
http://127.0.0.1:8765/sse
http://localhost:8765/mcp
```

Those local HTTP URLs can be rejected as unsafe in ChatGPT. For ChatGPT, run this server locally and expose it through either:

- a public HTTPS tunnel, such as ngrok or Cloudflare Tunnel, while developing
- OpenAI Secure MCP Tunnel, for private/on-premises servers where supported
- a real HTTPS deployment for longer-lived use

OpenAI's current docs describe ChatGPT MCP apps as remote MCP servers and list SSE and streaming HTTP as supported protocols in developer mode:

- https://developers.openai.com/api/docs/mcp
- https://developers.openai.com/api/docs/guides/developer-mode
- https://developers.openai.com/api/docs/guides/tools-connectors-mcp

## Prerequisites

- `uv` installed
- project dependencies installed with `make install`
- KiCad CLI available
- ChatGPT developer mode enabled if you want full MCP tool access

## Transport modes

The server supports configurable MCP transports through environment variables:

- `KICAD_MCP_TRANSPORT=stdio` keeps the historical local stdio behavior.
- `KICAD_MCP_TRANSPORT=sse` starts an HTTP/SSE MCP endpoint.
- `KICAD_MCP_TRANSPORT=streamable-http` or `http` starts an HTTP MCP endpoint for clients that support those FastMCP transports.

Optional HTTP/SSE variables:

- `KICAD_MCP_HOST`, default `127.0.0.1`
- `KICAD_MCP_PORT`, default `8000`
- `KICAD_MCP_PATH`, default `/sse` for SSE and `/mcp` for HTTP transports

## ChatGPT setup with HTTPS tunnel

Start the local server on an HTTP transport. SSE is the most conservative option:

```bash
KICAD_MCP_TRANSPORT=sse \
KICAD_MCP_HOST=127.0.0.1 \
KICAD_MCP_PORT=8765 \
KICAD_MCP_PATH=/sse \
KICAD_CLI_PATH=/path/to/kicad-cli \
uv run python main.py
```

On Windows PowerShell:

```powershell
$env:KICAD_MCP_TRANSPORT = "sse"
$env:KICAD_MCP_HOST = "127.0.0.1"
$env:KICAD_MCP_PORT = "8765"
$env:KICAD_MCP_PATH = "/sse"
$env:KICAD_CLI_PATH = "C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"
uv run python main.py
```

In another terminal, expose the local port through HTTPS. For ngrok:

```bash
ngrok http 8765
```

Register the HTTPS tunnel URL in ChatGPT, including the MCP path:

```text
https://your-ngrok-domain.ngrok-free.app/sse
```

If you use `KICAD_MCP_TRANSPORT=streamable-http`, keep the default `/mcp` path and register:

```text
https://your-domain.example/mcp
```

Keep both the MCP server and the tunnel process running while using the ChatGPT app/connector.

## ChatGPT unsafe URL troubleshooting

If ChatGPT shows `Unsafe URL`:

1. Confirm the URL starts with `https://`.
2. Confirm it is not `localhost`, `127.0.0.1`, a private LAN IP, or plain `http://`.
3. Confirm the path matches your transport: `/sse` for SSE, `/mcp` for streamable HTTP.
4. Use a short app name and description while testing.
5. Refresh the app/connector tools after restarting the server.

## Stdio setup for local MCP clients

Some MCP clients launch local stdio servers directly. For those clients, use this configuration instead of an HTTPS tunnel:

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

This lets ChatGPT inspect the schematic, preview conservative cleanup changes, apply only safe block/property edits, and export a final SVG preview.

## Current limitations

- No PCB routing yet.
- No automatic symbol rotation with attached wires.
- No complex multi-segment boundary wire movement yet.
- No mid-segment label movement yet.
- Block detection is geometric and heuristic, not full ERC/netlist-aware.
