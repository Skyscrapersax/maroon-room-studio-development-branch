from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from app import create_app, local_timestamp, fold_calendar, calendar_text


class StudioPilot(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = dict(TESTING=True, SECRET_KEY="s"*48, ADMIN_PASSWORD="private-test-password", DATABASE=str(Path(self.temp.name)/"studio.sqlite3"), SESSION_COOKIE_SECURE=False)
        self.app = create_app(self.config)
        self.artist = self.app.test_client()
        self.start = (datetime.now(ZoneInfo("America/New_York"))+timedelta(days=3)).replace(hour=10,minute=0).strftime("%Y-%m-%dT%H:%M")

    def fields(self, client, path="/"):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        return dict(re.findall(r'name="(csrf|request_id)" value="([^"]+)"', response.text))

    def submit(self, client=None, **changes):
        client = client or self.artist
        values = dict(self.fields(client), service="Recording", project="Paper Moon EP", name="Pilot Artist", email="artist@example.test", notes="Two voices, no backing tracks", start=self.start, minutes="120", consent="yes")
        values.update(changes)
        return client.post("/requests", data=values), values

    def operator(self, client=None):
        client = client or self.app.test_client()
        fields = self.fields(client, "/login")
        response = client.post("/login", data=dict(fields, password=self.config["ADMIN_PASSWORD"]))
        self.assertEqual(response.status_code, 303)
        return client

    def post(self, client, path, **values):
        with client.session_transaction() as s:
            values["csrf"] = s["csrf"]
        return client.post(path, data=values)

    def rows(self, table):
        with closing(sqlite3.connect(self.config["DATABASE"])) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()

    def test_request_approval_restart_calendar_cancellation_and_replay(self):
        response, values = self.submit()
        self.assertEqual(response.status_code, 303)
        receipt = response.location
        self.assertEqual(self.artist.post("/requests", data=values).location, receipt)
        self.assertEqual(len(self.rows("requests")), 1)
        self.assertEqual(self.artist.get(receipt+"/calendar.ics").status_code, 409)
        page = self.artist.get(receipt).text
        self.assertIn("Awaiting studio review", page)
        self.assertNotIn("artist@example.test", page)
        self.assertNotIn("Two voices", page)
        op = self.operator()
        self.assertEqual(self.post(op, "/requests/1/accept").status_code, 303)
        self.assertEqual(self.post(op, "/requests/1/accept").status_code, 303)
        self.assertEqual(len(self.rows("bookings")), 1)
        restarted = create_app(self.config).test_client()
        self.assertIn("Session confirmed", restarted.get(receipt).text)
        event = restarted.get(receipt+"/calendar.ics")
        self.assertIn("STATUS:CONFIRMED", event.text)
        self.assertIn("DTSTART:", event.text)
        self.assertNotIn("artist@example.test", event.text)

        # Two installations both create booking #1; calendar imports must not collide.
        other = create_app(dict(self.config, DATABASE=str(Path(self.temp.name)/"other.sqlite3"))).test_client()
        other_receipt, _ = self.submit(other)
        self.post(self.operator(other), "/requests/1/accept")
        other_event = other.get(other_receipt.location+"/calendar.ics").text
        uid = lambda text: re.search(r"^UID:(.+)$", text, re.MULTILINE).group(1)
        self.assertNotEqual(uid(event.text), uid(other_event))
        self.assertEqual(uid(event.text), uid(restarted.get(receipt+"/calendar.ics").text))
        self.assertEqual(self.post(op, "/bookings/1/cancel").status_code, 303)
        self.assertIn("Session cancelled", restarted.get(receipt).text)
        self.assertIn("STATUS:CANCELLED", restarted.get(receipt+"/calendar.ics").text)
        self.assertIsNotNone(self.rows("bookings")[0]["cancelled"])

        # Rehearse recovery using SQLite's online backup API, including related records.
        recovered_path = str(Path(self.temp.name)/"recovered.sqlite3")
        with closing(sqlite3.connect(self.config["DATABASE"])) as source, closing(sqlite3.connect(recovered_path)) as target:
            source.backup(target)
            self.assertEqual(target.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(target.execute("PRAGMA foreign_key_check").fetchall(), [])
        recovered = create_app(dict(self.config, DATABASE=recovered_path)).test_client()
        self.assertIn("Session cancelled", recovered.get(receipt).text)
        self.assertIn("STATUS:CANCELLED", recovered.get(receipt+"/calendar.ics").text)

    def test_concurrent_approvals_prevent_double_booking_and_allow_adjacent(self):
        self.submit()
        self.submit(project="Second artist")
        operators = [self.operator(), self.operator()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda i: self.post(operators[i], f"/requests/{i+1}/accept").status_code, range(2)))
        self.assertEqual(sorted(results), [303, 409])
        self.assertEqual(len(self.rows("bookings")), 1)
        later = (datetime.strptime(self.start, "%Y-%m-%dT%H:%M")+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        self.submit(project="Adjacent session", start=later)
        self.assertEqual(self.post(operators[0], "/requests/3/accept").status_code, 303)
        self.assertEqual(len(self.rows("bookings")), 2)
        pending = next(row for row in self.rows("requests") if row["status"] == "pending")
        self.assertEqual(self.post(operators[0], f"/requests/{pending['id']}/decline").status_code, 303)
        self.assertIn("Time declined", self.artist.get("/request/"+pending["token"]).text)

    def test_auth_csrf_host_private_data_and_http_headers(self):
        response, _ = self.submit()
        self.assertEqual(self.artist.get("/desk").status_code, 303)
        self.assertEqual(self.post(self.artist, "/requests/1/accept").status_code, 303)
        self.assertEqual(self.artist.post("/requests").status_code, 400)
        self.assertEqual(self.artist.get("/", base_url="http://evil.test").status_code, 400)
        self.assertEqual(self.artist.get("/request/"+"x"*43).status_code, 404)
        page = self.artist.get(response.location)
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("script-src 'none'", page.headers["Content-Security-Policy"])
        op = self.operator()
        self.assertEqual(self.post(op, "/requests/9999999999999999999999999/accept").status_code, 404)
        self.post(op, "/logout")
        self.assertEqual(op.get("/desk").status_code, 303)

    def test_validation_preserves_form_and_escapes_untrusted_project(self):
        for changes in [dict(email="bad"), dict(service="Fake"), dict(minutes="999"), dict(consent=""), dict(start="2020-01-01T10:00")]:
            response, _ = self.submit(**changes)
            self.assertEqual(response.status_code, 422)
            self.assertIn('value="Paper Moon EP"', response.text)
        self.assertEqual(len(self.rows("requests")), 0)
        other = self.app.test_client()
        response, _ = self.submit(other, project="<script>alert(1)</script>", start=self.start)
        # Separate network address for this request after the intentionally bad attempts.
        self.assertEqual(response.status_code, 429)
        values = dict(self.fields(other), service="Recording", project="<script>alert(1)</script>", name="Artist", email="a@example.test", notes="", start=self.start, minutes="60", consent="yes")
        response = other.post("/requests", data=values, environ_overrides={"REMOTE_ADDR":"127.0.0.2"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("&lt;script&gt;", other.get(response.location).text)
        self.assertNotIn("<script>", other.get(response.location).text)

    def test_rate_limits_survive_restart_and_cannot_use_forwarded_ip(self):
        values = dict(self.fields(self.artist, "/login"), password="bad")
        for _ in range(10):
            self.assertEqual(self.artist.post("/login", data=values).status_code, 401)
        restarted = create_app(self.config).test_client()
        values = dict(self.fields(restarted, "/login"), password=self.config["ADMIN_PASSWORD"])
        self.assertEqual(restarted.post("/login", data=values, headers={"X-Forwarded-For":"8.8.8.8"}).status_code, 429)

    def test_manual_block_cancel_then_accept_pending_request(self):
        response, _ = self.submit()
        op = self.operator()
        values = dict(self.fields(op, "/desk"), project="Studio maintenance", name="Studio", email="", notes="", start=self.start, minutes="120")
        self.assertEqual(op.post("/bookings", data=values).status_code, 303)
        self.assertEqual(op.post("/bookings", data=values).status_code, 303)
        self.assertEqual(len(self.rows("bookings")), 1)
        self.assertEqual(self.post(op, "/requests/1/accept").status_code, 409)
        self.assertEqual(self.post(op, "/bookings/1/cancel").status_code, 303)
        self.assertEqual(self.post(op, "/requests/1/accept").status_code, 303)
        self.assertIn("Session confirmed", self.artist.get(response.location).text)

    def test_dst_and_calendar_text_keep_booking_scheduler_contract(self):
        zone = ZoneInfo("America/New_York")
        for value in ["2027-03-14T02:30", "2026-11-01T01:30", "garbage"]:
            with self.assertRaises(ValueError):
                local_timestamp(value, zone)
        folded = fold_calendar(["SUMMARY:"+calendar_text("音"*80+"\r\nBEGIN:VEVENT;,")])
        self.assertTrue(all(len(line.encode()) <= 75 for line in folded.split("\r\n")))
        self.assertNotIn("\r\nBEGIN:VEVENT", folded)
        self.assertIn("\\;\\,", folded)


if __name__ == "__main__":
    unittest.main()
