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
- `KICAD_MCP_TOOL_PROFILE=agent` keeps the default LLM tool surface intent-first. Use `advanced`, `debug`, or `all` only when a client needs lower-level tools.

Optional HTTP/SSE variables:

- `KICAD_MCP_HOST`, default `127.0.0.1`
- `KICAD_MCP_PORT`, default `8000`
- `KICAD_MCP_PATH`, default `/sse` for SSE and `/mcp` for HTTP transports

## ChatGPT setup with HTTPS tunnel

Start the local server on an HTTP transport. SSE is the most conservative option:

```bash
KICAD_MCP_TRANSPORT=sse \
KICAD_MCP_TOOL_PROFILE=agent \
KICAD_MCP_HOST=127.0.0.1 \
KICAD_MCP_PORT=8765 \
KICAD_MCP_PATH=/sse \
KICAD_CLI_PATH=/path/to/kicad-cli \
uv run python main.py
```

On Windows PowerShell:

```powershell
$env:KICAD_MCP_TRANSPORT = "sse"
$env:KICAD_MCP_TOOL_PROFILE = "agent"
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
        "KICAD_MCP_TRANSPORT": "stdio",
        "KICAD_MCP_TOOL_PROFILE": "agent"
      }
    }
  }
}
```

Replace the placeholder paths with your local repository path and KiCad CLI path.

## Recommended schematic workflow

For generated schematics, use the netlist-first workflow in this order:

1. `schematic_engine_status`
2. `find_symbols` / `resolve_symbols`
3. `schematic_preview_design_intent`
4. `schematic_start_design_intent_job`
5. `schematic_get_job_status` until the job reaches `succeeded`, `failed`, or `cancelled`
6. `schematic_get_job_result`
7. `schematic_validate_generated_schematic`
8. `export_schematic_preview`

The apply job is always atomic: it writes through the required SKiDL/KiUtils/kicad-skip engine, verifies with KiCad CLI, and rolls back on failure. `schematic_cancel_job` can cancel queued work and requests cooperative rollback for running work.

For PCB layout after schematic validation:

1. `pcb_design_intent_schema`
2. `pcb_preview_layout_intent`
3. `pcb_start_layout_job`
4. `pcb_get_layout_job_status` until the job reaches `succeeded`, `failed`, or `cancelled`
5. `pcb_get_layout_job_result`
6. `pcb_validate_layout`
7. `pcb_export_fabrication_package`

The default agent profile does not expose manual coordinate routing tools. PCB layout jobs sync footprints from the schematic, assign pad nets, apply board/placement constraints, and report ratsnest and DRC status.

## Current limitations

- No PCB routing yet.
- Manual schematic edit and cleanup tools are not exposed by the installed server.
