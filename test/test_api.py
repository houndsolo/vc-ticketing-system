"""Integration tests. They run on SQLite by default. With TEST_DATABASE_URL set to a PostgreSQL
server (a role that may CREATE DATABASE, e.g. postgresql://postgres@127.0.0.1:5432/postgres),
every test gets its own new database there and the server runs with DATABASE_URL."""

from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "bootstrap-admin-pw"
PG_URL = os.environ.get("TEST_DATABASE_URL")
if PG_URL:
    import psycopg
DB_ERRORS = (sqlite3.Error, psycopg.Error) if PG_URL else (sqlite3.Error,)
sys.path.insert(0, str(ROOT))
import server  # noqa: E402  (constants and helpers only; the server under test is a subprocess)
sys.path.remove(str(ROOT))


def pg_database_url(name):
    parts = urlsplit(PG_URL)
    return urlunsplit(parts._replace(path="/" + name))


def create_pg_database():
    name = "dt_test_" + secrets.token_hex(6)
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    return name


def drop_pg_database(name):
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")

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


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("ACCESS_PASSWORD", "DEV_MODE", "ADMIN_USERNAME", "ADMIN_PASSWORD", "TRUST_PROXY", "DATABASE_URL", "DATA_DIR")}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.backend = self.fresh_backend()
        self.directory = self.backend["directory"]
        self.process = None
        self.start()
        self.admin = self.login("admin", ADMIN_PASSWORD)

    # --- backend helpers ---------------------------------------------------------

    def fresh_backend(self):
        """A new, empty database: a temp DATA_DIR, plus (on PostgreSQL) a new database."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = {"DATA_DIR": directory.name}
        if PG_URL:
            name = create_pg_database()
            self.addCleanup(drop_pg_database, name)  # runs after tearDown has stopped the server
            env["DATABASE_URL"] = pg_database_url(name)
        return {"directory": directory, "env": env}

    def backend_env(self, backend=None):
        return dict((backend or self.backend)["env"])

    def sql(self, query, params=(), backend=None):
        """Run one statement against the test's database (committed); returns all rows as tuples."""
        env = self.backend_env(backend)
        if "DATABASE_URL" in env:
            with psycopg.connect(env["DATABASE_URL"], autocommit=True) as db:
                cursor = db.execute(query.replace("?", "%s"), params)
                return cursor.fetchall() if cursor.description else []
        with closing(sqlite3.connect(Path(env["DATA_DIR"]) / "triage.db")) as db, db:
            return db.execute(query, params).fetchall()

    def columns(self, table, backend=None):
        if PG_URL:
            return {r[0] for r in self.sql("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?",
                                           (table,), backend=backend)}
        return {r[1] for r in self.sql(f"PRAGMA table_info({table})", backend=backend)}

    def spawn(self, env):
        return subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def start(self, **extra_env):
        env = clean_env()
        env.update(self.backend_env(), PORT="0", ADMIN_PASSWORD=ADMIN_PASSWORD)
        env.update(extra_env)
        self.process = self.spawn(env)
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
        data = {"title": "Test " + kind, "description": "Details <script>alert(1)</script>",
                "service": "Network", "priority": "High", "type": kind}
        if kind == "Change":
            data.update(planned_start="2030-01-02T03:00:00Z", planned_end="2030-01-02T05:30:00Z")
        return data

    # --- change windows -------------------------------------------------------

    def test_change_windows(self):
        bare = {k: v for k, v in self.ticket("Change").items() if not k.startswith("planned")}
        self.assertEqual(self.call(method="POST", data=bare, token=self.admin)[0], 400)
        for bad in ({"planned_start": "2030-01-02T03:00:00Z"},
                    {"planned_start": "2030-01-02T03:00:00", "planned_end": "2030-01-02T04:00:00"},  # no timezone
                    {"planned_start": "tomorrow", "planned_end": "2030-01-02T04:00:00Z"},
                    {"planned_start": "2030-01-02T05:00:00Z", "planned_end": "2030-01-02T04:00:00Z"}):
            status, data, _ = self.call(method="POST", data={**bare, **bad}, token=self.admin)
            self.assertEqual(status, 400, (bad, data))
        # Issues and Requests have no windows.
        issue = {**self.ticket(), "planned_start": "2030-01-02T03:00:00Z", "planned_end": "2030-01-02T04:00:00Z"}
        self.assertEqual(self.call(method="POST", data=issue, token=self.admin)[0], 400)
        plain = self.call(method="POST", data=self.ticket(), token=self.admin)[1]
        self.assertIsNone(plain["planned_start"])
        # Offsets are normalized to UTC.
        status, t, _ = self.call(method="POST", data={**bare, "planned_start": "2030-01-01T22:00:00-05:00",
                                                     "planned_end": "2030-01-02T05:30:00Z"}, token=self.admin)
        self.assertEqual(status, 201, t)
        self.assertEqual((t["planned_start"], t["planned_end"], t["actual_start"]),
                         ("2030-01-02T03:00:00.000Z", "2030-01-02T05:30:00.000Z", None))
        path = f"/api/tickets/{t['id']}"
        # Resolving needs the actual window; Canceled doesn't.
        status, data, _ = self.call(path, "PATCH", {"status": "Resolved"}, self.admin)
        self.assertEqual((status, data.get("code")), (400, "actual_window_required"), data)
        self.assertEqual(self.call(path, "PATCH", {"planned_start": None}, self.admin)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"actual_end": "2026-01-01T01:00:00Z"}, self.admin)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"actual_start": "2999-01-01T01:00:00Z"}, self.admin)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"actual_start": "2026-01-01T02:00:00Z",
                                                     "actual_end": "2026-01-01T01:00:00Z"}, self.admin)[0], 400)
        # Reschedule, then start it, then resolve with the end time.
        status, d, _ = self.call(path, "PATCH", {"planned_start": "2030-01-03T03:00:00Z", "planned_end": "2030-01-03T04:00:00Z"}, self.admin)
        self.assertEqual((status, d["planned_start"]), (200, "2030-01-03T03:00:00.000Z"), d)
        self.assertIn("rescheduled the planned window to 2030-01-03 03:00Z → 2030-01-03 04:00Z", d["comments"][-1]["body"])
        self.assertEqual(self.call(path, "PATCH", {"status": "In progress", "actual_start": "2026-01-01T01:00:00Z"}, self.admin)[0], 200)
        status, d, _ = self.call(path, "PATCH", {"status": "Resolved", "actual_end": "2026-01-01T02:15:00+00:00"}, self.admin)
        self.assertEqual((status, d["status"], d["actual_start"], d["actual_end"]),
                         (200, "Resolved", "2026-01-01T01:00:00.000Z", "2026-01-01T02:15:00.000Z"), d)
        self.assertIn("recorded the actual window: 2026-01-01 01:00Z → 2026-01-01 02:15Z", d["comments"][-1]["body"])
        # A Change filed before windows existed can still be edited and canceled without one.
        legacy = self.call(method="POST", data=self.ticket("Change"), token=self.admin)[1]
        self.sql(f"UPDATE tickets SET planned_start=NULL, planned_end=NULL WHERE id={legacy['id']}")
        self.assertEqual(self.call(f"/api/tickets/{legacy['id']}", "PATCH", {"priority": "Low"}, self.admin)[0], 200)
        self.assertEqual(self.call(f"/api/tickets/{legacy['id']}", "PATCH", {"status": "Canceled"}, self.admin)[0], 200)

    # --- canceled status and deleting tickets -------------------------------

    def test_canceled_status(self):
        status, t, _ = self.call("/api/tickets", "POST", self.ticket(), self.admin)
        self.assertEqual(status, 201, t)
        status, d, _ = self.call(f"/api/tickets/{t['id']}", "PATCH", {"status": "Canceled"}, self.admin)
        self.assertEqual((status, d["status"]), (200, "Canceled"), d)
        self.assertEqual(self.call(f"/api/tickets/{t['id']}", "PATCH", {"status": "Cancelled"}, self.admin)[0], 400)

    def test_admin_deletes_ticket_and_conversation(self):
        _, user = self.activate("haystack")
        status, t, _ = self.call("/api/tickets", "POST", self.ticket(), user)
        self.assertEqual(status, 201, t)
        self.assertEqual(self.call(f"/api/tickets/{t['id']}/comments", "POST", {"body": "hi"}, user)[0], 201)
        status, data, _ = self.call(f"/api/tickets/{t['id']}", "DELETE", None, user)
        self.assertEqual(status, 403, data)  # not an admin, not even for their own ticket
        status, data, _ = self.call(f"/api/tickets/{t['id']}", "DELETE", None, self.admin)
        self.assertEqual((status, data.get("deleted")), (200, t["id"]), data)
        self.assertEqual(self.call(f"/api/tickets/{t['id']}", token=self.admin)[0], 404)
        self.assertEqual(self.call(f"/api/tickets/{t['id']}", "DELETE", None, self.admin)[0], 404)
        self.assertNotIn(t["id"], [x["id"] for x in self.call(token=self.admin)[1]])
        # The next ticket still gets a fresh number (ids aren't reused after a delete of the newest).
        status, t2, _ = self.call("/api/tickets", "POST", self.ticket(), self.admin)
        self.assertEqual(status, 201, t2)

    def test_ticket_groups(self):
        _, user = self.activate("haystack")
        new = lambda: self.call("/api/tickets", "POST", self.ticket(), user)[1]["id"]
        a, b, c, d, e = new(), new(), new(), new(), new()
        patch = lambda t, pid, token=user: self.call(f"/api/tickets/{t}", "PATCH", {"parent_id": pid}, token)
        # b and c join a's group (any signed-in user can group tickets)
        self.assertEqual(patch(b, a)[0], 200)
        status, dc, _ = patch(c, a)
        self.assertEqual(status, 200, dc)
        self.assertEqual(dc["parent"]["id"], a)
        self.assertEqual([(m["id"], m["is_parent"]) for m in dc["group"]["members"]], [(a, True), (b, False), (c, False)])
        da = self.call(f"/api/tickets/{a}", token=user)[1]
        self.assertEqual((da["child_count"], da["parent"]), (2, None))
        # one level: can't use a child as a parent, can't parent itself, unknown ticket
        self.assertEqual(patch(d, b)[0], 400)
        self.assertEqual(patch(d, d)[0], 400)
        self.assertEqual(patch(d, 999999)[0], 400)
        self.assertEqual(self.call(f"/api/tickets/{d}", "PATCH", {"parent_id": "1"}, user)[0], 400)
        # d becomes a parent of e; then d joins a's group -> e moves along (flattened)
        self.assertEqual(patch(e, d)[0], 200)
        self.assertEqual(patch(d, a)[0], 200)
        self.assertEqual(self.call(f"/api/tickets/{e}", token=user)[1]["parent"]["id"], a)
        # make c the parent: a, b, d, e become c's children
        status, dp, _ = self.call(f"/api/tickets/{c}/make-parent", "POST", {}, user)
        self.assertEqual(status, 200, dp)
        self.assertEqual(dp["group"]["parent_id"], c)
        self.assertEqual(sorted(m["id"] for m in dp["group"]["members"] if not m["is_parent"]), sorted([a, b, d, e]))
        self.assertEqual(self.call(f"/api/tickets/{c}/make-parent", "POST", {}, user)[0], 400)  # already the parent
        # activity lines record it
        bodies = [x["body"] for x in self.call(f"/api/tickets/{a}", token=user)[1]["comments"] if x["kind"] == "system"]
        self.assertTrue(any("made DT-" in s and "group's parent" in s for s in bodies), bodies)
        # ungroup b
        self.assertIsNone(patch(b, None)[1]["parent"])
        # deleting the parent ungroups the rest
        self.assertEqual(self.call(f"/api/tickets/{c}", "DELETE", None, self.admin)[0], 200)
        for t in (a, d, e):
            dt_ = self.call(f"/api/tickets/{t}", token=user)[1]
            self.assertIsNone(dt_["parent"], t)
        # list rows carry parent/child_count for the UI chips
        rows = {r["id"]: r for r in self.call(token=user)[1]}
        self.assertIn("child_count", rows[a])

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
        stored = [row[0] for row in self.sql("SELECT token_hash FROM sessions")]
        password_hash = self.sql("SELECT password_hash FROM users")[0][0]
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
        for path, expected in (("/", b"dark-triage"), ("/app.js", b"'/login'"), ("/style.css", b":root"),
                               ("/favicon.svg", b"<svg"), ("/favicon.ico", b"\x00\x00\x01\x00"),
                               ("/apple-touch-icon.png", b"\x89PNG")):
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
                                              "color": alex_user["color"], "is_bot": False})
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
        self.assertEqual(set(people[0]), {"id", "username", "display_name", "color", "is_bot"})
        self.assertEqual(self.call("/api/users", token=sam)[0], 403)
        self.assertEqual(self.call("/api/users/assignable", token=None)[0], 401)
        self.assertEqual(self.call("/api/users/assignable", "POST", {}, sam)[0], 405)
        # Admin user management is unaffected by the new route.
        self.assertEqual(self.call("/api/users/assignable", token=self.admin)[0], 200)
        self.assertEqual(self.call(f"/api/users/{alex_user['id']}", token=self.admin)[1]["username"], "alex")

    # --- avatar colors -----------------------------------------------------------

    def test_default_color_is_deterministic_and_valid(self):
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

    def test_change_own_display_name(self):
        user, token = self.activate("sam", display_name="Sam")
        ticket = self.call(method="POST", data=self.ticket(), token=token)[1]
        self.call(f"/api/tickets/{ticket['id']}/comments", "POST", {"body": "Hello"}, token)
        status, me, _ = self.call("/api/me", "PATCH", {"display_name": "  Samantha Carter  "}, token)
        self.assertEqual(status, 200, me)
        self.assertEqual(me["display_name"], "Samantha Carter")  # trimmed
        self.assertEqual(me["custom_color"], None)  # untouched
        # The new name shows everywhere the account appears.
        detail = self.call(f"/api/tickets/{ticket['id']}", token=self.admin)[1]
        self.assertEqual((detail["requester"], detail["comments"][0]["author"]), ("Samantha Carter", "Samantha Carter"))
        self.assertIn("Samantha Carter", [p["display_name"] for p in self.call("/api/users/assignable", token=self.admin)[1]])
        # Same rules as admin edits: 1-80 characters, not blank, a string.
        for bad in ("", "   ", "x" * 81, None, 42, ["Sam"], True):
            self.assertEqual(self.call("/api/me", "PATCH", {"display_name": bad}, token)[0], 400, bad)
        self.assertEqual(self.call("/api/me", "PATCH", {"display_name": "y" * 80}, token)[0], 200)
        # Name and color together; an invalid color rejects the whole change.
        self.assertEqual(self.call("/api/me", "PATCH", {"display_name": "Sam C", "color": "nope"}, token)[0], 400)
        self.assertEqual(self.call("/api/me", token=token)[1]["display_name"], "y" * 80)
        both = self.call("/api/me", "PATCH", {"display_name": "Sam C", "color": "#123ABC"}, token)[1]
        self.assertEqual((both["display_name"], both["color"]), ("Sam C", "#123abc"))
        self.assertEqual(self.call("/api/me", "PATCH", {"role": "admin"}, token)[0], 400)  # nothing it may change
        self.assertEqual(self.call("/api/me", token=token)[1]["role"], "user")
        # Bots can rename themselves through their token, too.
        bot = self.call("/api/users", "POST", {"username": "claude", "display_name": "Claude", "bot": True}, self.admin)[1]
        bot_token = self.call(f"/api/users/{bot['id']}/tokens", "POST", {"name": "t"}, self.admin)[1]["token"]
        self.assertEqual(self.call("/api/me", "PATCH", {"display_name": "Claude Code"}, bot_token)[1]["display_name"], "Claude Code")

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

    # --- bots and API tokens -------------------------------------------------------

    def create_bot(self, username="claude", display_name="Claude", **extra):
        status, bot, _ = self.call("/api/users", "POST", {"username": username, "display_name": display_name, "bot": True, **extra}, self.admin)
        self.assertEqual(status, 201, bot)
        return bot

    def mint(self, user_id, name="agent", token=None):
        status, data, _ = self.call(f"/api/users/{user_id}/tokens", "POST", {"name": name}, token or self.admin)
        self.assertEqual(status, 201, data)
        return data

    def admin_cli(self, *args, backend=None, **extra_env):
        env = {**clean_env(), **self.backend_env(backend), **extra_env}
        return subprocess.run([sys.executable, "server.py", "admin", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)

    def dt(self, *args, token=None, stdin=None, **env):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("DT_")}
        environment.update({"DT_URL": self.base, "DT_TOKEN": token or "", "DT_TOKEN_FILE": str(Path(self.directory.name) / "missing-token")}, **env)
        return subprocess.run([sys.executable, str(ROOT / "tools" / "dt.py"), *args], env=environment,
                              input=stdin, capture_output=True, text=True, timeout=30)

    def test_api_token_auth_and_revocation(self):
        bot = self.create_bot()
        self.assertEqual((bot["is_bot"], bot["role"], bot["must_change"]), (True, "user", False))
        minted = self.mint(bot["id"], "a0-nix claude-code")
        token = minted["token"]
        self.assertRegex(token, r"^dt_[A-Za-z0-9_-]{40,}$")
        self.assertEqual(set(minted), {"id", "name", "created", "last_used", "token"})
        self.assertIsNone(minted["last_used"])
        # The token works on every endpoint with the bot's own permissions.
        me = self.call("/api/me", token=token)[1]
        self.assertEqual((me["username"], me["is_bot"]), ("claude", True))
        status, ticket, _ = self.call(method="POST", data=self.ticket(), token=token)
        self.assertEqual(status, 201, ticket)
        self.assertEqual((ticket["requester"], ticket["requester_is_bot"]), ("Claude", True))
        self.assertEqual(self.call(f"/api/tickets/{ticket['id']}/comments", "POST", {"body": "Filed by an agent"}, token)[0], 201)
        self.assertTrue(self.call(f"/api/tickets/{ticket['id']}", token=token)[1]["comments"][0]["author_is_bot"])
        self.assertEqual(self.call("/api/users", token=token)[0], 403)  # not an admin
        # Bots are assignable like anyone.
        people = {p["username"]: p for p in self.call("/api/users/assignable", token=self.admin)[1]}
        self.assertTrue(people["claude"]["is_bot"])
        assigned = self.call(f"/api/tickets/{ticket['id']}", "PATCH", {"assignee_id": bot["id"]}, self.admin)[1]
        self.assertEqual((assigned["assignee"]["username"], assigned["assignee"]["is_bot"]), ("claude", True))
        # Only the hash is stored; listing never shows the token.
        stored = self.sql("SELECT token_hash FROM api_tokens")
        self.assertNotIn(token, str(stored))
        listed = self.call(f"/api/users/{bot['id']}/tokens", token=self.admin)[1]
        self.assertEqual([t["name"] for t in listed], ["a0-nix claude-code"])
        self.assertNotIn("token", listed[0])
        # Names are unique among active tokens.
        self.assertEqual(self.call(f"/api/users/{bot['id']}/tokens", "POST", {"name": "a0-nix claude-code"}, self.admin)[0], 409)
        self.assertEqual(self.call(f"/api/users/{bot['id']}/tokens", "POST", {"name": " "}, self.admin)[0], 400)
        # Revocation takes effect immediately.
        path = f"/api/users/{bot['id']}/tokens/{minted['id']}"
        self.assertEqual(self.call(path, "DELETE", token=self.admin)[0], 200)
        status, data, _ = self.call("/api/me", token=token)
        self.assertEqual(status, 401)
        self.assertIn("revoked", data["error"])
        self.assertEqual(self.call(path, "DELETE", token=self.admin)[0], 404)
        self.assertEqual(self.call(f"/api/users/{bot['id']}/tokens", token=self.admin)[1], [])
        self.mint(bot["id"], "a0-nix claude-code")  # the name is free again
        self.assertEqual(self.call(token="dt_" + "x" * 43)[0], 401)
        # Session tokens keep working as before.
        self.assertEqual(self.call("/api/me", token=self.admin)[0], 200)

    def test_token_last_used_updates(self):
        bot = self.create_bot()
        token = self.mint(bot["id"])["token"]
        self.call("/api/me", token=token)
        first = self.call(f"/api/users/{bot['id']}/tokens", token=self.admin)[1][0]["last_used"]
        self.assertIsNotNone(first)
        time.sleep(0.01)
        self.call(token=token)
        second = self.call(f"/api/users/{bot['id']}/tokens", token=self.admin)[1][0]["last_used"]
        self.assertGreater(second, first)

    def test_bot_cannot_sign_in_with_a_password(self):
        bot = self.create_bot()
        # Same answer as an unknown username: no hint that the account exists.
        unknown = self.login("nobody-here", "whatever-password", expect=401)
        refused = self.login("claude", "whatever-password", expect=401)
        self.assertEqual(refused["error"], unknown["error"])
        self.assertEqual(self.login("claude", "!bot", expect=401)["error"], unknown["error"])
        # Throttling treats it like any unknown username, too.
        for _ in range(3):  # five failures in total, as for any username
            self.login("claude", "whatever-password", expect=401)
        self.login("claude", "whatever-password", expect=429)
        path = f"/api/users/{bot['id']}"
        self.assertEqual(self.call(path + "/password", "POST", {"password": "some-password-1"}, self.admin)[0], 400)
        self.assertEqual(self.call(path, "PATCH", {"role": "admin"}, self.admin)[0], 400)
        self.assertEqual(self.call("/api/users", "POST", {"username": "evil", "display_name": "Evil", "bot": True, "role": "admin"}, self.admin)[0], 400)
        self.assertEqual(self.call("/api/users", "POST", {"username": "evil", "display_name": "Evil", "bot": "yes"}, self.admin)[0], 400)
        # Through its token, a bot can't change a password or sign out a token.
        token = self.mint(bot["id"])["token"]
        self.assertEqual(self.call("/api/me/password", "POST", {"current_password": "x", "new_password": "long-enough-1"}, token)[0], 403)
        self.assertEqual(self.call("/api/logout", "POST", {}, token)[0], 400)
        self.assertEqual(self.call("/api/me", token=token)[0], 200)

    def test_disabled_bot_tokens_rejected(self):
        bot = self.create_bot()
        token = self.mint(bot["id"])["token"]
        self.assertEqual(self.call(token=token)[0], 200)
        self.assertEqual(self.call(f"/api/users/{bot['id']}", "PATCH", {"disabled": True}, self.admin)[0], 200)
        status, data, _ = self.call(token=token)
        self.assertEqual(status, 401)
        self.assertIn("disabled", data["error"])
        self.assertEqual(self.call(f"/api/users/{bot['id']}", "PATCH", {"disabled": False}, self.admin)[0], 200)
        self.assertEqual(self.call(token=token)[0], 200)

    def test_token_management_permissions(self):
        sam_user, sam = self.activate("sam")
        alex_user, _ = self.activate("alex")
        bot = self.create_bot()
        for user_id in (alex_user["id"], bot["id"], sam_user["id"]):
            self.assertEqual(self.call(f"/api/users/{user_id}/tokens", "POST", {"name": "x"}, sam)[0], 403)
            self.assertEqual(self.call(f"/api/users/{user_id}/tokens", token=sam)[0], 403)
        admin_token = self.mint(bot["id"], "keep")
        self.assertEqual(self.call(f"/api/users/{bot['id']}/tokens/{admin_token['id']}", "DELETE", token=sam)[0], 403)
        # Users manage their own tokens through /api/me/tokens.
        status, mine, _ = self.call("/api/me/tokens", "POST", {"name": "laptop"}, sam)
        self.assertEqual(status, 201, mine)
        self.assertEqual(self.call("/api/me", token=mine["token"])[1]["username"], "sam")
        self.assertEqual([t["name"] for t in self.call("/api/me/tokens", token=sam)[1]], ["laptop"])
        # Someone else's token id can't be revoked through /api/me/tokens.
        self.assertEqual(self.call(f"/api/me/tokens/{admin_token['id']}", "DELETE", token=sam)[0], 404)
        self.assertEqual(self.call("/api/me", token=admin_token["token"])[0], 200)
        # An API token can't mint more tokens, for itself or anyone.
        self.assertEqual(self.call("/api/me/tokens", "POST", {"name": "more"}, mine["token"])[0], 403)
        self.assertEqual(self.call("/api/me/tokens", token=mine["token"])[0], 403)
        self.assertEqual(self.call("/api/me/tokens/1", "PUT", {}, sam)[0], 405)
        self.assertEqual(self.call(f"/api/me/tokens/{mine['id']}", "DELETE", token=sam)[0], 200)
        self.assertEqual(self.call("/api/me", token=mine["token"])[0], 401)
        # The cross-origin check still applies to token requests; requests without Origin (CLI) are fine.
        other = self.call("/api/me/tokens", "POST", {"name": "laptop"}, sam)[1]["token"]
        self.assertEqual(self.call(method="POST", data=self.ticket(), token=other, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call(method="POST", data=self.ticket(), token=other)[0], 201)
        # Disabling a person's account stops their tokens too.
        self.call(f"/api/users/{sam_user['id']}", "PATCH", {"disabled": True}, self.admin)
        self.assertEqual(self.call("/api/me", token=other)[0], 401)

    def test_admin_cli(self):
        # Runs against the live database while the server is up.
        result = self.admin_cli("create-user", "--username", "Claude", "--display-name", "Claude", "--bot", "--color", "#F70997")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Created bot 'claude'", result.stdout)
        again = self.admin_cli("create-user", "--username", "claude", "--display-name", "Someone else")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already exists", again.stdout)
        result = self.admin_cli("create-token", "--username", "claude", "--name", "a0-nix")
        self.assertEqual(result.returncode, 0, result.stderr)
        token = result.stdout.strip()
        self.assertEqual(result.stdout, token + "\n")  # only the token on stdout
        me = self.call("/api/me", token=token)[1]
        self.assertEqual((me["username"], me["display_name"], me["is_bot"], me["color"]), ("claude", "Claude", True, "#f70997"))
        self.assertEqual(self.admin_cli("create-token", "--username", "claude", "--name", "a0-nix").returncode, 1)  # duplicate name
        listing = self.admin_cli("list-users")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        line = next(l for l in listing.stdout.splitlines() if " claude " in l)
        self.assertIn("bot", line)
        self.assertRegex(line, r"active\s+1\s")
        revoked = self.admin_cli("revoke-token", "--username", "claude", "--name", "a0-nix")
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertEqual(self.call("/api/me", token=token)[0], 401)
        self.assertEqual(self.admin_cli("revoke-token", "--username", "claude", "--name", "a0-nix").returncode, 1)
        missing = self.admin_cli("create-token", "--username", "ghost", "--name", "x")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no user named", missing.stderr)
        self.assertEqual(missing.stdout, "")
        self.assertEqual(self.admin_cli("create-user", "--username", "x", "--bot").returncode, 1)  # invalid username
        self.assertEqual(self.admin_cli("create-user", "--username", "rootbot", "--bot", "--role", "admin").returncode, 1)
        self.assertEqual(self.admin_cli("create-user", "--username", "badcolor", "--color", "pink").returncode, 1)
        self.assertNotEqual(self.admin_cli("bogus").returncode, 0)
        # A human account gets a temporary password it must change.
        human = self.admin_cli("create-user", "--username", "haystack", "--display-name", "Haystack", "--role", "admin")
        self.assertEqual(human.returncode, 0, human.stderr)
        temporary = human.stdout.split("Temporary password: ")[1].split()[0]
        token = self.login("haystack", temporary)
        me = self.call("/api/me", token=token)[1]
        self.assertEqual((me["role"], me["must_change"], me["is_bot"]), ("admin", True, False))
        # Without a database, commands other than create-user refuse to guess.
        result = self.admin_cli("list-users", backend=self.fresh_backend())
        self.assertEqual(result.returncode, 1)
        self.assertIn("no database", result.stderr)

    def test_dt_client_end_to_end(self):
        bot = self.create_bot()
        token = self.mint(bot["id"])["token"]
        sam_user, _ = self.activate("sam", display_name="Sam Carter")
        who = self.dt("whoami", token=token)
        self.assertEqual(who.returncode, 0, who.stderr)
        self.assertIn("Claude (@claude, bot", who.stdout)
        created = self.dt("new", "--title", "Plex buffering", "--description-file", "-", "--service", "Plex",
                          "--type", "request", "--priority", "high", "--assignee", "sam", token=token, stdin="Line one\nLine two\n")
        self.assertEqual(created.returncode, 0, created.stderr)
        label, url = created.stdout.split()
        self.assertRegex(label, r"^DT-\d{3}$")
        self.assertEqual(url, f"{self.base}/#{label}")
        ticket_id = int(label[3:])
        ticket = self.call(f"/api/tickets/{ticket_id}", token=self.admin)[1]
        self.assertEqual((ticket["type"], ticket["priority"], ticket["service"], ticket["requester"], ticket["assignee"]["id"]),
                         ("Request", "High", "Plex", "Claude", sam_user["id"]))
        self.assertEqual(ticket["description"], "Line one\nLine two")
        listed = self.dt("list", token=token)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(label, listed.stdout)
        self.assertIn("Sam Carter", listed.stdout)
        as_json = json.loads(self.dt("list", "--json", "--mine", token=token).stdout)
        self.assertEqual([t["id"] for t in as_json], [ticket_id])
        self.assertEqual(json.loads(self.dt("list", "--json", "--assignee", "sam", token=token).stdout)[0]["id"], ticket_id)
        self.assertEqual(json.loads(self.dt("list", "--json", "--assignee", "me", token=token).stdout), [])
        commented = self.dt("comment", label.lower(), "--body", "-", token=token, stdin="Looked at the logs.")
        self.assertEqual(commented.returncode, 0, commented.stderr)
        updated = self.dt("update", str(ticket_id), "--status", "in-progress", "--assignee", "me", "--priority", "Critical", token=token)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn("In progress", updated.stdout)
        shown = self.dt("show", label, token=token)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        for text in ("Plex buffering", "Status: In progress", "Priority: Critical", "Claude [bot]", "Looked at the logs.", "Line two"):
            self.assertIn(text, shown.stdout)
        detail = json.loads(self.dt("show", label, "--json", token=token).stdout)
        self.assertEqual((detail["status"], detail["assignee"]["username"]), ("In progress", "claude"))
        self.assertEqual(json.loads(self.dt("list", "--json", "--status", "in progress", token=token).stdout)[0]["id"], ticket_id)
        self.assertEqual(self.dt("update", label, "--status", "Resolved", token=token).returncode, 0)
        self.assertEqual(json.loads(self.dt("list", "--json", token=token).stdout), [])  # unresolved by default
        self.assertEqual(len(json.loads(self.dt("list", "--json", "--all", token=token).stdout)), 1)
        # --sort created|updated (newest first; --reverse flips). Touching the older ticket moves it up by updated.
        second = int(self.dt("new", "--title", "Second", "--service", "Plex", "--description", "x", token=token).stdout.split()[0][3:])
        ids = lambda *a: [t["id"] for t in json.loads(self.dt("list", "--json", "--all", *a, token=token).stdout)]
        self.assertEqual(ids("--sort", "created"), [second, ticket_id])
        self.assertEqual(ids("--sort", "created", "--reverse"), [ticket_id, second])
        self.dt("comment", label, "--body", "bump", token=token)
        self.assertEqual(ids("--sort", "updated"), [ticket_id, second])
        self.assertEqual(ids("-r"), [second, ticket_id])
        self.assertEqual(ids("--sort", "id"), [ticket_id, second])
        table = self.dt("list", "--all", token=token).stdout
        self.assertIn("UPDATED", table)
        self.assertRegex(table, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertEqual(self.dt("update", str(second), "--status", "Canceled", token=token).returncode, 0)
        # Change windows: planned on create (local time or ISO), actual to resolve.
        no_window = self.dt("new", "--type", "change", "--title", "Swap", "--service", "Network", "--description", "x", token=token)
        self.assertEqual(no_window.returncode, 2)
        self.assertIn("--planned-start", no_window.stderr)
        change = self.dt("new", "--type", "change", "--title", "Swap", "--service", "Network", "--description", "x",
                         "--planned-start", "2030-01-02T03:00Z", "--planned-end", "2030-01-02 05:00", token=token, TZ="UTC")
        self.assertEqual(change.returncode, 0, change.stderr)
        change_label = change.stdout.split()[0]
        refused = self.dt("update", change_label, "--status", "resolved", token=token)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("actual_start", refused.stderr)
        done = self.dt("update", change_label, "--status", "resolved", "--actual-start", "2026-01-01T01:00Z",
                       "--actual-end", "now", token=token, TZ="UTC")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("actual 2026-01-01", done.stdout)
        shown = self.dt("show", change_label, token=token, TZ="UTC").stdout
        self.assertIn("Planned: 2030-01-02 03:00 → 2030-01-02 05:00", shown)
        # Errors: clear messages and distinct exit codes.
        missing = self.dt("show", "DT-999", token=token)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Ticket not found", missing.stderr)
        self.assertEqual(self.dt("update", label, token=token).returncode, 2)
        self.assertEqual(self.dt("new", "--title", "x", "--service", "y", token=token).returncode, 2)  # no description
        self.assertEqual(self.dt("new", "--title", "x", "--description", "y", "--service", "z", "--priority", "urgent", token=token).returncode, 2)
        self.assertEqual(self.dt("update", label, "--assignee", "ghost", token=token).returncode, 1)
        bad = self.dt("whoami", token="dt_not-a-real-token")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("HTTP 401", bad.stderr)
        self.assertEqual(self.dt("whoami").returncode, 2)  # no token configured
        token_file = Path(self.directory.name) / "token"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        self.assertEqual(self.dt("whoami", DT_TOKEN_FILE=str(token_file)).returncode, 0)
        unreachable = self.dt("whoami", token=token, DT_URL="http://127.0.0.1:9")
        self.assertEqual(unreachable.returncode, 3)
        self.assertIn("can't reach", unreachable.stderr)

    # --- startup and migration -------------------------------------------------

    def test_migrates_old_schema_database(self):
        self.stop()
        self.backend = self.fresh_backend()
        old_schema = OLD_SCHEMA
        if PG_URL:  # the same add-only migration path, from an old-schema PostgreSQL database
            old_schema = OLD_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", server.DIALECTS["postgres"]["id"])
        for statement in old_schema.split(";"):
            if statement.strip():
                self.sql(statement)
        self.sql("INSERT INTO tickets(id,title,description,type,priority,service,requester,status,created,updated) VALUES(42,'Legacy','Keep me','Change','Low','Storage','Alex','Waiting','2026-11-07T12:00:00.000Z','2026-11-07T12:00:00.000Z')")
        self.sql("INSERT INTO comments(ticket_id,author,body,created) VALUES(42,'Alex','Existing update','2026-11-07T12:00:00.000Z')")
        if PG_URL:  # explicit ids don't advance an identity sequence (import-sqlite resets it the same way)
            self.sql("SELECT setval(pg_get_serial_sequence('tickets', 'id'), 42)")
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
        self.assertIn("assignee_id", self.columns("tickets"))
        self.assertIn("kind", self.columns("comments"))
        self.assertIn("color", self.columns("users"))
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
        self.sql("ALTER TABLE users DROP COLUMN color")  # the schema before avatar colors
        self.start()
        self.assertIn("color", self.columns("users"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM users WHERE color IS NULL")[0][0], 2)
        # Sessions, accounts and tickets survive; everyone gets their default color.
        me = self.call("/api/me", token=token)[1]
        self.assertEqual((me["username"], me["color"], me["custom_color"]), ("sam", user["color"], None))
        self.assertEqual(self.call(f"/api/tickets/{ticket['id']}", token=token)[1]["requester_color"], user["color"])
        self.assertEqual(self.call("/api/me", "PATCH", {"color": "#abcdef"}, token)[1]["color"], "#abcdef")

    def test_admin_password_required_on_first_start(self):
        empty = self.fresh_backend()
        env = {**clean_env(), **self.backend_env(empty), "PORT": "0"}
        result = subprocess.run([sys.executable, "server.py"], cwd=ROOT, env=env, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Set ADMIN_PASSWORD", result.stderr)
        short = subprocess.run([sys.executable, "server.py"], cwd=ROOT, env={**env, "ADMIN_PASSWORD": "short"},
                               capture_output=True, timeout=10)
        self.assertNotEqual(short.returncode, 0)
        self.assertEqual(self.count_users(empty), 0)
        # Once an account exists, ADMIN_PASSWORD is no longer needed.
        self.restart(ADMIN_PASSWORD="")
        self.login("admin", ADMIN_PASSWORD)

    def test_custom_admin_username_and_dev_mode(self):
        empty = self.fresh_backend()
        process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**clean_env(), **self.backend_env(empty), "PORT": "0", "DEV_MODE": "1", "ADMIN_USERNAME": "Root.Ops"})
        try:
            self.assertIn("listening on port", process.stdout.readline())
        finally:
            process.terminate()
            _, stderr = process.communicate(timeout=5)
        self.assertIn("generated password", stderr)
        self.assertEqual(self.sql("SELECT username,role,must_change FROM users", backend=empty), [("root.ops", "admin", 0)])

    # --- storage backends ------------------------------------------------------------

    def test_health_endpoint(self):
        expected = {"ok": True, "db": "postgres" if PG_URL else "sqlite"}
        status, data, headers = self.call("/api/health")  # no auth needed
        self.assertEqual((status, data), (200, expected))
        self.assertEqual(headers["Cache-Control"], "no-store")
        with urlopen(Request(self.base + "/api/health", method="HEAD"), timeout=10) as response:
            self.assertEqual((response.status, response.read()), (200, b""))
        self.assertEqual(self.call("/api/health", "POST", {})[0], 405)
        if not PG_URL:
            return
        # Outage, as in a Patroni failover: the database disappears and its connections are cut.
        name = urlsplit(self.backend_env()["DATABASE_URL"]).path[1:]
        moved = name + "_away"
        self.addCleanup(drop_pg_database, moved)
        token = self.admin
        with psycopg.connect(PG_URL, autocommit=True) as admin:
            for _ in range(50):
                admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (name,))
                try:
                    admin.execute(f"ALTER DATABASE {name} RENAME TO {moved}")
                    break
                except psycopg.errors.ObjectInUse:
                    time.sleep(0.1)
            status, data, _ = self.call("/api/health")
            self.assertEqual((status, data), (503, {"ok": False, "db": "postgres"}))
            status, data, headers = self.call("/api/me", token=token)
            self.assertEqual(status, 503, data)
            self.assertEqual(headers["Retry-After"], "5")
            self.assertEqual(self.call(method="POST", data=self.ticket(), token=token)[0], 503)
            admin.execute(f"ALTER DATABASE {moved} RENAME TO {name}")
        # The same server process recovers on the next request: no restart, nothing half-written.
        self.assertEqual(self.call("/api/health")[:2], (200, expected))
        self.assertEqual(self.call("/api/me", token=token)[0], 200)
        self.assertEqual(self.call(token=token)[1], [])

    def test_concurrent_startup_on_one_database(self):
        fresh = self.fresh_backend()
        env = {**clean_env(), **self.backend_env(fresh), "PORT": "0", "ADMIN_PASSWORD": ADMIN_PASSWORD}
        holder = None
        if PG_URL:
            # Hold the migration lock: neither instance may touch the schema until it is released.
            holder = psycopg.connect(fresh["env"]["DATABASE_URL"], autocommit=True)
            self.addCleanup(holder.close)
            holder.execute("SELECT pg_advisory_lock(%s)", (server.MIGRATION_LOCK,))
        processes = [self.spawn(env) for _ in range(2)]
        for process in processes:
            self.addCleanup(lambda p=process: (p.terminate(), p.wait(timeout=5), p.stdout.close()))
        if holder:
            ready, _, _ = select.select([p.stdout for p in processes], [], [], 2)
            self.assertEqual(ready, [], "a server started while the migration lock was held")
            self.assertTrue(all(p.poll() is None for p in processes))  # waiting, not crashed
            self.assertEqual(self.count_users(fresh), 0)
            holder.execute("SELECT pg_advisory_unlock(%s)", (server.MIGRATION_LOCK,))
        bases = []
        for process in processes:
            line = process.stdout.readline()
            self.assertIn("listening on port", line)
            bases.append("http://127.0.0.1:" + line.split()[-1])
        # Exactly one admin was bootstrapped, and both instances serve the same data.
        self.assertEqual(self.sql("SELECT username FROM users", backend=fresh), [("admin",)])
        self.base = bases[0]
        token = self.login("admin", ADMIN_PASSWORD)
        ticket = self.call(method="POST", data=self.ticket(), token=token)[1]
        self.base = bases[1]
        self.assertEqual(self.call(f"/api/tickets/{ticket['id']}", token=token)[1]["title"], ticket["title"])
        self.assertLessEqual({"color", "is_bot"}, self.columns("users", backend=fresh))

    def test_case_insensitive_usernames(self):
        user = self.create_user("Sam.Ops", "temp-password-1")
        self.assertEqual(user["username"], "sam.ops")  # stored lowercase
        for name in ("SAM.OPS", "sam.ops", "Sam.ops"):
            self.assertEqual(self.call("/api/users", "POST", {"username": name, "display_name": "Dup", "role": "user",
                                                              "password": "long-enough-1"}, self.admin)[0], 409, name)
        self.login("SAM.Ops", "temp-password-1")
        # A mixed-case row written directly (e.g. by an older version or an import) is still unique
        # case-insensitively and can sign in with any casing.
        stamp = "2026-11-11T00:00:00.000Z"
        insert = "INSERT INTO users(username,display_name,role,password_hash,must_change,created,updated) VALUES(?,?,'user',?,0,?,?)"
        self.sql(insert, ("Mixed", "Mixed", server.hash_password("mixed-password-1"), stamp, stamp))
        with self.assertRaises(DB_ERRORS):
            self.sql(insert, ("MIXED", "Other", "x", stamp, stamp))
        self.assertEqual(self.call("/api/users", "POST", {"username": "mixed", "display_name": "Dup", "role": "user",
                                                          "password": "long-enough-1"}, self.admin)[0], 409)
        self.assertEqual(self.call("/api/me", token=self.login("mIxEd", "mixed-password-1"))[1]["username"], "Mixed")
        self.assertIn("already exists", self.admin_cli("create-user", "--username", "MIXED").stdout)
        self.assertEqual(self.admin_cli("create-token", "--username", "mixed", "--name", "t").returncode, 0)
        if PG_URL:
            indexes = [r[0] for r in self.sql("SELECT indexdef FROM pg_indexes WHERE tablename='users'")]
            self.assertTrue(any("UNIQUE" in d and "lower(username)" in d for d in indexes), indexes)

    def test_import_sqlite(self):
        if not PG_URL:
            result = self.admin_cli("import-sqlite", "--from", str(Path(self.directory.name) / "triage.db"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("set DATABASE_URL", result.stderr)
            return
        target_with_admin = self.backend
        # Build a real SQLite database with a SQLite-mode server: people, a bot with a token, tickets,
        # assignments, comments and activity lines.
        self.stop()
        sqlite_dir = tempfile.TemporaryDirectory()
        self.addCleanup(sqlite_dir.cleanup)
        self.backend = {"directory": sqlite_dir, "env": {"DATA_DIR": sqlite_dir.name}}
        self.start()
        self.assertEqual(self.call("/api/health")[1]["db"], "sqlite")
        admin = self.login("admin", ADMIN_PASSWORD)
        self.admin = admin
        sam_user, sam = self.activate("sam", display_name="Sam Carter")
        self.call("/api/me", "PATCH", {"color": "#112233"}, sam)
        bot = self.create_bot()
        bot_token = self.mint(bot["id"], "a0-nix")["token"]
        self.call("/api/me", token=bot_token)  # sets last_used
        first = self.call(method="POST", data={**self.ticket(), "assignee_id": bot["id"]}, token=sam)[1]
        self.call(f"/api/tickets/{first['id']}/comments", "POST", {"body": "Looking"}, bot_token)
        second = self.call(method="POST", data=self.ticket("Change"), token=bot_token)[1]
        self.call(f"/api/tickets/{second['id']}", "PATCH", {"status": "Resolved", "assignee_id": sam_user["id"],
                                                         "actual_start": "2026-01-01T01:00:00Z", "actual_end": "2026-01-01T02:00:00Z"}, admin)
        snapshot = lambda: {"tickets": self.call(token=admin)[1],
                            "details": [self.call(f"/api/tickets/{t}", token=admin)[1] for t in (first["id"], second["id"])],
                            "users": self.call("/api/users", token=admin)[1],
                            "assignable": self.call("/api/users/assignable", token=admin)[1],
                            "tokens": self.call(f"/api/users/{bot['id']}/tokens", token=admin)[1]}
        before = snapshot()
        source = Path(sqlite_dir.name) / "triage.db"
        # Import while the SQLite server is still running, as with podman exec.
        fresh = self.fresh_backend()
        result = self.admin_cli("import-sqlite", "--from", str(source), backend=fresh)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("users 3, api_tokens 1, sessions 2, tickets 2, comments 3", result.stdout)
        self.assertNotIn("postgres@", result.stdout)
        self.stop()
        # The same API answers, the same sessions and tokens work.
        self.backend = fresh
        self.start(ADMIN_PASSWORD="")
        self.assertEqual(self.call("/api/health")[1]["db"], "postgres")
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.call("/api/me", token=sam)[1]["color"], "#112233")
        self.assertEqual(self.call("/api/me", token=bot_token)[1]["username"], "claude")
        self.login("admin", ADMIN_PASSWORD)
        # Sequences continue after the imported ids.
        third = self.call(method="POST", data=self.ticket(), token=sam)[1]
        self.assertEqual(third["id"], second["id"] + 1)
        self.assertEqual(self.create_user("alex")["id"], max(u["id"] for u in before["users"]) + 1)
        self.assertEqual(self.call(f"/api/tickets/{first['id']}/comments", "POST", {"body": "More"}, sam)[0], 201)
        self.assertEqual(self.mint(bot["id"], "second")["id"], before["tokens"][0]["id"] + 1)
        # A target that already has data is refused, unless --force (which replaces it).
        refused = self.admin_cli("import-sqlite", "--from", str(source), backend=target_with_admin)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("already has 1 users and 0 tickets", refused.stderr)
        self.assertIn("--force", refused.stderr)
        forced = self.admin_cli("import-sqlite", "--from", str(source), "--force", backend=target_with_admin)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tickets", backend=target_with_admin), [(2,)])
        missing = self.admin_cli("import-sqlite", "--from", str(Path(sqlite_dir.name) / "nope.db"), backend=self.fresh_backend())
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no SQLite database", missing.stderr)

    def test_import_old_schema_sqlite(self):
        if not PG_URL:
            self.skipTest("import-sqlite needs TEST_DATABASE_URL (PostgreSQL)")
        legacy = tempfile.TemporaryDirectory()
        self.addCleanup(legacy.cleanup)
        source = Path(legacy.name) / "triage.db"
        with closing(sqlite3.connect(source)) as db, db:
            db.executescript(OLD_SCHEMA)
            db.execute("INSERT INTO tickets(id,title,description,type,priority,service,requester,status,created,updated) VALUES(42,'Legacy','Keep me','Change','Low','Storage','Alex','Waiting','2026-11-07T12:00:00.000Z','2026-11-07T12:00:00.000Z')")
            db.execute("INSERT INTO comments(ticket_id,author,body,created) VALUES(42,'Alex','Existing update','2026-11-07T12:00:00.000Z')")
        self.stop()
        self.backend = self.fresh_backend()
        result = self.admin_cli("import-sqlite", "--from", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("users 0, api_tokens 0, sessions 0, tickets 1, comments 1", result.stdout)
        # The source file is only read: no columns were added to it.
        with closing(sqlite3.connect(source)) as db:
            self.assertNotIn("assignee_id", {row[1] for row in db.execute("PRAGMA table_info(tickets)")})
        self.start()  # no accounts were imported, so ADMIN_PASSWORD creates the first admin
        token = self.login("admin", ADMIN_PASSWORD)
        ticket = self.call("/api/tickets/42", token=token)[1]
        self.assertEqual((ticket["title"], ticket["requester"], ticket["requester_id"], ticket["status"]), ("Legacy", "Alex", None, "Waiting"))
        self.assertEqual([(c["author"], c["body"], c["kind"]) for c in ticket["comments"]], [("Alex", "Existing update", "comment")])
        self.assertEqual(self.call(method="POST", data=self.ticket(), token=token)[1]["id"], 43)

    def count_users(self, backend):
        try:
            return self.sql("SELECT COUNT(*) FROM users", backend=backend)[0][0]
        except DB_ERRORS:  # no schema at all
            return 0


if __name__ == "__main__":
    unittest.main()
