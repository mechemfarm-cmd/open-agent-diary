from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from agent_diary.config import Paths
from agent_diary.service import handlers

RouteFn = Callable[[Paths, dict[str, Any]], dict[str, Any]]


class AgentDiaryHandler(BaseHTTPRequestHandler):
    # Keep imports and normal UI/API reads usable, but reject obviously abusive requests.
    max_body_bytes = 10 * 1024 * 1024

    routes: dict[str, RouteFn] = {
        "/append_entry": handlers.append_entry,
        "/append_work_trace": handlers.append_work_trace_event,
        "/append_overlay": handlers.append_overlay,
        "/attach_artifact": handlers.attach_artifact,
        "/produce_open_loops": handlers.produce_open_loops,
        "/produce_conversation_briefs": handlers.produce_conversation_briefs,
        "/produce_compressed_memory": handlers.produce_compressed_memory,
        "/search_memory": handlers.search_memory,
        "/search_all": handlers.search_all,
        "/search_work_trace": handlers.search_work_trace,
        "/list_imports": handlers.list_imports,
        "/fetch_work_trace": handlers.fetch_work_trace_event,
        "/fetch_raw_entry": handlers.fetch_raw_entry,
        "/list_work_trace": handlers.list_work_trace,
        "/list_entries": handlers.list_entries,
        "/fetch_entry_detail": handlers.fetch_entry_detail,
    }

    ui_root: Path | None = None

    def _same_origin_cors_origin(self) -> str | None:
        """Return the request Origin only when it matches this server origin.

        The browser UI is served by this same process, so normal local use does
        not need permissive CORS. Avoid wildcard CORS because a LAN-exposed
        diary has unauthenticated write routes and contains private memory.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return None
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.netloc != host:
            return None
        return origin

    def _send_cors_headers(self) -> None:
        origin = self._same_origin_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, file_path: Path) -> None:
        if not file_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content = file_path.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(file_path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _resolve_ui_path(self, url_path: str) -> Path | None:
        if self.ui_root is None:
            return None
        # Strip query string
        clean_path = url_path.split("?")[0]
        # Normalise and prevent directory traversal
        clean = Path(clean_path).as_posix().lstrip("/")
        resolved = (self.ui_root / clean).resolve()
        if not str(resolved).startswith(str(self.ui_root.resolve())):
            return None  # traversal attempt
        if resolved.is_dir():
            resolved = resolved / "index.html"
        return resolved

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            self._send_json(HTTPStatus.OK, handlers.status(self.server.paths))
            return
        # Serve UI static files
        file_path = self._resolve_ui_path(self.path)
        if file_path is not None:
            self._send_file(file_path)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        route = self.routes.get(self.path)
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        try:
            body_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": "invalid_content_length", "message": "Content-Length must be an integer"}},
            )
            return
        if body_len < 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": "invalid_content_length", "message": "Content-Length must be non-negative"}},
            )
            return
        if body_len > self.max_body_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "ok": False,
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds {self.max_body_bytes} bytes",
                    },
                },
            )
            return
        raw = self.rfile.read(body_len) if body_len else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": "invalid_json", "message": str(exc)}},
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": "invalid_json", "message": "JSON payload must be an object"}},
            )
            return
        try:
            result = route(self.server.paths, payload)
            self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "not_found", "message": "requested record was not found"}})
        except Exception:  # keep internal paths/details out of HTTP responses
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": {"code": "request_failed", "message": "request could not be processed"}})


class AgentDiaryHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], paths: Paths):
        super().__init__(server_address, AgentDiaryHandler)
        self.paths = paths
        # Point the handler at the UI directory
        ui_dir = paths.root / "ui"
        AgentDiaryHandler.ui_root = ui_dir if ui_dir.is_dir() else None


def run_server(paths: Paths, host: str = "127.0.0.1", port: int = 8041) -> None:
    server = AgentDiaryHTTPServer((host, port), paths)
    print(f"agent-diary service listening on http://{host}:{port}")
    server.serve_forever()