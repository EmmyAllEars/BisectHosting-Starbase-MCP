#!/usr/bin/env python3
"""
bisect_mcp.py — Single-file MCP server for BisectHosting game panels.

Speaks the Starbase API (BisectHosting's customized Pterodactyl panel).
API docs: https://games.bisecthosting.com/docs
OpenAPI spec: https://games.bisecthosting.com/api-docs/openapi.json

Stdlib only — no third-party dependencies. Implements the Model Context
Protocol over stdio using JSON-RPC 2.0.

Credentials (checked in order — first non-empty wins):
    1. macOS Keychain (service: "bisect-game-servers", account: "api_key")
       — preferred, set via the store_credentials tool
    2. BISECT_API_KEY environment variable
    3. ~/.config/bisect-game-servers/credentials.json  (key: "api_key")
    4. ~/Library/Application Support/bisect-game-servers/credentials.json
       (macOS-native location, key: "api_key")

Panel URL is always https://games.bisecthosting.com (not configurable).

Future enhancement — WebSocket support:
    The Starbase API also exposes a WebSocket endpoint at
    GET /api/client/servers/{server}/websocket which returns a JWT token
    and wss:// URL. Once connected and authenticated, it streams real-time
    console output, status changes, stats, player lists, and chat — no
    polling or log-tailing needed. Tokens expire every 10 minutes (the
    server sends `token expiring` 3 min before). Events you can send:
    auth, send command, set state, send logs, send stats, send player list,
    send player chat. Events you receive: console output, status, stats,
    player list, player chat, token expiring/expired, daemon error.
    Would require a background thread + asyncio rewrite. See the Starbase
    API docs for the full WebSocket spec.
"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bisect"
SERVER_VERSION = "0.3.0"

PANEL_URL = "https://games.bisecthosting.com"
KEYCHAIN_SERVICE = "bisect-game-servers"

_IS_MACOS = platform.system() == "Darwin"


# ---------- macOS Keychain helpers (stdlib only, uses `security` CLI) ----------

def _keychain_read(account: str) -> str:
    """Read a value from the macOS Keychain. Returns '' on any failure."""
    if not _IS_MACOS:
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _keychain_write(account: str, value: str) -> bool:
    """Store a value in the macOS Keychain. Returns True on success.

    Uses -U (update-if-exists) so it's safe to call repeatedly.
    """
    if not _IS_MACOS:
        return False
    try:
        result = subprocess.run(
            ["security", "add-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", account, "-w", value, "-U"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _keychain_delete(account: str) -> bool:
    """Remove a value from the macOS Keychain. Returns True on success."""
    if not _IS_MACOS:
        return False
    try:
        result = subprocess.run(
            ["security", "delete-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", account],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _load_api_key() -> str:
    """Resolve API key.

    Priority:
        1. macOS Keychain
        2. BISECT_API_KEY environment variable
        3. Credentials JSON files on disk (legacy fallback)
    """
    # 1. Keychain (preferred)
    api_key = _keychain_read("api_key")
    if api_key:
        return api_key

    # 2. Environment variable
    api_key = os.environ.get("BISECT_API_KEY", "").strip()
    if api_key:
        return api_key

    # 3. Credentials files (legacy fallback)
    home = pathlib.Path(os.path.expanduser("~"))
    candidates = [
        home / ".config" / "bisect-game-servers" / "credentials.json",
        home / "Library" / "Application Support" / "bisect-game-servers" / "credentials.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"[bisect-mcp] failed to parse {path}: {e}\n")
            continue
        api_key = str(data.get("api_key", "")).strip()
        if api_key:
            return api_key

    return ""


# Lazy-loaded — stays empty until the first tool call that needs it.
# This avoids calling `security` (Keychain) at startup, which can hang
# if macOS prompts for permission while the server is running headless.
API_KEY: str = ""
_api_key_lock = threading.Lock()


def _get_api_key() -> str:
    """Return the API key, loading it lazily on first call. Thread-safe."""
    global API_KEY
    if API_KEY:
        return API_KEY
    with _api_key_lock:
        # Double-check after acquiring the lock.
        if not API_KEY:
            API_KEY = _load_api_key()
        return API_KEY


# ---------- HTTP helper ----------

# 10 MB default cap — generous for JSON/text, prevents OOM on huge files.
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _request(method: str, path: str, *, query: dict | None = None,
             json_body: Any = None, raw_body: bytes | None = None,
             accept: str = "application/json",
             max_response_bytes: int = _MAX_RESPONSE_BYTES) -> tuple[int, bytes, dict]:
    """Make a single HTTP request to the panel. Returns (status, body, headers)."""
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "No BisectHosting API key configured. "
            "Use the store_credentials tool to save your API key to the macOS Keychain. "
            "Ask the user for their ptlc_... key from https://games.bisecthosting.com → "
            "Profile → API Credentials."
        )

    url = PANEL_URL + path
    if query:
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": accept,
        "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
    }
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        headers.setdefault("Content-Type", "text/plain")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            try:
                body = resp.read(max_response_bytes + 1)
            except (OSError, ConnectionError) as e:
                raise RuntimeError(f"connection lost while reading response from {method} {path}: {e}") from e
            if len(body) > max_response_bytes:
                body = body[:max_response_bytes]
                _log(f"response truncated to {max_response_bytes} bytes for {method} {path}")
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        return e.code, body, dict(e.headers or {})
    except urllib.error.URLError as e:
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        raise RuntimeError(f"network error on {method} {path}: {reason}") from e


def _json_request(method: str, path: str, **kwargs) -> Any:
    status, body, _ = _request(method, path, **kwargs)
    if status >= 400:
        raise RuntimeError(f"HTTP {status} on {method} {path}: {body.decode('utf-8', 'replace')[:500]}")
    if not body:
        return {"ok": True, "status": status}
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {"status": status, "body": body.decode("utf-8", "replace")}


def _safe_int(value: Any, name: str, default: int) -> int:
    """Convert a value to int with a clear error message on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a number, got: {value!r}")


