# dark-triage

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
| See all tickets, create tickets, comment, change status, priority and assignee | ✓ | ✓ |
| Change your own password and avatar color, manage your own API tokens | ✓ | ✓ |
| Create accounts and bots, reset passwords, change roles and anyone's avatar color, manage anyone's API tokens, disable or enable accounts | | ✓ |

- **New accounts** get a username (2–32 characters: letters, digits, `.`, `-`, `_`; case-insensitive), a display name, a role, and a **temporary password**. At first sign-in the user must choose their own password before they can do anything else.
- **Reset password** (admin) sets a new temporary password, signs the user out everywhere, and forces another change at next sign-in.
- **Disable** (admin) blocks sign-in and revokes all of that account's sessions immediately. **Enable** lets them sign in again. Admins can't disable their own account.
- There must always be at least one active admin. The server refuses to demote or disable the last one.
- **Change password** (everyone): in the sidebar, or the ✎ button in the header. It requires the current password and signs out your other devices.
- Tickets and comments record the author's account. Names are shown from the account's current display name, so they can't be spoofed. Tickets and comments from the shared-password version keep the name that was typed in.

Passwords are hashed with scrypt (random salt per password) and must be 8–256 characters. Sign-in returns a random session token. The browser keeps it in local storage and sends it as `Authorization: Bearer <token>`. The server stores only a SHA-256 hash of the token. Sessions slide: each one lasts 14 days after its last use. **Log out** ends the current session. After 5 failed sign-ins for one username, or 20 from one IP address within 15 minutes, further attempts are delayed with exponential backoff (up to 15 minutes). The counters are in memory and reset on restart.

## Avatar colors

Every person's avatar circle has its own color: in list rows, kanban cards, the ticket view (requester, assignee, updates and activity lines), the sidebar profile and the Users page.

- **Default:** with no color chosen, the server picks one from a fixed palette of ten colors that read well on the dark theme (hot pink, cyan, violet, mint, gold, rose, light pink, blue, orange and lime). The pick is a hash of the username, so it is stable across restarts and devices. Tickets and comments from the shared-password version use the hash of their stored name.
- **Readable initials:** the circle is filled with the color, and the initials are dark or white, whichever contrasts more (WCAG luminance). Custom dark colors stay readable.
- **Your own color** (everyone): click **Color** under your name in the sidebar. Pick a palette swatch, or **Custom** for any color from the native picker. It saves immediately. **Use default** goes back to the hashed color.
- **Anyone's color** (admin): on the **Users** page, click a person's color circle to pick a new one. **Reset** returns it to the default.
- Colors are stored as `#rrggbb` in the nullable `users.color` column. `NULL` means the default.

## Bots and API

AI agents and scripts file and update tickets through the API, as **bot accounts** with **API tokens**. For example, Claude Code on a0-nix or Hermes running Qwen on the V100 box.

- A **bot** is an account with no password. It can't use the sign-in page: a password login for a bot gets the same 401 as an unknown username, with the same throttling, so it doesn't reveal that the account exists. A bot always has the `user` role. It has the same ticket rights as everyone else, and it can be **assigned** tickets ("assign to Claude"). Bots show a small `bot` tag next to their name everywhere: lists, cards, the ticket view, comments, activity lines, the assignee picker and the Users page.
- An **API token** (`dt_…`) acts as its account on every `/api` endpoint: `Authorization: Bearer dt_…`. It's shown **once** when created. The server stores only its SHA-256 and records when it was last used. Any account can have tokens, people included. Tokens don't expire: revoke them when they're no longer needed.
- **Revoke** one token, or **disable** the account to stop all its tokens at once. Re-enabling the account makes them work again.
- A token can't create or revoke tokens, change a password, or sign out. Those need a signed-in browser session, or the admin CLI. A leaked token therefore can't make itself permanent.

### Create a bot and a token in the web app

Users page (admin): **New bot** takes a username and a display name, then opens the bot's **API tokens** dialog. Name the token after where it will live (e.g. `a0-nix claude-code`), click **Create token** and **Copy** it. It won't be shown again. The same dialog lists each token's name, creation time and last use, and has a **Revoke** button per token. Everyone can manage their own tokens via **API tokens** in the sidebar profile area.

