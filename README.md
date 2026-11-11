# dark_triage

A little chaos. A lot more clarity.

A self-hosted helpdesk for your homelab and friends, built with Python's standard library and SQLite. No Node.js, npm, pip packages, or build step. The browser uses plain HTML, CSS, and JavaScript. A dark interface with hot-pink and cyan accents, responsive layouts, a list and kanban board, live search, priority and status filters, and keyboard shortcuts.

## Run with Docker

```sh
cp .env.example .env
# Edit .env and set ADMIN_PASSWORD to a long random password.
docker compose up -d --build
```

Open http://localhost:3000 and sign in as `admin` (or your `ADMIN_USERNAME`) with `ADMIN_PASSWORD`. Then open **Users** in the sidebar and create an account for each friend. SQLite records persist in the `triage-data` volume across container restarts and rebuilds.

For access outside your LAN, use a trusted VPN or an HTTPS reverse proxy. Forward the original Host header and set `X-Forwarded-Proto` correctly. Do not expose the plain HTTP port directly to the internet: passwords and session tokens travel in every request.

## Run with Python

Requires Python 3.11+ with SQLite support. No third-party dependencies or installation step.

```sh
ADMIN_PASSWORD='your-long-random-password' python3 server.py
```

## Accounts and roles

Everyone signs in with their own username and password. There is no self sign-up: an admin creates each account.

| | user | admin |
|---|---|---|
| See all tickets, create tickets, comment, change status and priority | ✓ | ✓ |
| Create accounts, reset passwords, change roles, disable or enable accounts | | ✓ |

- **New accounts** get a username (2–32 characters: letters, digits, `.`, `-`, `_`; case-insensitive), a display name, a role, and a **temporary password**. At first sign-in the user must choose their own password before they can do anything else.
- **Reset password** (admin) sets a new temporary password, signs the user out everywhere, and forces another change at next sign-in.
- **Disable** (admin) blocks sign-in and revokes all of that account's sessions immediately. **Enable** lets them sign in again. Admins can't disable their own account.
- There must always be at least one active admin. The server refuses to demote or disable the last one.
- **Change password** (everyone): in the sidebar, or the ✎ button in the header. It requires the current password and signs out your other devices.
- Tickets and comments record the author's account. Names are shown from the account's current display name, so they can't be spoofed. Tickets and comments from the shared-password version keep the name that was typed in.

Passwords are hashed with scrypt (random salt per password) and must be 8–256 characters. Sign-in returns a random session token. The browser keeps it in local storage and sends it as `Authorization: Bearer <token>`. The server stores only a SHA-256 hash of the token. Sessions slide: each one lasts 14 days after its last use. **Log out** ends the current session. After 5 failed sign-ins for one username, or 20 from one IP address within 15 minutes, further attempts are delayed with exponential backoff (up to 15 minutes). The counters are in memory and reset on restart.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Username of the admin account created on first start. |
| `ADMIN_PASSWORD` | — | Password of that account. **Required on first start** (empty database) unless `DEV_MODE=1`. Ignored once any account exists. To change it later, use **Change password** in the app. |
| `DEV_MODE` | unset | `1` relaxes the `ADMIN_PASSWORD` requirement for local development: a random admin password is generated and printed to the log. Never use it in production. |
| `TRUST_PROXY` | unset | `1` takes the client IP for login throttling from the last `X-Forwarded-For` entry. Set it only behind your own reverse proxy. |
| `PORT` | `3000` | Listening port. |
| `DATA_DIR` | `data` | Directory holding `triage.db`. |

### Lost the last admin password?

Stop the app and back up the data directory. Then delete the user rows. Tickets and comments are kept.

```sh
sqlite3 data/triage.db "DELETE FROM sessions; DELETE FROM users;"
```

Start again with `ADMIN_PASSWORD` set and a fresh admin is created. Re-create the other accounts. Their old tickets and comments still show the stored names.

## Upgrading from the shared-password version

The database file, ticket IDs, Docker volume, and container user ID (1000) are unchanged. The schema is migrated in place on startup: new `users` and `sessions` tables are added, plus nullable `tickets.requester_id` and `comments.author_id` columns. No rows are rewritten. Back up the data directory first anyway.

1. Stop the old app.
2. In `.env` (or your environment), replace `ACCESS_PASSWORD=…` with `ADMIN_PASSWORD=…` and optionally `ADMIN_USERNAME=…`. If `ACCESS_PASSWORD` is still set, it is **ignored** and a deprecation warning is logged.
3. `docker compose up -d --build`, or start `python3 server.py` with the same `DATA_DIR`.
4. Sign in as the admin and create accounts for your friends. The shared password no longer opens anything, and stored browser sessions from the old version are discarded.

API changes: the `requester` field on `POST /api/tickets` and the `author` field on comments are now ignored, because the signed-in user is the author. Every `/api/*` call except `POST /api/login` needs a session token.

## API

| Method & path | Who | Notes |
|---|---|---|
| `POST /api/login` `{username,password}` | anyone | → `{token, expires_in, user}`; 401 bad credentials, 403 disabled, 429 throttled (`Retry-After`) |
| `POST /api/logout` | signed in | Revokes the current token |
| `GET /api/me` | signed in | Current user (`must_change` is true while on a temporary password) |
| `POST /api/me/password` `{current_password,new_password}` | signed in | Clears `must_change`, signs out other sessions |
| `GET/POST /api/tickets`, `GET/PATCH /api/tickets/:id`, `POST /api/tickets/:id/comments` | signed in | As before, minus self-reported names |
| `GET/POST /api/users`, `GET/PATCH /api/users/:id` `{display_name?,role?,disabled?}`, `POST /api/users/:id/password` `{password}` | admin | 403 for users, 409 for duplicate username or last-admin protection |

While `must_change` is true, everything except `/api/me`, `/api/me/password` and `/api/logout` returns 403 with `"code": "password_change_required"`. Cross-origin writes are rejected: a request whose `Origin` doesn't match its `Host` gets 403.

## Ticket workflow

- **Issues:** report broken services, errors, or outages.
- **Requests:** ask for a service, media, access, or something new.
- **Changes:** propose upgrades, maintenance, and improvements. Include impact, timing, and rollback details in the description.

Create a ticket with a title, details, service, and priority. You are recorded as the requester. Open a ticket to move it through **Open → In progress → Waiting → Resolved**, adjust its priority, or post an update. Use **N** for a new ticket (a new user on the Users page) and **/** to search. Resolved tickets remain searchable and can be reopened. The app starts empty, with no fabricated service health or demo tickets.

## Storage and operations

SQLite uses WAL mode and parameterized queries. Back up the entire data directory while the app is stopped (or use SQLite's online backup tooling). Never delete the Docker volume unless you intend to delete all tickets and accounts.

```sh
python3 -m unittest discover -s test -v
```

The integration tests start the real server. They cover admin bootstrap, login and logout, wrong passwords and throttling, forced password change for temporary passwords, admin create, reset, role change, disable and enable, non-admins being refused user management, last-admin protection, session revocation, shared ticket rights (status, priority, comments), validation, cross-origin rejection, persistence across restarts, and migration of an old-schema database. Browser visual QA and Docker execution are not included.
