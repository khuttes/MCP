import json
import threading
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import bridge


class BridgeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = "test-bridge-token"
        cls.code = "test-one-time-code"
        cls.server = bridge.create_server(0, cls.token, cls.code)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(
        self,
        path: str,
        method: str = "GET",
        body: object | None = None,
        token: str | None = None,
        origin: str | None = None,
        timeout: float = 3,
    ) -> tuple[int, dict[str, str], bytes]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers: dict[str, str] = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, dict(response.headers.items()), response.read()

    def json_request(self, *args: object, **kwargs: object) -> tuple[int, object]:
        status, _, body = self.request(*args, **kwargs)
        return status, json.loads(body) if body else None

    def test_dashboard_and_probe_are_public_but_do_not_expose_credentials(self) -> None:
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<title>Helpyard", body)
        self.assertNotIn(self.code.encode(), body)
        self.assertNotIn(self.token.encode(), body)
        self.assertEqual(self.request("/api/bridge-probe")[0], 204)

    def test_one_time_bootstrap_requires_same_origin_and_cannot_be_replayed(self) -> None:
        wrong_origin, _ = self.json_request(
            "/api/bootstrap",
            "POST",
            {"code": self.code},
            origin="http://localhost:" + str(self.server.server_port),
        )
        self.assertEqual(wrong_origin, 403)
        invalid_code, _ = self.json_request(
            "/api/bootstrap",
            "POST",
            {"code": "wrong"},
            origin=self.base_url,
        )
        self.assertEqual(invalid_code, 401)
        expiry = self.server.bootstrap_expires
        self.server.bootstrap_expires = 0
        try:
            expired, _ = self.json_request(
                "/api/bootstrap",
                "POST",
                {"code": self.code},
                origin=self.base_url,
            )
        finally:
            self.server.bootstrap_expires = expiry
        self.assertEqual(expired, 410)
        status, payload = self.json_request(
            "/api/bootstrap",
            "POST",
            {"code": self.code},
            origin=self.base_url,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"token": self.token})
        replay, _ = self.json_request(
            "/api/bootstrap",
            "POST",
            {"code": self.code},
            origin=self.base_url,
        )
        self.assertEqual(replay, 401)

    def test_authenticated_data_routes_and_authentication(self) -> None:
        self.assertEqual(self.json_request("/api/ping", token=self.token)[0], 200)
        self.assertEqual(self.json_request("/api/ping")[0], 401)
        self.assertEqual(self.json_request("/api/ping", token="wrong")[0], 401)
        with patch("bridge.run_powershell_json", return_value={"ok": True}):
            self.assertEqual(self.json_request("/api/health", token=self.token)[0], 200)
            self.assertEqual(self.json_request("/api/stats", token=self.token)[0], 200)
            self.assertEqual(self.json_request("/api/processes", token=self.token)[0], 200)
            self.assertEqual(self.json_request("/api/services", token=self.token)[0], 200)
        with patch("bridge.get_projects", return_value=[]):
            self.assertEqual(self.json_request("/api/projects", token=self.token), (200, []))
        self.assertEqual(self.json_request("/api/actions", token=self.token)[0], 200)
        self.assertEqual(self.json_request("/api/mcp/status", token=self.token), (200, []))
        with patch("bridge.call_ollama", return_value={"models": []}):
            self.assertEqual(self.json_request("/api/ollama/tags", token=self.token), (200, {"models": []}))

    def test_stats_query_includes_processor_topology(self) -> None:
        with patch("bridge.run_powershell_json", return_value={"cpuCores": 8, "cpuThreads": 16, "cpuMaxMHz": 4200}) as query:
            status, payload = self.json_request("/api/stats", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"cpuCores": 8, "cpuThreads": 16, "cpuMaxMHz": 4200})
        script = query.call_args.args[0]
        self.assertIn("NumberOfCores", script)
        self.assertIn("NumberOfLogicalProcessors", script)
        self.assertIn("MaxClockSpeed", script)

    def test_skills_are_authenticated_validated_and_persisted_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_path = Path(temporary_directory) / "skills.json"
            with patch("bridge.skills_file_path", return_value=skills_path):
                self.assertEqual(self.json_request("/api/skills")[0], 401)
                initial_status, initial_catalog = self.json_request("/api/skills", token=self.token)
                self.assertEqual(initial_status, 200)
                self.assertEqual(initial_catalog["schemaVersion"], 2)
                self.assertEqual(len(initial_catalog["skills"]), len(bridge.BUILTIN_SKILL_DEFINITIONS))
                self.assertTrue(all(skill["builtIn"] and skill["runnerId"] == skill["id"] for skill in initial_catalog["skills"]))
                first_builtin_id = initial_catalog["skills"][0]["id"]
                pin_status, pin_payload = self.json_request(
                    "/api/skills",
                    "POST",
                    {"action": "pin", "id": first_builtin_id, "pinned": True},
                    token=self.token,
                )
                self.assertEqual(pin_status, 200)
                self.assertTrue(pin_payload["skill"]["pinned"])
                invalid, _ = self.json_request(
                    "/api/skills",
                    "POST",
                    {"action": "create", "skill": {"name": "Incomplete"}},
                    token=self.token,
                )
                self.assertEqual(invalid, 400)
                unsafe, _ = self.json_request(
                    "/api/skills",
                    "POST",
                    {
                        "action": "create",
                        "skill": {
                            "name": "Injected operation",
                            "category": "Test",
                            "instructions": "Do not run user-provided commands.",
                            "status": "enabled",
                            "command": "arbitrary command",
                        },
                    },
                    token=self.token,
                )
                self.assertEqual(unsafe, 400)
                created_status, created_payload = self.json_request(
                    "/api/skills",
                    "POST",
                    {
                        "action": "create",
                        "skill": {
                            "name": "Safe review",
                            "category": "Development",
                            "description": "A declarative checklist.",
                            "instructions": "Inspect evidence; do not execute commands.",
                            "status": "draft",
                        },
                    },
                    token=self.token,
                )
                self.assertEqual(created_status, 200)
                created = created_payload["skill"]
                self.assertEqual(created["version"], 1)
                self.assertEqual(created["status"], "draft")
                self.assertEqual(len(self.json_request("/api/skills", token=self.token)[1]["skills"]), len(initial_catalog["skills"]) + 1)
                original_content = skills_path.read_text(encoding="utf-8")
                update_status, update_payload = self.json_request(
                    "/api/skills",
                    "POST",
                    {
                        "action": "update",
                        "skill": {
                            "id": created["id"],
                            "name": "Safe review updated",
                            "category": "Development",
                            "description": "A declarative checklist.",
                            "instructions": "Inspect evidence; do not execute commands.",
                            "status": "enabled",
                        },
                    },
                    token=self.token,
                )
                self.assertEqual(update_status, 200)
                self.assertEqual(update_payload["skill"]["version"], 2)
                backups = list(Path(temporary_directory).glob("skills.json.bak-*"))
                self.assertTrue(backups)
                latest_backup = max(backups, key=lambda path: path.name)
                self.assertEqual(latest_backup.read_text(encoding="utf-8"), original_content)
                self.assertEqual(
                    self.json_request(
                        "/api/skills",
                        "POST",
                        {"action": "delete", "id": created["id"]},
                        token=self.token,
                    )[0],
                    200,
                )
                final_status, final_catalog = self.json_request("/api/skills", token=self.token)
                self.assertEqual(final_status, 200)
                self.assertEqual(final_catalog["schemaVersion"], 2)
                self.assertEqual(len(final_catalog["skills"]), len(initial_catalog["skills"]))
                self.assertFalse(any(skill["id"] == created["id"] for skill in final_catalog["skills"]))

    def test_builtin_skill_run_is_fixed_authenticated_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_path = Path(temporary_directory) / "skills.json"
            ip_skill_id = bridge.BUILTIN_SKILL_DEFINITIONS[0][0]
            self.assertEqual(
                set(bridge.READ_ONLY_SKILL_SCRIPTS),
                {definition[0] for definition in bridge.BUILTIN_SKILL_DEFINITIONS[:11]},
            )
            with patch("bridge.skills_file_path", return_value=skills_path):
                self.assertEqual(
                    self.json_request(
                        "/api/skills",
                        "POST",
                        {"action": "run", "id": ip_skill_id},
                    )[0],
                    401,
                )
                edited_status, edited_payload = self.json_request(
                    "/api/skills",
                    "POST",
                    {
                        "action": "update",
                        "skill": {
                            "id": ip_skill_id,
                            "name": "Renamed network check",
                            "category": "Network",
                            "description": "A user-edited label.",
                            "instructions": "This text cannot change the fixed check.",
                            "status": "enabled",
                        },
                    },
                    token=self.token,
                )
                self.assertEqual(edited_status, 200)
                self.assertEqual(edited_payload["skill"]["runnerId"], ip_skill_id)
                with patch("bridge.run_powershell_json", return_value=[{"IPAddress": "192.0.2.1"}]) as query:
                    status, payload = self.json_request(
                        "/api/skills",
                        "POST",
                        {"action": "run", "id": ip_skill_id},
                        token=self.token,
                    )
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"], [{"IPAddress": "192.0.2.1"}])
                self.assertIn("Get-NetIPAddress", query.call_args.args[0])
                self.assertNotIn("192.0.2.1", query.call_args.args[0])
                for skill_id in (definition[0] for definition in bridge.BUILTIN_SKILL_DEFINITIONS[1:11]):
                    with patch("bridge.run_powershell_json", return_value=[]):
                        check_status, _ = self.json_request(
                            "/api/skills",
                            "POST",
                            {"action": "run", "id": skill_id},
                            token=self.token,
                        )
                    self.assertEqual(check_status, 200)
                with patch("bridge._developer_tool_versions", return_value={"Python": {"available": True, "version": "test"}}):
                    self.assertEqual(
                        self.json_request(
                            "/api/skills",
                            "POST",
                            {"action": "run", "id": bridge.BUILTIN_SKILL_DEFINITIONS[11][0]},
                            token=self.token,
                        )[0],
                        200,
                    )
                with patch("bridge.call_ollama", return_value={"models": []}) as ollama:
                    self.assertEqual(
                        self.json_request(
                            "/api/skills",
                            "POST",
                            {"action": "run", "id": bridge.BUILTIN_SKILL_DEFINITIONS[12][0]},
                            token=self.token,
                        )[0],
                        200,
                    )
                    self.assertEqual(ollama.call_args.args[1], "/api/tags")
                custom_status, custom_payload = self.json_request(
                    "/api/skills",
                    "POST",
                    {
                        "action": "create",
                        "skill": {
                            "name": "Custom guidance",
                            "category": "Test",
                            "instructions": "Never executed.",
                            "status": "enabled",
                        },
                    },
                    token=self.token,
                )
                self.assertEqual(custom_status, 200)
                blocked, _ = self.json_request(
                    "/api/skills",
                    "POST",
                    {"action": "run", "id": custom_payload["skill"]["id"]},
                    token=self.token,
                )
                self.assertEqual(blocked, 403)
                self.assertEqual(len(self.json_request("/api/skills", token=self.token)[1]["skills"]), 14)
                self.assertEqual(
                    self.json_request(
                        "/api/skills",
                        "POST",
                        {"action": "delete", "id": ip_skill_id},
                        token=self.token,
                    )[0],
                    200,
                )
                self.assertEqual(
                    self.json_request(
                        "/api/skills",
                        "POST",
                        {"action": "run", "id": ip_skill_id},
                        token=self.token,
                    )[0],
                    404,
                )

    def test_corrupt_skills_file_is_preserved_and_write_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_path = Path(temporary_directory) / "skills.json"
            corrupt_content = '{"schemaVersion": 1, "skills": ['
            skills_path.write_text(corrupt_content, encoding="utf-8")
            with patch("bridge.skills_file_path", return_value=skills_path):
                status, payload = self.json_request("/api/skills", token=self.token)
                self.assertEqual(status, 500)
                self.assertIn("original file was preserved", payload["error"])
                write_status, _ = self.json_request(
                    "/api/skills",
                    "POST",
                    {
                        "action": "create",
                        "skill": {
                            "name": "Must not replace corrupt data",
                            "category": "Test",
                            "instructions": "Remain declarative.",
                            "status": "draft",
                        },
                    },
                    token=self.token,
                )
                self.assertEqual(write_status, 500)
            self.assertEqual(skills_path.read_text(encoding="utf-8"), corrupt_content)

    def test_skills_schema_v1_migrates_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_path = Path(temporary_directory) / "skills.json"
            legacy = {
                "schemaVersion": 1,
                "skills": [{
                    "id": "12345678-1234-4234-8234-123456789abc",
                    "name": "Existing custom skill",
                    "category": "Legacy",
                    "description": "Keep this record through migration.",
                    "instructions": "Declarative only.",
                    "status": "draft",
                    "createdAt": "2026-10-17T00:00:00+00:00",
                    "updatedAt": "2026-10-17T00:00:00+00:00",
                    "version": 3,
                }],
            }
            legacy_content = json.dumps(legacy)
            skills_path.write_text(legacy_content, encoding="utf-8")
            with patch("bridge.skills_file_path", return_value=skills_path):
                status, catalog = self.json_request("/api/skills", token=self.token)
            self.assertEqual(status, 200)
            self.assertEqual(catalog["schemaVersion"], 2)
            migrated = next(skill for skill in catalog["skills"] if skill["id"] == legacy["skills"][0]["id"])
            self.assertEqual(migrated["version"], 3)
            self.assertFalse(migrated["builtIn"])
            self.assertIsNone(migrated["runnerId"])
            backups = list(Path(temporary_directory).glob("skills.json.bak-*"))
            self.assertTrue(backups)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), legacy_content)

    def test_mutations_require_approval_and_unsafe_routes_remain_disabled(self) -> None:
        cases = [
            ("/api/run", {"command": "must not execute"}, 403),
            ("/api/mcp/start", {}, 403),
            ("/api/action", {"name": "restart", "approved": False}, 403),
            ("/api/process/stop", {"id": 123, "approved": False}, 403),
            ("/api/service", {"name": "Spooler", "action": "stop", "approved": False}, 403),
            ("/api/claude-config", {"content": "{}", "approved": False}, 403),
            ("/api/process/stop", [], 400),
            ("/api/not-a-route", {}, 404),
        ]
        for path, body, expected in cases:
            with self.subTest(path=path, body=body):
                status, _ = self.json_request(
                    path, "POST", body, token=self.token
                )
                self.assertEqual(status, expected)

    def test_rejects_malformed_and_oversized_json(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/api/process/stop",
            data=b"{",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.read()
        raised.exception.close()
        oversized = b" " * (bridge.MAX_REQUEST_BYTES + 1)
        request = urllib.request.Request(
            self.base_url + "/api/process/stop",
            data=oversized,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 413)
        raised.exception.read()
        raised.exception.close()

    def test_slow_ollama_chat_does_not_block_ping(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_chat(*_args: object, **_kwargs: object) -> dict[str, bool]:
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release simulated Ollama chat.")
            return {"ok": True}

        with patch("bridge.call_ollama", side_effect=slow_chat):
            with ThreadPoolExecutor(max_workers=1) as pool:
                chat = pool.submit(
                    self.json_request,
                    "/api/ollama/chat",
                    "POST",
                    {"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                    token=self.token,
                )
                try:
                    self.assertTrue(started.wait(timeout=2))
                    started_at = time.monotonic()
                    status, _ = self.json_request("/api/ping", token=self.token, timeout=2)
                    self.assertEqual(status, 200)
                    self.assertLess(time.monotonic() - started_at, 1)
                finally:
                    release.set()
                self.assertEqual(chat.result(timeout=3), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
