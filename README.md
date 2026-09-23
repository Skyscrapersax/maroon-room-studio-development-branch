# Maroon Room — Session Desk

A working, single-studio session request and booking pilot. Artists request recording, mixing or mastering time; the studio reviews each request before it reserves the calendar. **America/New_York** is the owner-confirmed default timezone.

The previous static React page only displayed an alert after date selection. This version saves requests in SQLite, gives the artist a private status page, and provides an authenticated studio desk. Flask renders the complete workflow; there is no frontend build or JavaScript dependency.

## Run locally

Requires Python 3.12+ and an IANA timezone database (present on the tested macOS and Ubuntu hosts).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export STUDIO_SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export STUDIO_PASSWORD="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')"
# Save this generated password in your password manager before continuing.
printf '%s\n' "$STUDIO_PASSWORD"
export STUDIO_COOKIE_SECURE=0  # loopback HTTP rehearsal only
.venv/bin/waitress-serve --listen=127.0.0.1:8795 --call app:create_app
```

Open `http://127.0.0.1:8795/`. Studio sign-in is `/desk`. Use a separate private browser session to rehearse an artist and operator independently. Keep the same secret across restarts to preserve sessions and keyed rate counters. Changing the password alone does not revoke already signed-in sessions; rotate the secret as well when revoking access.

## What works

- Recording, mixing and mastering requests with 1, 2, 3, 4 or 8-hour durations.
- Durable requests and opaque private status links. A request is explicitly **not a reservation**.
- Studio approval/decline, manual bookings or calendar blocks, and cancellation with retained history.
- Overlap checking and approval in the same SQLite write transaction; simultaneous approvals cannot double-book one studio. Adjacent sessions are allowed.
- Downloadable confirmed/cancelled `.ics` events with stable IDs and escaped, folded calendar text. Email and internal notes are excluded.
- New York display times, UTC storage and calendar exports. Skipped/repeated daylight-saving start times are rejected instead of guessed.
- CSRF protection, hashed password verification, bounded inputs, secure cookies by default, restrictive response headers and durable submission/login throttles.

There is no payment collection, email/SMS delivery, external calendar synchronization, artist account system or multi-room support. Importing an `.ics` file does not subscribe a calendar to future updates. Artists must revisit their saved status link; the operator contacts them through existing channels.

## Pilot operation

1. Invite a few known artists through an existing contact channel. Agree rates, scope and cancellation terms separately. No studio address, equipment claims, stock photos or unconfirmed public contact email is published here.
2. Artist sends a request and bookmarks its private status page. Anyone holding that link can see project name, service, times and decision; email and notes stay in the authenticated desk.
3. Operator reviews the request, confirms availability and scope, then accepts or declines it. Pending requests may overlap; only confirmed bookings occupy the calendar.
4. Artist checks status and downloads the calendar file after approval. If cancelled, the artist must update their calendar; the app does not send invitations or cancellation messages.
5. Operator backs up the database and checks the desk daily. It shows the oldest 200 pending requests and up to 200 bookings ending within the last day or later. Older records remain in the database; there is no history search/pagination yet.

## Hosting configuration

Use one app deployment with a persistent local disk, behind HTTPS and a service supervisor. Do not put this SQLite database on ephemeral/serverless storage or a shared network filesystem. At startup, the app enforces mode `0600` on the main database file, including existing files. The operator must still protect backups, SQLite sidecar files and existing directories.

| Variable | Meaning |
| --- | --- |
| `STUDIO_SECRET` | Required secret, at least 32 characters; persist outside Git. |
| `STUDIO_PASSWORD` | Required operator password, at least 16 characters; persist outside Git. |
| `STUDIO_DATABASE` | Persistent SQLite path; defaults to `instance/studio.sqlite3`. |
| `STUDIO_TIMEZONE` | Defaults to confirmed `America/New_York`. |
| `STUDIO_HOST` | Exact additional allowed hostname, without scheme/path. |
| `STUDIO_CONTACT_EMAIL` | Optional verified studio contact. Unset by default. Publishing it does not enable email sending. |
| `STUDIO_COOKIE_SECURE` | Secure by default. Set `0` only for a loopback HTTP rehearsal; leave unset for HTTPS. |

Waitress binds to loopback in the example. Keep that binding behind your HTTPS proxy; do not expose the development server. Configure TLS and access logs at the proxy. Avoid retaining request bodies, private status URLs, cookies or authentication data in logs.

Throttles use the socket client address, not untrusted forwarded headers: five request attempts per hour and ten login attempts per 15 minutes. Behind a proxy, clients can share one counter unless a separately reviewed trusted-proxy configuration is installed. Keep this as a small invited pilot; do not raise limits to compensate for an unreviewed public deployment. `/healthz` checks database access without exposing bookings.

## Backup and recovery

Back up through SQLite's online backup API, not by copying a live `.sqlite3` file while WAL writes are active. Store backups outside the checkout, with access restrictions and a retention policy:

```sh
export STUDIO_DATABASE=/absolute/private/path/studio.sqlite3
export STUDIO_BACKUP=/absolute/private/path/studio-backup.sqlite3
umask 077
.venv/bin/python - <<'PY'
import os, sqlite3
from contextlib import closing
with closing(sqlite3.connect(os.environ['STUDIO_DATABASE'])) as source:
    with closing(sqlite3.connect(os.environ['STUDIO_BACKUP'])) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not target.execute('PRAGMA foreign_key_check').fetchall()
PY
```

Rehearse restoration into a **new** database path: stop the app, keep the original database and its WAL files together, copy the verified backup to a new private path, set `STUDIO_DATABASE` to that path, restart and check known status links and the studio desk. Do not overwrite an open database or leave stale WAL files beside a restored database.

Requests, booking history and personal information remain until the operator removes them. There is no automated retention, correction or deletion UI. Before accepting public traffic, choose a retention period, establish an operator-assisted correction/deletion process, verify the public contact and hosting, and rehearse recovery on that host. Database backups must follow the same retention policy.

## Verification

```sh
.venv/bin/python -m pip check
.venv/bin/python -m unittest -v
```

Eight integration tests cover the request/approval/cancellation flow, persistence and backup recovery, existing database permissions, concurrent conflicts, adjacent bookings, authorization/CSRF/host rejection, private data exclusions, input errors, durable throttles, manual calendar blocks, DST and calendar injection. GitHub CI runs these checks on Python 3.12.

Browser acceptance: submit synthetic data → verify pending status → sign in → approve → verify confirmed status/calendar link → cancel → verify cancelled status/calendar link. Also inspect a narrow mobile viewport and keyboard navigation on target devices before widening the pilot. Automated tests do not certify a public deployment or external calendar client behavior.

## Reuse and provenance

Datetime conversion, calendar escaping/folding, authentication and SQLite transaction patterns were adapted from the portfolio's local **Booking Scheduler**, commit `a3937a2284baeee2a1e3600b77c1930bbe0d7652`. This app gives those patterns the studio-specific request/approval workflow. No separate shared framework or new backend service was introduced.

Source repository: [kohlkat/maroon-room-studio-development-branch](https://github.com/kohlkat/maroon-room-studio-development-branch). Replacing the inactive CRA interface removes its unused dependency tree and random third-party imagery; the prior implementation remains in Git history.
