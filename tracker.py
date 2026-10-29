#!/usr/bin/env python3
"""worktracker v2: a slim macOS activity tracker that protects your evenings.

Every few seconds it checks (1) how long since the last keyboard/mouse event,
(2) which app is in front, and (3) for browsers, the current domain. Time is
aggregated per day/hour/app/domain into a small SQLite file. A tiny HTTP server
on localhost serves the dashboard. Goals, a work window and a graded alert
ladder help you stop on time.

Idle time comes from the HID layer, so `caffeinate` does not affect it.
"""
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from AppKit import (
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSCompositingOperationSourceOver,
    NSDeviceRGBColorSpace,
    NSGraphicsContext,
    NSWorkspace,
    NSZeroRect,
)
from Foundation import NSAppleScript, NSRunLoop, NSTimer
from Quartz import (
    CGEventSourceSecondsSinceLastEventType,
    kCGAnyInputEventType,
    kCGEventSourceStateHIDSystemState,
)

CODE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("WORKTRACKER_HOME", CODE_DIR / "data"))
DB_PATH = APP_DIR / "tracker.db"
CONFIG_PATH = APP_DIR / "config.json"
DASHBOARD_PATH = CODE_DIR / "dashboard.html"
LOG_DIR = APP_DIR / "logs"

DEFAULTS = {
    "sample_seconds": 10,
    "idle_threshold_minutes": 5,
    # While these are in front, you count as active even without input (calls).
    "idle_exempt_apps": ["zoom.us", "FaceTime", "Microsoft Teams"],
    "idle_exempt_domains": ["meet.google.com", "zoom.us", "teams.microsoft.com"],
    "day_starts_at_hour": 4,          # work before this hour belongs to the previous day
    "dashboard_port": 8765,
    # Work window. Activity outside it, on non-work days, or after you close
    # the day counts as after-hours. Weekdays: 0 = Monday ... 6 = Sunday.
    "work_days": [0, 1, 2, 3, 4],
    "week_starts_on": 0,              # same numbering as work_days
    "work_start": "08:00",
    "work_end": "18:00",
    # Your own if-then response, quoted back to you in alerts.
    "shutdown_plan": "close the laptop and step outside for 10 minutes",
    # Placeholders: set these to what is right for you. They count *active*
    # time, which is lower than time at the desk.
    "goals": {
        "daily_target_hours": 7.5,
        "daily_ceiling_hours": 9,
        "weekly_target_hours": 38,
        "weekly_ceiling_hours": 48,
    },
    "alerts": {
        "enabled": True,
        "dialogs_for_important": True,   # pop-up dialogs for limits and wind-down
        "max_per_day": 8,                # cap on routine alerts (limits always show)
        "min_gap_minutes": 10,           # spacing between routine alerts
        "pace_pct": 80,                  # heads-up at this % of a target
        "wind_down_minutes_before_end": 30,
        "after_hours_nudge_minutes": 15,
        "continuous_work_minutes": 90,
        "break_minutes_to_reset": 5,
        "app_limits_minutes": {},        # e.g. {"Slack": 90}
        "domain_limits_minutes": {},     # e.g. {"youtube.com": 30}
        "hold_during_zoom_meetings": True,
        "after_meeting_gap_minutes": 3,  # spacing for alerts held during a meeting
    },
}

WRAPPED_MAX_AFTER_SECS = 15 * 60     # "wrapped on time" = under 15 min after hours
ICON_PX = 64
ICON_MISS_RETRY_SECONDS = 600
# Chromium-family browsers keep a per-profile favicon cache in <base>/<profile>/Favicons.
FAVICON_BASES = [Path.home() / "Library/Application Support" / p for p in (
    "Google/Chrome", "BraveSoftware/Brave-Browser", "Microsoft Edge",
    "Arc/User Data", "Chromium", "Vivaldi")]

BROWSER_SCRIPTS = {
    "Safari": 'tell application "Safari" to return URL of current tab of front window',
}
for _b in ("Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Chromium", "Vivaldi"):
    BROWSER_SCRIPTS[_b] = f'tell application "{_b}" to return URL of active tab of front window'

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage(
  day TEXT, hour INTEGER, app TEXT, domain TEXT, active INTEGER,
  after_hours INTEGER, secs REAL,
  PRIMARY KEY(day, hour, app, domain, active, after_hours));
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, start REAL, end REAL);
CREATE INDEX IF NOT EXISTS sessions_day ON sessions(day);
CREATE TABLE IF NOT EXISTS day_notes(
  day TEXT PRIMARY KEY, closed_at REAL, next_step TEXT);
CREATE TABLE IF NOT EXISTS alerts(
  day TEXT, key TEXT, ts REAL, message TEXT, PRIMARY KEY(day, key));
