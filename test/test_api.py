from contextlib import closing
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
ADMIN_PASSWORD = "bootstrap-admin-pw"

# The schema of the shared-password version, used to test in-place migration.
OLD_SCHEMA = """
CREATE TABLE tickets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
 description TEXT NOT NULL, type TEXT NOT NULL, priority TEXT NOT NULL,
 service TEXT NOT NULL, requester TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'Open', created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE comments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_id INTEGER NOT NULL REFERENCES tickets(id), author TEXT NOT NULL,
 body TEXT NOT NULL, created TEXT NOT NULL
);
"""


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.process = None
        self.start()
        self.admin = self.login("admin", ADMIN_PASSWORD)

    def start(self, **extra_env):
        env = {k: v for k, v in os.environ.items() if k not in ("ACCESS_PASSWORD", "DEV_MODE", "ADMIN_USERNAME", "TRUST_PROXY")}
        env.update({"PORT": "0", "DATA_DIR": self.directory.name, "ADMIN_PASSWORD": ADMIN_PASSWORD}, **extra_env)
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

    def restart(self, **extra_env):
        self.stop()
        self.start(**extra_env)

    def tearDown(self):
        self.stop()
        self.directory.cleanup()

    def call(self, path="/api/tickets", method="GET", data=None, token=None, headers=None, raw=None):
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        payload = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        request = Request(self.base + path, data=payload, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body, response.headers

    def login(self, username, password, expect=200):
        status, data, _ = self.call("/api/login", "POST", {"username": username, "password": password})
        self.assertEqual(status, expect, data)
        return data.get("token") if status == 200 else data

    def create_user(self, username, password="temp-password-1", role="user", display_name=None):
        status, user, _ = self.call("/api/users", "POST", {"username": username, "password": password, "role": role,
                                                           "display_name": display_name or username.title()}, self.admin)
        self.assertEqual(status, 201, user)
        return user

    def activate(self, username, temp="temp-password-1", new="chosen-password-1", **kwargs):
        """Create a user, log in with the temporary password and set a real one."""
        user = self.create_user(username, temp, **kwargs)
        token = self.login(username, temp)
        status, data, _ = self.call("/api/me/password", "POST", {"current_password": temp, "new_password": new}, token)
        self.assertEqual(status, 200, data)
        return user, token

    def ticket(self, kind="Issue"):
        return {"title": "Test " + kind, "description": "Details <script>alert(1)</script>",
                "service": "Network", "priority": "High", "type": kind}

    # --- accounts ------------------------------------------------------------

    def test_bootstrap_admin_login_and_logout(self):
        status, me, _ = self.call("/api/me", token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual((me["username"], me["role"], me["must_change"]), ("admin", "admin", False))
        self.assertNotIn("password_hash", me)
        # Usernames are case-insensitive; each login is its own session.
        second = self.login("ADMIN", ADMIN_PASSWORD)
        self.assertNotEqual(second, self.admin)
        self.assertEqual(self.call("/api/logout", "POST", {}, second)[0], 200)
        self.assertEqual(self.call("/api/me", token=second)[0], 401)
        self.assertEqual(self.call("/api/me", token=self.admin)[0], 200)
        # Tokens are stored hashed, never in plain text.
        with closing(sqlite3.connect(Path(self.directory.name) / "triage.db")) as db:
            stored = [row[0] for row in db.execute("SELECT token_hash FROM sessions")]
            password_hash = db.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn(self.admin, stored)
        self.assertTrue(password_hash.startswith("scrypt$"))
        self.assertNotIn(ADMIN_PASSWORD, password_hash)

    def test_unauthenticated_and_wrong_password(self):
        self.assertEqual(self.call()[0], 401)
        self.assertEqual(self.call(token="not-a-real-token")[0], 401)
        self.assertEqual(self.call(headers={"Authorization": "Bearer " + ADMIN_PASSWORD})[0], 401)
        self.login("admin", "wrong-password", expect=401)
        self.login("nobody", "wrong-password", expect=401)
        self.login("", "", expect=400)
        self.assertEqual(self.call("/api/login", "GET")[0], 405)
        self.assertEqual(self.call("/api/users", token=None)[0], 401)

    def test_login_throttling(self):
        self.activate("sam")
        for _ in range(5):
            self.login("sam", "nope-nope-nope", expect=401)
        status, data, headers = self.call("/api/login", "POST", {"username": "sam", "password": "chosen-password-1"})
        self.assertEqual(status, 429, data)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        # Throttling is per username: other accounts still work.
        self.login("admin", ADMIN_PASSWORD)

    def test_temporary_password_forces_change(self):
        user = self.create_user("sam", "temp-password-1", display_name="Sam Carter")
        self.assertTrue(user["must_change"])
        token = self.login("sam", "temp-password-1")
        me = self.call("/api/me", token=token)[1]
        self.assertTrue(me["must_change"])
        status, data, _ = self.call(token=token)
        self.assertEqual(status, 403)
        self.assertEqual(data["code"], "password_change_required")
        self.assertEqual(self.call(method="POST", data=self.ticket(), token=token)[0], 403)
        change = lambda current, new: self.call("/api/me/password", "POST", {"current_password": current, "new_password": new}, token)[0]
        self.assertEqual(change("wrong-current", "chosen-password-1"), 400)
        self.assertEqual(change("temp-password-1", "short"), 400)
        self.assertEqual(change("temp-password-1", "temp-password-1"), 400)
        self.assertEqual(change("temp-password-1", "chosen-password-1"), 200)
        self.assertFalse(self.call("/api/me", token=token)[1]["must_change"])
        self.assertEqual(self.call(token=token)[0], 200)
        self.login("sam", "temp-password-1", expect=401)
        self.login("sam", "chosen-password-1")

    def test_password_change_signs_out_other_sessions(self):
        _, token = self.activate("sam")
        other = self.login("sam", "chosen-password-1")
        status = self.call("/api/me/password", "POST", {"current_password": "chosen-password-1", "new_password": "another-password-2"}, token)[0]
        self.assertEqual(status, 200)
        self.assertEqual(self.call("/api/me", token=token)[0], 200)
        self.assertEqual(self.call("/api/me", token=other)[0], 401)

    def test_admin_manages_users(self):
        # Validation.
        bad = [{"username": "x", "display_name": "X", "role": "user", "password": "long-enough-1"},
               {"username": "bad name", "display_name": "X", "role": "user", "password": "long-enough-1"},
               {"username": "okname", "display_name": " ", "role": "user", "password": "long-enough-1"},
               {"username": "okname", "display_name": "Ok", "role": "root", "password": "long-enough-1"},
               {"username": "okname", "display_name": "Ok", "role": "user", "password": "short"},
               {"username": "okname", "display_name": "Ok", "role": "user", "password": "okname"}]
        for data in bad:
            self.assertEqual(self.call("/api/users", "POST", data, self.admin)[0], 400, data)
        user, token = self.activate("sam", display_name="Sam Carter")
        self.assertEqual(self.call("/api/users", "POST", {"username": "SAM", "display_name": "Dup", "role": "user", "password": "long-enough-1"}, self.admin)[0], 409)
        users = self.call("/api/users", token=self.admin)[1]
        self.assertEqual([u["username"] for u in users], ["admin", "sam"])
        path = f"/api/users/{user['id']}"
        # Reset password: temporary again, existing sessions revoked.
        self.assertEqual(self.call(path + "/password", "POST", {"password": "reset-password-1"}, self.admin)[0], 200)
        self.assertEqual(self.call("/api/me", token=token)[0], 401)
        self.login("sam", "chosen-password-1", expect=401)
        token = self.login("sam", "reset-password-1")
        self.assertTrue(self.call("/api/me", token=token)[1]["must_change"])
        self.assertEqual(self.call(path + "/password", "POST", {"password": "x"}, self.admin)[0], 400)
        # Role changes and display name.
        status, changed, _ = self.call(path, "PATCH", {"role": "admin", "display_name": "Sam C."}, self.admin)
        self.assertEqual(status, 200)
        self.assertEqual((changed["role"], changed["display_name"]), ("admin", "Sam C."))
        self.assertEqual(self.call(path, "PATCH", {"role": "superuser"}, self.admin)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"role": "user"}, self.admin)[1]["role"], "user")
        self.assertEqual(self.call("/api/users/9999", "PATCH", {"role": "user"}, self.admin)[0], 404)

    def test_disable_revokes_sessions_and_blocks_login(self):
        user, token = self.activate("sam")
        path = f"/api/users/{user['id']}"
        self.assertEqual(self.call(token=token)[0], 200)
        status, data, _ = self.call(path, "PATCH", {"disabled": True}, self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["disabled"])
        self.assertEqual(self.call(token=token)[0], 401)
        self.assertIn("disabled", self.login("sam", "chosen-password-1", expect=403)["error"])
        self.assertEqual(self.call(path, "PATCH", {"disabled": "yes"}, self.admin)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"disabled": False}, self.admin)[0], 200)
        token = self.login("sam", "chosen-password-1")
        self.assertEqual(self.call(token=token)[0], 200)

    def test_non_admin_cannot_manage_users(self):
        user, token = self.activate("sam")
        admin_id = self.call("/api/me", token=self.admin)[1]["id"]
        for path, method, data in (("/api/users", "GET", None),
                                   ("/api/users", "POST", {"username": "eve", "display_name": "Eve", "role": "admin", "password": "long-enough-1"}),
                                   (f"/api/users/{user['id']}", "PATCH", {"role": "admin"}),
                                   (f"/api/users/{admin_id}", "PATCH", {"disabled": True}),
                                   (f"/api/users/{admin_id}/password", "POST", {"password": "hijacked-pass-1"})):
            status, body, _ = self.call(path, method, data, token)
            self.assertEqual(status, 403, (path, method, body))
        self.assertEqual(self.call("/api/me", token=token)[1]["role"], "user")
        self.login("admin", ADMIN_PASSWORD)

    def test_last_admin_protection(self):
        admin_id = self.call("/api/me", token=self.admin)[1]["id"]
        path = f"/api/users/{admin_id}"
        self.assertEqual(self.call(path, "PATCH", {"role": "user"}, self.admin)[0], 409)
        self.assertEqual(self.call(path, "PATCH", {"disabled": True}, self.admin)[0], 400)  # never yourself
        second, second_token = self.activate("ops", role="admin")
        # With another active admin, demotion is allowed...
        self.assertEqual(self.call(path, "PATCH", {"role": "user"}, self.admin)[0], 200)
        # ...after which "ops" is the last admin and can't be demoted or disabled.
        self.assertEqual(self.call(f"/api/users/{second['id']}", "PATCH", {"role": "user"}, second_token)[0], 409)
        self.assertEqual(self.call(path, "PATCH", {"role": "admin"}, second_token)[0], 200)
        self.assertEqual(self.call(f"/api/users/{second['id']}", "PATCH", {"disabled": True}, self.admin)[0], 200)
        self.assertEqual(self.call(path, "PATCH", {"role": "user"}, self.admin)[0], 409)
        # A disabled admin doesn't count as active.
        users = {u["username"]: u for u in self.call("/api/users", token=self.admin)[1]}
        self.assertTrue(users["ops"]["disabled"])

    # --- tickets -------------------------------------------------------------

    def test_users_share_tickets_and_restart_persistence(self):
        _, sam = self.activate("sam", display_name="Sam Carter")
        _, alex = self.activate("alex", display_name="Alex Doe")
        self.assertEqual(self.call(method="POST", data={}, token=sam)[0], 400)
        created = []
        for kind in ("Issue", "Request", "Change"):
            status, ticket, _ = self.call(method="POST", data={**self.ticket(kind), "requester": "Spoofed"}, token=sam)
            self.assertEqual(status, 201)
            self.assertEqual(ticket["type"], kind)
            self.assertEqual(ticket["requester"], "Sam Carter")
            created.append(ticket)
        path = "/api/tickets/" + str(created[0]["id"])
        # Everyone sees all tickets and can change any of them.
        self.assertEqual(len(self.call(token=alex)[1]), 3)
        self.assertEqual(len(self.call(token=self.admin)[1]), 3)
        self.assertEqual(self.call(path, "PATCH", {"status": "Invalid", "priority": "High"}, alex)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"status": "Resolved", "priority": "Low"}, alex)[0], 200)
        self.assertEqual(self.call(path + "/comments", "POST", {"body": "Fixed the switch", "author": "Spoofed"}, alex)[0], 201)
        self.assertEqual(self.call(path + "/comments", "POST", {"body": " "}, alex)[0], 400)
        self.assertEqual(self.call(path + "/comments", "POST", {"body": "Thanks!"}, sam)[0], 201)
        self.assertEqual(self.call("/api/tickets/9999", token=sam)[0], 404)
        self.assertEqual(self.call(method="POST", data=self.ticket(), token=sam, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("/api/login", "POST", {"username": "sam", "password": "chosen-password-1"}, headers={"Origin": "https://evil.example"})[0], 403)
        self.restart()
        # Sessions and data survive a restart.
        saved = self.call(path, token=sam)[1]
        self.assertEqual(saved["status"], "Resolved")
        self.assertEqual(saved["priority"], "Low")
        self.assertEqual([(c["author"], c["body"]) for c in saved["comments"]], [("Alex Doe", "Fixed the switch"), ("Sam Carter", "Thanks!")])
        self.assertIsInstance(saved["comments"][0]["author_id"], int)
        self.assertEqual(len(self.call(token=alex)[1]), 3)
        self.assertEqual(self.call(path, "PATCH", {"status": "Open", "priority": "Medium"}, sam)[0], 200)
        # ADMIN_PASSWORD is ignored once accounts exist.
        self.restart(ADMIN_PASSWORD="a-different-password")
        self.login("admin", "a-different-password", expect=401)
        self.login("admin", ADMIN_PASSWORD)

    def test_display_name_changes_follow_the_account(self):
        user, token = self.activate("sam", display_name="Sam")
        ticket = self.call(method="POST", data=self.ticket(), token=token)[1]
        self.call(f"/api/users/{user['id']}", "PATCH", {"display_name": "Samantha"}, self.admin)
        self.assertEqual(self.call(f"/api/tickets/{ticket['id']}", token=token)[1]["requester"], "Samantha")

    def test_bad_inputs_and_static_files(self):
        for data in ([], None, "oops", {**self.ticket(), "title": " "}, {**self.ticket(), "priority": {}}):
            self.assertEqual(self.call(method="POST", raw=json.dumps(data).encode(), token=self.admin)[0], 400)
        self.assertEqual(self.call(method="POST", raw=b"{", token=self.admin)[0], 400)
        self.assertEqual(self.call(method="POST", raw=b"x" * 24001, token=self.admin)[0], 413)
        self.assertEqual(self.call(method="DELETE", token=self.admin)[0], 405)
        self.assertEqual(self.call("/../server.py")[0], 404)
        for path, expected in (("/", b"dark_triage"), ("/app.js", b"'/login'"), ("/style.css", b":root")):
            status, body, headers = self.call(path)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.call("/", "HEAD")[1], b"")
        self.assertEqual(self.call("/", "POST", {})[0], 405)

    # --- startup and migration -------------------------------------------------

    def test_migrates_old_schema_database(self):
        self.stop()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory, original = directory, self.directory
        self.addCleanup(original.cleanup)
        with closing(sqlite3.connect(Path(directory.name) / "triage.db")) as db, db:
            db.executescript(OLD_SCHEMA)
            db.execute("INSERT INTO tickets(id,title,description,type,priority,service,requester,status,created,updated) VALUES(42,'Legacy','Keep me','Change','Low','Storage','Alex','Waiting','2026-11-07T12:00:00.000Z','2026-11-07T12:00:00.000Z')")
            db.execute("INSERT INTO comments(ticket_id,author,body,created) VALUES(42,'Alex','Existing update','2026-11-07T12:00:00.000Z')")
        self.start(ACCESS_PASSWORD="old-shared-password")  # deprecated and ignored
        self.assertEqual(self.call(headers={"Authorization": "Bearer old-shared-password"})[0], 401)
        token = self.login("admin", ADMIN_PASSWORD)
        ticket = self.call("/api/tickets/42", token=token)[1]
        self.assertEqual((ticket["title"], ticket["requester"], ticket["requester_id"]), ("Legacy", "Alex", None))
        self.assertEqual((ticket["comments"][0]["author"], ticket["comments"][0]["body"]), ("Alex", "Existing update"))
        new = self.call(method="POST", data=self.ticket(), token=token)[1]
        self.assertGreater(new["id"], 42)
        self.assertEqual(new["requester"], "admin")
        self.assertEqual(self.call("/api/tickets/42/comments", "POST", {"body": "Still here"}, token)[0], 201)
        self.restart()  # running the migration again is a no-op
        self.assertEqual(len(self.call("/api/tickets/42", token=token)[1]["comments"]), 2)

    def test_admin_password_required_on_first_start(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ACCESS_PASSWORD", "ADMIN_PASSWORD", "DEV_MODE")}
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run([sys.executable, "server.py"], cwd=ROOT, env={**env, "DATA_DIR": empty, "PORT": "0"},
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Set ADMIN_PASSWORD", result.stderr)
            short = subprocess.run([sys.executable, "server.py"], cwd=ROOT, env={**env, "DATA_DIR": empty, "PORT": "0", "ADMIN_PASSWORD": "short"},
                                   capture_output=True, timeout=10)
            self.assertNotEqual(short.returncode, 0)
            self.assertFalse((Path(empty) / "triage.db").exists() and self.count_users(empty))
        # Once an account exists, ADMIN_PASSWORD is no longer needed.
        self.restart(ADMIN_PASSWORD="")
        self.login("admin", ADMIN_PASSWORD)

    def test_custom_admin_username_and_dev_mode(self):
        with tempfile.TemporaryDirectory() as empty:
            env = {k: v for k, v in os.environ.items() if k not in ("ACCESS_PASSWORD", "ADMIN_PASSWORD")}
            process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env={**env, "DATA_DIR": empty, "PORT": "0", "DEV_MODE": "1", "ADMIN_USERNAME": "Root.Ops"})
            try:
                self.assertIn("listening on port", process.stdout.readline())
            finally:
                process.terminate()
                _, stderr = process.communicate(timeout=5)
            self.assertIn("generated password", stderr)
            with closing(sqlite3.connect(Path(empty) / "triage.db")) as db:
                self.assertEqual(db.execute("SELECT username,role,must_change FROM users").fetchall(), [("root.ops", "admin", 0)])

    @staticmethod
    def count_users(directory):
        with closing(sqlite3.connect(Path(directory) / "triage.db")) as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
