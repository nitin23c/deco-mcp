"""MCP over streamable HTTP (JSON responses, no SSE), standard library only."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import Config
from .tools import Router, Tool, ToolError, enabled_tools, redact

log = logging.getLogger("deco_mcp")

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY = 64 * 1024
INSTRUCTIONS = (
    "Tools for a TP-Link Deco router on the local network. Read tools are safe. Any tool that "
    "changes the router needs the user's explicit yes for that exact change first."
)


class McpApp:
    """JSON-RPC dispatch, independent of HTTP so it is easy to test."""

    def __init__(self, cfg: Config, router: Router) -> None:
        self.cfg = cfg
        self.router = router
        self.tools: dict[str, Tool] = enabled_tools(cfg)

    def handle(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
        if "id" not in msg:  # notification, e.g. notifications/initialized
            return None
        mid, method = msg["id"], msg["method"]
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _err(mid, -32602, "params must be an object")

        if method == "initialize":
            want = params.get("protocolVersion")
            return _ok(mid, {
                "protocolVersion": want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "deco-mcp", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.schema,
                    "annotations": {
                        "readOnlyHint": not t.writes,
                        "destructiveHint": t.writes,
                        "idempotentHint": not t.writes,
                        "openWorldHint": False,
                    },
                }
                for t in self.tools.values()
            ]})
        if method == "tools/call":
            return self._call(mid, params)
        return _err(mid, -32601, f"Method not found: {method}")

    def _call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _err(mid, -32602, f"Unknown or disabled tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(mid, -32602, "arguments must be an object")
        try:
            result = tool.run(self.router, args)
        except ToolError as exc:
            return _text(mid, str(exc), error=True)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the server
            log.error("tool %s failed: %s: %s", name, type(exc).__name__, redact(str(exc)))
            return _text(mid, f"Router call failed: {type(exc).__name__}: {redact(str(exc))}", error=True)
        return _text(mid, json.dumps(result, indent=1, default=str))


def _ok(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _text(mid: Any, text: str, error: bool = False) -> dict[str, Any]:
    return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": error})


def make_handler(app: McpApp) -> type[BaseHTTPRequestHandler]:
    cfg = app.cfg
    token = cfg.token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "deco-mcp"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.client_address[0], fmt % args)

        def _send(self, code: int, body: Any = None) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            try:
                addr = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                addr = None
            if addr is None or not any(addr in n for n in cfg.allow_nets):
                self._send(403, {"error": "forbidden"})
                return False
            # Browsers always send Origin on cross-site requests; MCP clients don't.
            # Refusing it blocks DNS-rebinding and drive-by requests from web pages.
            if self.headers.get("Origin"):
                self._send(403, {"error": "origin not allowed"})
                return False
            auth = self.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), token)):
                self._send(401, {"error": "unauthorized"})
                return False
            if self.path.split("?", 1)[0] != "/mcp":
                self._send(404, {"error": "not found"})
                return False
            return True

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._send(413 if length > MAX_BODY else 400, _err(None, -32700, "Bad request body"))
                return
            try:
                msg = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, _err(None, -32700, "Parse error"))
                return
            if isinstance(msg, list):
                replies = [r for r in map(app.handle, msg) if r is not None]
                self._send(200, replies) if replies else self._send(202)
                return
            reply = app.handle(msg)
            self._send(202) if reply is None else self._send(200, reply)

        def do_GET(self) -> None:  # noqa: N802 - no server-initiated stream
            if self._allowed():
                self._send(405, {"error": "method not allowed"})

        def do_DELETE(self) -> None:  # noqa: N802 - stateless, nothing to end
            if self._allowed():
                self._send(405, {"error": "method not allowed"})

    return Handler


def _healthcheck() -> int:
    """An unauthenticated POST should be refused with 401 or 403: that means it's up.

    Needs no secrets and never reaches the router.
    """
    import http.client
    import os

    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(os.environ.get("MCP_PORT", "8080")), timeout=3)
        conn.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"})
        status = conn.getresponse().status
    except (OSError, ValueError, http.client.HTTPException):
        return 1
    return 0 if status in (401, 403) else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="deco-mcp", description=__doc__)
    parser.add_argument("--check", action="store_true", help="log in once, print the mesh nodes, exit")
    parser.add_argument("--new-token", action="store_true", help="print a random token for MCP_TOKEN, exit")
    parser.add_argument("--healthcheck", action="store_true",
                        help="exit 0 if the server on MCP_PORT is answering (for container health checks)")
    parser.add_argument("--version", action="version", version=__version__)
    opts = parser.parse_args(argv)

    if opts.new_token:
        import secrets

        print(secrets.token_hex(32))
        return
    if opts.healthcheck:
        sys.exit(_healthcheck())

    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    if cfg.problems:
        sys.exit("deco-mcp: " + "; ".join(cfg.problems))
    router = Router(cfg)
    if opts.check:
        from .tools import status

        print(json.dumps(status(router, {})["nodes"], indent=1))
        return
    app = McpApp(cfg, router)
    srv = ThreadingHTTPServer((cfg.bind, cfg.port), make_handler(app))
    log.info(
        "deco-mcp %s on %s:%d, router %s, tools %s, allowed %s",
        __version__, cfg.bind, cfg.port, cfg.host, ",".join(app.tools),
        ",".join(map(str, cfg.allow_nets)),
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