"""

log = logging.getLogger("worktracker")
cfg = json.loads(json.dumps(DEFAULTS))
cfg_mtime = 0.0
live = {"app": "", "domain": "", "idle": 0, "active": False}
signals = {}
app_paths = {}                       # app name -> bundle path, learned while sampling
icon_cache = {}                      # (kind, name) -> (fetched_at, png bytes or None)
icon_lock = threading.Lock()


# ---------- config ----------
def load_config():
    global cfg_mtime
    merged = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        except Exception:
            log.exception("Bad config.json, using defaults")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
    cfg_mtime = CONFIG_PATH.stat().st_mtime
    return merged


HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _num(v, lo, hi):
    v = float(v)
    if not lo <= v <= hi:
        raise ValueError("out of range")
    return v


def apply_config_patch(patch):
    """Validate a settings patch from the dashboard and merge it into config.json."""
    global cfg
    clean = {}
    g = patch.get("goals", {})
    for k in ("daily_target_hours", "daily_ceiling_hours",
              "weekly_target_hours", "weekly_ceiling_hours"):
        if k in g:
            clean.setdefault("goals", {})[k] = _num(g[k], 0, 168)
    if "work_days" in patch:
        days = sorted({int(d) for d in patch["work_days"]})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("bad work_days")
        clean["work_days"] = days
    for k in ("work_start", "work_end"):
        if k in patch:
            if not HHMM.match(str(patch[k])):
                raise ValueError("bad time")
            clean[k] = str(patch[k])
    if hm(clean.get("work_start", cfg["work_start"])) >= hm(clean.get("work_end", cfg["work_end"])):
        raise ValueError("work_end must be after work_start")
    if "shutdown_plan" in patch:
        clean["shutdown_plan"] = str(patch["shutdown_plan"])[:200]
    if "week_starts_on" in patch:
        clean["week_starts_on"] = int(_num(patch["week_starts_on"], 0, 6))
    if "day_starts_at_hour" in patch:
        clean["day_starts_at_hour"] = int(_num(patch["day_starts_at_hour"], 0, 12))
    if "idle_threshold_minutes" in patch:
        clean["idle_threshold_minutes"] = int(_num(patch["idle_threshold_minutes"], 1, 60))
    a = patch.get("alerts", {})
    for k in ("enabled", "dialogs_for_important"):
        if k in a:
            clean.setdefault("alerts", {})[k] = bool(a[k])
    if "continuous_work_minutes" in a:
        clean.setdefault("alerts", {})["continuous_work_minutes"] = int(
            _num(a["continuous_work_minutes"], 10, 480))
    user = {}
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text())
    for k, v in clean.items():
        if isinstance(v, dict):
            user.setdefault(k, {}).update(v)
        else:
            user[k] = v
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(user, indent=2))
    tmp.replace(CONFIG_PATH)
    cfg = load_config()


def day_key(ts):
    shifted = datetime.fromtimestamp(ts) - timedelta(hours=cfg["day_starts_at_hour"])
    return shifted.strftime("%Y-%m-%d")


def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def logical_day_start(d):
    return datetime(d.year, d.month, d.day, cfg["day_starts_at_hour"]).timestamp()


def week_start_of(d):
    return d - timedelta(days=(d.weekday() - cfg["week_starts_on"]) % 7)


def at(d, hhmm):
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m)).timestamp()


def hours_text(secs):
    m = int(round(secs / 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage)")]
    legacy = bool(cols) and "after_hours" not in cols
    if legacy:
        conn.execute("ALTER TABLE usage RENAME TO usage_v1")
    conn.executescript(SCHEMA)
    if legacy:
        conn.execute("INSERT INTO usage SELECT day,hour,app,domain,active,0,secs FROM usage_v1")
        conn.execute("DROP TABLE usage_v1")
    conn.commit()


# ---------- sampling ----------
_compiled = {}
_warned = set()


def browser_url(app):
    script = _compiled.get(app)
    if script is None:
        script = NSAppleScript.alloc().initWithSource_(BROWSER_SCRIPTS[app])
        script.compileAndReturnError_(None)
        _compiled[app] = script
    result, err = script.executeAndReturnError_(None)
    if err is not None or result is None:
        if app not in _warned:
            _warned.add(app)
            log.warning("Cannot read URL from %s. Allow Automation access in "
                        "System Settings > Privacy & Security.", app)
        return ""
    return result.stringValue() or ""


def domain_of(url):
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def sample():
    idle = CGEventSourceSecondsSinceLastEventType(
        kCGEventSourceStateHIDSystemState, kCGAnyInputEventType)
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    app = str(front.localizedName()) if front else "Unknown"
    if front and front.bundleURL():
        app_paths[app] = str(front.bundleURL().path())
    domain = domain_of(browser_url(app)) if app in BROWSER_SCRIPTS else ""
    return idle, app, domain


def in_zoom_meeting():
    # Zoom runs its CptHost helper only while a meeting is in progress.
    return subprocess.run(["pgrep", "-qx", "CptHost"]).returncode == 0


def matches(domain, rule):
    return domain == rule or domain.endswith("." + rule)


# ---------- alerts ----------
def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify(title, message):
    script = f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'
    subprocess.Popen(["osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


DIALOG_RE = re.compile(
    r"button returned:(?P<b>.*?)(?:, text returned:(?P<t>.*))?, gave up:(?P<g>true|false)\s*$",
    re.S)


def run_dialog(title, message, buttons, default, answer=False, give_up=120):
    btns = ", ".join(f'"{esc(b)}"' for b in buttons)
    answer_part = ' default answer ""' if answer else ""
    script = (f'display dialog "{esc(message)}"{answer_part} with title "{esc(title)}" '
              f'buttons {{{btns}}} default button "{esc(default)}" giving up after {give_up}')
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=give_up + 15).stdout
    except Exception:
        log.exception("dialog failed")
        return None
    m = DIALOG_RE.search(out)
    if not m or m.group("g") == "true":
        return None
    return {"button": m.group("b"), "text": (m.group("t") or "").strip()}


class Tracker:
    def __init__(self):
        self.db = connect()
        init_db(self.db)
        self.last = None
        self.cont = 0.0              # continuous active seconds
        self.pause = 0.0             # inactive seconds since last active
        self.block_start = 0.0
        self.after_cont = 0.0        # continuous after-hours active seconds
        self.after_start = 0.0
        self.sess_id = None
        self.sess_end = 0.0
        self.snooze_until = 0.0
        self.last_alert = 0.0
        self.in_meeting = False
        self.meeting_ended = 0.0
        self.recent = []             # recent active credits that could be idle time
        self.last_input = time.time()
        self.pending = []            # results from dialog threads
        self.day = None
        self.load_day(day_key(time.time()))

    # --- state ---
    def load_day(self, day):
        self.day = day
        self.calendar_cfg = (cfg["week_starts_on"], cfg["day_starts_at_hour"])
        d = parse_day(day)
        mon = week_start_of(d)
        self.week_key = "W" + mon.isoformat()
        q = self.db.execute
        self.tot = q("SELECT COALESCE(SUM(secs),0) FROM usage WHERE day=? AND active=1",
                     (day,)).fetchone()[0]
        self.tot_week = q(
            "SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 AND day BETWEEN ? AND ?",
            (mon.isoformat(), (mon + timedelta(days=6)).isoformat())).fetchone()[0]
        self.by_app = dict(q("SELECT app, SUM(secs) FROM usage WHERE day=? AND active=1 "
                             "GROUP BY app", (day,)).fetchall())
        self.by_domain = dict(q("SELECT domain, SUM(secs) FROM usage WHERE day=? AND active=1 "
                                "AND domain<>'' GROUP BY domain", (day,)).fetchall())
        self.fired = {(r[0], r[1]) for r in q(
            "SELECT day, key FROM alerts WHERE day IN (?, ?)", (day, self.week_key))}
        self.alerts_today = q("SELECT COUNT(*) FROM alerts WHERE day=?", (day,)).fetchone()[0]
        self.sess_id = None
        self.reload_closed()

    def reload_closed(self):
        row = self.db.execute("SELECT closed_at FROM day_notes WHERE day=?",
                              (self.day,)).fetchone()
        self.closed_at = row[0] if row and row[0] else None

    def is_after_hours(self, now):
        t = datetime.fromtimestamp(now)
        if t.weekday() not in cfg["work_days"]:
            return True
        minutes = t.hour * 60 + t.minute
        if minutes < hm(cfg["work_start"]) or minutes >= hm(cfg["work_end"]):
            return True
        return self.closed_at is not None

    def close_day(self, note):
        now = time.time()
        self.db.execute(
            "INSERT INTO day_notes(day, closed_at, next_step) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET closed_at=excluded.closed_at, "
            "next_step=excluded.next_step", (self.day, now, note))
        self.closed_at = now
        log.info("Day closed. Next step: %s", note or "(none)")

    def handle_pending(self):
        while self.pending:
            item = self.pending.pop(0)
            if item[0] == "snooze":
                self.snooze_until = time.time() + 30 * 60
            elif item[0] == "snooze15":
                self.snooze_until = time.time() + 15 * 60
            elif item[0] == "close":
                self.close_day(item[1])

    # --- main loop ---
    def tick(self):
        global cfg
        if CONFIG_PATH.exists() and CONFIG_PATH.stat().st_mtime != cfg_mtime:
            cfg = load_config()
            log.info("Config reloaded")
        if signals.pop("reload_closed", None):
            self.reload_closed()
        if (cfg["week_starts_on"], cfg["day_starts_at_hour"]) != self.calendar_cfg:
            self.load_day(day_key(time.time()))
        self.handle_pending()

        a = cfg["alerts"]
        now = time.time()
        gap = now - self.last if self.last else 0.0
        self.last = now
        dt = gap if 0 < gap <= cfg["sample_seconds"] * 3 else 0.0   # skip sleep gaps
        if gap >= a["break_minutes_to_reset"] * 60:
            self.cont = 0.0
            self.after_cont = 0.0

        idle, app, domain = sample()
        meeting = in_zoom_meeting()
        if self.in_meeting and not meeting:
            self.meeting_ended = now
        self.in_meeting = meeting
        exempt = (meeting or app in cfg["idle_exempt_apps"]
                  or any(matches(domain, r) for r in cfg["idle_exempt_domains"] if domain))
        active = idle < cfg["idle_threshold_minutes"] * 60 or exempt
        live.update(app=app, domain=domain, idle=int(idle), active=active, meeting=meeting)

        # A sleep/timer gap hides the idle threshold crossing; judge the break from
        # the last input seen before the gap.
        away_before_gap = now - self.last_input
        if gap and not dt and away_before_gap >= cfg["idle_threshold_minutes"] * 60:
            if self.recent:
                self.trim_idle_tail(self.last_input)
            if away_before_gap >= a["break_minutes_to_reset"] * 60:
                self.cont = 0.0
                self.after_cont = 0.0
        self.last_input = now - idle
        if not active and self.recent:
            self.trim_idle_tail(now - idle)

        day = day_key(now)
        if day != self.day:
            self.load_day(day)
        if dt <= 0:
            return

        after = self.is_after_hours(now)
        hour = datetime.fromtimestamp(now).hour
        self.db.execute(
            "INSERT INTO usage VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(day,hour,app,domain,active,after_hours) "
            "DO UPDATE SET secs=secs+excluded.secs",
            (day, hour, app, domain, int(active), int(after), dt))

        if active:
            if self.cont == 0:
                self.block_start = now
            self.cont += dt
            self.pause = 0.0
            if after:
                if self.after_cont == 0:
                    self.after_start = now
                self.after_cont += dt
            else:
                self.after_cont = 0.0
            self.tot += dt
            self.tot_week += dt
            self.by_app[app] = self.by_app.get(app, 0) + dt
            if domain:
                self.by_domain[domain] = self.by_domain.get(domain, 0) + dt
            self.touch_session(day, now, dt)
            if exempt:
                self.recent = []         # meeting time is work; never trim across it
            else:
                horizon = now - cfg["idle_threshold_minutes"] * 60 - 60
                self.recent = [r for r in self.recent if r[0] > horizon]
                self.recent.append((now, dt, day, hour, app, domain, int(after), self.sess_id))
            self.check_alerts(now, after)
        else:
            self.pause += dt
            if self.pause >= a["break_minutes_to_reset"] * 60:
                self.cont = 0.0
                self.after_cont = 0.0
        self.db.commit()

    def trim_idle_tail(self, last_input):
        """Reclassify the time counted as active between the last input and the
        moment the idle threshold confirmed you were away."""
        trimmed = 0.0
        sessions = set()
        week = week_start_of(parse_day(self.day))
        for ts, dt, day, hour, app, domain, after, sess_id in self.recent:
            if ts - dt < last_input:
                continue
            sessions.add(sess_id)
            self.db.execute(
                "UPDATE usage SET secs = MAX(0, secs - ?) WHERE day=? AND hour=? AND app=? "
                "AND domain=? AND active=1 AND after_hours=?",
                (dt, day, hour, app, domain, after))
            self.db.execute(
                "INSERT INTO usage VALUES(?,?,?,?,0,?,?) "
                "ON CONFLICT(day,hour,app,domain,active,after_hours) "
                "DO UPDATE SET secs=secs+excluded.secs",
                (day, hour, app, domain, after, dt))
            if week_start_of(parse_day(day)) == week:
                self.tot_week -= dt
            if day == self.day:
                self.tot -= dt
                self.by_app[app] = self.by_app.get(app, 0) - dt
                if domain:
                    self.by_domain[domain] = self.by_domain.get(domain, 0) - dt
            trimmed += dt
        self.recent = []
        if not trimmed:
            return
        for sess_id in sessions:
            self.db.execute("UPDATE sessions SET end=MAX(start, ?) WHERE id=?",
                            (last_input, sess_id))
            self.db.execute("DELETE FROM sessions WHERE id=? AND end<=start", (sess_id,))
        if self.sess_id in sessions and not self.db.execute(
                "SELECT 1 FROM sessions WHERE id=?", (self.sess_id,)).fetchone():
            self.sess_id = None
        self.sess_end = min(self.sess_end, last_input)
        self.cont = max(0.0, self.cont - trimmed)
        self.after_cont = max(0.0, self.after_cont - trimmed)
        self.pause += trimmed
        self.db.commit()

    def touch_session(self, day, now, dt):
        brk = cfg["alerts"]["break_minutes_to_reset"] * 60
        if self.sess_id is None or now - self.sess_end > brk:
            cur = self.db.execute("INSERT INTO sessions(day,start,end) VALUES(?,?,?)",
                                  (day, now - dt, now))
            self.sess_id = cur.lastrowid
        else:
            self.db.execute("UPDATE sessions SET end=? WHERE id=?", (now, self.sess_id))
        self.sess_end = now

    # --- alert ladder ---
    def next_work_label(self, now):
        t = datetime.fromtimestamp(now)
        for i in range(8):
            c = t.date() + timedelta(days=i)
            if c.weekday() in cfg["work_days"]:
                if datetime.fromtimestamp(at(c, cfg["work_start"])) > t:
                    if i == 0:
                        return f"today {cfg['work_start']}"
                    day_name = "tomorrow" if i == 1 else c.strftime("%A")
                    return f"{day_name} {cfg['work_start']}"
        return cfg["work_start"]

    def pick(self, options):
        return options[parse_day(self.day).toordinal() % len(options)]

    def check_alerts(self, now, after):
        a, g = cfg["alerts"], cfg["goals"]
        if not a["enabled"]:
            return
        clk = datetime.fromtimestamp(now).strftime("%H:%M")
        plan = cfg["shutdown_plan"]
        nxt = self.next_work_label(now)
        done = hours_text(self.tot)

        # Daily target ladder
        tgt, ceil = g["daily_target_hours"] * 3600, g["daily_ceiling_hours"] * 3600
        if tgt:
            if a["pace_pct"] / 100 * tgt <= self.tot < tgt:
                left = hours_text(tgt - self.tot)
                self.fire("pace", "Heads-up", self.pick([
                    f"It's {clk}. {done} active, about {left} left of today's target. "
                    f"Pick what must be finished and let the rest wait.",
                    f"{clk}: {done} in, roughly {left} to go. "
                    f"Which one thing would make today feel complete?",
                ]))
            if self.tot >= tgt:
                self.fire("target", "Target reached", self.pick([
                    f"It's {clk} and you've reached today's target ({done}). "
                    f"Anything more is overtime. Finish what's open, then: {plan}.",
                    f"Target reached at {clk} ({done} active). A good place to stop. "
                    f"Your plan: {plan}.",
                ]), "important")
        if ceil and self.tot >= ceil:
            self.fire("ceiling", "Daily limit", (
                f"{done} active today, past your {hours_text(ceil)} limit. "
                f"Long days make it harder to switch off tonight. Stop here and "
                f"write tomorrow's first step."), "important")
            over_h = int((self.tot - ceil) // 3600)
            if over_h >= 1:
                self.fire(f"over:{over_h}", "Still working", (
                    f"It's {clk}, {hours_text(self.tot - ceil)} past your limit. "
                    f"Is this urgent, or can it wait until {nxt}?"), "important")

        # Wind-down prompt before the end of the workday
        t = datetime.fromtimestamp(now)
        if (t.weekday() in cfg["work_days"] and self.closed_at is None
                and not after and a["wind_down_minutes_before_end"]):
            end_ts = at(t.date(), cfg["work_end"])
            start_ts = end_ts - a["wind_down_minutes_before_end"] * 60
            if start_ts <= now < end_ts + 3600:
                mins = max(0, int((end_ts - now) // 60))
                self.fire("winddown", "Wind down", (
                    f"It's {clk}, {mins} min to the end of your workday ({cfg['work_end']}). "
                    f"Finish what you can. Then write tomorrow's first step: what, and when "
                    f"will you start? Saving it closes your day."), "prompt")

        # After-hours nudges
        nudge = a["after_hours_nudge_minutes"] * 60
        if after and nudge and self.after_cont >= nudge:
            mins = int(self.after_cont // 60)
            blk = int(self.after_start)
            if self.closed_at:
                closed = datetime.fromtimestamp(self.closed_at).strftime("%H:%M")
                text = (f"You closed the day at {closed} and have been active again for "
                        f"{mins} min. Urgent, or can it wait until {nxt}? "
                        f"If it can wait, jot it down, then: {plan}.")
            else:
                text = (f"It's {clk}, outside your workday ({cfg['work_start']}-"
                        f"{cfg['work_end']}). {mins} min active so far. "
                        f"Urgent, or can it wait until {nxt}?")
            self.fire(f"after1:{blk}", "After hours", text)
            if self.after_cont >= 3 * nudge:
                self.fire(f"after2:{blk}", "After hours", (
                    f"{mins} min of after-hours work. Switching off in the evening "
                    f"is part of how you recover. Stop here, or decide when you will."),
                    "important")

        # Continuous work without a break
        mins = a["continuous_work_minutes"]
        if mins and self.cont >= mins * 60:
            rounds = int(self.cont // (mins * 60))
            self.fire(f"continuous:{int(self.block_start)}:{rounds}", "Break time", self.pick([
                f"{int(self.cont // 60)} min without a break. Even a 5 to 10 minute "
                f"pause away from work lifts energy. Window, water or a short walk counts.",
                f"{int(self.cont // 60)} min straight. Step away for a few minutes. "
                f"Something unrelated to work recovers you best.",
            ]))

        # Weekly ladder
        wt, wc = g["weekly_target_hours"] * 3600, g["weekly_ceiling_hours"] * 3600
        wk = hours_text(self.tot_week)
        ws = cfg["week_starts_on"]
        days_left = sum(1 for d in cfg["work_days"] if (d - ws) % 7 > (t.weekday() - ws) % 7)
        if wt:
            if a["pace_pct"] / 100 * wt <= self.tot_week < wt:
                self.fire("week_pace", "Weekly check", (
                    f"{wk} active this week, target {hours_text(wt)}. "
                    f"{days_left} workday(s) left after today, so you can ease off."),
                    day=self.week_key)
            if self.tot_week >= wt:
                self.fire("week_target", "Weekly target reached", (
                    f"{wk} active this week, your target is {hours_text(wt)}. "
                    f"The rest of the week can be lighter."), "important", day=self.week_key)
        if wc and self.tot_week >= wc:
            self.fire("week_ceiling", "Weekly limit", (
                f"{wk} active this week, above your {hours_text(wc)} limit. WHO and ILO "
                f"link regular 55h+ weeks to higher stroke and heart-disease risk, and "
                f"tracked active time undercounts desk time."), "important", day=self.week_key)

        # Optional per-app and per-site limits
        for name, m in a.get("app_limits_minutes", {}).items():
            if self.by_app.get(name, 0) >= m * 60:
                self.fire(f"app:{name}", "App limit", f"{name}: {m} min limit reached today.")
        for name, m in a.get("domain_limits_minutes", {}).items():
            used = sum(v for d, v in self.by_domain.items() if matches(d, name))
            if used >= m * 60:
                self.fire(f"domain:{name}", "Site limit", f"{name}: {m} min limit reached today.")

    def fire(self, key, title, message, level="info", day=None):
        scope = day or self.day
        if (scope, key) in self.fired:
            return
        a = cfg["alerts"]
        now = time.time()
        if now < self.snooze_until:
            return
        # Returning before the alert is recorded postpones it: it fires on a later
        # tick if its condition still holds.
        if a["hold_during_zoom_meetings"]:
            if self.in_meeting:
                return
            gap = a["after_meeting_gap_minutes"] * 60
            if now - self.meeting_ended < 30 * 60 and now - max(self.last_alert, self.meeting_ended) < gap:
                return
        if level == "info":
            if now - self.last_alert < a["min_gap_minutes"] * 60:
                return
            if self.alerts_today >= a["max_per_day"]:
                return
        self.fired.add((scope, key))
        self.alerts_today += 1
        self.last_alert = now
        self.db.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?)",
                        (scope, key, now, message))
        log.info("ALERT [%s] %s", level, message)
        if level == "prompt" and a["dialogs_for_important"]:
            threading.Thread(target=self._prompt, args=(title, message), daemon=True).start()
        elif level in ("important", "prompt") and a["dialogs_for_important"]:
            threading.Thread(target=self._dialog, args=(title, message), daemon=True).start()
        else:
            notify(title, message)

    def _dialog(self, title, message):
        res = run_dialog(title, message, ["Snooze 30 min", "OK"], "OK")
        if res and res["button"].startswith("Snooze"):
            self.pending.append(("snooze",))

    def _prompt(self, title, message):
        res = run_dialog(title, message, ["Not yet", "Save & close day"],
                         "Save & close day", answer=True, give_up=300)
        if not res:
            return
        if res["button"].startswith("Save"):
            self.pending.append(("close", res["text"]))
        else:
            self.pending.append(("snooze15",))

    def safe_tick(self):
        try:
            self.tick()
        except Exception:
            log.exception("tick failed")


# ---------- dashboard API ----------
def week_progress(to_day, today):
    """Fraction of the week's work window that has elapsed up to the viewed day."""
    d = parse_day(to_day)
    mon = week_start_of(d)
    td = parse_day(today)
    days = cfg["work_days"]
    if not days:
        return 1.0
    ws, we = hm(cfg["work_start"]), hm(cfg["work_end"])
    total = 0.0
    for wd in days:
        day = mon + timedelta(days=(wd - cfg["week_starts_on"]) % 7)
        if day > d:
            continue
        if day < td:
            total += 1
        elif day == td and datetime.now().date() > td:
            total += 1                    # past midnight, before the logical day starts
        elif day == td:
            now = datetime.now()
            m = now.hour * 60 + now.minute
            total += min(1.0, max(0.0, (m - ws) / max(1, we - ws)))
    return total / len(days)