### Or with the admin CLI on the server host

`server.py admin` manages accounts and tokens directly in the database. It reads `DATA_DIR` like the server, and it is safe to run while the server is up (SQLite WAL).

```sh
# Docker / Podman: run inside the container
podman exec dark-triage python server.py admin create-user --username claude --display-name Claude --bot
podman exec dark-triage python server.py admin create-user --username qwen --display-name "Qwen V100" --bot --color '#21e2d7'
podman exec dark-triage python server.py admin create-token --username claude --name a0-nix > claude.token   # prints only the token
podman exec dark-triage python server.py admin list-users
podman exec dark-triage python server.py admin revoke-token --username claude --name a0-nix

# Plain Python
DATA_DIR=/path/to/data python3 server.py admin list-users
```

- `create-user --username U --display-name D [--role user|admin] [--bot] [--color '#rrggbb']` is idempotent. If the username exists, it prints a message, changes nothing and exits 0. A person (not `--bot`) gets a random temporary password, printed once, which they must change at first sign-in. On an empty database this can also create the first admin.
- `create-token --username U --name N` prints only the token on stdout, so it can be redirected straight into a file. Token names are unique among a user's active tokens.
- `list-users` shows kind (admin, user or bot), status, active token count and last activity.
- `revoke-token --username U --name N` revokes that active token.
- Errors go to stderr with exit code 1. Commands other than `create-user` refuse to run if there is no database at `DATA_DIR`, which usually means a wrong path.

### The `dt` command-line client

`tools/dt.py` is a single file that needs only the standard library (Python 3.11+). Copy it anywhere, e.g. `install -m 755 tools/dt.py ~/.local/bin/dt`.

| Setting | Default | |
|---|---|---|
| `DT_URL` | `http://dark-triage.lylat.space` | Server base URL |
| `DT_TOKEN` | — | API token. Takes precedence over the file |
| `DT_TOKEN_FILE` | `~/.config/dark-triage/token` | File holding the token. Keep it `chmod 600`: `dt` warns if others can read it |

```sh
mkdir -p ~/.config/dark-triage && install -m 600 claude.token ~/.config/dark-triage/token
dt whoami
dt new --title "Jellyfin transcodes stall" --service Jellyfin --priority High --description - < notes.txt
dt new --title "Add AdGuard" --type Request --service Network --description "From Haystack" --assignee haystack
dt list                              # unresolved tickets; --all, --status, --assignee me|none|USER, --mine, --type, --priority, --json
dt show DT-007                       # ticket and conversation; --json for the raw object
dt comment DT-007 --body "Checked the GPU passthrough; it's the ffmpeg preset."
echo "Longer update" | dt comment 7 --body -
dt update DT-007 --status "In progress" --assignee me    # also --priority; --assignee none unassigns
dt users                             # usernames for --assignee
```

- `new` prints `DT-007 http://…/#DT-007`. The link opens the ticket in the web app.
- Valid values: `--type` Issue, Request or Change (default Issue). `--priority` Low, Medium, High or Critical (default Medium). `--status` Open, In progress, Waiting or Resolved. These are case-insensitive, and `in-progress` also works.
- `--service` is free text (1–80 characters). The web form suggests Jellyfin, Plex, Network, Proxmox, Storage and Other.
- Limits: title 160, description 10 000, update 5000 characters.
- `--assignee` takes a username, `me`, or (for `update`) `none`. Only active accounts can be newly assigned.
- Exit codes: `0` ok, `1` the server refused the request (the message says why), `2` usage or configuration error (e.g. no token), `3` server unreachable.

### Raw HTTP

```sh
TOKEN=$(cat ~/.config/dark-triage/token)
curl -s -H "Authorization: Bearer $TOKEN" http://dark-triage.lylat.space/api/me
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"title":"Plex buffering","description":"Since 21:00","service":"Plex","priority":"High","type":"Issue"}' \
     http://dark-triage.lylat.space/api/tickets
```

