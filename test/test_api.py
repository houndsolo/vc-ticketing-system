from contextlib import closing
import json
import os
from pathlib import Path
import re
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
        for path, expected in (("/", b"dark-triage"), ("/app.js", b"'/login'"), ("/style.css", b":root")):
            status, body, headers = self.call(path)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.call("/", "HEAD")[1], b"")
        self.assertEqual(self.call("/", "POST", {})[0], 405)

    # --- assignment ------------------------------------------------------------

    def test_assign_on_create_reassign_and_unassign(self):
        sam_user, sam = self.activate("sam", display_name="Sam Carter")
        alex_user, alex = self.activate("alex", display_name="Alex Doe")
        status, ticket, _ = self.call(method="POST", data={**self.ticket(), "assignee_id": alex_user["id"]}, token=sam)
        self.assertEqual(status, 201, ticket)
        self.assertEqual(ticket["assignee"], {"id": alex_user["id"], "username": "alex", "display_name": "Alex Doe", "disabled": False,
                                              "color": alex_user["color"]})
        unassigned = self.call(method="POST", data=self.ticket(), token=sam)[1]
        self.assertIsNone(unassigned["assignee"])
        self.assertIsNone(self.call(method="POST", data={**self.ticket(), "assignee_id": None}, token=sam)[1]["assignee"])
        path = f"/api/tickets/{ticket['id']}"
        # A non-admin reassigns; status and priority are untouched by a partial PATCH.
        status, changed, _ = self.call(path, "PATCH", {"assignee_id": sam_user["id"]}, alex)
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["assignee"]["username"], "sam")
        self.assertEqual((changed["status"], changed["priority"]), ("Open", "High"))
        # Re-sending the same assignee records nothing new.
        self.assertEqual(self.call(path, "PATCH", {"assignee_id": sam_user["id"], "status": "In progress"}, sam)[0], 200)
        status, changed, _ = self.call(path, "PATCH", {"assignee_id": None}, sam)
        self.assertEqual(status, 200)
        self.assertIsNone(changed["assignee"])
        self.assertEqual(changed["status"], "In progress")
        # The old full PATCH shape still works and leaves the assignee alone.
        self.call(path, "PATCH", {"assignee_id": alex_user["id"]}, self.admin)
        self.assertEqual(self.call(path, "PATCH", {"status": "Waiting", "priority": "Low"}, sam)[1]["assignee"]["id"], alex_user["id"])
        activity = [(c["kind"], c["author"], c["body"]) for c in self.call(path, token=sam)[1]["comments"]]
        self.assertEqual(activity, [("system", "Sam Carter", "assigned this to Alex Doe"),
                                    ("system", "Alex Doe", "assigned this to Sam Carter"),
                                    ("system", "Sam Carter", "unassigned Sam Carter"),
                                    ("system", "admin", "assigned this to Alex Doe")])
        # Posted updates are kind "comment"; system lines can't be forged through the comments API.
        self.call(path + "/comments", "POST", {"body": "On it", "kind": "system"}, alex)
        self.assertEqual(self.call(path, token=sam)[1]["comments"][-1]["kind"], "comment")
        for bad in ({}, {"assignee_id": "2"}, {"assignee_id": True}, {"assignee_id": 1.5}, {"status": "Nope"}):
            self.assertEqual(self.call(path, "PATCH", bad, sam)[0], 400, bad)

    def test_assignee_filter(self):
        sam_user, sam = self.activate("sam")
        alex_user, _ = self.activate("alex")
        mine = self.call(method="POST", data={**self.ticket(), "assignee_id": sam_user["id"]}, token=sam)[1]
        theirs = self.call(method="POST", data={**self.ticket("Request"), "assignee_id": alex_user["id"]}, token=sam)[1]
        nobody = self.call(method="POST", data=self.ticket("Change"), token=sam)[1]
        ids = lambda query, token=sam: [t["id"] for t in self.call("/api/tickets" + query, token=token)[1]]
        self.assertEqual(ids("?assignee=me"), [mine["id"]])
        self.assertEqual(ids("?assignee=me", self.admin), [])
        self.assertEqual(ids("?assignee=none"), [nobody["id"]])
        self.assertEqual(ids(f"?assignee={alex_user['id']}"), [theirs["id"]])
        self.assertEqual(ids("?assignee=9999"), [])
        self.assertEqual(sorted(ids("")), sorted([mine["id"], theirs["id"], nobody["id"]]))
        self.assertEqual(self.call("/api/tickets?assignee=bob", token=sam)[0], 400)

    def test_disabled_and_nonexistent_assignees(self):
        sam_user, sam = self.activate("sam", display_name="Sam Carter")
        alex_user, alex = self.activate("alex", display_name="Alex Doe")
        self.assertEqual(self.call(method="POST", data={**self.ticket(), "assignee_id": 9999}, token=sam)[0], 400)
        ticket = self.call(method="POST", data={**self.ticket(), "assignee_id": alex_user["id"]}, token=sam)[1]
        path = f"/api/tickets/{ticket['id']}"
        self.assertEqual(self.call(path, "PATCH", {"assignee_id": 9999}, sam)[0], 400)
        self.call(f"/api/users/{alex_user['id']}", "PATCH", {"disabled": True}, self.admin)
        # The existing assignment is kept and shown as disabled.
        kept = self.call(path, token=sam)[1]["assignee"]
        self.assertEqual((kept["id"], kept["disabled"]), (alex_user["id"], True))
        self.assertTrue(self.call("/api/tickets?assignee=" + str(alex_user["id"]), token=sam)[1][0]["assignee"]["disabled"])
        # Saving the ticket with its current (disabled) assignee is fine...
        self.assertEqual(self.call(path, "PATCH", {"assignee_id": alex_user["id"], "status": "Waiting"}, sam)[0], 200)
        # ...but a disabled account can't be newly assigned.
        other = self.call(method="POST", data=self.ticket(), token=sam)[1]
        self.assertEqual(self.call(f"/api/tickets/{other['id']}", "PATCH", {"assignee_id": alex_user["id"]}, sam)[0], 400)
        self.assertEqual(self.call(method="POST", data={**self.ticket(), "assignee_id": alex_user["id"]}, token=sam)[0], 400)
        # Moving away from the disabled account works; moving back doesn't.
        self.assertEqual(self.call(path, "PATCH", {"assignee_id": sam_user["id"]}, sam)[0], 200)
        self.assertEqual(self.call(path, "PATCH", {"assignee_id": alex_user["id"]}, sam)[0], 400)

    def test_assignable_users_visible_to_everyone(self):
        _, sam = self.activate("sam", display_name="Sam Carter")
        alex_user, _ = self.activate("alex", display_name="Alex Doe")
        self.call(f"/api/users/{alex_user['id']}", "PATCH", {"disabled": True}, self.admin)
        status, people, _ = self.call("/api/users/assignable", token=sam)
        self.assertEqual(status, 200)
        self.assertEqual([p["username"] for p in people], ["admin", "sam"])
        self.assertEqual(set(people[0]), {"id", "username", "display_name", "color"})
        self.assertEqual(self.call("/api/users", token=sam)[0], 403)
        self.assertEqual(self.call("/api/users/assignable", token=None)[0], 401)
        self.assertEqual(self.call("/api/users/assignable", "POST", {}, sam)[0], 405)
        # Admin user management is unaffected by the new route.
        self.assertEqual(self.call("/api/users/assignable", token=self.admin)[0], 200)
        self.assertEqual(self.call(f"/api/users/{alex_user['id']}", token=self.admin)[1]["username"], "alex")

    # --- avatar colors -----------------------------------------------------------

    def test_default_color_is_deterministic_and_valid(self):
        sys.path.insert(0, str(ROOT))
        try:
            import server
        finally:
            sys.path.remove(str(ROOT))
        me = self.call("/api/me", token=self.admin)[1]
        self.assertIsNone(me["custom_color"])
        self.assertRegex(me["color"], r"^#[0-9a-f]{6}$")
        self.assertIn(me["color"], server.PALETTE)
        self.assertEqual(me["color"], server.default_color("admin"))
        self.assertEqual(server.default_color("Admin"), server.default_color("admin"))
        # The owner's example: admin and Haystack get different circles out of the box.
        self.assertNotEqual(server.default_color("admin"), server.default_color("haystack"))
        user = self.create_user("haystack", display_name="Haystack")
        self.assertEqual(user["color"], server.default_color("haystack"))
        # Stable across requests and restarts.
        self.restart()
        self.assertEqual(self.call("/api/me", token=self.admin)[1]["color"], me["color"])
        self.assertTrue(all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in server.PALETTE))
        self.assertGreaterEqual(len(set(server.PALETTE)), 8)

    def test_set_and_reset_own_color(self):
        user, token = self.activate("sam")
        default = user["color"]
        status, me, _ = self.call("/api/me", "PATCH", {"color": "#A1B2C3"}, token)
        self.assertEqual(status, 200, me)
        self.assertEqual((me["color"], me["custom_color"]), ("#a1b2c3", "#a1b2c3"))
        self.assertEqual(self.call("/api/me", token=token)[1]["color"], "#a1b2c3")
        status, me, _ = self.call("/api/me", "PATCH", {"color": None}, token)
        self.assertEqual(status, 200)
        self.assertEqual((me["color"], me["custom_color"]), (default, None))
        for bad in ({}, {"color": "red"}, {"color": "#abc"}, {"color": "#abcdeg"}, {"color": "a1b2c3"},
                    {"color": "#a1b2c3 "}, {"color": "#a1b2c3\n"}, {"color": 123456}, {"color": ["#a1b2c3"]}, {"color": True}):
            self.assertEqual(self.call("/api/me", "PATCH", bad, token)[0], 400, bad)
        self.assertIsNone(self.call("/api/me", token=token)[1]["custom_color"])
        self.assertEqual(self.call("/api/me", "PUT", {"color": "#000000"}, token)[0], 405)
        self.assertEqual(self.call("/api/me", "PATCH", {"color": "#000000"})[0], 401)

    def test_color_permissions(self):
        sam_user, sam = self.activate("sam")
        alex_user, alex = self.activate("alex")
        admin_id = self.call("/api/me", token=self.admin)[1]["id"]
        # Users can't change anyone else's color, the admin's included.
        for target in (alex_user["id"], admin_id, sam_user["id"]):
            self.assertEqual(self.call(f"/api/users/{target}", "PATCH", {"color": "#123456"}, sam)[0], 403)
        self.assertIsNone(self.call("/api/me", token=alex)[1]["custom_color"])
        # Admins can set and clear anyone's color; other fields are untouched.
        status, changed, _ = self.call(f"/api/users/{alex_user['id']}", "PATCH", {"color": "#00FF00"}, self.admin)
        self.assertEqual(status, 200, changed)
        self.assertEqual((changed["color"], changed["role"], changed["display_name"]), ("#00ff00", "user", "Alex"))
        self.assertEqual(self.call("/api/me", token=alex)[1]["color"], "#00ff00")
        # A later PATCH without color keeps it.
        self.assertEqual(self.call(f"/api/users/{alex_user['id']}", "PATCH", {"display_name": "Alex D"}, self.admin)[1]["color"], "#00ff00")
        self.assertEqual(self.call(f"/api/users/{alex_user['id']}", "PATCH", {"color": "green"}, self.admin)[0], 400)
        self.assertEqual(self.call(f"/api/users/{alex_user['id']}", "PATCH", {"color": None}, self.admin)[1]["color"], alex_user["color"])
        users = {u["username"]: u for u in self.call("/api/users", token=self.admin)[1]}
        self.assertEqual(users["alex"]["custom_color"], None)
        # A user can still set their own color through /api/me.
        self.assertEqual(self.call("/api/me", "PATCH", {"color": "#fedcba"}, sam)[1]["color"], "#fedcba")

    def test_colors_in_tickets_comments_and_assignable(self):
        sam_user, sam = self.activate("sam", display_name="Sam Carter")
        alex_user, alex = self.activate("alex", display_name="Alex Doe")
        self.call("/api/me", "PATCH", {"color": "#112233"}, sam)
        ticket = self.call(method="POST", data={**self.ticket(), "assignee_id": alex_user["id"]}, token=sam)[1]
        self.assertEqual(ticket["requester_color"], "#112233")
        self.assertEqual(ticket["assignee"]["color"], alex_user["color"])
        path = f"/api/tickets/{ticket['id']}"
        self.call(path + "/comments", "POST", {"body": "On it"}, alex)
        detail = self.call(path, token=sam)[1]
        self.assertEqual([(c["kind"], c["author_color"]) for c in detail["comments"]],
                         [("system", "#112233"), ("comment", alex_user["color"])])
        # Changing a color is reflected everywhere at once.
        self.call(f"/api/users/{alex_user['id']}", "PATCH", {"color": "#445566"}, self.admin)
        listed = self.call(token=sam)[1][0]
        self.assertEqual((listed["requester_color"], listed["assignee"]["color"]), ("#112233", "#445566"))
        self.assertEqual(self.call(path, token=sam)[1]["comments"][1]["author_color"], "#445566")
        people = {p["username"]: p["color"] for p in self.call("/api/users/assignable", token=sam)[1]}
        self.assertEqual(people["alex"], "#445566")
        self.assertEqual(people["sam"], "#112233")
        self.assertRegex(people["admin"], r"^#[0-9a-f]{6}$")

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
        self.assertIsNone(ticket["assignee"])
        self.assertEqual(ticket["comments"][0]["kind"], "comment")
        # Legacy rows without an account get the default color of their stored name.
        self.assertRegex(ticket["requester_color"], r"^#[0-9a-f]{6}$")
        self.assertEqual(ticket["requester_color"], ticket["comments"][0]["author_color"])  # both "Alex"
        with closing(sqlite3.connect(Path(directory.name) / "triage.db")) as db:
            self.assertIn("assignee_id", {row[1] for row in db.execute("PRAGMA table_info(tickets)")})
            self.assertIn("kind", {row[1] for row in db.execute("PRAGMA table_info(comments)")})
            self.assertIn("color", {row[1] for row in db.execute("PRAGMA table_info(users)")})
        admin_id = self.call("/api/me", token=token)[1]["id"]
        self.assertEqual(self.call("/api/tickets/42", "PATCH", {"assignee_id": admin_id}, token)[1]["assignee"]["id"], admin_id)
        self.assertEqual([t["id"] for t in self.call("/api/tickets?assignee=me", token=token)[1]], [42])
        new = self.call(method="POST", data=self.ticket(), token=token)[1]
        self.assertGreater(new["id"], 42)
        self.assertEqual(new["requester"], "admin")
        self.assertEqual(self.call("/api/tickets/42/comments", "POST", {"body": "Still here"}, token)[0], 201)
        self.restart()  # running the migration again is a no-op
        legacy = self.call("/api/tickets/42", token=token)[1]
        self.assertEqual(len(legacy["comments"]), 3)  # original, assignment activity, new update
        self.assertEqual(legacy["assignee"]["id"], admin_id)

    def test_migrates_accounts_database_without_color(self):
        user, token = self.activate("sam", display_name="Sam Carter")
        ticket = self.call(method="POST", data=self.ticket(), token=token)[1]
        self.stop()
        with closing(sqlite3.connect(Path(self.directory.name) / "triage.db")) as db, db:
            db.execute("ALTER TABLE users DROP COLUMN color")  # the schema before avatar colors
        self.start()
        with closing(sqlite3.connect(Path(self.directory.name) / "triage.db")) as db:
            self.assertIn("color", {row[1] for row in db.execute("PRAGMA table_info(users)")})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE color IS NULL").fetchone()[0], 2)
        # Sessions, accounts and tickets survive; everyone gets their default color.
        me = self.call("/api/me", token=token)[1]
        self.assertEqual((me["username"], me["color"], me["custom_color"]), ("sam", user["color"], None))
        self.assertEqual(self.call(f"/api/tickets/{ticket['id']}", token=token)[1]["requester_color"], user["color"])
        self.assertEqual(self.call("/api/me", "PATCH", {"color": "#abcdef"}, token)[1]["color"], "#abcdef")

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