# ---------- Tool implementations ----------

def tool_list_servers(_: dict) -> Any:
    return _json_request("GET", "/api/client", query={"per_page": 100})


def tool_get_server(args: dict) -> Any:
    sid = args["server_id"]
    return _json_request("GET", f"/api/client/servers/{sid}")


def tool_get_server_resources(args: dict) -> Any:
    sid = args["server_id"]
    return _json_request("GET", f"/api/client/servers/{sid}/resources")


def tool_power_action(args: dict) -> Any:
    sid = args["server_id"]
    action = args["signal"]
    if action not in ("start", "stop", "restart", "kill"):
        raise ValueError("signal must be one of: start, stop, restart, kill")
    return _json_request("POST", f"/api/client/servers/{sid}/power", json_body={"signal": action})


def tool_send_command(args: dict) -> Any:
    sid = args["server_id"]
    command = args["command"]
    return _json_request("POST", f"/api/client/servers/{sid}/command", json_body={"command": command})


def tool_list_files(args: dict) -> Any:
    sid = args["server_id"]
    directory = args.get("directory", "/")
    return _json_request("GET", f"/api/client/servers/{sid}/files/list", query={"directory": directory})


def tool_read_file(args: dict) -> Any:
    sid = args["server_id"]
    file_path = args["file"]
    max_bytes = _safe_int(args.get("max_bytes"), "max_bytes", 200_000)
    tail_bytes = _safe_int(args.get("tail_bytes"), "tail_bytes", 0)
    status, body, _ = _request(
        "POST",
        f"/api/client/servers/{sid}/files/contents",
        query={"file": file_path},
        json_body={},
        accept="text/plain",
    )
    if status >= 400:
        raise RuntimeError(f"HTTP {status} reading {file_path}: {body.decode('utf-8', 'replace')[:500]}")
    text = body.decode("utf-8", "replace")
    total_len = len(text)
    truncated = False
    if tail_bytes > 0 and total_len > tail_bytes:
        text = text[-tail_bytes:]
        truncated = True
    elif total_len > max_bytes:
        text = text[:max_bytes]
        truncated = True
    return {
        "file": file_path,
        "total_chars": total_len,
        "returned_chars": len(text),
        "truncated": truncated,
        "content": text,
    }


def tool_write_file(args: dict) -> Any:
    sid = args["server_id"]
    file_path = args["file"]
    content = args["content"]
    status, body, _ = _request(
        "POST",
        f"/api/client/servers/{sid}/files/write",
        json_body={"file": file_path, "content": content},
    )
    if status >= 400:
        raise RuntimeError(f"HTTP {status} writing {file_path}: {body.decode('utf-8', 'replace')[:500]}")
    return {"ok": True, "file": file_path, "bytes_written": len(content.encode("utf-8"))}