Requests without an `Origin` header (CLI tools) are fine. Requests whose `Origin` doesn't match the host are still rejected.

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
sqlite3 data/triage.db "DELETE FROM sessions; UPDATE tickets SET assignee_id=NULL; DELETE FROM users;"
```

Start again with `ADMIN_PASSWORD` set and a fresh admin is created. Re-create the other accounts. Their old tickets and comments still show the stored names.

## Upgrading from the shared-password version

The database file, ticket IDs, Docker volume, and container user ID (1000) are unchanged. The schema is migrated in place on startup: new `users` and `sessions` tables are added, plus nullable `tickets.requester_id`, `tickets.assignee_id` and `comments.author_id` columns, a `comments.kind` column (default `comment`), a nullable `users.color` column, a `users.is_bot` column (default 0) and an `api_tokens` table. No rows are rewritten. Databases from earlier accounts releases get the assignment, color and bot columns and the token table the same way. Back up the data directory first anyway.

1. Stop the old app.
2. In `.env` (or your environment), replace `ACCESS_PASSWORD=…` with `ADMIN_PASSWORD=…` and optionally `ADMIN_USERNAME=…`. If `ACCESS_PASSWORD` is still set, it is **ignored** and a deprecation warning is logged.
3. `docker compose up -d --build`, or start `python3 server.py` with the same `DATA_DIR`.
4. Sign in as the admin and create accounts for your friends. The shared password no longer opens anything, and stored browser sessions from the old version are discarded.

API changes: the `requester` field on `POST /api/tickets` and the `author` field on comments are now ignored, because the signed-in user is the author. Every `/api/*` call except `POST /api/login` needs a session token or an API token.

## API

| Method & path | Who | Notes |
|---|---|---|
| `POST /api/login` `{username,password}` | anyone | → `{token, expires_in, user}`; 401 bad credentials, 403 disabled, 429 throttled (`Retry-After`) |
| `POST /api/logout` | signed in | Revokes the current token |
| `GET /api/me` | signed in | Current user (`must_change` is true while on a temporary password) |
| `GET/POST /api/me/tokens` `{name}`, `DELETE /api/me/tokens/:tid` | signed in (session, not a token) | Your API tokens. POST → 201 `{id,name,created,last_used,token}`: the only time `token` is returned. 409 for a duplicate active name |
| `PATCH /api/me` `{color}` | signed in | Set your avatar color: `"#rrggbb"`, or `null` for the default. 400 for anything else → updated user |
| `POST /api/me/password` `{current_password,new_password}` | signed in | Clears `must_change`, signs out other sessions |
| `GET /api/tickets?assignee=<id>\|me\|none` | signed in | All tickets, newest activity first; `assignee` is an optional filter (400 if it's anything else) |
| `POST /api/tickets` `{title,description,service,priority,type,assignee_id?}` | signed in | → 201 ticket. You are the requester. `assignee_id` is a user id or `null` |
| `GET /api/tickets/:id` | signed in | Ticket plus `comments` (each with `kind`: `comment` or `system`) |
| `PATCH /api/tickets/:id` `{status?,priority?,assignee_id?}` | signed in | Partial update; send at least one field. → updated ticket with comments |
| `POST /api/tickets/:id/comments` `{body}` | signed in | Posts an update as you |
| `GET /api/users/assignable` | signed in | `[{id, username, display_name, color}]` of active accounts, for the assignee picker |
| `GET/POST /api/users`, `GET/PATCH /api/users/:id` `{display_name?,role?,disabled?,color?}`, `POST /api/users/:id/password` `{password}` | admin | 403 for users, 409 for duplicate username or last-admin protection. `POST /api/users` with `{"bot": true, username, display_name}` creates a bot (no password, role `user`) |
| `GET/POST /api/users/:id/tokens` `{name}`, `DELETE /api/users/:id/tokens/:tid` | admin (session) | Any account's API tokens, same shapes as `/api/me/tokens` |

Every ticket object has `requester_color` and `assignee`: either `null` or `{id, username, display_name, disabled, color}`. Every comment, including activity lines, has `author_color`. User objects (`/api/me`, `/api/users`, login) have `color` (the effective color, always `#rrggbb`), `custom_color` (the stored choice, or `null` when using the default) and `is_bot`. Tickets also have `requester_is_bot`, assignees `is_bot`, comments `author_is_bot`, and assignable users `is_bot`.

With an API token, `POST /api/me/password` and token management return 403 and `POST /api/logout` returns 400. Bad, revoked or disabled-account tokens get 401.

A color must match `^#[0-9a-fA-F]{6}$`. It is stored in lowercase. Anything else (names like `red`, 3-digit hex, missing `#`, numbers) gets a 400. Non-admins get 403 from `PATCH /api/users/:id`, including for their own id: they use `PATCH /api/me`.

While `must_change` is true, everything except `/api/me`, `/api/me/password` and `/api/logout` returns 403 with `"code": "password_change_required"`. Cross-origin writes are rejected: a request whose `Origin` doesn't match its `Host` gets 403.

## Ticket workflow

- **Issues:** report broken services, errors, or outages.
- **Requests:** ask for a service, media, access, or something new.
- **Changes:** propose upgrades, maintenance, and improvements. Include impact, timing, and rollback details in the description.

Create a ticket with a title, details, service, and priority. You are recorded as the requester. Open a ticket to move it through **Open → In progress → Waiting → Resolved**, adjust its priority, or post an update. Assign it to someone, or to yourself, so everyone knows who's on it. See [Assignment](#assignment) for the keyboard shortcuts. Resolved tickets remain searchable and can be reopened. The app starts empty, with no fabricated service health or demo tickets.

## Assignment

Each ticket has at most one **assignee**: an account, or nobody. Everyone can set, change, or clear the assignee on any ticket. Assignment uses the same shared ticket rights as status and priority.

- Pick an assignee when you create a ticket, or change it in the ticket's detail view. **Assign to me** (or the **A** key while a ticket is open) selects yourself.
- The ticket view has one **Save** button: it applies any status, priority or assignee change and posts the update text (optional) in one go. **Ctrl+Enter** in the update box saves too.
- Kanban cards and list rows show the assignee's initials. A dashed `–` means unassigned. The **Anyone / Assigned to me / Unassigned / <person>** filter sits next to the priority filter, and search also matches the assignee's name.
- Only active accounts can be newly assigned. A disabled or nonexistent account gets a 400. Disabling an account keeps its existing assignments: they are shown greyed out as "(disabled)" until someone reassigns them.
- Every assignment change is recorded on the ticket as a system activity line, such as "◈ admin assigned this to Haystack" or "◈ Sam unassigned Haystack". These lines appear in the conversation but don't count as comments. Status and priority changes are not logged.

### Keyboard shortcuts

| Key | Where | Action |
|---|---|---|
| `N` | anywhere | New ticket (new user on the Users page) |
| `/` | ticket list | Focus search |
| `A` | open ticket | Assign the ticket to me |

Shortcuts are ignored while you type in a field.

## Storage and operations

SQLite uses WAL mode and parameterized queries. Back up the entire data directory while the app is stopped (or use SQLite's online backup tooling). Never delete the Docker volume unless you intend to delete all tickets and accounts.

```sh
python3 -m unittest discover -s test -v
```

The integration tests start the real server. They cover admin bootstrap, login and logout, wrong passwords and throttling, forced password change for temporary passwords, admin create, reset, role change, disable and enable, non-admins being refused user management, last-admin protection, session revocation, shared ticket rights (status, priority, comments), assignment (on create, reassign, unassign, the `me`/`none`/id filters, disabled and nonexistent assignees, the assignable-user list vs. admin-only `/api/users`, activity lines), validation, cross-origin rejection, persistence across restarts, avatar colors (deterministic valid defaults, set and reset your own, strict validation, admin-only changes to others, colors in tickets, comments, activity lines and the assignable list), bots and API tokens (token auth on every endpoint, revocation, `last_used`, bot password login refused like an unknown user, disabled bots' tokens rejected, non-admins can't manage other accounts' tokens, `/api/me/tokens`, tokens can't mint tokens, cross-origin check with tokens), the admin CLI (run as a subprocess against the live test database), the `dt` client end to end (`whoami`, `new`, `list`, `show`, `comment`, `update`, error exit codes), and migration of an old-schema database and of an accounts database without `users.color`. Browser visual QA and Docker execution are not included.
