from __future__ import annotations

import json
import sqlite3
import unittest

from comanagement.api import JsonApplication
from comanagement.service import ComanagementService


class ComanagementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ComanagementService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_header(self) -> None:
        response = self.app.handle(
            "POST", "/organizations", body=json.dumps({"org_id": "o1", "name": "村组", "kind": "village_group"}).encode()
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_organization_and_role_enforcement(self) -> None:
        created = self.app.handle("POST", "/users", {"X-Actor-Id": "s1"}, body=json.dumps({
            "user_id": "s1", "display_name": "站员", "role": "station",
        }).encode())
        self.assertEqual(created.status, 201)
        ok = self.app.handle("POST", "/organizations", {"X-Actor-Id": "s1"},
                             json.dumps({"org_id": "vg-1", "name": "一村", "kind": "village_group"}).encode())
        self.assertEqual(ok.status, 201)
        # 普通村民不能建组织。
        self.app.handle("POST", "/users", {"X-Actor-Id": "s1"}, body=json.dumps({
            "user_id": "v1", "display_name": "村民", "role": "villager", "village_group_id": "vg-1",
        }).encode())
        forbidden = self.app.handle("POST", "/organizations", {"X-Actor-Id": "v1"},
                                   json.dumps({"org_id": "vg-2", "name": "二村", "kind": "village_group"}).encode())
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(forbidden.body["error"]["code"], "forbidden")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "s1"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
