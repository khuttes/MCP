import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
