from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .web_service import WorkbenchReadService


ASSET_ROOT = Path(__file__).with_name("web_assets")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


@dataclass(frozen=True, slots=True)
class WebResponse:
    status: int
    content_type: str
    body: bytes


class WorkbenchWebApplication:
    def __init__(self, service: WorkbenchReadService):
        self.service = service

    def handle(self, method: str, target: str) -> WebResponse:
        if method not in {"GET", "HEAD"}:
            return self._json(405, {"error": "首版 Web 工作台仅提供只读访问"})
        parsed = urlsplit(target)
        if parsed.path in ASSETS:
            filename, content_type = ASSETS[parsed.path]
            try:
                body = (ASSET_ROOT / filename).read_bytes()
            except OSError:
                return self._json(500, {"error": "Web 静态资源不可用"})
            return WebResponse(200, content_type, body)
        try:
            query = parse_qs(parsed.query)
            if parsed.path == "/api/v1/health":
                return self._json(
                    200,
                    {"status": "ok", "mode": "local-read-only", "api_version": "v1"},
                )
            if parsed.path == "/api/v1/bootstrap":
                return self._json(200, self.service.bootstrap())
            if parsed.path == "/api/v1/summary":
                return self._json(200, self.service.summary())
            if parsed.path == "/api/v1/documents":
                return self._json(
                    200,
                    self.service.documents(
                        limit=_integer_query(query, "limit", 20),
                        offset=_integer_query(query, "offset", 0),
                    ),
                )
            if parsed.path == "/api/v1/review-queue":
                return self._json(
                    200,
                    self.service.review_queue(
                        limit=_integer_query(query, "limit", 20)
                    ),
                )
            if parsed.path == "/api/v1/evaluations":
                return self._json(
                    200,
                    self.service.evaluations(
                        limit=_integer_query(query, "limit", 10)
                    ),
                )
            if parsed.path == "/api/v1/activity":
                return self._json(
                    200,
                    self.service.activity(limit=_integer_query(query, "limit", 20)),
                )
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "资源不存在"})

    @staticmethod
    def _json(status: int, payload: object) -> WebResponse:
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return WebResponse(status, "application/json; charset=utf-8", body)


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server_version = "KnowledgeWorkbench/0.1"

    def do_GET(self) -> None:
        self._respond()

    def do_HEAD(self) -> None:
        self._respond(head_only=True)

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()

    def do_PATCH(self) -> None:
        self._respond()

    def do_DELETE(self) -> None:
        self._respond()

    def _respond(self, *, head_only: bool = False) -> None:
        application = self.server.application  # type: ignore[attr-defined]
        response = application.handle(self.command, self.path)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if not head_only:
            self.wfile.write(response.body)

    def log_message(self, format: str, *args) -> None:
        return


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: WorkbenchWebApplication):
        super().__init__(server_address, WorkbenchRequestHandler)
        self.application = application


def build_web_server(
    database: Database,
    paths: WorkspacePaths,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> WorkbenchHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise KnowledgeWorkbenchError(
            "首版 Web 工作台只能绑定 127.0.0.1 或 localhost"
        )
    if port < 0 or port > 65535:
        raise KnowledgeWorkbenchError("Web 端口必须在 0 到 65535 之间")
    service = WorkbenchReadService(database, paths)
    return WorkbenchHTTPServer((host, port), WorkbenchWebApplication(service))


def serve_web(
    database: Database,
    paths: WorkspacePaths,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = build_web_server(database, paths, host=host, port=port)
    actual_host, actual_port = server.server_address[:2]
    print(f"本地 Web 工作台：http://{actual_host}:{actual_port}")
    print("当前为只读模式；按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _integer_query(query: dict[str, list[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        return int(values[-1])
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
