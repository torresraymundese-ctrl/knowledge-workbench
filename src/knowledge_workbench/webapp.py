from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .web_service import WorkbenchActionService, WorkbenchReadService


ASSET_ROOT = Path(__file__).with_name("web_assets")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MAX_REQUEST_BODY = 16 * 1024


@dataclass(frozen=True, slots=True)
class WebResponse:
    status: int
    content_type: str
    body: bytes


class WorkbenchWebApplication:
    def __init__(
        self,
        read_service: WorkbenchReadService,
        action_service: WorkbenchActionService,
        *,
        csrf_token: str | None = None,
    ):
        self.read_service = read_service
        self.action_service = action_service
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)

    def handle(
        self,
        method: str,
        target: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> WebResponse:
        parsed = urlsplit(target)
        if method == "POST":
            return self._handle_post(parsed.path, body, headers or {})
        if method not in {"GET", "HEAD"}:
            return self._json(405, {"error": "Web 工作台不支持该请求方法"})
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
                    {
                        "status": "ok",
                        "mode": "local-controlled-write",
                        "api_version": "v1",
                    },
                )
            if parsed.path == "/api/v1/bootstrap":
                payload = self.read_service.bootstrap()
                payload["web"] = {
                    "csrf_token": self.csrf_token,
                    "write_capabilities": [
                        "evidence-transition",
                        "conflict-transition",
                        "wiki-revision-submit-review",
                        "wiki-revision-reject",
                        "wiki-revision-publish",
                        "conflict-candidate-label",
                        "conflict-candidate-submit",
                        "conflict-candidate-review",
                        "entity-candidate-accept",
                        "entity-candidate-reject",
                        "entity-merge-propose",
                        "entity-merge-review",
                        "entity-relationship-create",
                        "entity-relationship-retract",
                    ],
                }
                return self._json(200, payload)
            if parsed.path == "/api/v1/summary":
                return self._json(200, self.read_service.summary())
            if parsed.path == "/api/v1/documents":
                return self._json(
                    200,
                    self.read_service.documents(
                        limit=_integer_query(query, "limit", 20),
                        offset=_integer_query(query, "offset", 0),
                    ),
                )
            if parsed.path == "/api/v1/review-queue":
                kind = _string_query(query, "kind")
                if kind is not None:
                    return self._json(
                        200,
                        self.read_service.review_queue_page(
                            kind=kind,
                            limit=_integer_query(query, "limit", 5),
                            offset=_integer_query(query, "offset", 0),
                            status=_string_query(query, "status"),
                            classification=_string_query(query, "classification"),
                            query=_string_query(query, "q"),
                        ),
                    )
                return self._json(
                    200,
                    self.read_service.review_queue(
                        limit=_integer_query(query, "limit", 20)
                    ),
                )
            if parsed.path == "/api/v1/wiki-revisions/history":
                return self._json(
                    200,
                    self.read_service.rejected_revision_history(
                        limit=_integer_query(query, "limit", 5),
                        offset=_integer_query(query, "offset", 0),
                        classification=_string_query(query, "classification"),
                        query=_string_query(query, "q"),
                    ),
                )
            if parsed.path == "/api/v1/evaluations":
                return self._json(
                    200,
                    self.read_service.evaluations(
                        limit=_integer_query(query, "limit", 10)
                    ),
                )
            if parsed.path == "/api/v1/activity":
                return self._json(
                    200,
                    self.read_service.activity(
                        limit=_integer_query(query, "limit", 20)
                    ),
                )
            if parsed.path == "/api/v1/conflict-candidate-packs":
                return self._json(200, self.read_service.conflict_candidate_packs())
            if parsed.path == "/api/v1/conflict-labeling-plans":
                return self._json(
                    200,
                    self.read_service.conflict_labeling_plans(
                        source_pack_id=_string_query(
                            query, "source_pack_id"
                        ),
                    ),
                )
            if parsed.path == "/api/v1/graph-pilot-packs":
                return self._json(200, self.read_service.graph_pilot_packs())
            if parsed.path == "/api/v1/entity-candidates":
                return self._json(
                    200,
                    self.read_service.entity_candidate_page(
                        limit=_integer_query(query, "limit", 10),
                        offset=_integer_query(query, "offset", 0),
                        status=_string_query(query, "status") or "pending",
                        query=_string_query(query, "q"),
                    ),
                )
            if parsed.path == "/api/v1/entity-merges":
                return self._json(
                    200,
                    self.read_service.entity_merge_page(
                        limit=_integer_query(query, "limit", 10),
                        offset=_integer_query(query, "offset", 0),
                        query=_string_query(query, "q"),
                    ),
                )
            if parsed.path == "/api/v1/entity-relationships":
                return self._json(
                    200,
                    self.read_service.entity_relationship_page(
                        limit=_integer_query(query, "limit", 10),
                        offset=_integer_query(query, "offset", 0),
                        status=_string_query(query, "status") or "active",
                        query=_string_query(query, "q"),
                    ),
                )
            if parsed.path == "/api/v1/entity-relationships/evidence":
                return self._json(
                    200,
                    self.read_service.entity_relationship_evidence_page(
                        _string_query(query, "source_entity_id") or "",
                        _string_query(query, "target_entity_id") or "",
                        limit=_integer_query(query, "limit", 50),
                        offset=_integer_query(query, "offset", 0),
                        query=_string_query(query, "q"),
                    ),
                )
            candidate_pack_id = _route_entity_id(
                parsed.path, entity="conflict-candidate-packs", action="detail"
            )
            if candidate_pack_id is not None:
                return self._json(
                    200,
                    self.read_service.conflict_candidate_page(
                        candidate_pack_id,
                        limit=_integer_query(query, "limit", 10),
                        offset=_integer_query(query, "offset", 0),
                        state=_string_query(query, "state"),
                        query=_string_query(query, "q"),
                        plan_id=_string_query(query, "plan_id"),
                        batch_id=_string_query(query, "batch_id"),
                    ),
                )
            graph_pilot_pack_id = _route_entity_id(
                parsed.path, entity="graph-pilot-packs", action="detail"
            )
            if graph_pilot_pack_id is not None:
                return self._json(
                    200,
                    self.read_service.graph_pilot_pack_page(
                        graph_pilot_pack_id,
                        limit=_integer_query(query, "limit", 10),
                        offset=_integer_query(query, "offset", 0),
                        status=_string_query(query, "status"),
                        query=_string_query(query, "q"),
                    ),
                )
            evidence_id = _route_entity_id(
                parsed.path, entity="evidence", action="detail"
            )
            if evidence_id is not None:
                return self._json(
                    200, self.read_service.evidence_detail(evidence_id)
                )
            revision_id = _route_entity_id(
                parsed.path, entity="wiki-revisions", action="detail"
            )
            if revision_id is not None:
                return self._json(
                    200, self.read_service.revision_detail(revision_id)
                )
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except KnowledgeWorkbenchError as exc:
            return self._json(404, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "资源不存在"})

    def _handle_post(
        self, path: str, body: bytes, headers: dict[str, str]
    ) -> WebResponse:
        evidence_id = _route_entity_id(
            path, entity="evidence", action="transition"
        )
        conflict_id = _route_entity_id(
            path, entity="conflicts", action="transition"
        )
        revision_submit_id = _route_entity_id(
            path, entity="wiki-revisions", action="submit-review"
        )
        revision_reject_id = _route_entity_id(
            path, entity="wiki-revisions", action="reject"
        )
        revision_publish_id = _route_entity_id(
            path, entity="wiki-revisions", action="publish"
        )
        candidate_label_id = _route_entity_id(
            path, entity="conflict-candidate-packs", action="label"
        )
        candidate_submit_id = _route_entity_id(
            path, entity="conflict-candidate-packs", action="submit"
        )
        candidate_review_id = _route_entity_id(
            path, entity="conflict-candidate-packs", action="review"
        )
        entity_candidate_accept_id = _route_entity_id(
            path, entity="entity-candidates", action="accept"
        )
        entity_candidate_reject_id = _route_entity_id(
            path, entity="entity-candidates", action="reject"
        )
        entity_merge_propose = path == "/api/v1/entity-merges/propose"
        entity_merge_review_id = _route_entity_id(
            path, entity="entity-merges", action="review"
        )
        entity_relationship_create = (
            path == "/api/v1/entity-relationships/create"
        )
        entity_relationship_retract_id = _route_entity_id(
            path, entity="entity-relationships", action="retract"
        )
        if (
            evidence_id is None
            and conflict_id is None
            and revision_submit_id is None
            and revision_reject_id is None
            and revision_publish_id is None
            and candidate_label_id is None
            and candidate_submit_id is None
            and candidate_review_id is None
            and entity_candidate_accept_id is None
            and entity_candidate_reject_id is None
            and not entity_merge_propose
            and entity_merge_review_id is None
            and not entity_relationship_create
            and entity_relationship_retract_id is None
        ):
            return self._json(405, {"error": "该资源不支持 Web 写操作"})
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        content_type = normalized_headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            return self._json(415, {"error": "写操作只接受 application/json"})
        supplied_token = normalized_headers.get("x-workbench-csrf", "")
        if not secrets.compare_digest(supplied_token, self.csrf_token):
            return self._json(403, {"error": "CSRF 校验失败，请刷新页面后重试"})
        origin = normalized_headers.get("origin")
        host = normalized_headers.get("host")
        if origin and host and origin.rstrip("/") != f"http://{host}":
            return self._json(403, {"error": "写操作来源不是当前本地工作台"})
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是 JSON 对象")
            actor_value = payload.get("actor", "")
            if not isinstance(actor_value, str):
                raise ValueError("actor 必须是字符串")
            actor = actor_value
            target = str(payload.get("target", ""))
            if evidence_id is not None:
                result = self.action_service.transition_evidence(
                    evidence_id, target, actor=actor
                )
            elif conflict_id is not None:
                result = self.action_service.transition_conflict(
                    conflict_id or "",
                    target,
                    actor=actor,
                    note=(
                        None
                        if payload.get("note") is None
                        else str(payload.get("note"))
                    ),
                )
            elif revision_submit_id is not None:
                result = self.action_service.submit_revision_review(
                    revision_submit_id, actor=actor
                )
            elif revision_reject_id is not None:
                result = self.action_service.reject_revision_review(
                    revision_reject_id or "",
                    actor=actor,
                    note=str(payload.get("note", "")),
                )
            elif revision_publish_id is not None:
                result = self.action_service.publish_revision_web(
                    revision_publish_id or "",
                    actor=actor,
                    confirmation=str(payload.get("confirmation", "")),
                )
            elif candidate_label_id is not None:
                result = self.action_service.update_conflict_candidate_label(
                    candidate_label_id,
                    str(payload.get("candidate_id", "")),
                    expected_content_sha256=str(
                        payload.get("expected_content_sha256", "")
                    ),
                    expected_conflict=payload.get("expected_conflict"),
                    expected_type=payload.get("expected_type"),
                    note=payload.get("note"),
                    actor=actor,
                )
            elif candidate_submit_id is not None:
                result = self.action_service.submit_conflict_candidate_pack(
                    candidate_submit_id,
                    expected_content_sha256=str(
                        payload.get("expected_content_sha256", "")
                    ),
                    actor=actor,
                )
            elif candidate_review_id is not None:
                result = self.action_service.update_conflict_candidate_review(
                    candidate_review_id or "",
                    str(payload.get("candidate_id", "")),
                    expected_content_sha256=str(
                        payload.get("expected_content_sha256", "")
                    ),
                    decision=str(payload.get("decision", "")),
                    note=payload.get("note"),
                    actor=actor,
                )
            elif entity_candidate_accept_id is not None:
                result = self.action_service.accept_entity_candidate_web(
                    entity_candidate_accept_id,
                    str(payload.get("entity_id", "")),
                    actor=actor,
                    note=(
                        None
                        if payload.get("note") is None
                        else str(payload.get("note"))
                    ),
                )
            elif entity_candidate_reject_id is not None:
                result = self.action_service.reject_entity_candidate_web(
                    entity_candidate_reject_id,
                    actor=actor,
                    note=str(payload.get("note", "")),
                )
            elif entity_merge_propose:
                result = self.action_service.propose_entity_merge_web(
                    str(payload.get("source_entity_id", "")),
                    str(payload.get("target_entity_id", "")),
                    actor=actor,
                    note=str(payload.get("note", "")),
                )
            elif entity_merge_review_id is not None:
                result = self.action_service.review_entity_merge_web(
                    entity_merge_review_id,
                    str(payload.get("decision", "")),
                    actor=actor,
                    note=str(payload.get("note", "")),
                    confirmation=str(payload.get("confirmation", "")),
                )
            elif entity_relationship_create:
                result = self.action_service.create_entity_relationship_web(
                    str(payload.get("relation_key", "")),
                    str(payload.get("source_entity_id", "")),
                    str(payload.get("target_entity_id", "")),
                    payload.get("evidence_ids", []),
                    actor=actor,
                    note=str(payload.get("note", "")),
                    confirmation=str(payload.get("confirmation", "")),
                )
            else:
                result = self.action_service.retract_entity_relationship_web(
                    entity_relationship_retract_id or "",
                    actor=actor,
                    note=str(payload.get("note", "")),
                    confirmation=str(payload.get("confirmation", "")),
                )
            return self._json(200, {"ok": True, "result": result})
        except (UnicodeError, json.JSONDecodeError):
            return self._json(400, {"error": "请求正文不是有效 UTF-8 JSON"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except KnowledgeWorkbenchError as exc:
            return self._json(409, {"error": str(exc)})

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
        if not _allowed_host_header(self.headers.get("Host", "")):
            response = application._json(403, {"error": "Host 不是本地工作台"})
        else:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                response = application._json(400, {"error": "Content-Length 无效"})
            else:
                if content_length < 0 or content_length > MAX_REQUEST_BODY:
                    response = application._json(413, {"error": "请求正文过大"})
                else:
                    body = self.rfile.read(content_length) if content_length else b""
                    response = application.handle(
                        self.command,
                        self.path,
                        body=body,
                        headers={
                            "Content-Type": self.headers.get("Content-Type", ""),
                            "X-Workbench-CSRF": self.headers.get(
                                "X-Workbench-CSRF", ""
                            ),
                            "Origin": self.headers.get("Origin", ""),
                            "Host": self.headers.get("Host", ""),
                        },
                    )
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
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
    actions = WorkbenchActionService(database, paths)
    application = WorkbenchWebApplication(service, actions)
    return WorkbenchHTTPServer((host, port), application)


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
    print("当前开放受控证据审核与冲突处理；按 Ctrl+C 停止。")
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


def _string_query(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    return values[-1]


def _route_entity_id(path: str, *, entity: str, action: str) -> str | None:
    parts = path.split("/")
    if action == "detail":
        if len(parts) == 5 and parts[1:4] == ["api", "v1", entity]:
            return unquote(parts[4]) or None
        return None
    if (
        len(parts) == 6
        and parts[1:4] == ["api", "v1", entity]
        and parts[5] == action
    ):
        return unquote(parts[4]) or None
    return None


def _allowed_host_header(value: str) -> bool:
    host = value.strip().lower()
    if not host:
        return False
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname in {"127.0.0.1", "localhost"}