def stats(q):
    now = time.time()
    today = day_key(now)
    frm = q.get("from") or today
    to = q.get("to") or frm
    a, b = parse_day(frm), parse_day(to)
    if a > b:
        a, b = b, a
    if (b - a).days > 366:
        a = b - timedelta(days=366)
    n_days = (b - a).days + 1
    pa, pb = a - timedelta(days=n_days), a - timedelta(days=1)
    f_app, f_dom = q.get("app") or None, q.get("domain") or None
    scope = q.get("scope", "all")
    if scope not in ("all", "in", "after"):
        scope = "all"

    conn = connect()

    def rows(sql, *p):
        return conn.execute(sql, p).fetchall()

    def flt(extra_domain=False):
        sql, p = "", []
        if f_app:
            sql += " AND app=?"
            p.append(f_app)
        if f_dom:
            sql += " AND domain=?"
            p.append(f_dom)
        if scope == "in":
            sql += " AND after_hours=0"
        elif scope == "after":
            sql += " AND after_hours=1"
        if extra_domain:
            sql += " AND domain<>''"
        return sql, p

    fs, fp = flt()
    fds, fdp = flt(True)
    A, B = a.isoformat(), b.isoformat()
    PA, PB = pa.isoformat(), pb.isoformat()
    ca = min(a, b - timedelta(days=13))      # chart window: at least 14 days
    CA = ca.isoformat()

    total = rows(f"SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 "
                 f"AND day BETWEEN ? AND ?{fs}", A, B, *fp)[0][0]
    prev_total = rows(f"SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 "
                      f"AND day BETWEEN ? AND ?{fs}", PA, PB, *fp)[0][0]
    idle = rows("SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=0 "
                "AND day BETWEEN ? AND ?", A, B)[0][0]

    by_day = {}
    for day, aft, secs in rows(
            f"SELECT day, after_hours, SUM(secs) FROM usage WHERE active=1 "
            f"AND day BETWEEN ? AND ?{fs} GROUP BY day, after_hours", CA, B, *fp):
        by_day.setdefault(day, [0.0, 0.0])[aft] = secs
    after_all = {r[0]: (r[1], r[2]) for r in rows(
        "SELECT day, SUM(CASE WHEN after_hours=1 THEN secs ELSE 0 END), SUM(secs) "
        "FROM usage WHERE active=1 AND day BETWEEN ? AND ? GROUP BY day", A, B)}
    prev_after = rows(
        f"SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 AND after_hours=1 "
        f"AND day BETWEEN ? AND ?{fs}", PA, PB, *fp)[0][0]

    sess = {}
    for day, s, e in rows("SELECT day, start, end FROM sessions WHERE day BETWEEN ? AND ? "
                          "ORDER BY start", CA, B):
        sess.setdefault(day, []).append((s, e))

    daily, wrapped, workdays_with_activity, breaks, longest = [], 0, 0, [], 0.0
    work_days = set(cfg["work_days"])
    for i in range((b - ca).days + 1):
        d = ca + timedelta(days=i)
        k = d.isoformat()
        in_range = d >= a
        ins, aft = by_day.get(k, [0.0, 0.0])
        s_list = sess.get(k, [])
        daily.append({"day": k, "in_secs": ins, "after_secs": aft,
                      "workday": d.weekday() in work_days, "in_range": in_range})
        if not in_range:
            continue
        if s_list:
            breaks.append(len(s_list) - 1)
            longest = max(longest, max(e - s for s, e in s_list))
        if d.weekday() in work_days and k in after_all and after_all[k][1] > 0:
            workdays_with_activity += 1
            if after_all[k][0] < WRAPPED_MAX_AFTER_SECS:
                wrapped += 1

    heat = [[0.0] * 24 for _ in range(7)]
    for w, h, secs in rows(
            f"SELECT CAST(strftime('%w', day) AS INTEGER), hour, SUM(secs) FROM usage "
            f"WHERE active=1 AND day BETWEEN ? AND ?{fs} GROUP BY 1, 2", A, B, *fp):
        heat[(w + 6) % 7][h] = secs

    def breakdown(col, extra, params):
        cur = {r[0]: r[1] for r in rows(
            f"SELECT {col}, SUM(secs) s FROM usage WHERE active=1 AND day BETWEEN ? AND ?"
            f"{extra} GROUP BY {col} ORDER BY s DESC LIMIT 25", A, B, *params)}
        prev = {r[0]: r[1] for r in rows(
            f"SELECT {col}, SUM(secs) FROM usage WHERE active=1 AND day BETWEEN ? AND ?"
            f"{extra} GROUP BY {col}", PA, PB, *params)}
        return [{"name": k, "secs": v, "prev": prev.get(k, 0)} for k, v in cur.items()]

    app_opts = [r[0] for r in rows(
        "SELECT app FROM usage WHERE active=1 AND day BETWEEN ? AND ? "
        "GROUP BY app ORDER BY SUM(secs) DESC LIMIT 40", A, B)]
    dom_opts = [r[0] for r in rows(
        "SELECT domain FROM usage WHERE active=1 AND domain<>'' AND day BETWEEN ? AND ? "
        "GROUP BY domain ORDER BY SUM(secs) DESC LIMIT 40", A, B)]

    # Timeline for the last day of the range
    tl_day = b
    tl_key = tl_day.isoformat()
    tl_sessions = [[x, y] for x, y in sess.get(tl_key, [])]
    note_row = rows("SELECT closed_at, next_step FROM day_notes WHERE day=?", tl_key)
    prior = rows("SELECT day, next_step FROM day_notes WHERE day<? AND next_step<>'' "
                 "ORDER BY day DESC LIMIT 1", tl_key)
    day_start = logical_day_start(tl_day)
    day_end = logical_day_start(tl_day + timedelta(days=1))

    # Week pace (unfiltered, week containing the last day)
    mon = week_start_of(b)
    week_total = rows("SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 "
                      "AND day BETWEEN ? AND ?", mon.isoformat(), B)[0][0]  # as of range end
    frac = week_progress(tl_key, today)
    wt = cfg["goals"]["weekly_target_hours"] * 3600

    # Local-calendar boundaries: a logical day is 23 or 25 hours across DST changes.
    start_ts = logical_day_start(a)
    end_ts = logical_day_start(b + timedelta(days=1))
    alerts = [{"ts": r[0], "message": r[1]} for r in rows(
        "SELECT ts, message FROM alerts WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 60",
        start_ts, end_ts)]

    result = {
        "from": A, "to": B, "today": today, "n_days": n_days,
        "filters": {"app": f_app, "domain": f_dom, "scope": scope,
                    "apps": app_opts, "domains": dom_opts},
        "totals": {"active": total, "prev_active": prev_total, "idle": idle,
                   "after": sum(d["after_secs"] for d in daily if d["in_range"]),
                   "prev_after": prev_after},
        "daily": daily,
        "sessions": {"longest": longest,
                     "avg_breaks": (sum(breaks) / len(breaks)) if breaks else 0},
        "wrapped": {"ok": wrapped, "of": workdays_with_activity},
        "heat": heat,
        "by_app": breakdown("app", fs, fp),
        "by_domain": breakdown("domain", fds, fdp),
        "timeline": {
            "day": tl_key, "sessions": tl_sessions, "now": now, "is_today": tl_key == today,
            "is_workday": tl_day.weekday() in work_days,
            "ws": at(tl_day, cfg["work_start"]), "we": at(tl_day, cfg["work_end"]),
            "day_start": day_start, "day_end": day_end,
            "closed_at": note_row[0][0] if note_row else None,
            "note": note_row[0][1] if note_row else "",
        },
        "first_step": {"day": prior[0][0], "text": prior[0][1]} if prior else None,
        "week": {"from": mon.isoformat(), "active": week_total, "target": wt,
                 "ceiling": cfg["goals"]["weekly_ceiling_hours"] * 3600,
                 "expected": wt * frac, "frac": frac,
                 "projection": (week_total / frac) if frac >= 0.1 else None},
        "alerts": alerts,
        "live": live if B == today else None,
        "settings": {
            "goals": cfg["goals"], "work_days": cfg["work_days"],
            "week_starts_on": cfg["week_starts_on"],
            "day_starts_at_hour": cfg["day_starts_at_hour"],
            "work_start": cfg["work_start"], "work_end": cfg["work_end"],
            "shutdown_plan": cfg["shutdown_plan"],
            "idle_threshold_minutes": cfg["idle_threshold_minutes"],
            "alerts": {k: cfg["alerts"][k] for k in
                       ("enabled", "dialogs_for_important", "continuous_work_minutes",
                        "wind_down_minutes_before_end")},
        },
    }
    conn.close()
    return result


