"""无第三方依赖的社区共管任务与补偿核算 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ComanagementError, ValidationFailed
from .service import ComanagementService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class JsonApplication:
    def __init__(self, service: ComanagementService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service

            if method == "POST" and path == "/users":
                return Response(201, service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"], payload.get("village_group_id")))
            if method == "POST" and path == "/organizations":
                return Response(201, service.create_organization(actor, payload))
            if method == "POST" and path == "/service_areas":
                return Response(201, service.create_service_area(actor, payload))
            if method == "POST" and path == "/checkpoints":
                return Response(201, service.create_checkpoint(actor, payload))
            if method == "POST" and path == "/qualifications":
                return Response(201, service.grant_qualification(actor, payload))
            if method == "POST" and path == "/pricing_rules":
                return Response(201, service.publish_pricing_rule(actor, payload))
            if method == "POST" and path == "/tasks":
                return Response(201, service.create_task(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "tasks":
                return Response(200, service.task(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "revisions":
                return Response(200, service.revise_task(
                    actor, parts[1], payload, payload["change_reason"], int(payload["expected_version"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "start":
                return Response(200, service.start_task(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "close":
                return Response(200, service.close_task(actor, parts[1], payload["state"]))
            if method == "POST" and path == "/devices":
                return Response(201, service.register_device(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "devices" and parts[2] == "receipts":
                return Response(202, service.upload_receipts(actor, parts[1], payload.get("items", [])))
            if method == "POST" and path == "/blockades":
                return Response(201, service.register_blockade(actor, payload))
            if method == "POST" and path == "/transfers":
                return Response(201, service.propose_transfer(
                    actor, payload["task_id"], payload["to_org_id"],
                    payload.get("proposed_person_ids", []), payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "transfers" and parts[2] == "decide":
                return Response(200, service.decide_transfer(
                    actor, parts[1], bool(payload["accept"]), payload.get("note", "")))
            if method == "POST" and path == "/periods":
                return Response(201, service.open_period(
                    actor, payload["period_id"], payload["starts_on"], payload["ends_on"]))
            if method == "GET" and len(parts) == 2 and parts[0] == "periods":
                return Response(200, service.period_summary(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "periods" and parts[2] == "compose":
                return Response(200, service.compose_settlement(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "periods" and parts[2] == "confirm":
                return Response(200, service.confirm_scope(
                    actor, parts[1], payload["scope"], payload.get("line_ids"),
                    payload["decision"], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "periods" and parts[2] == "payments":
                return Response(200, service.finalize_payment(actor, parts[1]))
            if method == "GET" and path == "/disputes/open":
                return Response(200, service.recover_open_disputes(actor))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, service.resolve_dispute(
                    actor, int(parts[1]), payload["resolution"],
                    str(payload.get("amount_delta_cny", "0")), payload["reason"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "lines" and parts[2] == "explain":
                return Response(200, service.explain_line(actor, int(parts[1])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ComanagementError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Comanagement/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动社区共管任务与补偿核算服务")
    parser.add_argument("--database", type=Path, default=Path("comanagement.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(ComanagementService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
