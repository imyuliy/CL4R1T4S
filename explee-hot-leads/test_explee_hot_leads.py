"""Tests for explee_hot_leads.py. Run with:  python3 -m unittest -v test_explee_hot_leads"""
import datetime as dt
import email
import http.server
import io
import json
import os
import socketserver
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

import explee_hot_leads as ehl

API_KEY = "sk_test_123"


def lead(n, hot_at, **extra):
    base = {
        "name": "Person %d" % n,
        "email": "person%d@example.com" % n,
        "job_title": "CEO",
        "company_name": "Company %d" % n,
        "company_domain": "company%d.example" % n,
        "linkedin_url": "https://www.linkedin.com/in/person%d" % n,
        "country": "NL",
        "phone": "+31 6 1234 567%d" % n,
        "why_hot": "Yes, interested! Call me.",
        "became_hot_at": hot_at,
    }
    base.update(extra)
    return base


class FakeExplee:
    """Serves hot leads; `since` is inclusive (the worst case for duplicates)."""

    def __init__(self):
        self.leads = []
        self.requests = []
        self.envelope = "list"       # list | hot_leads | paged
        self.page_size = None
        self.newest_first = False
        self.fail_with = None        # HTTP status to return instead of data
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                fake.requests.append({"path": url.path, "query": q,
                                      "key": self.headers.get("X-API-Key")})
                if fake.fail_with:
                    return self._send(fake.fail_with, {"detail": "boom"})
                if self.headers.get("X-API-Key") != API_KEY:
                    return self._send(401, {"detail": "bad key"})
                if url.path != ehl.HOT_LEADS_PATH:
                    return self._send(404, {"detail": "not found"})
                since = ehl.parse_time(q["since"])
                rows = [l for l in fake.leads if ehl.parse_time(l["became_hot_at"]) >= since]
                rows.sort(key=lambda l: ehl.parse_time(l["became_hot_at"]),
                          reverse=fake.newest_first)
                more = False
                if fake.page_size and len(rows) > fake.page_size:
                    rows, more = rows[: fake.page_size], True
                if fake.envelope == "list":
                    body = rows
                elif fake.envelope == "hot_leads":
                    body = {"hot_leads": rows}
                else:
                    body = {"data": rows, "has_more": more}
                self._send(200, body)

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Outbox:
    def __init__(self, fail_after=None):
        self.messages = []
        self.fail_after = fail_after

    def __call__(self, cfg, msg):
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            raise OSError("smtp down")
        self.messages.append(msg)

    @property
    def subjects(self):
        return [m["Subject"] for m in self.messages]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.explee = FakeExplee()
        self.cfg = dict(ehl.DEFAULTS)
        self.cfg.update(
            EXPLEE_BASE_URL=self.explee.url, EXPLEE_API_KEY=API_KEY, STATE_DIR=self.tmp.name,
            MAIL_FROM="me@example.com", MAIL_TO="me@example.com", SMTP_USER="me@example.com",
        )
        sleep = mock.patch.object(ehl.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        self.explee.close()
        self.tmp.cleanup()

    def hours_ago(self, h):
        return ehl.iso_utc(ehl.now_utc() - dt.timedelta(hours=h))

    def run_sync(self, outbox):
        state = ehl.State(self.tmp.name)
        return ehl.sync(self.cfg, state, sender=outbox), ehl.State(self.tmp.name)


class SyncTests(Base):
    def test_first_run_emails_recent_leads_oldest_first_and_sends_key_and_project(self):
        self.explee.leads = [lead(2, self.hours_ago(1)), lead(1, self.hours_ago(5)),
                             lead(9, self.hours_ago(24 * 30))]  # older than the 7-day lookback
        out = Outbox()
        sent, state = self.run_sync(out)
        self.assertEqual(sent, 2)
        self.assertEqual(out.subjects, ["Hot lead: Person 1 (Company 1)",
                                        "Hot lead: Person 2 (Company 2)"])
        self.assertEqual(state.data["cursor"], self.explee.leads[0]["became_hot_at"])
        req = self.explee.requests[0]
        self.assertEqual(req["key"], API_KEY)
        self.assertEqual(req["query"]["project_id"], "42547")

    def test_second_run_with_inclusive_since_sends_no_duplicates(self):
        self.explee.leads = [lead(1, self.hours_ago(2)), lead(2, self.hours_ago(1))]
        out = Outbox()
        self.run_sync(out)
        sent, _ = self.run_sync(out)
        self.assertEqual(sent, 0)
        self.assertEqual(len(out.messages), 2)

    def test_second_run_asks_from_cursor_minus_overlap(self):
        hot = self.hours_ago(3)
        self.explee.leads = [lead(1, hot)]
        self.run_sync(Outbox())
        self.run_sync(Outbox())
        since = ehl.parse_time(self.explee.requests[-1]["query"]["since"])
        self.assertEqual(ehl.parse_time(hot) - since, dt.timedelta(minutes=60))

    def test_late_arriving_older_lead_inside_overlap_is_still_sent(self):
        self.explee.leads = [lead(1, self.hours_ago(1))]
        out = Outbox()
        self.run_sync(out)
        # Explee stores a lead that became hot 20 minutes *before* the one already sent.
        self.explee.leads.append(lead(2, self.hours_ago(1.33)))
        sent, _ = self.run_sync(out)
        self.assertEqual(sent, 1)
        self.assertEqual(out.subjects[-1], "Hot lead: Person 2 (Company 2)")

    def test_mail_failure_mid_batch_keeps_cursor_and_resumes_exactly_once(self):
        self.explee.leads = [lead(i, self.hours_ago(10 - i)) for i in range(1, 6)]
        failing = Outbox(fail_after=2)
        with self.assertRaises(OSError):
            self.run_sync(failing)
        state = ehl.State(self.tmp.name)
        self.assertEqual(state.data["cursor"], self.explee.leads[1]["became_hot_at"])
        out = Outbox()
        sent, _ = self.run_sync(out)
        self.assertEqual(sent, 3)
        self.assertEqual(out.subjects[0], "Hot lead: Person 3 (Company 3)")

    def test_same_timestamp_leads_are_both_sent(self):
        t = self.hours_ago(1)
        self.explee.leads = [lead(1, t), lead(2, t)]
        out = Outbox()
        sent, _ = self.run_sync(out)
        self.assertEqual(sent, 2)
        self.explee.leads.append(lead(3, t))  # a third one with the very same timestamp
        sent, _ = self.run_sync(out)
        self.assertEqual(sent, 1)

    def test_leads_without_id_or_email_do_not_collide(self):
        t = self.hours_ago(1)
        self.explee.leads = [lead(1, t, email=None), lead(2, t, email=None)]
        sent, _ = self.run_sync(Outbox())
        self.assertEqual(sent, 2)

    def test_check_command_fails_when_lead_fields_differ(self):
        odd = [{"full_name": "X", "hot_since": self.hours_ago(1)}]
        with mock.patch.object(ehl, "fetch_hot_leads", return_value=(odd, False)), \
                mock.patch.object(ehl, "send_email", Outbox()), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(ehl.cmd_test(self.cfg), 1)
        self.assertIn("missing email, became_hot_at, why_hot", out.getvalue())

    def test_wrapped_envelope(self):
        self.explee.envelope = "hot_leads"
        self.explee.leads = [lead(1, self.hours_ago(1))]
        sent, _ = self.run_sync(Outbox())
        self.assertEqual(sent, 1)

    def test_paged_envelope_with_has_more_drains_all_pages(self):
        self.explee.envelope = "paged"
        self.explee.page_size = 2
        self.explee.leads = [lead(i, self.hours_ago(10 - i)) for i in range(1, 6)]
        out = Outbox()
        sent, _ = self.run_sync(out)
        self.assertEqual(sent, 5)
        self.assertEqual(len(set(out.subjects)), 5)

    def test_unknown_envelope_is_an_error_not_silence(self):
        with mock.patch.object(ehl, "http_get_json", return_value={"weird": 1}):
            with self.assertRaises(ehl.ExpleeError):
                self.run_sync(Outbox())

    def test_first_run_lookback_zero_sends_nothing_old(self):
        self.cfg["FIRST_RUN_LOOKBACK_DAYS"] = "0"
        self.explee.leads = [lead(1, self.hours_ago(1))]
        sent, _ = self.run_sync(Outbox())
        self.assertEqual(sent, 0)


class FailureTests(Base):
    def test_bad_key_is_not_retried_and_says_so(self):
        self.cfg["EXPLEE_API_KEY"] = "wrong"
        with self.assertRaises(ehl.ExpleeError) as ctx:
            self.run_sync(Outbox())
        self.assertIn("API key", str(ctx.exception))
        self.assertEqual(len(self.explee.requests), 1)

    def test_server_error_is_retried(self):
        self.explee.fail_with = 503
        with self.assertRaises(ehl.ExpleeError):
            self.run_sync(Outbox())
        self.assertEqual(len(self.explee.requests), 3)

    def test_alert_once_after_n_failures_then_recovery_notice(self):
        self.cfg["ALERT_AFTER_FAILURES"] = "3"
        state = ehl.State(self.tmp.name)
        out = Outbox()
        for _ in range(5):
            ehl.handle_failure(self.cfg, state, ehl.ExpleeError("down"), sender=out)
        self.assertEqual(out.subjects, ["Explee hot leads: sync is failing"])
        ehl.handle_success(self.cfg, state, sender=out)
        self.assertEqual(out.subjects[-1], "Explee hot leads: working again")
        self.assertEqual(ehl.State(self.tmp.name).data["consecutive_failures"], 0)

    def test_cmd_run_returns_error_code_and_records_error(self):
        self.explee.fail_with = 500
        with mock.patch.object(ehl, "send_email", Outbox()):
            self.assertEqual(ehl.cmd_run(self.cfg), 1)
        self.assertIn("HTTP 500", ehl.State(self.tmp.name).data["last_error"])

    def test_missing_settings_are_reported(self):
        self.cfg["EXPLEE_API_KEY"] = ""
        with self.assertRaises(ehl.ConfigError):
            ehl.cmd_run(self.cfg)


class FormatTests(unittest.TestCase):
    def test_parse_time_variants(self):
        z = ehl.parse_time("2026-09-30T12:00:00Z")
        self.assertEqual(z, dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(ehl.parse_time("2026-09-30T14:00:00+02:00"), z)
        self.assertEqual(ehl.parse_time("2026-09-30T12:00:00"), z)
        self.assertEqual(ehl.parse_time("2026-09-30T12:00:00.123456789Z").microsecond, 123456)
        self.assertIsNone(ehl.parse_time("yesterday"))
        self.assertIsNone(ehl.parse_time(None))

    def test_email_escapes_html_and_skips_empty_fields(self):
        cfg = dict(ehl.DEFAULTS, MAIL_FROM="me@example.com", MAIL_TO="me@example.com")
        msg = ehl.build_lead_email(cfg, lead(1, "2026-09-30T12:00:00Z", phone=None,
                                             why_hot="<script>x</script>\nline two"))
        html_part = msg.get_body(("html",)).get_content()
        text_part = msg.get_body(("plain",)).get_content()
        self.assertNotIn("<script>", html_part)
        self.assertIn("&lt;script&gt;", html_part)
        self.assertNotIn("Phone", text_part)
        self.assertIn("> line two", text_part)
        self.assertIn("30-09-2026 14:00", text_part)  # shown in Amsterdam time
        self.assertIsNone(msg["Reply-To"])  # replying never mails the lead by accident

    def test_nested_contact_and_first_last_name(self):
        l = {"contact": {"first_name": "Ada", "last_name": "Lovelace", "email": "a@x.io"},
             "why_hot": "ok", "became_hot_at": "2026-09-30T12:00:00Z"}
        self.assertEqual(ehl.full_name(ehl.contact_of(l)), "Ada Lovelace")
        self.assertEqual(ehl.lead_key(l), "email:a@x.io|2026-09-30T12:00:00Z")
        self.assertEqual(ehl.lead_key({"id": 7, "email": "a@x.io"}), "id:7")

    def test_config_file_parsing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# comment\nEXPLEE_API_KEY=sk_a=b#c\nSMTP_PASSWORD='abcd efgh'\n"
                     "SMTP_USER=me@gmail.com\n")
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                cfg = ehl.load_config(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(cfg["EXPLEE_API_KEY"], "sk_a=b#c")
        self.assertEqual(cfg["SMTP_PASSWORD"], "abcd efgh")
        self.assertEqual(cfg["MAIL_FROM"], "me@gmail.com")
        self.assertEqual(cfg["EXPLEE_PROJECT_ID"], "42547")


class SMTPStub(socketserver.StreamRequestHandler):
    """Just enough SMTP to receive one message, no TLS (only allowed for localhost)."""
    received = []

    def handle(self):
        w = lambda s: self.wfile.write((s + "\r\n").encode())
        w("220 stub")
        data, in_data = [], False
        for raw in self.rfile:
            line = raw.decode().rstrip("\r\n")
            if in_data:
                if line == ".":
                    in_data = False
                    SMTPStub.received.append("\r\n".join(data))
                    w("250 ok")
                else:
                    data.append(line[1:] if line.startswith("..") else line)
                continue
            cmd = line[:4].upper()
            if cmd == "EHLO":
                w("250 stub")
            elif cmd == "DATA":
                in_data = True
                w("354 go")
            elif cmd == "QUIT":
                w("221 bye")
                return
            else:
                w("250 ok")


class SMTPTests(unittest.TestCase):
    def setUp(self):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTPStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def cfg(self, **extra):
        return dict(ehl.DEFAULTS, MAIL_FROM="me@example.com", MAIL_TO="me@example.com", **extra)

    def test_send_email_over_real_smtp_to_localhost(self):
        cfg = self.cfg(SMTP_HOST="127.0.0.1", SMTP_PORT=str(self.port))
        ehl.send_email(cfg, ehl.build_lead_email(cfg, lead(1, "2026-09-30T12:00:00Z")))
        msg = email.message_from_string(SMTPStub.received[-1])
        self.assertEqual(msg["Subject"], "Hot lead: Person 1 (Company 1)")

    def test_refuses_plaintext_login_to_remote_host(self):
        port, real = self.port, ehl.smtplib.SMTP

        class Redirect(real):  # pretend mail.example.com is the stub, which has no STARTTLS
            def __init__(self, host, p, timeout=30):
                super().__init__("127.0.0.1", port, timeout=timeout)

        cfg = self.cfg(SMTP_HOST="mail.example.com", SMTP_PORT="587",
                       SMTP_USER="me", SMTP_PASSWORD="secret")
        before = len(SMTPStub.received)
        with mock.patch.object(ehl.smtplib, "SMTP", Redirect):
            with self.assertRaises(ehl.smtplib.SMTPException) as ctx:
                ehl.send_email(cfg, ehl.build_notice_email(cfg, "x", "y"))
        self.assertIn("encryption", str(ctx.exception))
        self.assertEqual(len(SMTPStub.received), before)


if __name__ == "__main__":
    unittest.main()