def icon_png(kind, name):
    """PNG icon for an app or website, cached; None when none can be found."""
    key = (kind, name)
    with icon_lock:
        hit = icon_cache.get(key)
        if hit and (hit[1] is not None or time.time() - hit[0] < ICON_MISS_RETRY_SECONDS):
            return hit[1]
        try:
            png = app_icon_png(name) if kind == "app" else favicon_png(name)
        except Exception:
            log.exception("icon lookup failed for %s %s", kind, name)
            png = None
        icon_cache[key] = (time.time(), png)
        return png


def app_bundle_path(name):
    if name in app_paths:
        return app_paths[name]
    ws = NSWorkspace.sharedWorkspace()
    for running in ws.runningApplications():
        if str(running.localizedName() or "") == name and running.bundleURL():
            return str(running.bundleURL().path())
    # Some apps prefix their display name with invisible direction marks (e.g. U+200E).
    visible = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    return ws.fullPathForApplication_(visible)


def app_icon_png(name):
    ws = NSWorkspace.sharedWorkspace()
    path = app_bundle_path(name)
    if not path:
        return None
    image = ws.iconForFile_(path)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, ICON_PX, ICON_PX, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        image.drawInRect_fromRect_operation_fraction_(
            ((0, 0), (ICON_PX, ICON_PX)), NSZeroRect, NSCompositingOperationSourceOver, 1.0)
    finally:
        NSGraphicsContext.restoreGraphicsState()
    return bytes(rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}))


