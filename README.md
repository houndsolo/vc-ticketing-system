# dark_triage

A little chaos. A lot more clarity.

A self-hosted helpdesk for your homelab and friends, built with Python's standard library and SQLite. No Node.js, npm, pip packages, or build step. The browser uses plain HTML, CSS, and JavaScript. A dark interface with violet and green accents, responsive layouts, a list and kanban board, live search, priority and status filters, and keyboard shortcuts.

## Run with Docker

```sh
cp .env.example .env
# Edit .env and set ACCESS_PASSWORD to a long random password.
docker compose up -d --build
```

Open http://localhost:3000 and enter your lab password. Share the address and password with your friends. SQLite records persist in the `triage-data` volume across container restarts and rebuilds.

For access outside your LAN, use a trusted VPN or an HTTPS reverse proxy. Forward the original Host header and set `X-Forwarded-Proto` correctly. Do not expose the plain HTTP port directly to the internet. This app uses one shared lab password: everyone with it can read all tickets, update status and priority, and comment. Names are self-reported, not verified identities. There are no separate admin roles, accounts, or email notifications.

## Run with Python

Requires Python 3.11+ with SQLite support. No third-party dependencies or installation step.

```sh
ACCESS_PASSWORD='your-long-random-password' python3 server.py
```

The server requires a password by default. For local development only, `DEV_MODE=1 python3 server.py` allows access without one. Configuration: `PORT` defaults to `3000`; `DATA_DIR` defaults to `data`.

## Upgrading from the initial version

The database filename, tables, ticket IDs, API, and Docker volume are unchanged. Stop the old app before starting the Python version against the same data. For Docker, pull the updated source and run `docker compose up -d --build` from the same directory. Keep your `.env` and `triage-data` volume; no data export or migration is required. The container retains user ID 1000 so existing volume permissions continue to work. For a direct installation, replace the old start command with the Python command above and keep `DATA_DIR` pointing at your existing directory.

## Ticket workflow

- **Issues:** report broken services, errors, or outages.
- **Requests:** ask for a service, media, access, or something new.
- **Changes:** propose upgrades, maintenance, and improvements. Include impact, timing, and rollback details in the description.

Create a ticket with a title, details, service, priority, and your name. Open a ticket to move it through **Open → In progress → Waiting → Resolved**, adjust its priority, or post an update. Use **N** for a new ticket and **/** to search. Resolved tickets remain searchable and can be reopened. The app starts empty, with no fabricated service health or demo tickets.

## Storage and operations

SQLite uses WAL mode and parameterized queries. Back up the entire data directory while the app is stopped (or use SQLite's online backup tooling). Never delete the Docker volume unless you intend to delete all tickets. Restarting with a new `ACCESS_PASSWORD` rotates shared access; existing browser sessions must enter the new password. The password is retained in browser session storage and clears when that tab session ends. Only the optional display name is kept in local storage.

```sh
python3 -m unittest discover -s test -v
```

The integration test covers password enforcement, validation, all three ticket types, status/priority changes, comments, cross-origin write rejection, and persistence across process restarts. Browser visual QA and Docker execution are not included in this test.
