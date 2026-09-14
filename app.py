"""Studio requests and a private calendar; no external messages or payments.

Datetime, ICS and SQLite booking patterns adapted from Booking-Scheduler
commit a3937a2284baeee2a1e3600b77c1930bbe0d7652 (same portfolio).
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import SecurityError

SERVICES = ("Recording", "Mixing", "Mastering")
DURATIONS = (60, 120, 180, 240, 480)


def local_timestamp(value, zone):
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError("Enter a valid date and time.") from None
    stamps = {int(naive.replace(tzinfo=zone, fold=fold).timestamp()) for fold in (0, 1)
              if datetime.fromtimestamp(naive.replace(tzinfo=zone, fold=fold).timestamp(), zone).replace(tzinfo=None) == naive}
    if len(stamps) != 1:
        raise ValueError("That local time is skipped or repeated by daylight saving. Choose another time.")
    return stamps.pop()


def calendar_text(value):
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_calendar(lines):
    folded = []
    for line in lines:
        data = line.encode("utf-8")
        while len(data) > 75:
            cut = 75
            while data[cut] & 0xC0 == 0x80:
                cut -= 1
            folded.append(data[:cut].decode("utf-8"))
            data = b" " + data[cut:]
        folded.append(data.decode("utf-8"))
    return "\r\n".join(folded) + "\r\n"


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("STUDIO_SECRET", ""), ADMIN_PASSWORD=os.environ.get("STUDIO_PASSWORD", ""),
        DATABASE=os.environ.get("STUDIO_DATABASE", str(Path(app.instance_path) / "studio.sqlite3")),
        TIMEZONE=os.environ.get("STUDIO_TIMEZONE", "America/New_York"),
        CONTACT_EMAIL=os.environ.get("STUDIO_CONTACT_EMAIL", ""),
        SESSION_COOKIE_NAME="maroon_room_session", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=os.environ.get("STUDIO_COOKIE_SECURE") != "0",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=16384,
        TRUSTED_HOSTS=["localhost", "127.0.0.1"] + ([os.environ["STUDIO_HOST"]] if os.environ.get("STUDIO_HOST") else []),
        REQUEST_LIMIT=5, LOGIN_LIMIT=10,
    )
    app.config.update(config or {})
    if len(app.config["SECRET_KEY"]) < 32 or len(app.config["ADMIN_PASSWORD"]) < 16:
        raise ValueError("Set STUDIO_SECRET (32+ characters) and STUDIO_PASSWORD (16+ characters).")
    password_hash = generate_password_hash(app.config.pop("ADMIN_PASSWORD"))
    if app.config["CONTACT_EMAIL"] and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", app.config["CONTACT_EMAIL"]):
        raise ValueError("STUDIO_CONTACT_EMAIL must be an email address.")
    zone = ZoneInfo(app.config["TIMEZONE"])
    database = Path(app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database.touch(mode=0o600, exist_ok=True)

    def db():
        conn = sqlite3.connect(database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def now():
        return int(datetime.now(timezone.utc).timestamp())

    with closing(db()) as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, client TEXT NOT NULL,
                contact TEXT NOT NULL, notes TEXT NOT NULL, starts INTEGER NOT NULL,
                ends INTEGER NOT NULL CHECK(ends > starts), created INTEGER NOT NULL,
                cancelled INTEGER, request_id TEXT NOT NULL UNIQUE);
            CREATE INDEX IF NOT EXISTS booking_times ON bookings(starts, ends) WHERE cancelled IS NULL;
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, form_id TEXT NOT NULL UNIQUE,
                service TEXT NOT NULL, project TEXT NOT NULL, client TEXT NOT NULL,
                email TEXT NOT NULL, notes TEXT NOT NULL, starts INTEGER NOT NULL,
                ends INTEGER NOT NULL CHECK(ends > starts), created INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined')),
                booking_id INTEGER REFERENCES bookings(id), decided INTEGER);
            CREATE TABLE IF NOT EXISTS limits (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, resets INTEGER NOT NULL);
        """)

    @app.before_request
    def protect():
        if request.routing_exception is not None:
            return
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.method == "POST" and not hmac.compare_digest(session["csrf"].encode(), request.form.get("csrf", "").encode()):
            abort(400, "Form expired. Reload the page and try again.")
        if request.method == "POST" and request.endpoint in {"request_session", "login"}:
            limit, window = (app.config["REQUEST_LIMIT"], 3600) if request.endpoint == "request_session" else (app.config["LOGIN_LIMIT"], 900)
            bucket = hmac.new(app.config["SECRET_KEY"].encode(), f"{request.endpoint}:{request.remote_addr}".encode(), hashlib.sha256).hexdigest()
            with closing(db()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM limits WHERE resets <= ?", (now(),))
                row = conn.execute("SELECT count FROM limits WHERE bucket=?", (bucket,)).fetchone()
                if row and row["count"] >= limit:
                    abort(429, "Too many attempts. Please try again later.")
                conn.execute("INSERT INTO limits VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1", (bucket, now()+window))
                conn.commit()

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        return response

    @app.context_processor
    def context():
        return dict(zone=zone.key, services=SERVICES, durations=DURATIONS, contact_email=app.config["CONTACT_EMAIL"],
            today=datetime.now(zone).date().isoformat(), values=request.form)

    @app.template_filter("localtime")
    def localtime(stamp):
        return datetime.fromtimestamp(stamp, zone).strftime("%a %d %b %Y · %H:%M %Z")

    def staff(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return fn(*args, **kwargs) if session.get("signed_in") else redirect(url_for("login"), code=303)
        return wrapped

    def text_value(key, limit, required=True):
        value = request.form.get(key, "").strip()
        if (required and not value) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError(f"Check {key}: {'required, ' if required else ''}maximum {limit} characters.")
        return value

    def times():
        starts = local_timestamp(request.form.get("start", ""), zone)
        try:
            minutes = int(request.form.get("minutes", "0"))
        except ValueError:
            raise ValueError("Choose a session length.") from None
        if minutes not in DURATIONS or not now() < starts <= now()+366*86400:
            raise ValueError("Choose a listed session length and a future time within the next year.")
        return starts, starts+minutes*60

    def form_id():
        value = request.form.get("request_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", value):
            raise ValueError("Form identifier invalid. Reload the page and try again.")
        return value

    def conflict(conn, starts, ends):
        return conn.execute("SELECT id FROM bookings WHERE cancelled IS NULL AND starts < ? AND ends > ?", (ends, starts)).fetchone()

    @app.get("/")
    def home():
        return render_template("home.html", request_id=secrets.token_urlsafe(24))

    @app.post("/requests")
    def request_session():
        try:
            fid = form_id()
            service = request.form.get("service", "")
            if service not in SERVICES:
                raise ValueError("Choose recording, mixing or mastering.")
            project, client, email, notes = text_value("project", 120), text_value("name", 120), text_value("email", 200), text_value("notes", 2000, False)
            if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email):
                raise ValueError("Enter a valid email address.")
            if request.form.get("consent") != "yes":
                raise ValueError("Confirm that the studio may use these details to review your request.")
            starts, ends = times()
        except ValueError as exc:
            return render_template("home.html", error=str(exc), request_id=request.form.get("request_id") or secrets.token_urlsafe(24)), 422
        with closing(db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute("SELECT token FROM requests WHERE form_id=?", (fid,)).fetchone()
            if duplicate:
                token = duplicate["token"]
            else:
                token = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO requests(token,form_id,service,project,client,email,notes,starts,ends,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (token, fid, service, project, client, email, notes, starts, ends, now()))
                conn.commit()
        return redirect(url_for("receipt", token=token), code=303)

    def get_request(token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            abort(404)
        with closing(db()) as conn:
            row = conn.execute("SELECT requests.*, bookings.cancelled FROM requests LEFT JOIN bookings ON requests.booking_id=bookings.id WHERE token=?", (token,)).fetchone()
        if row is None:
            abort(404)
        return row

    @app.get("/request/<token>")
    def receipt(token):
        row = get_request(token)
        return render_template("receipt.html", item=row, state="cancelled" if row["cancelled"] else row["status"])

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if check_password_hash(password_hash, request.form.get("password", "")):
                session.clear()
                session.update(signed_in=True, csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect(url_for("desk"), code=303)
            return render_template("login.html", error="Password not recognized."), 401
        return render_template("login.html")

    @app.post("/logout")
    @staff
    def logout():
        session.clear()
        return redirect(url_for("home"), code=303)

    def render_desk(error=None, status=200):
        # ponytail: one studio, 200 visible rows; add pagination before a larger rollout.
        with closing(db()) as conn:
            pending = conn.execute("SELECT * FROM requests WHERE status='pending' ORDER BY created,id LIMIT 200").fetchall()
            bookings = conn.execute("SELECT * FROM bookings WHERE ends>? ORDER BY starts,id LIMIT 200", (now()-86400,)).fetchall()
            total = conn.execute("SELECT count(*) FROM requests WHERE status='pending'").fetchone()[0]
        return render_template("desk.html", pending=pending, bookings=bookings, total=total, error=error, request_id=secrets.token_urlsafe(24)), status

    @app.get("/desk")
    @staff
    def desk():
        return render_desk()

    @app.post("/requests/<int:item_id>/<action>")
    @staff
    def decide(item_id, action):
        if action not in {"accept", "decline"} or item_id > 2**63-1:
            abort(404)
        with closing(db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM requests WHERE id=?", (item_id,)).fetchone()
            if row is None:
                abort(404)
            if row["status"] != "pending":
                flash("This request already has a decision.")
            elif action == "decline":
                conn.execute("UPDATE requests SET status='declined', decided=? WHERE id=?", (now(), item_id))
                conn.commit()
                flash("Request declined. Its private status page is updated; no email sent.")
            elif row["starts"] <= now():
                return render_desk("This requested time has passed. Decline it and arrange a new request.", 409)
            elif conflict(conn, row["starts"], row["ends"]):
                return render_desk("That time overlaps a confirmed session. This request remains pending.", 409)
            else:
                booking = conn.execute("INSERT INTO bookings(title,client,contact,notes,starts,ends,created,request_id) VALUES(?,?,?,?,?,?,?,?)",
                    (f"{row['service']}: {row['project']}", row["client"], row["email"], row["notes"], row["starts"], row["ends"], now(), "studio-request-"+str(item_id)))
                conn.execute("UPDATE requests SET status='accepted', booking_id=?, decided=? WHERE id=?", (booking.lastrowid, now(), item_id))
                conn.commit()
                flash("Session confirmed. Its private status page now offers a calendar download; no email sent.")
        return redirect(url_for("desk"), code=303)

    @app.post("/bookings")
    @staff
    def book():
        try:
            title, client, contact, notes = text_value("project", 120), text_value("name", 120), text_value("email", 200, False), text_value("notes", 2000, False)
            starts, ends = times()
            fid = form_id()
        except ValueError as exc:
            return render_desk(str(exc), 422)
        with closing(db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM bookings WHERE request_id=?", (fid,)).fetchone():
                flash("This booking was already saved.")
            elif conflict(conn, starts, ends):
                return render_desk("That time overlaps a confirmed session.", 409)
            else:
                conn.execute("INSERT INTO bookings(title,client,contact,notes,starts,ends,created,request_id) VALUES(?,?,?,?,?,?,?,?)", (title, client, contact, notes, starts, ends, now(), fid))
                conn.commit()
                flash("Booking saved. No message sent.")
        return redirect(url_for("desk"), code=303)

    @app.post("/bookings/<int:item_id>/cancel")
    @staff
    def cancel(item_id):
        if item_id > 2**63-1:
            abort(404)
        with closing(db()) as conn:
            if not conn.execute("SELECT id FROM bookings WHERE id=?", (item_id,)).fetchone():
                abort(404)
            conn.execute("UPDATE bookings SET cancelled=? WHERE id=? AND cancelled IS NULL", (now(), item_id))
            conn.commit()
        flash("Session cancelled. History and its updated status page are retained.")
        return redirect(url_for("desk"), code=303)

    def calendar_response(row):
        utc = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Maroon Room//Session Desk//EN", "BEGIN:VEVENT",
            f"UID:{row['request_id']}@maroon-room.local", f"DTSTAMP:{utc(row['cancelled'] or row['created'])}",
            f"DTSTART:{utc(row['starts'])}", f"DTEND:{utc(row['ends'])}", "SUMMARY:"+calendar_text(row["title"]),
            "STATUS:"+("CANCELLED" if row["cancelled"] else "CONFIRMED"),
            "SEQUENCE:"+("1" if row["cancelled"] else "0"), "END:VEVENT", "END:VCALENDAR"]
        return Response(fold_calendar(lines), mimetype="text/calendar", headers={"Content-Disposition": 'attachment; filename="studio-session.ics"'})

    @app.get("/request/<token>/calendar.ics")
    def public_calendar(token):
        item = get_request(token)
        if item["status"] != "accepted":
            abort(409, "A calendar event is available after the studio confirms the session.")
        with closing(db()) as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (item["booking_id"],)).fetchone()
        return calendar_response(row)

    @app.get("/bookings/<int:item_id>.ics")
    @staff
    def calendar(item_id):
        if item_id > 2**63-1:
            abort(404)
        with closing(db()) as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (item_id,)).fetchone()
        if row is None:
            abort(404)
        return calendar_response(row)

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.get("/healthz")
    def health():
        with closing(db()) as conn:
            conn.execute("SELECT id FROM bookings LIMIT 1").fetchone()
        return {"status": "ok"}

    def http_error(exc):
        if isinstance(exc, SecurityError):
            return exc.get_response()  # Rejected hosts have no safe URL adapter for template links.
        return render_template("error.html", error=exc.description, code=exc.code), exc.code

    for code in (400, 404, 409, 413, 429):
        app.register_error_handler(code, http_error)
    return app
