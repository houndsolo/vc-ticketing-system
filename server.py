"""dark_triage: dependency-free Python HTTP API and SQLite storage."""

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
TYPES = ("Issue", "Request", "Change")
PRIORITIES = ("Low", "Medium", "High", "Critical")
STATUSES = ("Open", "In progress", "Waiting", "Resolved")
MAX_BODY = 24000
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
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid(value, maximum):
    return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= maximum


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Handler(BaseHTTPRequestHandler):
    # One request per connection also safely discards unread rejected bodies.
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, content, content_type="application/json; charset=utf-8"):
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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

    def handle_request(self):
        try:
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                password = self.server.password
                token = self.headers.get("Authorization", "")
                if password and not hmac.compare_digest(token.encode(), ("Bearer " + password).encode()):
                    raise RequestError(401, "Enter the lab access password.")
                origin = self.headers.get("Origin")
                scheme = self.headers.get("X-Forwarded-Proto", "http")
                if origin and origin != f"{scheme}://{self.headers.get('Host')}":
                    raise RequestError(403, "Cross-origin request rejected")
                with sqlite3.connect(self.server.database, timeout=10) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA foreign_keys=ON")
                    status, data = self.api(db, path)
                # Commit the transaction before returning success to the client.
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
            self.respond(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.exception("Request failed")
            self.respond(500, {"error": "Unable to process request"})

    def api(self, db, path):
        if path == "/api/tickets":
            if self.command == "GET":
                return 200, [dict(row) for row in db.execute("SELECT * FROM tickets ORDER BY updated DESC, id DESC")]
            if self.command == "POST":
                x = self.body()
                fields = {"title": 160, "description": 10000, "requester": 80, "service": 80}
                if not all(valid(x.get(k), limit) for k, limit in fields.items()) or x.get("type") not in TYPES or x.get("priority") not in PRIORITIES:
                    raise RequestError(400, "Complete all fields with valid values.")
                timestamp = now()
                cursor = db.execute("INSERT INTO tickets(title,description,type,priority,service,requester,created,updated) VALUES(?,?,?,?,?,?,?,?)", (x["title"].strip(), x["description"].strip(), x["type"], x["priority"], x["service"].strip(), x["requester"].strip(), timestamp, timestamp))
                return 201, dict(db.execute("SELECT * FROM tickets WHERE id=?", (cursor.lastrowid,)).fetchone())
            raise RequestError(405, "Method not allowed")
        match = re.fullmatch(r"/api/tickets/([0-9]{1,18})(/comments)?", path)
        if not match:
            raise RequestError(404, "Endpoint not found")
        ticket_id = int(match[1])
        row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None:
            raise RequestError(404, "Ticket not found")
        if self.command == "GET":
            ticket = dict(row)
            ticket["comments"] = [dict(c) for c in db.execute("SELECT * FROM comments WHERE ticket_id=? ORDER BY id", (ticket_id,))]
            return 200, ticket
        if self.command == "PATCH" and not match[2]:
            x = self.body()
            if x.get("status") not in STATUSES or x.get("priority") not in PRIORITIES:
                raise RequestError(400, "Invalid status or priority")
            db.execute("UPDATE tickets SET status=?,priority=?,updated=? WHERE id=?", (x["status"], x["priority"], now(), ticket_id))
            return 200, {"ok": True}
        if self.command == "POST" and match[2]:
            x = self.body()
            if not valid(x.get("author"), 80) or not valid(x.get("body"), 5000):
                raise RequestError(400, "Enter your name and a comment.")
            timestamp = now()
            db.execute("INSERT INTO comments(ticket_id,author,body,created) VALUES(?,?,?,?)", (ticket_id, x["author"].strip(), x["body"].strip(), timestamp))
            db.execute("UPDATE tickets SET updated=? WHERE id=?", (timestamp, ticket_id))
            return 201, {"ok": True}
        raise RequestError(405, "Method not allowed")

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request
    do_PUT = handle_request


def create_server(host="0.0.0.0", port=3000, data_dir="data", password=""):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "triage.db"
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
    server = ThreadingHTTPServer((host, port), Handler)
    server.database = database
    server.password = password
    return server


def main():
    password = os.environ.get("ACCESS_PASSWORD", "")
    if not password and os.environ.get("DEV_MODE") != "1":
        raise SystemExit("Set ACCESS_PASSWORD before running, or DEV_MODE=1 for local development.")
    server = create_server(port=int(os.environ.get("PORT", "3000")), data_dir=os.environ.get("DATA_DIR", "data"), password=password)
    print(f"dark_triage listening on port {server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
