# Maroon Room on Vercel

This app runs as a Flask Vercel Function with external PostgreSQL. SQLite remains available for local use. The hosted entrypoint refuses SQLite and insecure cookies; it does not create or migrate schema during a cold start.

## What this release adds

Artists keep one private status link when the studio moves their confirmed session. Operators can reschedule without losing the original request, inspect prior session times, and search all requests or bookings, including declines and cancellations.
Calendar downloads preserve their UID and increase SEQUENCE on each edit/cancellation. Re-importing is manual; no invitations, messages or payments are sent.

Each app owns an independent calendar. Do not use both to allocate the same room: neither database knows the other's bookings.

## Configure one Vercel project

1. Import this repository/branch. Framework: **Flask**. Root: repository root. Keep the checked-in build command; it stages CSS and vendored JS at `public/static/` for Vercel's CDN. Python 3.12 is pinned.
2. Connect a **Neon Postgres** database through Vercel Marketplace (another standard PostgreSQL provider also works). Use a separate database for this app, and a separate preview database/branch. Never point preview deployments at production data.
3. Set `DATABASE_URL` to the provider's pooled connection URI with `sslmode=require`. A project-specific `STUDIO_DATABASE_URL` takes precedence when present.
4. Generate a stable `STUDIO_SECRET` (32+ characters) and `STUDIO_PASSWORD` (16+ characters) in a password manager. A local alternative is `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. Set both as sensitive Vercel environment variables; keep them distinct for each app/environment.
5. Add your exact custom domain as `STUDIO_HOST` (hostname only). Vercel deployment, branch and production hostnames are included from their system variables. No wildcard hosts. Keep `STUDIO_COOKIE_SECURE` unset. `STUDIO_TIMEZONE` defaults to America/New_York.

The tracked `.env.example` contains names only, not deployable credentials. The app does not automatically load dotenv files; export variables in your shell or configure them in Vercel.

## Initialize before first deployment

Use the provider's **direct** PostgreSQL URL for initialization, import, backups and restore. Use the pooled URL for runtime traffic. With the matching secrets and database URL exported:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py init-db
```

Migrations are additive and serialized with the same transaction lock used by booking writes. Existing SQLite databases gain calendar revision fields and a history table on local startup. Hosted startup validates those fields and fails if initialization was skipped.

If the old app has data, stop its writes, make a consistent SQLite backup using its documented online backup API, then run this once against the new, empty PostgreSQL database:

```sh
.venv/bin/python manage.py import-sqlite /absolute/private/path/verified-backup.sqlite3
```

The importer opens the source read-only, validates integrity/foreign keys, refuses a nonempty destination, imports inside one transaction, retains IDs/private tokens/calendar UIDs and adjusts generated IDs. Keep the same session secret to retain existing keyed throttles and sessions. Verify known records/status links before retiring the old instance. Do not copy an active WAL database's main file alone.

## Verify and deploy

```sh
.venv/bin/python -m pip check
.venv/bin/python -m unittest -q
.venv/bin/python scripts/build_assets.py
vercel deploy
```

First validate the preview with synthetic data. Check `/healthz`, sign-in, creation/approval, a rejected overlap, rescheduling, history, calendar downloads, sign-out, CSS/JS and a narrow mobile viewport. Check a cold start/restart against the same database. Test through actual Vercel ingress that forged forwarded headers cannot evade throttles. Only then promote using `vercel promote <verified-preview-url>` or deploy production.

No Vercel project, paid database, production secrets or live deployment is created by this source change. Actual hosted acceptance remains required after those are configured.

## Recovery

Use Neon backup/PITR features and/or a restricted PostgreSQL backup. With `DIRECT_DATABASE_URL` pointing to the direct endpoint, create a new backup file:

```sh
umask 077
pg_dump --dbname="$DIRECT_DATABASE_URL" --format=custom --no-owner --file=studio-backup.dump
pg_restore --list studio-backup.dump
```

Rehearse `pg_restore --no-owner --dbname="$RESTORE_DATABASE_URL" studio-backup.dump` into a **new empty database**, initialize any later additive schema, and check known sessions/calendar UIDs with a protected preview before changing runtime `DATABASE_URL`. Never restore over an active database. Protect snapshots like contact records and apply the chosen retention policy to backups too.

## Runtime limits

One database-wide advisory lock serializes booking/throttle mutations across Vercel instances. That is appropriate for one low-volume calendar; split locks by calendar only when adding multiple calendars. Connections are short-lived; prepared statements are disabled for pooled compatibility. Private pages/ICS/history are `no-store`. Use TLS, restrict logs containing private status paths, and rotate the session secret with the password to revoke existing sessions.

Confirm a public contact and operating/cancellation terms before inviting artists. Rates and scope remain agreed with the studio; the app does not invent them.

## Repeatable PostgreSQL checks

CI starts PostgreSQL 17 and runs the workflow suite against it. Locally, set `TEST_DATABASE_URL` to a **disposable loopback** PostgreSQL database and run:

```sh
TEST_DATABASE_URL='postgresql://user:password@127.0.0.1:5432/testdb' .venv/bin/python -m unittest -q test_deployment
```

Each test creates and removes its own schema. Tests assert the Postgres backend, race separate processes for a slot, reject stale/conflicting edits, verify rollback and restart, exercise durable throttling, history privacy/pagination, and import legacy SQLite IDs with post-2038 timestamps.

References: [Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask), [Python runtime](https://vercel.com/docs/functions/runtimes/python), [Postgres on Vercel](https://vercel.com/docs/postgres).
