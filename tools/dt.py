#!/usr/bin/env python3
"""dt: a command-line client for dark-triage, for people, scripts and AI agents.

Standard library only (Python 3.11+). Authenticates with an API token.

Configuration (environment):
  DT_URL         server base URL (default: http://dark-triage.lylat.space)
  DT_TOKEN       API token (dt_...). Takes precedence over DT_TOKEN_FILE.
  DT_TOKEN_FILE  file holding the token (default: ~/.config/dark-triage/token)

Exit codes: 0 ok, 1 the server refused the request (validation, not found,
permissions), 2 usage or configuration error, 3 the server could not be reached.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_URL = "http://dark-triage.lylat.space"
DEFAULT_TOKEN_FILE = "~/.config/dark-triage/token"
# Mirrors server.py (TYPES, PRIORITIES, STATUSES).
TYPES = ("Issue", "Request", "Change")
PRIORITIES = ("Low", "Medium", "High", "Critical")
STATUSES = ("Open", "In progress", "Waiting", "Resolved", "Canceled")
# The web form suggests these services; any text of 1-80 characters is accepted.
SERVICES = ("Jellyfin", "Plex", "Network", "Proxmox", "Storage", "Other")


class DtError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def choice(options, label):
    """argparse type: case-insensitive match; '-', '_' and spaces are interchangeable."""
    key = lambda value: re.sub(r"[\s_-]+", " ", value.strip().lower())
    lookup = {key(option): option for option in options}

    def parse(value):
        if key(value) in lookup:
            return lookup[key(value)]
        raise argparse.ArgumentTypeError(f"invalid {label} {value!r} (choose from {', '.join(options)})")
    return parse


def ticket_id(value):
    match = re.fullmatch(r"(?i)(?:dt-?)?0*(\d{1,18})", value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"not a ticket id: {value!r} (use DT-007 or 7)")
    return int(match[1])


def label(ticket_id_value):
    return f"DT-{ticket_id_value:03d}"


def read_text(value, what):
    """'-' reads stdin; anything else is the literal text."""
    text = sys.stdin.read() if value == "-" else value
    if not text.strip():
        raise DtError(f"the {what} is empty", 2)
    return text


class Client:
    def __init__(self):
        self.base = os.environ.get("DT_URL", DEFAULT_URL).rstrip("/")
        self.token = os.environ.get("DT_TOKEN", "").strip()
        if not self.token:
            path = Path(os.environ.get("DT_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()
            try:
                self.token = path.read_text().strip()
                if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    print(f"dt: warning: {path} is readable by others; run: chmod 600 {path}", file=sys.stderr)
            except FileNotFoundError:
                raise DtError(f"no API token: set DT_TOKEN, or put the token in {path} (or point DT_TOKEN_FILE at it)", 2) from None
            except OSError as exc:
                raise DtError(f"can't read the token file {path}: {exc.strerror}", 2) from None
        if not self.token:
            raise DtError("the API token is empty", 2)
        self._me = None

    def call(self, path, method="GET", data=None):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.base + "/api" + path, data=body, method=method,
                          headers={"Authorization": "Bearer " + self.token, "Accept": "application/json",
                                   "Content-Type": "application/json", "User-Agent": "dt/1"})
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b"null")
        except HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error") or exc.reason
            except (ValueError, AttributeError):
                message = exc.reason
            hint = {401: " (check DT_TOKEN / the token file; the token may be revoked or its account disabled)"}.get(exc.code, "")
            raise DtError(f"{message} [HTTP {exc.code}]{hint}") from None
        except URLError as exc:
            raise DtError(f"can't reach {self.base}: {exc.reason} (set DT_URL?)", 3) from None
        except TimeoutError:
            raise DtError(f"{self.base} timed out", 3) from None

    def me(self):
        if self._me is None:
            self._me = self.call("/me")
        return self._me

    def resolve_user(self, value):
        """me | none | user id | username (or exact display name) of an active account."""
        lowered = value.strip().lower()
        if lowered in ("none", ""):
            return None
        if lowered == "me":
            return self.me()["id"]
        people = self.call("/users/assignable")
        if lowered.isdigit():
            return int(lowered)
        for person in people:
            if person["username"] == lowered:
                return person["id"]
        matches = [p for p in people if p["display_name"].lower() == lowered]
        if len(matches) == 1:
            return matches[0]["id"]
        raise DtError(f"no active user {value!r}; see `dt users` for usernames")

    def url(self, ticket):
        return f"{self.base}/#{label(ticket)}"


def name(person, fallback="unassigned"):
    if not person:
        return fallback
    text = person["display_name"] + (" [bot]" if person.get("is_bot") else "")
    return text + (" (disabled)" if person.get("disabled") else "")


def print_table(rows):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def print_ticket(client, t):
    print(f"{label(t['id'])}  {t['title']}")
    print(f"Status: {t['status']} · Priority: {t['priority']} · Type: {t['type']} · Service: {t['service']}")
    requester = t["requester"] + (" [bot]" if t.get("requester_is_bot") else "")
    print(f"Requester: {requester} · Assignee: {name(t['assignee'])}")
    print(f"Created: {t['created']} · Updated: {t['updated']}")
    print(f"URL: {client.url(t['id'])}")
    print()
    print(t["description"])
    comments = t.get("comments", [])
    print()
    print(f"--- Conversation ({sum(c['kind'] != 'system' for c in comments)}) ---")
    for c in comments:
        author = c["author"] + (" [bot]" if c.get("author_is_bot") else "")
        if c["kind"] == "system":
            print(f"  ◈ {c['created']}  {author} {c['body']}")
        else:
            print(f"[{c['created']}] {author}:")
            for line in c["body"].splitlines() or [""]:
                print("    " + line)
    if not comments:
        print("(no updates yet)")


# --- commands ---------------------------------------------------------------------

def cmd_whoami(client, args):
    me = client.me()
    if args.json:
        return print(json.dumps(me, indent=2, ensure_ascii=False))
    kind = "bot" if me.get("is_bot") else me["role"]
    print(f"{me['display_name']} (@{me['username']}, {kind}, id {me['id']}) on {client.base}")


def cmd_users(client, args):
    people = client.call("/users/assignable")
    if args.json:
        return print(json.dumps(people, indent=2, ensure_ascii=False))
    print_table([("USERNAME", "DISPLAY NAME", "KIND")] +
                [(p["username"], p["display_name"], "bot" if p.get("is_bot") else "person") for p in people])


def cmd_new(client, args):
    if args.description is not None and args.description_file is not None:
        raise DtError("use either --description or --description-file, not both", 2)
    if args.description_file is not None:
        if args.description_file == "-":
            description = read_text("-", "description")
        else:
            try:
                description = Path(args.description_file).read_text()
            except OSError as exc:
                raise DtError(f"can't read {args.description_file}: {exc.strerror}", 2) from None
    elif args.description is not None:
        description = read_text(args.description, "description")
    else:
        raise DtError("a description is required: --description TEXT, --description - (stdin) or --description-file FILE", 2)
    data = {"title": args.title, "description": description, "service": args.service,
            "type": args.type, "priority": args.priority}
    if args.assignee is not None:
        data["assignee_id"] = client.resolve_user(args.assignee)
    ticket = client.call("/tickets", "POST", data)
    if args.json:
        return print(json.dumps(ticket, indent=2, ensure_ascii=False))
    print(f"{label(ticket['id'])} {client.url(ticket['id'])}")


def cmd_list(client, args):
    query = {}
    assignee_username = None
    if args.assignee is not None:
        lowered = args.assignee.strip().lower()
        if lowered in ("me", "none"):
            query["assignee"] = lowered
        else:
            try:
                query["assignee"] = client.resolve_user(args.assignee)
            except DtError:
                assignee_username = lowered  # e.g. a disabled account: filter locally
    tickets = client.call("/tickets" + ("?" + urlencode(query) if query else ""))
    if assignee_username:
        tickets = [t for t in tickets if t["assignee"] and t["assignee"]["username"] == assignee_username]
    if args.status:
        tickets = [t for t in tickets if t["status"] in args.status]
    elif not args.all:
        tickets = [t for t in tickets if t["status"] not in ("Resolved", "Canceled")]
    if args.type:
        tickets = [t for t in tickets if t["type"] == args.type]
    if args.priority:
        tickets = [t for t in tickets if t["priority"] == args.priority]
    if args.mine:
        tickets = [t for t in tickets if t["requester_id"] == client.me()["id"]]
    if args.json:
        return print(json.dumps(tickets, indent=2, ensure_ascii=False))
    if not tickets:
        return print("No matching tickets.", file=sys.stderr)
    rows = [("ID", "STATUS", "PRIORITY", "TYPE", "SERVICE", "ASSIGNEE", "TITLE")]
    for t in tickets:
        title = t["title"] if len(t["title"]) <= 70 else t["title"][:69] + "…"
        rows.append((label(t["id"]), t["status"], t["priority"], t["type"], t["service"], name(t["assignee"], "-"), title))
    print_table(rows)


def cmd_show(client, args):
    ticket = client.call(f"/tickets/{args.id}")
    if args.json:
        return print(json.dumps(ticket, indent=2, ensure_ascii=False))
    print_ticket(client, ticket)


def cmd_comment(client, args):
    body = read_text(args.body, "comment")
    client.call(f"/tickets/{args.id}/comments", "POST", {"body": body})
    print(f"Posted an update on {label(args.id)} {client.url(args.id)}")


def cmd_delete(client, args):
    if not args.yes:
        ticket = client.call(f"/tickets/{args.id}")
        sys.stderr.write(f"Delete {label(args.id)} \"{ticket['title']}\" and its conversation permanently? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            raise SystemExit("Not deleted.")
    client.call(f"/tickets/{args.id}", "DELETE")
    print(f"Deleted {label(args.id)}")


def cmd_update(client, args):
    changes = {}
    if args.status:
        changes["status"] = args.status
    if args.priority:
        changes["priority"] = args.priority
    if args.assignee is not None:
        changes["assignee_id"] = client.resolve_user(args.assignee)
    if not changes:
        raise DtError("nothing to update: pass --status, --priority and/or --assignee", 2)
    ticket = client.call(f"/tickets/{args.id}", "PATCH", changes)
    if args.json:
        return print(json.dumps(ticket, indent=2, ensure_ascii=False))
    print(f"{label(ticket['id'])}: {ticket['status']} · {ticket['priority']} · assignee {name(ticket['assignee'])}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dt", description="Command-line client for dark-triage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""environment:
  DT_URL         server URL (default {DEFAULT_URL})
  DT_TOKEN       API token, or
  DT_TOKEN_FILE  file with the token (default {DEFAULT_TOKEN_FILE})

values:
  --type      {', '.join(TYPES)}
  --priority  {', '.join(PRIORITIES)}
  --status    {', '.join(STATUSES)}  (case-insensitive; in-progress works)
  --service   free text, 1-80 characters; the web form suggests {', '.join(SERVICES)}

exit codes: 0 ok, 1 request refused by the server, 2 usage/config error, 3 server unreachable""")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    status, priority = choice(STATUSES, "status"), choice(PRIORITIES, "priority")

    p = commands.add_parser("whoami", help="show the account the token belongs to")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_whoami)

    p = commands.add_parser("users", help="list active accounts (usernames for --assignee)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_users)

    p = commands.add_parser("new", help="create a ticket; prints DT-### and its URL")
    p.add_argument("--title", required=True, help="up to 160 characters")
    p.add_argument("--description", metavar="TEXT|-", help="details (up to 10000 characters); - reads stdin")
    p.add_argument("--description-file", metavar="FILE|-", help="read the details from a file; - reads stdin")
    p.add_argument("--service", required=True, help=f"e.g. {', '.join(SERVICES)} (free text)")
    p.add_argument("--type", type=choice(TYPES, "type"), default="Issue", help=" | ".join(TYPES) + " (default Issue)")
    p.add_argument("--priority", type=priority, default="Medium", help=" | ".join(PRIORITIES) + " (default Medium)")
    p.add_argument("--assignee", metavar="USERNAME|me", help="assign on creation")
    p.add_argument("--json", action="store_true", help="print the created ticket as JSON")
    p.set_defaults(func=cmd_new)

    p = commands.add_parser("list", help="list tickets (open work by default: not Resolved or Canceled)")
    p.add_argument("--status", type=status, action="append", help="repeatable; overrides the unresolved default")
    p.add_argument("--all", action="store_true", help="include resolved tickets")
    p.add_argument("--assignee", metavar="me|none|USERNAME")
    p.add_argument("--mine", action="store_true", help="only tickets you requested")
    p.add_argument("--type", type=choice(TYPES, "type"))
    p.add_argument("--priority", type=priority)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = commands.add_parser("show", help="show a ticket and its conversation")
    p.add_argument("id", type=ticket_id, metavar="DT-###|ID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = commands.add_parser("comment", help="post an update on a ticket")
    p.add_argument("id", type=ticket_id, metavar="DT-###|ID")
    p.add_argument("--body", required=True, metavar="TEXT|-", help="update text (up to 5000 characters); - reads stdin")
    p.set_defaults(func=cmd_comment)

    p = commands.add_parser("delete", help="delete a ticket and its conversation (admins only)")
    p.add_argument("id", type=ticket_id, metavar="DT-###|ID")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_delete)

    p = commands.add_parser("update", help="change status, priority or assignee")
    p.add_argument("id", type=ticket_id, metavar="DT-###|ID")
    p.add_argument("--status", type=status)
    p.add_argument("--priority", type=priority)
    p.add_argument("--assignee", metavar="USERNAME|me|none", help="none unassigns")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_update)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(Client(), args)
    except DtError as exc:
        print(f"dt: error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