def favicon_png(domain):
    # Prefix ranges ("url >= p AND url < p-with-last-char-bumped") use the page_url
    # index; LIKE would scan the whole table, which can be 100+ MB.
    prefixes = [f"{scheme}://{host}{domain}/" for scheme in ("https", "http")
                for host in ("", "www.")]
    where = " OR ".join("(m.page_url >= ? AND m.page_url < ?)" for _ in prefixes)
    params = [v for p in prefixes for v in (p, p[:-1] + "0")]
    for base in FAVICON_BASES:
        for db in sorted(base.glob("*/Favicons")):
            try:
                # immutable=1 reads without taking locks the browser holds.
                conn = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    # The site's usual icon is the one most of its pages map to; pages
                    # can swap in temporary ones (e.g. build-status badges).
                    row = conn.execute(
                        "SELECT image_data FROM favicon_bitmaps WHERE icon_id = ("
                        "  SELECT m.icon_id FROM icon_mapping m "
                        "  JOIN favicon_bitmaps b ON b.icon_id = m.icon_id "
                        f"  WHERE ({where}) AND b.image_data IS NOT NULL "
                        "  GROUP BY m.icon_id ORDER BY COUNT(DISTINCT m.page_url) DESC LIMIT 1) "
                        "AND image_data IS NOT NULL ORDER BY ABS(width - 32) LIMIT 1",
                        params).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
            if row and row[0]:
                return bytes(row[0])
    return None