def tool_rename_file(args: dict) -> Any:
    sid = args["server_id"]
    root = args.get("root", "/")
    from_path = args["from"]
    to_path = args["to"]
    return _json_request(
        "PUT",
        f"/api/client/servers/{sid}/files/rename",
        json_body={"root": root, "files": [{"from": from_path, "to": to_path}]},
    )


def tool_delete_files(args: dict) -> Any:
    sid = args["server_id"]
    root = args.get("root", "/")
    files = args["files"]
    if isinstance(files, str):
        files = [files]
    return _json_request(
        "POST",
        f"/api/client/servers/{sid}/files/delete",
        json_body={"root": root, "files": files},
    )


def tool_create_folder(args: dict) -> Any:
    sid = args["server_id"]
    root = args.get("root", "/")
    name = args["name"]
    return _json_request(
        "POST",
        f"/api/client/servers/{sid}/files/create-folder",
        json_body={"root": root, "name": name},
    )


def tool_list_backups(args: dict) -> Any:
    sid = args["server_id"]
    return _json_request("GET", f"/api/client/servers/{sid}/backups")


def tool_create_backup(args: dict) -> Any:
    sid = args["server_id"]
    body: dict[str, Any] = {}
    if "name" in args:
        body["name"] = args["name"]
    if "ignored" in args:
        body["ignored"] = args["ignored"]
    return _json_request("POST", f"/api/client/servers/{sid}/backups", json_body=body)


def tool_get_backup_download_url(args: dict) -> Any:
    sid = args["server_id"]
    backup = args["backup_id"]
    return _json_request("GET", f"/api/client/servers/{sid}/backups/{backup}/download")


def tool_get_file_download_url(args: dict) -> Any:
    sid = args["server_id"]
    file_path = args["file"]
    return _json_request(
        "GET",
        f"/api/client/servers/{sid}/files/download",
        query={"file": file_path},
    )


def tool_search_file_text(args: dict) -> Any:
    """Read a server file and regex-search it client-side.

    Useful for scanning logs for a pattern without pulling the full file.
    """
    sid = args["server_id"]
    file_path = args["file"]
    pattern = args["pattern"]
    max_matches = _safe_int(args.get("max_matches"), "max_matches", 50)
    context_lines = _safe_int(args.get("context_lines"), "context_lines", 0)
    tail_bytes = _safe_int(args.get("tail_bytes"), "tail_bytes", 0)

    query = {"file": file_path}
    status, body, _ = _request(
        "POST",
        f"/api/client/servers/{sid}/files/contents",
        query=query,
        json_body={},
        accept="text/plain",
    )
    if status >= 400:
        raise RuntimeError(f"HTTP {status} reading {file_path}: {body.decode('utf-8', 'replace')[:500]}")
    text = body.decode("utf-8", "replace")
    if tail_bytes > 0 and len(text) > tail_bytes:
        text = text[-tail_bytes:]

    lines = text.split("\n")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid regex: {e}")

    matches = []
    for idx, line in enumerate(lines):
        if rx.search(line):
            start = max(0, idx - context_lines)
            end = min(len(lines), idx + context_lines + 1)
            matches.append({
                "line_number": idx + 1,
                "line": line,
                "context": lines[start:end] if context_lines else None,
            })
            if len(matches) >= max_matches:
                break

    return {
        "file": file_path,
        "pattern": pattern,
        "total_lines_scanned": len(lines),
        "matches_returned": len(matches),
        "matches": matches,
    }


def tool_store_credentials(args: dict) -> Any:
    """Store BisectHosting API key in the macOS Keychain."""
    global API_KEY

    api_key = str(args.get("api_key", "")).strip()

    if not api_key:
        raise ValueError("api_key is required")
    if not api_key.startswith("ptlc_"):
        raise ValueError(
            "API key should start with 'ptlc_'. "
            "Get yours from https://games.bisecthosting.com → Profile → API Credentials."
        )

    if not _IS_MACOS:
        raise RuntimeError(
            "Keychain storage requires macOS. "
            "On other platforms, set BISECT_API_KEY as an environment variable."
        )

    ok = _keychain_write("api_key", api_key)

    if not ok:
        raise RuntimeError(
            "Failed to write api_key to Keychain. "
            "macOS may have prompted for permission — try again after approving."
        )

    # Reload so tools work immediately without restarting the server.
    with _api_key_lock:
        API_KEY = api_key

    return {
        "ok": True,
        "message": "API key saved to macOS Keychain. All tools are now ready to use.",
    }


# ---------- Tool registry ----------

