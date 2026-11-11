"""dark_triage: dependency-free Python HTTP API and SQLite storage."""

from contextlib import closing
import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
TYPES = ("Issue", "Request", "Change")
PRIORITIES = ("Low", "Medium", "High", "Critical")
STATUSES = ("Open", "In progress", "Waiting", "Resolved")
ROLES = ("admin", "user")
MAX_BODY = 24000
SESSION_TTL = 14 * 24 * 3600  # sliding: every authenticated request extends it
SESSION_REFRESH = 300  # write the new expiry at most this often per session
PASSWORD_MIN, PASSWORD_MAX = 8, 256
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
 description TEXT NOT NULL, type TEXT NOT NULL, priority TEXT NOT NULL,
 service TEXT NOT NULL, requester TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'Open', created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_id INTEGER NOT NULL REFERENCES tickets(id), author TEXT NOT NULL,
 body TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
 password_hash TEXT NOT NULL,
 must_change INTEGER NOT NULL DEFAULT 0,
 disabled INTEGER NOT NULL DEFAULT 0,
 created TEXT NOT NULL, updated TEXT NOT NULL, last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 created INTEGER NOT NULL, expires INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS comments_ticket ON comments(ticket_id);
"""
# Columns added after the shared-password version. Existing rows keep NULL ids
# and their stored requester/author names.
MIGRATIONS = (
    ("tickets", "requester_id", "INTEGER REFERENCES users(id)"),
    ("comments", "author_id", "INTEGER REFERENCES users(id)"),
)
TICKET_COLUMNS = ("t.id,t.title,t.description,t.type,t.priority,t.service,"
                  "COALESCE(u.display_name,t.requester) AS requester,t.requester_id,"
                  "t.status,t.created,t.updated")
TICKET_QUERY = f"SELECT {TICKET_COLUMNS} FROM tickets t LEFT JOIN users u ON u.id=t.requester_id"
COMMENT_QUERY = ("SELECT c.id,c.ticket_id,COALESCE(u.display_name,c.author) AS author,c.author_id,"
                 "c.body,c.created FROM comments c LEFT JOIN users u ON u.id=c.author_id")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid(value, maximum):
    return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= maximum


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    encode = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the username doesn't exist, so timing doesn't reveal accounts.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(password, username=""):
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f"Passwords must be {PASSWORD_MIN} to {PASSWORD_MAX} characters."
    if username and password.lower() == username.lower():
        return "The password can't be the same as the username."
    return None


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(row):
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "role": row["role"], "must_change": bool(row["must_change"]), "disabled": bool(row["disabled"]),
            "created": row["created"], "last_login": row["last_login"]}


class Throttle:
    """In-memory failure backoff. After `limit` failures inside `window`
    seconds, each further failure doubles the lockout up to `cap` seconds."""

    def __init__(self, limit, window=900, cap=900):
        self.limit, self.window, self.cap = limit, window, cap
        self.entries = {}  # key -> [failures, last_failure, blocked_until]
        self.lock = threading.Lock()

    def retry_after(self, key):
        with self.lock:
            entry = self.entries.get(key)
            return max(0, int(entry[2] - time.monotonic() + 0.999)) if entry else 0

    def fail(self, key):
        moment = time.monotonic()
        with self.lock:
            if len(self.entries) > 10000:
                self.entries = {k: e for k, e in self.entries.items()
                                if moment - e[1] < self.window or e[2] > moment}
            entry = self.entries.get(key)
            if entry is None or (moment - entry[1] > self.window and entry[2] <= moment):
                entry = self.entries[key] = [0, moment, 0]
            entry[0] += 1
            entry[1] = moment
            if entry[0] >= self.limit:
                entry[2] = moment + min(self.cap, 2 ** (entry[0] - self.limit))

    def success(self, key):
        with self.lock:
            self.entries.pop(key, None)


class RequestError(Exception):
    def __init__(self, status, message, headers=None, **extra):
        self.status, self.message, self.headers, self.extra = status, message, headers or {}, extra


class Handler(BaseHTTPRequestHandler):
    # One request per connection also safely discards unread rejected bodies.
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, content, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Transfer-Encoding is not supported")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError(400, "Invalid content length") from None
        if length < 0 or length > MAX_BODY:
            raise RequestError(413, "Request too large")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            data = json.loads(raw or b"{}")
        except (ValueError, UnicodeError):
            raise RequestError(400, "Invalid JSON body") from None
        if not isinstance(data, dict):
            raise RequestError(400, "Expected a JSON object")
        return data

    def client_ip(self):
        if self.server.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded.strip():
                # The last hop is the one the trusted proxy appended.
                return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def handle_request(self):
        try:
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                origin = self.headers.get("Origin")
                scheme = self.headers.get("X-Forwarded-Proto", "http")
                if origin and origin != f"{scheme}://{self.headers.get('Host')}":
                    raise RequestError(403, "Cross-origin request rejected")
                with closing(sqlite3.connect(self.server.database, timeout=10)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA foreign_keys=ON")
                    status, data = self.api(db, path)
                # The transaction is committed before success is returned to the client.
                self.respond(status, data)
                return
            files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if path not in files:
                raise RequestError(404, "Not found")
            if self.command not in ("GET", "HEAD"):
                raise RequestError(405, "Method not allowed")
            name, mime = files[path]
            self.respond(200, (ROOT / "public" / name).read_bytes(), mime)
        except RequestError as exc:
            self.respond(exc.status, {"error": exc.message, **exc.extra}, headers=exc.headers)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.exception("Request failed")
            self.respond(500, {"error": "Unable to process request"})

    # --- authentication -------------------------------------------------

    def login(self, db):
        x = self.body()
        username, password = x.get("username"), x.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
            raise RequestError(400, "Enter your username and password.")
        username = username.strip().lower()[:64]
        password = password[:PASSWORD_MAX * 4]
        user_key, ip_key = username, self.client_ip()
        wait = max(self.server.user_throttle.retry_after(user_key), self.server.ip_throttle.retry_after(ip_key))
        if wait:
            raise RequestError(429, f"Too many failed sign-ins. Try again in {wait} seconds.", {"Retry-After": str(wait)})
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not verify_password(password, user["password_hash"] if user else DUMMY_HASH) or user is None:
            self.server.user_throttle.fail(user_key)
            self.server.ip_throttle.fail(ip_key)
            raise RequestError(401, "That username and password didn’t match.")
        self.server.user_throttle.success(user_key)
        if user["disabled"]:
            raise RequestError(403, "This account is disabled. Ask an admin to re-enable it.")
        token = secrets.token_urlsafe(32)
        moment = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires<?", (moment,))
        db.execute("INSERT INTO sessions(token_hash,user_id,created,expires,last_seen) VALUES(?,?,?,?,?)",
                   (token_hash(token), user["id"], moment, moment + SESSION_TTL, moment))
        db.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user["id"]))
        user = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        return 200, {"token": token, "expires_in": SESSION_TTL, "user": public_user(user)}

    def authenticate(self, db):
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or len(token) > 256:
            raise RequestError(401, "Sign in to continue.")
        self.token_hash = token_hash(token)
        row = db.execute("SELECT user_id,expires,last_seen FROM sessions WHERE token_hash=?", (self.token_hash,)).fetchone()
        moment = int(time.time())
        if row is None or row["expires"] <= moment:
            raise RequestError(401, "Your session has ended. Sign in again.")
        user = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if user is None or user["disabled"]:
            raise RequestError(401, "Sign in to continue.")
        if moment - row["last_seen"] >= SESSION_REFRESH:
            db.execute("UPDATE sessions SET expires=?,last_seen=? WHERE token_hash=?",
                       (moment + SESSION_TTL, moment, self.token_hash))
        return user

    def change_own_password(self, db, user):
        x = self.body()
        current, new = x.get("current_password"), x.get("new_password")
        wait = self.server.user_throttle.retry_after(user["username"])
        if wait:
            raise RequestError(429, f"Too many failed attempts. Try again in {wait} seconds.", {"Retry-After": str(wait)})
        if not isinstance(current, str) or not verify_password(current[:PASSWORD_MAX * 4], user["password_hash"]):
            self.server.user_throttle.fail(user["username"])
            raise RequestError(400, "Your current password is incorrect.")
        problem = password_problem(new, user["username"])
        if problem:
            raise RequestError(400, problem)
        if new == current:
            raise RequestError(400, "Choose a password different from the current one.")
        db.execute("UPDATE users SET password_hash=?,must_change=0,updated=? WHERE id=?",
                   (hash_password(new), now(), user["id"]))
        # Sign out every other device; keep this session.
        db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], self.token_hash))
        return 200, {"user": public_user(db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())}

    # --- user management (admins only) ------------------------------------

    def other_active_admins(self, db, user_id):
        return db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?", (user_id,)).fetchone()[0]

    def users_api(self, db, path, admin):
        if admin["role"] != "admin":
            raise RequestError(403, "Only admins can manage users.")
        if path == "/api/users":
            if self.command == "GET":
                return 200, [public_user(r) for r in db.execute("SELECT * FROM users ORDER BY username")]
            if self.command == "POST":
                x = self.body()
                username = x.get("username").strip().lower() if isinstance(x.get("username"), str) else ""
                if not USERNAME_RE.fullmatch(username):
                    raise RequestError(400, "Usernames are 2–32 characters: letters, digits, dot, dash, underscore.")
                if not valid(x.get("display_name"), 80):
                    raise RequestError(400, "Enter a display name (up to 80 characters).")
                if x.get("role") not in ROLES:
                    raise RequestError(400, "Role must be admin or user.")
                problem = password_problem(x.get("password"), username)
                if problem:
                    raise RequestError(400, problem)
                timestamp = now()
                try:
                    cursor = db.execute("INSERT INTO users(username,display_name,role,password_hash,must_change,created,updated) VALUES(?,?,?,?,1,?,?)",
                                        (username, x["display_name"].strip(), x["role"], hash_password(x["password"]), timestamp, timestamp))
                except sqlite3.IntegrityError:
                    raise RequestError(409, "That username is already taken.") from None
                return 201, public_user(db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone())
            raise RequestError(405, "Method not allowed")
        match = re.fullmatch(r"/api/users/([0-9]{1,18})(/password)?", path)
        if not match:
            raise RequestError(404, "Endpoint not found")
        user_id = int(match[1])
        x = self.body() if self.command in ("POST", "PATCH") else {}
        # Serialize admin changes so two requests can't both remove "the other" admin.
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise RequestError(404, "User not found")
        if match[2]:
            if self.command != "POST":
                raise RequestError(405, "Method not allowed")
            password = x.get("password")
            problem = password_problem(password, target["username"])
            if problem:
                raise RequestError(400, problem)
            db.execute("UPDATE users SET password_hash=?,must_change=1,updated=? WHERE id=?",
                       (hash_password(password), now(), user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return 200, public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
        if self.command == "GET":
            return 200, public_user(target)
        if self.command != "PATCH":
            raise RequestError(405, "Method not allowed")
        display_name = x.get("display_name", target["display_name"])
        role = x.get("role", target["role"])
        disabled = x.get("disabled", bool(target["disabled"]))
        if not valid(display_name, 80):
            raise RequestError(400, "Enter a display name (up to 80 characters).")
        if role not in ROLES:
            raise RequestError(400, "Role must be admin or user.")
        if not isinstance(disabled, bool):
            raise RequestError(400, "disabled must be true or false.")
        if disabled and user_id == admin["id"]:
            raise RequestError(400, "You can’t disable your own account.")
        was_active_admin = target["role"] == "admin" and not target["disabled"]
        stays_active_admin = role == "admin" and not disabled
        if was_active_admin and not stays_active_admin and self.other_active_admins(db, user_id) == 0:
            raise RequestError(409, "Keep at least one active admin.")
        db.execute("UPDATE users SET display_name=?,role=?,disabled=?,updated=? WHERE id=?",
                   (display_name.strip(), role, int(disabled), now(), user_id))
        if disabled:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return 200, public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    # --- routing ------------------------------------------------------------

    def api(self, db, path):
        if path == "/api/login":
            if self.command != "POST":
                raise RequestError(405, "Method not allowed")
            return self.login(db)
        user = self.authenticate(db)
        if path == "/api/me":
            if self.command != "GET":
                raise RequestError(405, "Method not allowed")
            return 200, public_user(user)
        if path == "/api/logout":
            if self.command != "POST":
                raise RequestError(405, "Method not allowed")
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash,))
            return 200, {"ok": True}
        if path == "/api/me/password":
            if self.command != "POST":
                raise RequestError(405, "Method not allowed")
            return self.change_own_password(db, user)
        if user["must_change"]:
            raise RequestError(403, "Set a new password before continuing.", code="password_change_required")
        if path == "/api/users" or path.startswith("/api/users/"):
            return self.users_api(db, path, user)
        return self.tickets_api(db, path, user)

    def tickets_api(self, db, path, user):
        if path == "/api/tickets":
            if self.command == "GET":
                return 200, [dict(row) for row in db.execute(TICKET_QUERY + " ORDER BY t.updated DESC, t.id DESC")]
            if self.command == "POST":
                x = self.body()
                fields = {"title": 160, "description": 10000, "service": 80}
                if not all(valid(x.get(k), limit) for k, limit in fields.items()) or x.get("type") not in TYPES or x.get("priority") not in PRIORITIES:
                    raise RequestError(400, "Complete all fields with valid values.")
                timestamp = now()
                cursor = db.execute("INSERT INTO tickets(title,description,type,priority,service,requester,requester_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                                    (x["title"].strip(), x["description"].strip(), x["type"], x["priority"], x["service"].strip(), user["display_name"], user["id"], timestamp, timestamp))
                return 201, dict(db.execute(TICKET_QUERY + " WHERE t.id=?", (cursor.lastrowid,)).fetchone())
            raise RequestError(405, "Method not allowed")
        match = re.fullmatch(r"/api/tickets/([0-9]{1,18})(/comments)?", path)
        if not match:
            raise RequestError(404, "Endpoint not found")
        ticket_id = int(match[1])
        row = db.execute(TICKET_QUERY + " WHERE t.id=?", (ticket_id,)).fetchone()
        if row is None:
            raise RequestError(404, "Ticket not found")
        if self.command == "GET" and not match[2]:
            ticket = dict(row)
            ticket["comments"] = [dict(c) for c in db.execute(COMMENT_QUERY + " WHERE c.ticket_id=? ORDER BY c.id", (ticket_id,))]
            return 200, ticket
        if self.command == "PATCH" and not match[2]:
            x = self.body()
            if x.get("status") not in STATUSES or x.get("priority") not in PRIORITIES:
                raise RequestError(400, "Invalid status or priority")
            db.execute("UPDATE tickets SET status=?,priority=?,updated=? WHERE id=?", (x["status"], x["priority"], now(), ticket_id))
            return 200, {"ok": True}
        if self.command == "POST" and match[2]:
            x = self.body()
            if not valid(x.get("body"), 5000):
                raise RequestError(400, "Write a comment first.")
            timestamp = now()
            db.execute("INSERT INTO comments(ticket_id,author,author_id,body,created) VALUES(?,?,?,?,?)",
                       (ticket_id, user["display_name"], user["id"], x["body"].strip(), timestamp))
            db.execute("UPDATE tickets SET updated=? WHERE id=?", (timestamp, ticket_id))
            return 201, {"ok": True}
        raise RequestError(405, "Method not allowed")

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request
    do_PUT = handle_request


def migrate(db):
    db.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logging.info("Migrated database: added %s.%s", table, column)
    db.commit()


def bootstrap_admin(db, username, password, dev_mode):
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        if password:
            logging.info("ADMIN_PASSWORD is ignored because user accounts already exist.")
        return
    username = (username or "admin").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise SystemExit("ADMIN_USERNAME must be 2–32 characters: letters, digits, dot, dash, underscore.")
    if not password:
        if not dev_mode:
            raise SystemExit("Set ADMIN_PASSWORD to create the first admin account, or DEV_MODE=1 for local development.")
        password = secrets.token_urlsafe(12)
        logging.warning("DEV_MODE: created admin %r with generated password: %s", username, password)
    problem = password_problem(password, username)
    if problem:
        raise SystemExit("ADMIN_PASSWORD: " + problem)
    timestamp = now()
    db.execute("INSERT INTO users(username,display_name,role,password_hash,must_change,created,updated) VALUES(?,?,'admin',?,0,?,?)",
               (username, username, hash_password(password), timestamp, timestamp))
    db.commit()
    logging.warning("Created the first admin account %r.", username)


def create_server(host="0.0.0.0", port=3000, data_dir="data", admin_username="admin", admin_password="",
                  dev_mode=False, trust_proxy=False):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "triage.db"
    with closing(sqlite3.connect(database)) as db:
        migrate(db)
        bootstrap_admin(db, admin_username, admin_password, dev_mode)
    server = ThreadingHTTPServer((host, port), Handler)
    server.database = database
    server.trust_proxy = trust_proxy
    server.user_throttle = Throttle(limit=5)
    server.ip_throttle = Throttle(limit=20)
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.environ.get("ACCESS_PASSWORD"):
        logging.warning("ACCESS_PASSWORD is deprecated and ignored: dark_triage now uses user accounts. "
                        "Remove it from your environment; see README.md for upgrade notes.")
    server = create_server(port=int(os.environ.get("PORT", "3000")), data_dir=os.environ.get("DATA_DIR", "data"),
                           admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
                           admin_password=os.environ.get("ADMIN_PASSWORD", ""),
                           dev_mode=os.environ.get("DEV_MODE") == "1",
                           trust_proxy=os.environ.get("TRUST_PROXY") == "1")
    print(f"dark_triage listening on port {server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