def set_day_closed(note, reopen=False):
    conn = connect()
    day = day_key(time.time())
    if reopen:
        conn.execute("UPDATE day_notes SET closed_at=NULL WHERE day=?", (day,))
    else:
        conn.execute(
            "INSERT INTO day_notes(day, closed_at, next_step) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET closed_at=excluded.closed_at, "
            "next_step=excluded.next_step", (day, time.time(), note[:500]))
    conn.commit()
    conn.close()
    signals["reload_closed"] = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        port = cfg["dashboard_port"]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b'{"error":"forbidden"}')
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/stats":
            raw = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                for k in ("from", "to"):
                    if raw.get(k):
                        parse_day(raw[k])
                self._send(200, json.dumps(stats(raw)).encode())
            except ValueError:
                self._send(400, b'{"error":"bad request"}')
        elif url.path == "/api/icon":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            kind = "app" if q.get("app") else "domain" if q.get("domain") else None
            png = icon_png(kind, q[kind]) if kind else None
            if png:
                self._send(200, png, "image/png", "max-age=86400")
            else:
                self._send(404, b'{"error":"not found"}', cache="max-age=600")
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        # Custom header + JSON type force a CORS preflight, which we never answer,
        # so other websites cannot post to this local server.
        if (not self._host_ok() or self.headers.get("X-WT") != "1"
                or "application/json" not in self.headers.get("Content-Type", "")):
            return self._send(403, b'{"error":"forbidden"}')
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(size, 20_000)) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/config":
                apply_config_patch(body)
            elif path == "/api/close":
                set_day_closed(str(body.get("note", "")), bool(body.get("reopen")))
            else:
                return self._send(404, b'{"error":"not found"}')
            self._send(200, b'{"ok":true}')
        except (ValueError, TypeError, KeyError):
            self._send(400, b'{"error":"bad request"}')


def serve():
    ThreadingHTTPServer(("127.0.0.1", cfg["dashboard_port"]), Handler).serve_forever()


def main():
    global cfg
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "worktracker.log", maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    cfg = load_config()
    tracker = Tracker()
    threading.Thread(target=serve, daemon=True).start()
    log.info("Started. Dashboard: http://127.0.0.1:%s", cfg["dashboard_port"])

    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    interval = cfg["sample_seconds"]
    timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        interval, True, lambda _t: tracker.safe_tick())
    timer.setTolerance_(interval * 0.3)  # lets macOS coalesce wakeups
    NSRunLoop.currentRunLoop().run()


if __name__ == "__main__":
    main()
