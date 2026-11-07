import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.process = None
        self.start()

    def start(self):
        env = {**os.environ, "PORT": "0", "DATA_DIR": self.directory.name,
               "ACCESS_PASSWORD": "test-password"}
        self.process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT,
                                        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = self.process.stdout.readline()
        self.assertIn("listening on port", line)
        self.base = "http://127.0.0.1:" + line.split()[-1]

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process = None

    def tearDown(self):
        self.stop()
        self.directory.cleanup()

    def call(self, path="/api/tickets", method="GET", data=None, auth=True, headers=None, raw=None):
        request_headers = {"Content-Type": "application/json"}
        if auth:
            request_headers["Authorization"] = "Bearer test-password"
        request_headers.update(headers or {})
        payload = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        request = Request(self.base + path, data=payload, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body, response.headers

    def ticket(self, kind="Issue"):
        return {"title": "Test " + kind, "description": "Details <script>alert(1)</script>",
                "requester": "Sam", "service": "Network", "priority": "High", "type": kind}

    def test_full_workflow_and_restart_persistence(self):
        self.assertEqual(self.call(auth=False)[0], 401)
        self.assertEqual(self.call(headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.call(method="POST", data={})[0], 400)
        created = []
        for kind in ("Issue", "Request", "Change"):
            status, ticket, _ = self.call(method="POST", data=self.ticket(kind))
            self.assertEqual(status, 201)
            self.assertEqual(ticket["type"], kind)
            created.append(ticket)
        path = "/api/tickets/" + str(created[0]["id"])
        self.assertEqual(len(self.call()[1]), 3)
        self.assertEqual(self.call(path, "PATCH", {"status": "Invalid", "priority": "High"})[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"status": "Resolved", "priority": "Low"})[0], 200)
        self.assertEqual(self.call(path + "/comments", "POST", {"author": "Sam", "body": "Fixed the switch"})[0], 201)
        self.assertEqual(self.call("/api/tickets/9999")[0], 404)
        self.assertEqual(self.call(method="POST", data=self.ticket(), headers={"Origin": "https://evil.example"})[0], 403)
        self.stop()
        self.start()
        saved = self.call(path)[1]
        self.assertEqual(saved["status"], "Resolved")
        self.assertEqual(saved["priority"], "Low")
        self.assertEqual(saved["comments"][0]["body"], "Fixed the switch")
        self.assertEqual(len(self.call()[1]), 3)
        self.assertEqual(self.call(path, "PATCH", {"status": "Open", "priority": "Medium"})[0], 200)

    def test_bad_inputs_and_static_files(self):
        for data in ([], None, "oops", {**self.ticket(), "title": " "}, {**self.ticket(), "priority": {}}):
            self.assertEqual(self.call(method="POST", raw=json.dumps(data).encode())[0], 400)
        self.assertEqual(self.call(method="POST", raw=b"{")[0], 400)
        self.assertEqual(self.call(method="POST", raw=b"x" * 24001)[0], 413)
        self.assertEqual(self.call(method="DELETE")[0], 405)
        self.assertEqual(self.call("/../server.py")[0], 404)
        for path, expected in (("/", b"dark_triage"), ("/app.js", b"/api/tickets"), ("/style.css", b":root")):
            status, body, headers = self.call(path)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.call("/", "HEAD")[1], b"")
        self.assertEqual(self.call("/", "POST", {})[0], 405)

    def test_existing_database_is_preserved(self):
        self.stop()
        # Insert a record in the original schema, with the original ISO timestamp format.
        with sqlite3.connect(Path(self.directory.name) / "triage.db") as db:
            db.execute("INSERT INTO tickets(id,title,description,type,priority,service,requester,status,created,updated) VALUES(42,'Legacy','Keep me','Change','Low','Storage','Alex','Waiting','2026-11-07T12:00:00.000Z','2026-11-07T12:00:00.000Z')")
            db.execute("INSERT INTO comments(ticket_id,author,body,created) VALUES(42,'Alex','Existing update','2026-11-07T12:00:00.000Z')")
        self.start()
        ticket = self.call("/api/tickets/42")[1]
        self.assertEqual(ticket["title"], "Legacy")
        self.assertEqual(ticket["comments"][0]["body"], "Existing update")
        self.assertGreater(self.call(method="POST", data=self.ticket())[1]["id"], 42)

    def test_password_required_by_default(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ACCESS_PASSWORD", "DEV_MODE")}
        result = subprocess.run([sys.executable, "server.py"], cwd=ROOT, env=env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Set ACCESS_PASSWORD", result.stderr)


if __name__ == "__main__":
    unittest.main()
