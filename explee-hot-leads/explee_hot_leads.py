#!/usr/bin/env python3
"""Explee hot leads -> email.

Every run (a systemd timer starts it every 10 minutes) this script:
  1. asks Explee for hot leads that became hot since the last check,
  2. sends one email per new hot lead,
  3. remembers the newest `became_hot_at` so the next run only gets new ones.

Python 3.8+ standard library only, so there is nothing to `pip install`.

Commands:
  run     fetch new hot leads and email them (default; used by the timer)
  test    check the Explee connection and send one test email
  status  show when it last ran, the saved position and any error
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

VERSION = "1.0.0"
DEFAULT_CONFIG = "/etc/explee-hot-leads/config.env"
HOT_LEADS_PATH = "/public/api/v1/autogtm/hot-leads"
USER_AGENT = "vayu-explee-hot-leads/" + VERSION

DEFAULTS = {
    "EXPLEE_BASE_URL": "https://api.explee.com",
    "EXPLEE_PROJECT_ID": "42547",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "FIRST_RUN_LOOKBACK_DAYS": "7",
    "OVERLAP_MINUTES": "60",
    "ALERT_AFTER_FAILURES": "6",
    "TIMEZONE": "Europe/Amsterdam",
    "STATE_DIR": "/var/lib/explee-hot-leads",
    "HTTP_TIMEOUT_SECONDS": "30",
}

# Lead lists may come back bare or wrapped in an object under one of these keys.
LIST_KEYS = ("hot_leads", "leads", "items", "data", "results")
MAX_PAGES_PER_RUN = 50
MAX_SEEN_KEYS = 2000


class ConfigError(Exception):
    pass


class ExpleeError(Exception):
    pass


# --------------------------------------------------------------------------- config

def load_config(path: str) -> dict:
    """Read KEY=VALUE lines. Environment variables override the file."""
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                cfg[key] = value
    for key in list(DEFAULTS) + [
        "EXPLEE_API_KEY", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO",
    ]:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    if not cfg.get("MAIL_FROM"):
        cfg["MAIL_FROM"] = cfg.get("SMTP_USER", "")
    return cfg


def require(cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise ConfigError("missing setting(s) in config: " + ", ".join(missing))


def int_setting(cfg: dict, key: str) -> int:
    try:
        return int(cfg[key])
    except (KeyError, ValueError):
        raise ConfigError("setting %s must be a whole number, got %r" % (key, cfg.get(key)))


# --------------------------------------------------------------------------- time

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_time(value) -> dt.datetime | None:
    """Parse ISO 8601 into an aware UTC datetime. Naive values are taken as UTC."""
    if not isinstance(value, str):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
    second = int(m.group(6) or 0)
    micro = int((m.group(7) or "0")[:6].ljust(6, "0"))
    tz = dt.timezone.utc
    off = m.group(8)
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        tz = dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=tz).astimezone(
            dt.timezone.utc
        )
    except ValueError:
        return None


def iso_utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def local_display(moment: dt.datetime | None, tz_name: str) -> str:
    if moment is None:
        return ""
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+

        return moment.astimezone(ZoneInfo(tz_name)).strftime("%d-%m-%Y %H:%M (%Z)")
    except Exception:
        return moment.strftime("%d-%m-%Y %H:%M (UTC)")


# --------------------------------------------------------------------------- state

class State:
    """Small JSON file holding the position (cursor) and recently sent lead keys."""

    def __init__(self, state_dir: str):
        self.dir = state_dir
        self.path = os.path.join(state_dir, "state.json")
        self.data = {
            "cursor": None,          # newest became_hot_at already emailed (raw string)
            "seen": [],              # keys of recently emailed leads, oldest first
            "consecutive_failures": 0,
            "alert_sent": False,
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "total_sent": 0,
        }
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def mark_sent(self, key: str, became_hot_raw: str | None) -> None:
        seen = self.data["seen"]
        if key not in seen:
            seen.append(key)
        del seen[:-MAX_SEEN_KEYS]
        current = parse_time(self.data.get("cursor"))
        new = parse_time(became_hot_raw)
        if new is not None and (current is None or new > current):
            self.data["cursor"] = became_hot_raw
        self.data["total_sent"] = int(self.data.get("total_sent") or 0) + 1


class RunLock:
    """Stops two runs overlapping if one is slow."""

    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.fh = open(os.path.join(state_dir, "run.lock"), "w")

    def acquire(self) -> bool:
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------- explee

def http_get_json(url: str, api_key: str, timeout: int, attempts: int = 3):
    last_error = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            headers={"X-API-Key": api_key, "Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                try:
                    return json.loads(body)
                except ValueError:
                    raise ExpleeError("Explee sent something that is not JSON: %s" % body[:300])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last_error = ExpleeError("Explee answered HTTP %s: %s" % (exc.code, detail))
            if exc.code in (401, 403):
                raise ExpleeError(
                    "Explee refused the API key (HTTP %s). Check EXPLEE_API_KEY. %s"
                    % (exc.code, detail)
                )
            if exc.code != 429 and exc.code < 500:
                raise last_error
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 5 * attempt
        except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
            last_error = ExpleeError("could not reach Explee: %s" % exc)
            wait = 5 * attempt
        if attempt < attempts:
            time.sleep(min(wait, 60))
    raise last_error


def extract_leads(payload) -> tuple[list, bool]:
    """Return (leads, more_available) from whatever envelope Explee uses."""
    if isinstance(payload, list):
        return payload, False
    if isinstance(payload, dict):
        for key in LIST_KEYS:
            if isinstance(payload.get(key), list):
                more = bool(payload.get("has_more") or payload.get("hasMore"))
                return payload[key], more
    raise ExpleeError("unexpected answer from Explee: %s" % json.dumps(payload)[:300])


def fetch_hot_leads(cfg: dict, since_raw: str) -> tuple[list, bool]:
    params = {"since": since_raw}
    if cfg.get("EXPLEE_PROJECT_ID"):
        params["project_id"] = cfg["EXPLEE_PROJECT_ID"]
    url = cfg["EXPLEE_BASE_URL"].rstrip("/") + HOT_LEADS_PATH + "?" + urllib.parse.urlencode(params)
    payload = http_get_json(url, cfg["EXPLEE_API_KEY"], int_setting(cfg, "HTTP_TIMEOUT_SECONDS"))
    return extract_leads(payload)


def contact_of(lead: dict) -> dict:
    """Leads are flat, but accept a nested `contact` object too."""
    merged = {}
    nested = lead.get("contact")
    if isinstance(nested, dict):
        merged.update(nested)
    merged.update({k: v for k, v in lead.items() if k != "contact"})
    return merged


def lead_key(lead: dict) -> str:
    c = contact_of(lead)
    for k in ("id", "lead_id", "hot_lead_id"):
        if c.get(k) not in (None, ""):
            return "id:%s" % c[k]
    email = str(c.get("email") or "").strip().lower()
    if email:
        return "email:%s|%s" % (email, c.get("became_hot_at") or "")
    # No id and no email: fall back to the whole record so two such leads never collide.
    digest = hashlib.sha256(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest()
    return "hash:" + digest[:32]


def full_name(c: dict) -> str:
    name = str(c.get("name") or "").strip()
    if not name:
        name = " ".join(str(c.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return name or str(c.get("email") or "Unknown contact")


# --------------------------------------------------------------------------- email

FIELDS = (
    ("Name", "name"),
    ("Job title", "job_title"),
    ("Company", "company_name"),
    ("Website", "company_domain"),
    ("Email", "email"),
    ("Phone", "phone"),
    ("Country", "country"),
    ("LinkedIn", "linkedin_url"),
)


def build_lead_email(cfg: dict, lead: dict) -> EmailMessage:
    c = contact_of(lead)
    name = full_name(c)
    company = str(c.get("company_name") or "").strip()
    became_hot = local_display(parse_time(c.get("became_hot_at")), cfg["TIMEZONE"])
    why_hot = str(c.get("why_hot") or "").strip()
    email = str(c.get("email") or "").strip()

    rows = []
    for label, key in FIELDS:
        value = name if key == "name" else str(c.get(key) or "").strip()
        if value:
            rows.append((label, key, value))
    if became_hot:
        rows.append(("Became hot", "became_hot_at", became_hot))

    subject = "Hot lead: %s" % name + (" (%s)" % company if company else "")

    text = ["New hot lead from Explee", ""]
    text += ["%-11s %s" % (label + ":", value) for label, _, value in rows]
    text += ["", "Their reply:", ""]
    text += ["> " + line for line in (why_hot or "(no reply text)").splitlines()]
    if email:
        text += ["", "To answer them, write a new email to %s." % email]

    def cell(key, value):
        v = html.escape(value)
        if key == "email":
            return '<a href="mailto:%s">%s</a>' % (html.escape(value, quote=True), v)
        if key == "linkedin_url" or key == "company_domain":
            href = value if value.startswith("http") else "https://" + value
            return '<a href="%s">%s</a>' % (html.escape(href, quote=True), v)
        if key == "phone":
            return '<a href="tel:%s">%s</a>' % (html.escape(re.sub(r"[^\d+]", "", value)), v)
        return v

    table = "".join(
        '<tr><td style="padding:4px 12px 4px 0;color:#666;white-space:nowrap">%s</td>'
        '<td style="padding:4px 0">%s</td></tr>' % (html.escape(label), cell(key, value))
        for label, key, value in rows
    )
    quote = html.escape(why_hot or "(no reply text)").replace("\n", "<br>")
    button = ""
    if email:
        reply_href = "mailto:%s?subject=%s" % (
            urllib.parse.quote(email),
            urllib.parse.quote("Re: your reply"),
        )
        button = (
            '<p><a href="%s" style="display:inline-block;padding:10px 16px;background:#e8590c;'
            'color:#fff;text-decoration:none;border-radius:6px">Email %s</a></p>'
            % (html.escape(reply_href, quote=True), html.escape(name))
        )
    body_html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222">'
        '<h2 style="margin:0 0 12px">&#128293; New hot lead: %s</h2>'
        '<table style="border-collapse:collapse">%s</table>'
        '<h3 style="margin:20px 0 8px">Their reply</h3>'
        '<blockquote style="margin:0;padding:10px 14px;border-left:4px solid #e8590c;'
        'background:#fff4e6">%s</blockquote>%s'
        '<p style="color:#999;font-size:12px;margin-top:24px">Sent automatically from your '
        "server by explee-hot-leads.</p></div>"
    ) % (html.escape(name), table, quote, button)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Explee hot leads", cfg["MAIL_FROM"]))
    msg["To"] = cfg["MAIL_TO"]
    msg["Message-ID"] = make_msgid(domain="explee-hot-leads.local")
    msg.set_content("\n".join(text) + "\n")
    msg.add_alternative(body_html, subtype="html")
    return msg


def build_notice_email(cfg: dict, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Explee hot leads", cfg["MAIL_FROM"]))
    msg["To"] = cfg["MAIL_TO"]
    msg["Message-ID"] = make_msgid(domain="explee-hot-leads.local")
    msg.set_content(body)
    return msg


def send_email(cfg: dict, msg: EmailMessage) -> None:
    host = cfg["SMTP_HOST"]
    port = int_setting(cfg, "SMTP_PORT")
    local = host in ("localhost", "127.0.0.1", "::1")
    password = cfg.get("SMTP_PASSWORD", "")
    if host.endswith("gmail.com"):
        password = re.sub(r"\s+", "", password)  # Google shows app passwords with spaces
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        server.ehlo()
        if port != 465:
            if server.has_extn("starttls"):
                server.starttls(context=context)
                server.ehlo()
            elif not local:
                raise smtplib.SMTPException(
                    "%s:%s does not offer encryption (STARTTLS); refusing to send the password "
                    "unencrypted" % (host, port)
                )
        if cfg.get("SMTP_USER"):
            server.login(cfg["SMTP_USER"], password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------- run

def log(message: str) -> None:
    print(message, flush=True)


def sort_key(lead: dict):
    moment = parse_time(contact_of(lead).get("became_hot_at"))
    return (moment is None, moment or dt.datetime.min.replace(tzinfo=dt.timezone.utc))


def query_since(cfg: dict, state: State) -> str:
    cursor = parse_time(state.data.get("cursor"))
    if cursor is None:
        return iso_utc(now_utc() - dt.timedelta(days=int_setting(cfg, "FIRST_RUN_LOOKBACK_DAYS")))
    # Ask a little further back than the cursor so a lead Explee stores slightly late is not
    # skipped; leads we already emailed are filtered out by their key.
    return iso_utc(cursor - dt.timedelta(minutes=int_setting(cfg, "OVERLAP_MINUTES")))


def sync(cfg: dict, state: State, sender=send_email) -> int:
    """Fetch and email new hot leads. Returns how many were emailed."""
    sent = 0
    for _ in range(MAX_PAGES_PER_RUN):
        since = query_since(cfg, state)
        leads, more = fetch_hot_leads(cfg, since)
        leads = [l for l in leads if isinstance(l, dict)]
        leads.sort(key=sort_key)  # oldest first, so the cursor never passes an unsent lead
        seen = set(state.data["seen"])
        new = [l for l in leads if lead_key(l) not in seen]
        log("asked Explee for hot leads since %s: %d returned, %d new" % (since, len(leads), len(new)))
        cursor_before = state.data.get("cursor")
        for lead in new:
            c = contact_of(lead)
            sender(cfg, build_lead_email(cfg, lead))
            state.mark_sent(lead_key(lead), c.get("became_hot_at"))
            state.save()  # after every email, so a crash never re-sends or skips
            sent += 1
            log("emailed hot lead: %s <%s>" % (full_name(c), c.get("email") or ""))
        if not more or not new or state.data.get("cursor") == cursor_before:
            break
    return sent


def handle_failure(cfg: dict, state: State, error: Exception, sender=send_email) -> None:
    state.data["consecutive_failures"] = int(state.data.get("consecutive_failures") or 0) + 1
    state.data["last_error"] = "%s: %s" % (iso_utc(now_utc()), error)
    limit = int_setting(cfg, "ALERT_AFTER_FAILURES")
    if state.data["consecutive_failures"] >= limit and not state.data.get("alert_sent"):
        body = (
            "Your Explee hot-leads sync has failed %d times in a row, so new hot leads are NOT "
            "reaching your inbox right now.\n\nLast error:\n%s\n\nNothing is lost: once the "
            "problem is fixed, the next run picks up every hot lead it missed.\n\nTo look at it "
            "on the server, run:  sudo explee-hot-leads status\n"
            % (state.data["consecutive_failures"], error)
        )
        try:
            sender(cfg, build_notice_email(cfg, "Explee hot leads: sync is failing", body))
            state.data["alert_sent"] = True
        except Exception as exc:  # mail itself is broken; the journal still has it
            log("could not send the failure alert either: %s" % exc)
    state.save()


def handle_success(cfg: dict, state: State, sender=send_email) -> None:
    if state.data.get("alert_sent"):
        try:
            sender(
                cfg,
                build_notice_email(
                    cfg,
                    "Explee hot leads: working again",
                    "The Explee hot-leads sync is working again. Any hot leads that came in "
                    "while it was down have now been emailed to you.\n",
                ),
            )
        except Exception as exc:
            log("could not send the recovery notice: %s" % exc)
    state.data.update(
        consecutive_failures=0, alert_sent=False, last_error=None,
        last_success_at=iso_utc(now_utc()),
    )
    state.save()


def cmd_run(cfg: dict) -> int:
    require(cfg, "EXPLEE_API_KEY", "SMTP_HOST", "MAIL_FROM", "MAIL_TO")
    state = State(cfg["STATE_DIR"])
    lock = RunLock(cfg["STATE_DIR"])
    if not lock.acquire():
        log("another run is still busy; skipping this one")
        return 0
    state.data["last_run_at"] = iso_utc(now_utc())
    try:
        sent = sync(cfg, state)
    except Exception as exc:
        log("ERROR: %s" % exc)
        handle_failure(cfg, state, exc)
        return 1
    handle_success(cfg, state)
    log("done: %d hot lead(s) emailed" % sent)
    return 0


def cmd_test(cfg: dict) -> int:
    require(cfg, "EXPLEE_API_KEY", "SMTP_HOST", "MAIL_FROM", "MAIL_TO")
    ok = True
    since = iso_utc(now_utc() - dt.timedelta(days=30))
    try:
        leads, _ = fetch_hot_leads(cfg, since)
        print("OK   Explee: connected; %d hot lead(s) in the last 30 days" % len(leads))
        if leads and isinstance(leads[0], dict):
            fields = contact_of(leads[0])
            missing = [k for k in ("email", "became_hot_at", "why_hot") if k not in fields]
            if missing:
                ok = False
                print("FAIL Explee: leads are missing %s; fields present: %s"
                      % (", ".join(missing), ", ".join(sorted(fields))))
    except Exception as exc:
        ok = False
        print("FAIL Explee: %s" % exc)
    try:
        send_email(
            cfg,
            build_notice_email(
                cfg,
                "Explee hot leads: test email",
                "This is a test from your server. If you can read this, hot leads will reach "
                "this inbox.\n",
            ),
        )
        print("OK   Email: test email sent to %s" % cfg["MAIL_TO"])
    except Exception as exc:
        ok = False
        print("FAIL Email: %s" % exc)
    return 0 if ok else 1


def cmd_status(cfg: dict) -> int:
    state = State(cfg["STATE_DIR"])
    if not state.exists:
        print("No run has finished yet.")
        return 0
    d = state.data
    print("Last run:            %s" % (d.get("last_run_at") or "-"))
    print("Last successful run: %s" % (d.get("last_success_at") or "-"))
    print("Newest lead emailed: %s" % (d.get("cursor") or "-"))
    print("Hot leads emailed:   %s" % (d.get("total_sent") or 0))
    print("Failures in a row:   %s" % (d.get("consecutive_failures") or 0))
    if d.get("last_error"):
        print("Last error:          %s" % d["last_error"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Email new Explee hot leads.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "test", "status"))
    parser.add_argument(
        "--config", default=os.environ.get("EXPLEE_HOT_LEADS_CONFIG", DEFAULT_CONFIG)
    )
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        return {"run": cmd_run, "test": cmd_test, "status": cmd_status}[args.command](cfg)
    except ConfigError as exc:
        print("CONFIG ERROR: %s (file: %s)" % (exc, args.config), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