TOOLS: list[dict] = [
    {
        "name": "store_credentials",
        "description": (
            "Save BisectHosting API credentials to the macOS Keychain. "
            "Call this when no API key is configured or when the user wants to update their key. "
            "The API key starts with 'ptlc_' and is obtained from "
            "https://games.bisecthosting.com → Profile → API Credentials. "
            "Ask the user for the key before calling this tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "BisectHosting API key (starts with ptlc_)",
                },
            },
            "required": ["api_key"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Store BisectHosting API credentials", "openWorldHint": False},
        "handler": tool_store_credentials,
    },
    {
        "name": "list_servers",
        "description": "List all game servers the API key can see on the BisectHosting panel. Returns server identifiers, names, and current instance (game) for each.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"title": "List game servers", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_list_servers,
    },
    {
        "name": "get_server",
        "description": "Get full details for one server: name, current instance (game), node, limits, status. Use the server identifier (short UUID prefix) from list_servers.",
        "inputSchema": {
            "type": "object",
            "properties": {"server_id": {"type": "string", "description": "Server identifier (short UUID shown in the panel URL)"}},
            "required": ["server_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Get server details", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_get_server,
    },
    {
        "name": "get_server_resources",
        "description": "Get live resource usage: CPU %, memory, disk, network, and power state (running/offline/starting/stopping).",
        "inputSchema": {
            "type": "object",
            "properties": {"server_id": {"type": "string"}},
            "required": ["server_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Get server resource usage", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_get_server_resources,
    },
    {
        "name": "power_action",
        "description": "Send a power signal to the server. Signals: start, stop, restart, kill. Ask the user before using 'kill' (force-terminate may cause data loss).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "signal": {"type": "string", "enum": ["start", "stop", "restart", "kill"]},
            },
            "required": ["server_id", "signal"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Send power signal (start/stop/restart/kill)", "destructiveHint": True, "openWorldHint": True},
        "handler": tool_power_action,
    },
    {
        "name": "send_command",
        "description": "Send a console command to the running server (equivalent to typing into the panel console). Server must be running.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "command": {"type": "string", "description": "Raw console command, no leading slash unless the game expects one"},
            },
            "required": ["server_id", "command"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Send console command", "openWorldHint": True},
        "handler": tool_send_command,
    },
    {
        "name": "list_files",
        "description": "List files in a directory on the server. directory defaults to '/'. Returns name, size, modified time, and whether each entry is a file or directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "directory": {"type": "string", "description": "Absolute path from the server root, e.g. '/WS/Saved/Logs'"},
            },
            "required": ["server_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "List server files", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_list_files,
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file on the server. Use tail_bytes to read only the end of a large log file. Default max_bytes is 200000.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "file": {"type": "string", "description": "Absolute path from the server root"},
                "max_bytes": {"type": "integer", "description": "Maximum characters to return (default 200000)"},
                "tail_bytes": {"type": "integer", "description": "If set, return only the last N characters"},
            },
            "required": ["server_id", "file"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Read server file", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_read_file,
    },
    {
        "name": "write_file",
        "description": "Write (create or overwrite) a file on the server. For config file edits — ask the user before overwriting anything important.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "file": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["server_id", "file", "content"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Write server file", "idempotentHint": True, "openWorldHint": True},
        "handler": tool_write_file,
    },
    {
        "name": "rename_file",
        "description": "Rename or move a single file within the server filesystem.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "root": {"type": "string", "description": "Directory the paths are relative to (default '/')"},
                "from": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["server_id", "from", "to"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Rename or move file", "openWorldHint": True},
        "handler": tool_rename_file,
    },
    {
        "name": "delete_files",
        "description": "Delete one or more files/directories on the server. Destructive — ask the user for confirmation before calling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "root": {"type": "string", "description": "Directory the file names are relative to (default '/')"},
                "files": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "One file name or a list of them",
                },
            },
            "required": ["server_id", "files"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Delete files (destructive)", "destructiveHint": True, "openWorldHint": True},
        "handler": tool_delete_files,
    },
    {
        "name": "create_folder",
        "description": "Create a new directory on the server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "root": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["server_id", "name"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Create folder", "openWorldHint": True},
        "handler": tool_create_folder,
    },
    {
        "name": "list_backups",
        "description": "List backups for the server (name, uuid, size, creation time, is_locked).",
        "inputSchema": {
            "type": "object",
            "properties": {"server_id": {"type": "string"}},
            "required": ["server_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "List backups", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_list_backups,
    },
    {
        "name": "create_backup",
        "description": "Create a new backup of the server. Counts against the server's backup slot limit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "name": {"type": "string", "description": "Optional backup name"},
                "ignored": {"type": "string", "description": "Optional newline-separated list of paths to exclude"},
            },
            "required": ["server_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Create backup", "openWorldHint": True},
        "handler": tool_create_backup,
    },
    {
        "name": "get_backup_download_url",
        "description": "Get a signed download URL for an existing backup. URL is short-lived.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "backup_id": {"type": "string", "description": "Backup UUID from list_backups"},
            },
            "required": ["server_id", "backup_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Get backup download URL", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_get_backup_download_url,
    },
    {
        "name": "get_file_download_url",
        "description": "Get a signed download URL for a single file on the server. URL is short-lived. Use this for binary files (databases, save files, images) where read_file would corrupt the bytes — fetch the URL with curl/fetch outside the MCP. For text files, prefer read_file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "file": {"type": "string", "description": "Absolute path from the server root, e.g. '/WS/Saved/Worlds/Dedicated/world.db'"},
            },
            "required": ["server_id", "file"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Get file download URL", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_get_file_download_url,
    },
    {
        "name": "search_file_text",
        "description": "Read a file from the server and regex-search it locally. Good for scanning logs for a specific pattern. Use tail_bytes to scan only the recent portion of a big log.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "file": {"type": "string"},
                "pattern": {"type": "string", "description": "Python regex pattern"},
                "max_matches": {"type": "integer", "description": "Stop after N matches (default 50)"},
                "context_lines": {"type": "integer", "description": "Lines of context around each match"},
                "tail_bytes": {"type": "integer", "description": "Only scan the last N characters of the file"},
            },
            "required": ["server_id", "file", "pattern"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Regex-search file contents", "readOnlyHint": True, "openWorldHint": True},
        "handler": tool_search_file_text,
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------- JSON-RPC loop ----------

_stdout_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2)


def _write(msg: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _log(msg: str) -> None:
    sys.stderr.write(f"[bisect-mcp] {msg}\n")
    sys.stderr.flush()


def _make_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _make_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _run_tool_in_background(req_id: Any, tool: dict, arguments: dict) -> None:
    """Execute a tool handler in a background thread and write the response."""
    try:
        result = tool["handler"](arguments)
        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        response = _make_result(req_id, {"content": [{"type": "text", "text": text}]})
    except Exception as e:
        response = _make_result(req_id, {
            "content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}],
            "isError": True,
        })
    try:
        _write(response)
    except (BrokenPipeError, IOError) as e:
        _log(f"failed to write tool response (pipe closed): {e!r}")


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no id → no response.
    is_notification = "id" not in msg

    try:
        if method == "initialize":
            return _make_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            # Forward only protocol-relevant fields. "annotations" is optional
            # and lets clients distinguish read-only / destructive tools so the
            # user can bulk-approve safe ones.
            public_keys = ("name", "description", "inputSchema", "annotations")
            tools_public = [
                {k: t[k] for k in public_keys if k in t}
                for t in TOOLS
            ]
            return _make_result(req_id, {"tools": tools_public})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            tool = TOOL_BY_NAME.get(name)
            if tool is None:
                return _make_error(req_id, -32602, f"unknown tool: {name}")
            # Dispatch to background thread so the main loop stays responsive
            # to pings and other protocol messages during long API calls.
            _executor.submit(_run_tool_in_background, req_id, tool, arguments)
            return None  # response sent from background thread

        if method == "ping":
            return _make_result(req_id, {})

        if is_notification:
            return None
        return _make_error(req_id, -32601, f"method not found: {method}")

    except Exception as e:
        _log(f"handler error: {e!r}")
        if is_notification:
            return None
        return _make_error(req_id, -32603, f"internal error: {e}")


def main() -> None:
    # Ignore SIGPIPE so writes to a closed pipe raise BrokenPipeError
    # instead of killing the process outright.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    _log(f"starting, panel={PANEL_URL}, key_loading=lazy")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                _log(f"bad json: {e}")
                continue
            response = handle(msg)
            if response is not None:
                _write(response)
    except (BrokenPipeError, IOError):
        _log("stdout pipe closed, shutting down")
    except KeyboardInterrupt:
        _log("interrupted, shutting down")
    finally:
        _executor.shutdown(wait=False)
        _log("exited")


if __name__ == "__main__":
    main()
