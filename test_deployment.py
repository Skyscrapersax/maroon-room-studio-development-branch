"""Real workflow checks. Set TEST_DATABASE_URL to test a disposable local Postgres."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import json
import importlib.util
from multiprocessing import get_context
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import app as application
from database import connect, Postgres

STUDIO = hasattr(application, "SERVICES")
PASSWORD = "only-a-synthetic-test-password"


def login(app):
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    assert client.post("/login", data={"csrf": csrf, "password": PASSWORD}).status_code == 303
    return client


def form(client, start, title="Session"):
    with client.session_transaction() as session:
        csrf = session["csrf"]
    return {"csrf": csrf, "request_id": secrets.token_urlsafe(24), "start": start, "minutes": "60",
            "title": title, "client": "Example Artist", "contact": "artist@example.test",
            "project": title, "name": "Example Artist", "email": "artist@example.test", "notes": ""}


def competing_booking(config, start):
    app = application.create_app(config | {"AUTO_MIGRATE": False})
    client = login(app)
    return client.post("/bookings", data=form(client, start)).status_code


class DeploymentWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        location = str(Path(self.temp.name) / "test.sqlite3")
        database_url = os.environ.get("TEST_DATABASE_URL")
        if database_url:
            import psycopg
            from psycopg import sql
            url = urlsplit(database_url)
            self.assertIn(url.hostname, {"localhost", "127.0.0.1", "::1"}, "Tests require disposable local Postgres")
            schema = "check_" + secrets.token_hex(8)
            admin = psycopg.connect(database_url, autocommit=True)
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            def cleanup():
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                admin.close()
            self.addCleanup(cleanup)
            params = dict(parse_qsl(url.query))
            params["options"] = "-c search_path=" + schema
            location = urlunsplit(url._replace(query=urlencode(params, quote_via=quote)))
        self.config = {"TESTING": True, "DATABASE": location, "SECRET_KEY": "synthetic-secret-"*4,
                       "ADMIN_PASSWORD": PASSWORD, "SESSION_COOKIE_SECURE": False, "HOSTED": False}
        self.app = application.create_app(self.config)
        self.client = login(self.app)
        self.start = (datetime.now(ZoneInfo("America/New_York"))+timedelta(days=10)).replace(hour=10, minute=0).strftime("%Y-%m-%dT%H:%M")
        with closing(connect(location)) as conn:
            self.assertEqual(isinstance(conn, Postgres), bool(database_url))

    def read(self, sql, values=()):
        with closing(connect(self.config["DATABASE"])) as conn:
            return [dict(row) for row in conn.execute(sql, values).fetchall()]

    def move(self, item_id, start, revision=0):
        data = form(self.client, start)
        data["sequence"] = str(revision)
        path = f"/bookings/{item_id}/" + ("reschedule" if STUDIO else "edit")
        return self.client.post(path, data=data)

    def test_reschedule_collision_revision_history_and_restart(self):
        self.assertEqual(self.client.post("/bookings", data=form(self.client, self.start)).status_code, 303)
        second = self.start[:11] + "12:00"
        self.assertEqual(self.client.post("/bookings", data=form(self.client, second, "Second")).status_code, 303)
        first = self.read("SELECT * FROM bookings WHERE id=1")[0]
        original_ics = self.client.get("/bookings/1.ics").text
        self.assertEqual(self.move(1, second).status_code, 409)
        self.assertEqual(self.read("SELECT * FROM bookings WHERE id=1")[0], first)
        self.assertEqual(self.move(1, self.start[:11]+"11:00").status_code, 303)
        self.assertEqual(self.move(1, self.start[:11]+"13:00").status_code, 409)
        current_ics = self.client.get("/bookings/1.ics").text
        uid = lambda text: re.search(r"^UID:(.+)$", text, re.MULTILINE).group(1)
        self.assertEqual(uid(original_ics), uid(current_ics))
        self.assertIn("SEQUENCE:1", current_ics)
        self.assertEqual(len(self.read("SELECT * FROM booking_history")), 1)
        self.assertEqual(self.client.get("/bookings/1/history").status_code, 200)
        cancel = {"csrf": form(self.client, self.start)["csrf"]}
        for _ in range(2):
            self.assertEqual(self.client.post("/bookings/1/cancel", data=cancel).status_code, 303)
        self.assertIn("SEQUENCE:2", self.client.get("/bookings/1.ics").text)
        self.assertEqual(len(self.read("SELECT * FROM booking_history")), 2)
        restarted = login(application.create_app(self.config | {"AUTO_MIGRATE": False}))
        self.assertIn("STATUS:CANCELLED", restarted.get("/bookings/1.ics").text)
        self.assertEqual(restarted.get("/healthz").json, {"status": "ok"})

    def test_processes_cannot_double_book(self):
        with ProcessPoolExecutor(max_workers=3, mp_context=get_context("spawn")) as pool:
            results = list(pool.map(competing_booking, [self.config]*3, [self.start]*3))
        self.assertEqual(sorted(results), [303, 409, 409])
        self.assertEqual(len(self.read("SELECT * FROM bookings")), 1)

    def test_history_is_private_bounded_and_search_is_literal(self):
        with closing(connect(self.config["DATABASE"])) as conn:
            for i in range(27):
                conn.execute("INSERT INTO bookings(title,client,contact,notes,starts,ends,created,request_id) VALUES(?,?,?,?,?,?,?,?)",
                             (f"Batch {i:02}", "Example", "", "", 2000000000, 2000003600, 2000000000, secrets.token_urlsafe(24)))
            conn.execute("INSERT INTO bookings(title,client,contact,notes,starts,ends,created,request_id) VALUES(?,?,?,?,?,?,?,?)",
                         ("100% session", "Example", "", "", 2000000000, 2000003600, 2000000000, secrets.token_urlsafe(24)))
            conn.commit()
        path = "/desk/history" if STUDIO else "/history"
        options = {"kind": "bookings"} if STUDIO else {}
        self.assertIn(self.app.test_client().get(path).status_code, (302, 303))
        first = self.client.get(path, query_string=options | {"q": "Batch"}).text
        second = self.client.get(path, query_string=options | {"q": "Batch", "page": 2}).text
        self.assertIn("Batch 26", first)
        self.assertNotIn("Batch 00", first)
        self.assertIn("Batch 00", second)
        self.assertNotIn("Batch 26", second)
        literal = self.client.get(path, query_string=options | {"q": "%"}).text
        self.assertIn("100% session", literal)
        self.assertNotIn("Batch 26", literal)
        self.assertEqual(self.client.get(path+"?page=0").status_code, 400)

    def test_failed_update_rolls_back_history_and_booking(self):
        self.client.post("/bookings", data=form(self.client, self.start))
        before = self.read("SELECT * FROM bookings")[0]
        class FailingConnection:
            def __init__(self, location):
                self.conn = connect(location)
            def __getattr__(self, name):
                return getattr(self.conn, name)
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE bookings SET"):
                    raise sqlite3.DatabaseError("synthetic write failure")
                return self.conn.execute(sql, parameters)
        with patch.object(application, "connect", FailingConnection):
            self.assertEqual(self.move(1, self.start[:11]+"13:00").status_code, 503)
        self.assertEqual(self.read("SELECT * FROM bookings")[0], before)
        self.assertEqual(self.read("SELECT * FROM booking_history"), [])

    def test_durable_throttle_and_hosted_fail_closed(self):
        app = application.create_app(self.config | {"LOGIN_LIMIT": 2})
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        for _ in range(2):
            self.assertEqual(client.post("/login", data={"csrf": csrf, "password": "bad"},
                                        environ_overrides={"REMOTE_ADDR": "192.0.2.8"}).status_code, 401)
        other = application.create_app(self.config | {"LOGIN_LIMIT": 2}).test_client()
        other.get("/login")
        with other.session_transaction() as session:
            csrf = session["csrf"]
        self.assertEqual(other.post("/login", data={"csrf": csrf, "password": PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.7"}, environ_overrides={"REMOTE_ADDR": "192.0.2.8"}).status_code, 429)
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            application.create_app(self.config | {"HOSTED": True, "DATABASE": str(Path(self.temp.name)/"forbidden.db")})
        self.assertFalse((Path(self.temp.name)/"forbidden.db").exists())
        self.assertEqual(self.client.post("/bookings/1/"+("reschedule" if STUDIO else "edit"), data={}).status_code, 400)


    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "SQLite to PostgreSQL import")
    def test_import_preserves_ids_calendar_and_identity_sequence(self):
        from manage import import_sqlite
        source = Path(self.temp.name)/"legacy.sqlite3"
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("""CREATE TABLE bookings (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                client TEXT NOT NULL, contact TEXT NOT NULL, notes TEXT NOT NULL,
                starts INTEGER NOT NULL, ends INTEGER NOT NULL, created INTEGER NOT NULL,
                cancelled INTEGER, request_id TEXT NOT NULL UNIQUE)""")
            conn.execute("INSERT INTO bookings VALUES(41,?,?,?,?,?,?,?,?,?)",
                         ("Legacy", "Example", "", "", 2200000000, 2200003600, 1800000000, None, "preserved-legacy-calendar-uid"))
            conn.commit()
        with closing(connect(self.config["DATABASE"])) as conn:
            conn.execute("DELETE FROM limits")
            conn.commit()
        self.assertEqual(import_sqlite(source, self.config["DATABASE"]), {"bookings": 1})
        imported = self.read("SELECT * FROM bookings WHERE id=41")[0]
        self.assertEqual(imported["starts"], 2200000000)
        self.assertEqual(imported["request_id"], "preserved-legacy-calendar-uid")
        self.assertIn("preserved-legacy-calendar-uid@", self.client.get("/bookings/41.ics").text)
        with self.assertRaisesRegex(ValueError, "empty destination"):
            import_sqlite(source, self.config["DATABASE"])
        self.assertEqual(self.client.post("/bookings", data=form(self.client, self.start)).status_code, 303)
        self.assertEqual([r["id"] for r in self.read("SELECT id FROM bookings ORDER BY id")], [41, 42])

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Hosted PostgreSQL entrypoint")
    def test_hosted_entrypoint_validates_without_migrating(self):
        prefix = "STUDIO" if STUDIO else "BOOKING"
        settings = {prefix+"_DATABASE_URL": self.config["DATABASE"],
                    prefix+"_SECRET": self.config["SECRET_KEY"], prefix+"_PASSWORD": PASSWORD,
                    prefix+"_COOKIE_SECURE": "1"}
        spec = importlib.util.spec_from_file_location("hosted_check", Path(__file__).with_name("index.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, settings), patch.object(application, "initialize", side_effect=AssertionError("Hosted DDL")):
            spec.loader.exec_module(module)
        self.assertTrue(module.app.config["HOSTED"])
        self.assertFalse(module.app.config["AUTO_MIGRATE"])
        self.assertEqual(module.app.test_client().get("/healthz").json, {"status": "ok"})

    @unittest.skipUnless(STUDIO, "Studio artist workflow")
    def test_artist_status_and_private_calendar_follow_reschedule(self):
        artist = self.app.test_client()
        artist.get("/")
        data = form(artist, self.start)
        data.update(service="Recording", consent="yes")
        created = artist.post("/requests", data=data)
        self.assertEqual(created.status_code, 303)
        self.assertEqual(self.client.post("/requests/1/accept", data={"csrf": form(self.client,self.start)["csrf"]}).status_code, 303)
        self.assertEqual(self.move(1, self.start[:11]+"14:00").status_code, 303)
        page = artist.get(created.location).text
        self.assertIn("14:00", page)
        self.assertNotIn("artist@example.test", page)
        self.assertIn("SEQUENCE:1", artist.get(created.location+"/calendar.ics").text)
