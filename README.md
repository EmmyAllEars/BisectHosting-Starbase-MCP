# BisectHosting Starbase MCP

A single-file [Model Context Protocol](https://modelcontextprotocol.io) server for the [BisectHosting](https://www.bisecthosting.com) game-server panel (Starbase API — BisectHosting's customized Pterodactyl panel).

Lets Claude (or any MCP client) inspect and control your game servers: power actions, console commands, file read/write, log search, and backups.

- **Panel:** https://games.bisecthosting.com
- **API docs:** https://games.bisecthosting.com/docs
- **OpenAPI spec:** https://games.bisecthosting.com/api-docs/openapi.json

## Requirements

- Python 3.10+ (stdlib only — no third-party deps)
- macOS for Keychain credential storage (other platforms can use `BISECT_API_KEY`)
- A BisectHosting API key starting with `ptlc_` (Profile → API Credentials)

## Install

### Recommended: on-demand via `uvx` (no clone needed)

Add to your MCP config (e.g. `~/.claude/.mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bisect": {
      "command": "uvx",
      "args": [
        "--refresh",
        "--from",
        "git+https://github.com/EmmyAllEars/BisectHosting-Starbase-MCP",
        "bisect-mcp"
      ]
    }
  }
}
```

`--refresh` makes `uvx` pull the latest commit on every server start (~1-2s of network) so updates are hands-off — just `git push` and the next launch picks them up. Drop `--refresh` to cache forever, or pin to a tag with `git+https://github.com/EmmyAllEars/BisectHosting-Starbase-MCP@v0.4.0`.

Requires [`uv`](https://docs.astral.sh/uv/) on PATH (`brew install uv` on macOS).

Restart the client. The first time you call a tool, run `store_credentials` with your `ptlc_...` API key — it'll be saved to the macOS Keychain (service: `bisect-game-servers`, account: `api_key`).

### Alternative: clone and run locally (for development)

```bash
git clone https://github.com/EmmyAllEars/BisectHosting-Starbase-MCP.git
```

Then point your MCP client at the local file:

```json
{
  "mcpServers": {
    "bisect": {
      "command": "python3",
      "args": ["/absolute/path/to/BisectHosting-Starbase-MCP/bisect_mcp.py"]
    }
  }
}
```

## Credential resolution

Checked in order; first non-empty wins:

1. macOS Keychain — service `bisect-game-servers`, account `api_key`
2. `BISECT_API_KEY` environment variable
3. `~/.config/bisect-game-servers/credentials.json`
4. `~/Library/Application Support/bisect-game-servers/credentials.json`

## Tools

| Tool | Purpose |
| --- | --- |
| `store_credentials` | Save API key to macOS Keychain |
| `list_servers` | List all servers visible to the API key |
| `get_server` | Server details (name, game, node, limits) |
| `get_server_resources` | Live CPU / memory / disk / power state |
| `power_action` | start / stop / restart / kill |
| `send_command` | Send a console command |
| `list_files` | List a directory on the server |
| `read_file` | Read a file (supports `tail_bytes` for big logs) |
| `write_file` | Create or overwrite a file |
| `rename_file` | Rename / move a file |
| `delete_files` | Delete one or more files (destructive) |
| `create_folder` | Make a directory |
| `list_backups` | List backups |
| `create_backup` | Create a new backup |
| `get_backup_download_url` | Signed download URL for a backup |
| `search_file_text` | Regex-search a file (great for log scanning) |

## Notes

- Tool calls run in a background thread pool so the JSON-RPC loop stays responsive to pings during long API calls.
- Response bodies are capped at 10 MB.
- API key is loaded lazily on the first tool call (avoids startup hang from a Keychain prompt).

## License

No license specified — private project.
