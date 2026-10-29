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
import queue
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSButton,
    NSCompositingOperationSourceOver,
    NSDeviceRGBColorSpace,
    NSFont,
    NSGraphicsContext,
    NSPanel,
    NSScreen,
    NSSound,
    NSStatusWindowLevel,
    NSTextField,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskHUDWindow,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
    NSWorkspace,
    NSZeroRect,
)
from Foundation import NSAppleScript, NSObject, NSTimer
import objc
from PyObjCTools import AppHelper
from Quartz import (
    CGEventSourceSecondsSinceLastEventType,
    CGSessionCopyCurrentDictionary,
    kCGAnyInputEventType,
    kCGEventSourceStateHIDSystemState,
)

CODE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("WORKTRACKER_HOME", CODE_DIR / "data"))
DB_PATH = APP_DIR / "tracker.db"
CONFIG_PATH = APP_DIR / "config.json"
DASHBOARD_PATH = CODE_DIR / "dashboard.html"
STATIC_DIR = CODE_DIR / "static"
# URL path -> content type. The manifest sits at the root so the installed app's scope is "/".
STATIC_FILES = {
    "/manifest.webmanifest": "application/manifest+json",
    "/static/icon.svg": "image/svg+xml",
    "/static/icon-180.png": "image/png",
    "/static/icon-192.png": "image/png",
    "/static/icon-512.png": "image/png",
}
LOG_DIR = APP_DIR / "logs"
# The dashboard can show a generated sample dataset instead of your own. Only what the
# dashboard and chat read and write switches; the sampler always records into APP_DIR.
DATASETS = {"real": APP_DIR, "mock": CODE_DIR / "data-mock"}
MOCK_GENERATOR = CODE_DIR / "tools" / "make_mock_data.py"

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
    # Durations offered by the dashboard's personal-time button. Personal time
    # is not recorded at all and holds alerts until it ends.
    "personal_time_minutes": [30, 60],
    # The day timeline draws work stretches separated by a shorter break as one block.
    # Display only; breaks are still recorded and counted.
    "timeline_merge_minutes": 10,
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
        # How alerts appear, one of ALERT_STYLES. "important" covers limits,
        # personal-time end and the wind-down prompt.
        "routine_style": "banner",
        "important_style": "overlay",
        "max_per_day": 8,                # cap on routine alerts (limits always show)
        "min_gap_minutes": 10,           # spacing between routine alerts
        "pace_pct": 80,                  # heads-up at this % of a target
        "wind_down_minutes_before_end": 30,
        "after_hours_nudge_minutes": 15,
        # On work days, starting early from this time until work_start raises no
        # after-hours alerts (the time still counts as after hours). "" = always alert.
        "early_start_from": "07:00",
        "continuous_work_minutes": 90,
        "break_minutes_to_reset": 5,
        "app_limits_minutes": {},        # e.g. {"Slack": 90}
        "domain_limits_minutes": {},     # e.g. {"youtube.com": 30}
        "hold_during_zoom_meetings": True,
        "after_meeting_gap_minutes": 3,  # spacing for alerts held during a meeting
    },
}

ALERT_STYLES = (
    "banner",    # macOS notification; may be hidden by Focus or notification settings
    "dialog",    # system dialog with Snooze/OK; closes itself after 2 minutes
    "overlay",   # floating card on top of every window and space; stays until closed
)
OVERLAY_SIZE = (440, 170)
WRAPPED_MAX_AFTER_SECS = 15 * 60     # "wrapped on time" = under 15 min after hours
TIMELINE_TOP_PER_HOUR = 4
TODO_MAX_CHARS = 1000                # matches TODO_MAX in dashboard.html
TODO_BULLET = re.compile(r"^\s*(?:[-*•]+|\[[ xX]?\]|\d+[.)])\s+")
ICON_PX = 64
ICON_MISS_RETRY_SECONDS = 600
ICON_CACHE_MAX = 500
ICON_NAME_MAX = 200
# Chromium-family browsers keep a per-profile favicon cache in <base>/<profile>/Favicons.
FAVICON_BASES = [Path.home() / "Library/Application Support" / p for p in (
    "Google/Chrome", "BraveSoftware/Brave-Browser", "Microsoft Edge",
    "Arc/User Data", "Chromium", "Vivaldi")]

# Safari and Arc have no scriptable way to tell a private window apart, so their
# private browsing is recorded like any other window.
BROWSER_SCRIPTS = {
    "Safari": 'tell application "Safari" to return URL of current tab of front window',
    "Arc": 'tell application "Arc" to return URL of active tab of front window',
}
for _b in ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium", "Vivaldi"):
    # Incognito windows return "", so the site is never recorded.
    BROWSER_SCRIPTS[_b] = (f'tell application "{_b}"\n'
                           f'if mode of front window is "incognito" then return ""\n'
                           f'return URL of active tab of front window\n'
                           f'end tell')

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
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, for_day TEXT NOT NULL, text TEXT NOT NULL,
  created_at REAL NOT NULL, done_at REAL, done_day TEXT, dropped INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS todos_open ON todos(done_at, for_day);
CREATE TABLE IF NOT EXISTS alerts(
  day TEXT, key TEXT, ts REAL, message TEXT, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS meetings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL, end REAL, source TEXT);
CREATE TABLE IF NOT EXISTS personal_time(
  id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL, planned_end REAL, ended_at REAL);
"""

log = logging.getLogger("worktracker")
cfg = json.loads(json.dumps(DEFAULTS))
cfg_mtime = 0.0
live = {"app": "", "domain": "", "idle": 0, "active": False}
tracker = None                       # the running Tracker, for HTTP handlers
signals = {}
app_paths = {}                       # app name -> bundle path, learned while sampling
icon_cache = OrderedDict()           # (kind, name) -> (fetched_at, png bytes or None), LRU
config_lock = threading.Lock()       # serialises config.json read-merge-write
bound_port = None                    # port the dashboard actually listens on
dataset = "real"                     # which of DATASETS the dashboard shows; resets on restart
dataset_lock = threading.Lock()
icon_lock = threading.Lock()


# ---------- config ----------
def load_config(previous=None):
    """Defaults merged with config.json. An invalid file keeps the previous settings
    (or the defaults at startup) and logs why, so a typo can't stop tracking."""
    global cfg_mtime
    merged = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            if not isinstance(user, dict):
                raise ValueError("config.json must be a JSON object")
            for k, v in user.items():
                if isinstance(merged.get(k), dict):
                    if not isinstance(v, dict):
                        raise ValueError(f"{k} must be an object")
                    merged[k].update(v)
                else:
                    merged[k] = v
            check_config(merged)
        except Exception as e:
            log.error("config.json is invalid (%s); %s", e,
                      "keeping the previous settings" if previous else "using the defaults")
            merged = previous or json.loads(json.dumps(DEFAULTS))
    else:
        # First run: write the defaults as a starting point to edit.
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
    cfg_mtime = CONFIG_PATH.stat().st_mtime
    return merged


def _is_num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def check_config(c):
    """Raise ValueError when merged settings would break sampling, alerts or the API."""
    def need(ok, what):
        if not ok:
            raise ValueError(what)
    need(_is_num(c["sample_seconds"], 2, 120), "sample_seconds must be 2-120")
    need(_is_num(c["idle_threshold_minutes"], 1, 60), "idle_threshold_minutes must be 1-60")
    need(_is_num(c["timeline_merge_minutes"], 0, 120), "timeline_merge_minutes must be 0-120")
    need(_is_num(c["dashboard_port"], 1024, 65535) and isinstance(c["dashboard_port"], int),
         "dashboard_port must be a port number")
    need(isinstance(c["day_starts_at_hour"], int) and _is_num(c["day_starts_at_hour"], 0, 12),
         "day_starts_at_hour must be 0-12")
    need(isinstance(c["week_starts_on"], int) and _is_num(c["week_starts_on"], 0, 6),
         "week_starts_on must be 0-6")
    need(isinstance(c["work_days"], list) and all(isinstance(d, int) and _is_num(d, 0, 6)
                                                  for d in c["work_days"]), "work_days must be 0-6")
    for k in ("work_start", "work_end"):
        need(isinstance(c[k], str) and HHMM.match(c[k]), f"{k} must be HH:MM")
    need(hm(c["work_start"]) < hm(c["work_end"]), "work_end must be after work_start")
    need(c["day_starts_at_hour"] * 60 <= hm(c["work_start"]),
         "day_starts_at_hour must not be after work_start")
    need(isinstance(c["shutdown_plan"], str), "shutdown_plan must be text")
    for k in ("idle_exempt_apps", "idle_exempt_domains"):
        need(isinstance(c[k], list) and all(isinstance(x, str) for x in c[k]), f"{k} must be a list of names")
    need(isinstance(c["personal_time_minutes"], list) and c["personal_time_minutes"]
         and all(_is_num(m, 1, 480) for m in c["personal_time_minutes"]),
         "personal_time_minutes must list 1-480")
    for k, v in c["goals"].items():
        need(_is_num(v, 0, 168), f"goals.{k} must be 0-168")
    a = c["alerts"]
    need(isinstance(a["enabled"], bool), "alerts.enabled must be true or false")
    need(isinstance(a["hold_during_zoom_meetings"], bool), "alerts.hold_during_zoom_meetings must be true or false")
    for k in ("routine_style", "important_style"):
        need(a[k] in ALERT_STYLES, f"alerts.{k} must be one of {', '.join(ALERT_STYLES)}")
    need(a["early_start_from"] == "" or (isinstance(a["early_start_from"], str) and HHMM.match(a["early_start_from"])),
         "alerts.early_start_from must be HH:MM or empty")
    for k in ("max_per_day", "min_gap_minutes", "pace_pct", "wind_down_minutes_before_end",
              "after_hours_nudge_minutes", "continuous_work_minutes", "break_minutes_to_reset",
              "after_meeting_gap_minutes"):
        need(_is_num(a[k], 0, 1440), f"alerts.{k} must be a number")
    for k in ("app_limits_minutes", "domain_limits_minutes"):
        need(isinstance(a[k], dict) and all(_is_num(v, 0, 1440) for v in a[k].values()),
             f"alerts.{k} must map names to minutes")


HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError("not a number")
    v = float(v)
    if not lo <= v <= hi:
        raise ValueError("out of range")
    return v


def _obj(v):
    if not isinstance(v, dict):
        raise ValueError("expected an object")
    return v


def apply_config_patch(patch):
    """Validate a settings patch from the dashboard and merge it into config.json."""
    global cfg
    clean = {}
    g = _obj(patch.get("goals", {}))
    for k in ("daily_target_hours", "daily_ceiling_hours",
              "weekly_target_hours", "weekly_ceiling_hours"):
        if k in g:
            clean.setdefault("goals", {})[k] = _num(g[k], 0, 168)
    if "work_days" in patch:
        if not isinstance(patch["work_days"], list):
            raise ValueError("bad work_days")
        days = sorted({int(_num(d, 0, 6)) for d in patch["work_days"]})
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
    if "timeline_merge_minutes" in patch:
        clean["timeline_merge_minutes"] = int(_num(patch["timeline_merge_minutes"], 0, 120))
    start_hour = clean.get("day_starts_at_hour", cfg["day_starts_at_hour"])
    if start_hour * 60 > hm(clean.get("work_start", cfg["work_start"])):
        raise ValueError("the day must start before work starts")
    a = _obj(patch.get("alerts", {}))
    if "enabled" in a:
        if not isinstance(a["enabled"], bool):
            raise ValueError("bad enabled")
        clean.setdefault("alerts", {})["enabled"] = a["enabled"]
    for k in ("routine_style", "important_style"):
        if k in a:
            if a[k] not in ALERT_STYLES:
                raise ValueError("bad alert style")
            clean.setdefault("alerts", {})[k] = a[k]
    if "early_start_from" in a:
        if a["early_start_from"] != "" and not HHMM.match(str(a["early_start_from"])):
            raise ValueError("bad early_start_from")
        clean.setdefault("alerts", {})["early_start_from"] = str(a["early_start_from"])
    if "continuous_work_minutes" in a:
        clean.setdefault("alerts", {})["continuous_work_minutes"] = int(
            _num(a["continuous_work_minutes"], 10, 480))
    with config_lock:
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
        cfg = load_config(cfg)


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


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def view_home():
    return DATASETS[dataset]


def connect_view():
    """A connection to the dataset the dashboard is showing."""
    conn = connect(view_home() / "tracker.db")
    init_db(conn)
    return conn


def select_dataset(name):
    """Point the dashboard and chat at a dataset, generating the mock one on first use."""
    global dataset
    if name not in DATASETS:
        raise ValueError("unknown dataset")
    with dataset_lock:
        if name == "mock" and not (DATASETS["mock"] / "tracker.db").exists():
            log.info("Generating mock data in %s", DATASETS["mock"])
            done = subprocess.run([sys.executable, str(MOCK_GENERATOR), "--out", str(DATASETS["mock"])],
                                  capture_output=True, text=True, timeout=120)
            if done.returncode:
                log.error("Mock data generation failed: %s", done.stderr.strip()[-500:])
                raise ValueError("mock data generation failed")
        dataset = name
    log.info("Dashboard now shows %s data", name)


def init_db(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage)")]
    legacy = bool(cols) and "after_hours" not in cols
    had_todos = conn.execute("SELECT 1 FROM sqlite_master WHERE name='todos'").fetchone()
    if legacy:
        conn.execute("ALTER TABLE usage RENAME TO usage_v1")
    conn.executescript(SCHEMA)
    if legacy:
        conn.execute("INSERT INTO usage SELECT day,hour,app,domain,active,0,secs FROM usage_v1")
        conn.execute("DROP TABLE usage_v1")
    if not had_todos:
        migrate_notes_to_todos(conn)
    elif "dropped" not in [r[1] for r in conn.execute("PRAGMA table_info(todos)")]:
        conn.execute("ALTER TABLE todos ADD COLUMN dropped INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def todo_lines(text):
    """Split pasted text into to-do items: one per line, bullets removed."""
    items = (TODO_BULLET.sub("", line).strip() for line in str(text).splitlines())
    return [i[:TODO_MAX_CHARS] for i in items if i]


def todo_text(text):
    """One to-do that may span several lines; blank lines at the ends are dropped."""
    lines = [line.rstrip() for line in str(text).strip().splitlines()]
    return "\n".join(lines)[:TODO_MAX_CHARS]


def migrate_notes_to_todos(conn):
    """Turn the current plan from the old free-text notes into open to-dos: the latest
    note written before today becomes today's list, and notes written today or later
    become the following day's. Older notes stay in day_notes as history."""
    today = day_key(time.time())
    plans = conn.execute(
        "SELECT day, next_step FROM day_notes WHERE COALESCE(next_step, '') <> '' AND day >= "
        "(SELECT COALESCE(MAX(day), '') FROM day_notes WHERE day < ? AND COALESCE(next_step, '') <> '') "
        "ORDER BY day", (today,)).fetchall()
    for day, text in plans:
        for_day = today if day < today else (parse_day(day) + timedelta(days=1)).isoformat()
        conn.executemany("INSERT INTO todos(for_day, text, created_at) VALUES(?,?,?)",
                         [(for_day, item, time.time()) for item in todo_lines(text)])


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


def screen_locked():
    session = CGSessionCopyCurrentDictionary()
    return bool(session and session.get("CGSSessionScreenIsLocked"))


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


class OverlayActions(NSObject):
    """Button target and window delegate for alert overlays."""

    def snooze_(self, sender):
        if tracker:
            tracker.pending.append(("snooze",))
        sender.window().close()

    def windowWillClose_(self, notification):
        window = notification.object()
        if window in overlays:
            overlays.remove(window)


overlays = []                        # open overlay windows; AppKit does not retain them
overlay_actions = None


def show_overlay(title, message):
    """Floating card at the top right of the main screen, above every window and
    space, that stays until closed. Must run on the main thread."""
    global overlay_actions
    if overlay_actions is None:
        overlay_actions = OverlayActions.alloc().init()
    w, h = OVERLAY_SIZE
    screen = NSScreen.mainScreen().visibleFrame()
    slot = len(overlays) % 4
    x = screen.origin.x + screen.size.width - w - 20
    y = screen.origin.y + screen.size.height - (h + 40) * (slot + 1)
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskHUDWindow
             | NSWindowStyleMaskUtilityWindow | NSWindowStyleMaskNonactivatingPanel)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (w, h)), style, NSBackingStoreBuffered, False)
    panel.setTitle_(title)
    panel.setLevel_(NSStatusWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setReleasedWhenClosed_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
    panel.setDelegate_(overlay_actions)
    text = NSTextField.wrappingLabelWithString_(message)
    text.setFont_(NSFont.systemFontOfSize_(15))
    text.setFrame_(((18, 52), (w - 36, h - 64)))
    button = NSButton.buttonWithTitle_target_action_("Snooze 30 min", overlay_actions, "snooze:")
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setFrame_(((w - 150, 12), (132, 30)))
    panel.contentView().addSubview_(text)
    panel.contentView().addSubview_(button)
    overlays.append(panel)
    panel.orderFrontRegardless()
    NSSound.soundNamed_("Glass").play()


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
        self.meeting_id = None       # meetings row being extended while a meeting runs
        self.recent = []             # recent active credits that could be idle time
        self.last_input = time.time()
        self.pending = []            # results from dialog threads
        self.personal_notice = None  # end-of-personal-time alert waiting to be shown
        self.load_personal()
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

    def load_personal(self):
        self.personal = self.db.execute(
            "SELECT id, start, planned_end FROM personal_time WHERE ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()

    def update_personal(self, now):
        if signals.pop("personal", None):
            self.load_personal()
        p = self.personal
        if p and now >= p[2]:
            self.db.execute("UPDATE personal_time SET ended_at=? WHERE id=?", (p[2], p[0]))
            self.db.commit()
            self.personal = None
            ended = datetime.fromtimestamp(p[2]).strftime("%H:%M")
            self.personal_notice = (
                f"personal_end:{p[0]}", "Personal time is over",
                f"Your {round((p[2] - p[1]) / 60)} min of personal time ended at {ended}. "
                f"Tracking is back on.", "important")
        if self.personal_notice:
            self.fire(*self.personal_notice)
            if (self.day, self.personal_notice[0]) in self.fired:
                self.personal_notice = None

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

    def close_day(self, items):
        now = time.time()
        add_todos(self.db, items.replace(";", "\n"), "tomorrow")
        self.db.execute(
            "INSERT INTO day_notes(day, closed_at) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET closed_at=excluded.closed_at", (self.day, now))
        self.db.commit()
        self.closed_at = now
        log.info("Day closed. Added for tomorrow: %s", items or "(nothing)")

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
            new = load_config(cfg)
            if new is not cfg:
                cfg = new
                log.info("Config reloaded")
        if signals.pop("reload_closed", None):
            self.reload_closed()
        if (cfg["week_starts_on"], cfg["day_starts_at_hour"]) != self.calendar_cfg:
            self.load_day(day_key(time.time()))
        self.handle_pending()

        a = cfg["alerts"]
        now = time.time()
        gap = now - self.last if self.last else 0.0
        prev_tick, self.last = self.last, now
        dt = gap if 0 < gap <= cfg["sample_seconds"] * 3 else 0.0   # skip sleep gaps
        if gap and not dt and self.meeting_id is not None:
            # Attendance wasn't observed during the gap; end the meeting where it was last seen.
            self.db.execute("UPDATE meetings SET end=? WHERE id=?", (prev_tick, self.meeting_id))
            self.db.commit()
            self.meeting_id = None
        if gap >= a["break_minutes_to_reset"] * 60:
            self.cont = 0.0
            self.after_cont = 0.0

        day = day_key(now)
        if day != self.day:
            self.load_day(day)
        self.update_personal(now)
        if self.personal:
            # Nothing is sampled or stored; the time counts as a break.
            self.recent = []
            self.cont = self.after_cont = 0.0
            self.sess_id = None
            self.in_meeting = False
            self.record_meeting(False, now)
            live.update(app="", domain="", idle=0, active=False, meeting=False)
            return

        idle, app, domain = sample()
        meeting = in_zoom_meeting()
        if self.in_meeting and not meeting:
            self.meeting_ended = now
        self.in_meeting = meeting
        self.record_meeting(meeting, now)
        # A locked screen is away straight away, even mid-meeting; the idle tail since
        # the last input is trimmed below like any confirmed break.
        locked = screen_locked()
        exempt = not locked and (meeting or app in cfg["idle_exempt_apps"]
                                 or any(matches(domain, r) for r in cfg["idle_exempt_domains"] if domain))
        active = not locked and (idle < cfg["idle_threshold_minutes"] * 60 or exempt)
        live.update(app=app, domain=domain, idle=int(idle), active=active, meeting=meeting,
                    locked=locked)

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

    def record_meeting(self, meeting, now):
        if meeting and self.meeting_id is None:
            self.meeting_id = self.db.execute(
                "INSERT INTO meetings(start, end, source) VALUES(?, ?, 'zoom')", (now, now)).lastrowid
        elif self.meeting_id is not None:
            self.db.execute("UPDATE meetings SET end=? WHERE id=?", (now, self.meeting_id))
            if not meeting:
                self.meeting_id = None
        else:
            return
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

    def early_start(self, now):
        """Working early on a work day, between early_start_from and work_start."""
        start = cfg["alerts"]["early_start_from"]
        t = datetime.fromtimestamp(now)
        minutes = t.hour * 60 + t.minute
        return bool(start) and not self.closed_at and t.weekday() in cfg["work_days"] \
            and hm(start) <= minutes < hm(cfg["work_start"])

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
                f"add tomorrow's to-dos."), "important")
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
                    f"Finish what you can. Then add tomorrow's to-dos below, separated by "
                    f"semicolons. Saving them closes your day."), "prompt")

        # After-hours nudges
        nudge = a["after_hours_nudge_minutes"] * 60
        if after and nudge and self.after_cont >= nudge and not self.early_start(now):
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
        style = a["important_style"] if level in ("important", "prompt") else a["routine_style"]
        if level == "prompt" and style != "banner":
            # The wind-down prompt takes text, which only the system dialog offers.
            threading.Thread(target=self._prompt, args=(title, message), daemon=True).start()
        else:
            self.show(style, title, message)

    def show(self, style, title, message):
        """Present an alert in the given style. Must run on the main thread."""
        if style == "overlay":
            show_overlay(title, message)
        elif style == "dialog":
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

    conn = connect_view()

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
    # Chart window: whole weeks, from the week before the range end through the end of
    # its week, so bars line up by weekday. Days outside the range still show their data
    # (dimmed); only days that haven't happened yet are empty.
    ca = min(week_start_of(a), week_start_of(b) - timedelta(days=7))
    ce = week_start_of(b) + timedelta(days=6)
    CA, CE = ca.isoformat(), min(ce, parse_day(today)).isoformat()

    total = rows(f"SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 "
                 f"AND day BETWEEN ? AND ?{fs}", A, B, *fp)[0][0]
    prev_total = rows(f"SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=1 "
                      f"AND day BETWEEN ? AND ?{fs}", PA, PB, *fp)[0][0]
    idle = rows("SELECT COALESCE(SUM(secs),0) FROM usage WHERE active=0 "
                "AND day BETWEEN ? AND ?", A, B)[0][0]

    by_day = {}
    for day, aft, secs in rows(
            f"SELECT day, after_hours, SUM(secs) FROM usage WHERE active=1 "
            f"AND day BETWEEN ? AND ?{fs} GROUP BY day, after_hours", CA, CE, *fp):
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
    for i in range((ce - ca).days + 1):
        d = ca + timedelta(days=i)
        k = d.isoformat()
        in_range = a <= d <= b
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
            f"{extra} GROUP BY {col} ORDER BY s DESC LIMIT 1000", A, B, *params)}
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
    note_row = rows("SELECT closed_at FROM day_notes WHERE day=?", tl_key)
    # Top activities per clock hour, for the timeline tooltip; a site stands in for its browser.
    tl_hours = {}
    for hour, name, secs in rows(
            "SELECT hour, CASE WHEN domain<>'' THEN domain ELSE app END, SUM(secs) FROM usage "
            "WHERE day=? AND active=1 GROUP BY 1, 2 HAVING SUM(secs) >= 30 ORDER BY 3 DESC", tl_key):
        top = tl_hours.setdefault(hour, [])
        if len(top) < TIMELINE_TOP_PER_HOUR:
            top.append([name, secs])
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
            "hours": tl_hours,
            "meetings": [[s0, e0] for s0, e0 in rows(
                "SELECT start, end FROM meetings WHERE start < ? AND end > ? ORDER BY start",
                day_end, day_start)],
            "personal": [[s0, e0] for s0, e0 in rows(
                "SELECT start, COALESCE(ended_at, MIN(planned_end, ?)) FROM personal_time "
                "WHERE start < ? AND COALESCE(ended_at, planned_end) > ?",
                now, day_end, day_start)],
        },
        "todos": todo_lists(conn, today),
        "done_on_day": [{"id": i, "text": x, "done_at": d, "dropped": bool(dr)} for i, x, d, dr in rows(
            "SELECT id, text, done_at, dropped FROM todos WHERE done_day=? ORDER BY dropped, done_at", tl_key)],
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
            "timeline_merge_minutes": cfg["timeline_merge_minutes"],
            "alerts": {k: cfg["alerts"][k] for k in
                       ("enabled", "routine_style", "important_style",
                        "continuous_work_minutes", "wind_down_minutes_before_end",
                        "early_start_from")},
            "personal_time_minutes": cfg["personal_time_minutes"],
        },
        "personal": personal_status(conn, now, today),
        "dataset": dataset,
    }
    conn.close()
    return result


def resolve_for_day(when, today=None):
    """'today', 'tomorrow' or YYYY-MM-DD -> the logical day an item is planned for."""
    today = today or day_key(time.time())
    if when in (None, "today"):
        return today
    if when == "tomorrow":
        return (parse_day(today) + timedelta(days=1)).isoformat()
    return parse_day(str(when)).isoformat()


def add_todos(conn, text, when, split=True):
    """Add one item per line of text, or the whole text as one multi-line item when
    split is false; returns the new ids. Caller commits."""
    for_day = resolve_for_day(when)
    items = todo_lines(text) if split else [t for t in [todo_text(text)] if t]
    return [conn.execute("INSERT INTO todos(for_day, text, created_at) VALUES(?,?,?)",
                         (for_day, item, time.time())).lastrowid for item in items]


def todo_lists(conn, today):
    """Today: items planned for today or earlier that are open (unfinished ones carry
    over) or were closed today. Tomorrow: items planned for later days that are open or
    were closed today. A closed item stays in the list it was planned for, struck
    through and last; dropped means closed without being done."""
    closed_last = "ORDER BY done_at IS NOT NULL, dropped, done_at, for_day, id"
    return {
        "today": todo_rows(conn, f"for_day <= ? AND (done_at IS NULL OR done_day = ?) {closed_last}",
                           (today, today)),
        "tomorrow": todo_rows(conn, f"for_day > ? AND (done_at IS NULL OR done_day = ?) {closed_last}",
                              (today, today)),
    }


def todo_rows(conn, where, params):
    keys = ("id", "text", "for_day", "done_at", "dropped")
    return [dict(zip(keys, r)) for r in conn.execute(
        f"SELECT {', '.join(keys)} FROM todos WHERE {where}", params).fetchall()]


def change_todo(action, todo_id=None, text=None, when=None, split=False):
    """Apply one to-do action and return the current lists. Adding with split=True
    makes one item per line (a pasted list); otherwise the text is one item."""
    conn = connect_view()
    try:
        today = day_key(time.time())
        if action == "add":
            ids = add_todos(conn, text or "", when, split)
            if not ids:
                raise ValueError("empty to-do")
            log.info("To-do added for %s: %s", resolve_for_day(when, today), text)
        else:
            todo_id = int(todo_id)
            row = conn.execute("SELECT text FROM todos WHERE id=?", (todo_id,)).fetchone()
            if not row:
                raise ValueError("no such to-do")
            if action in ("done", "drop"):
                conn.execute("UPDATE todos SET done_at=?, done_day=?, dropped=? WHERE id=?",
                             (time.time(), today, int(action == "drop"), todo_id))
            elif action == "undo":
                conn.execute("UPDATE todos SET done_at=NULL, done_day=NULL, dropped=0 WHERE id=?", (todo_id,))
            elif action == "edit":
                new_text = todo_text(text or "")
                if not new_text:
                    raise ValueError("empty to-do")
                conn.execute("UPDATE todos SET text=? WHERE id=?", (new_text, todo_id))
            elif action == "move":
                conn.execute("UPDATE todos SET for_day=? WHERE id=?",
                             (resolve_for_day(when, today), todo_id))
            elif action == "delete":
                conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
            else:
                raise ValueError("unknown action")
            log.info("To-do %s %s: %s", todo_id, action, text if action == "edit" else row[0])
        conn.commit()
        return todo_lists(conn, today)
    finally:
        conn.close()


def personal_status(conn, now, today):
    """The running personal-time block, if any, and today's personal time so far."""
    start = logical_day_start(parse_day(today))
    rows = conn.execute(
        "SELECT MAX(start, ?), MIN(COALESCE(ended_at, planned_end), ?) FROM personal_time "
        "WHERE start < ? AND COALESCE(ended_at, planned_end) > ?", (start, now, now, start)).fetchall()
    running = conn.execute(
        "SELECT start, planned_end FROM personal_time WHERE ended_at IS NULL AND planned_end > ? "
        "ORDER BY id DESC LIMIT 1", (now,)).fetchone()
    return {"today_secs": sum(max(0.0, e - s) for s, e in rows),
            "running": {"start": running[0], "end": running[1]} if running else None}


def set_personal_time(minutes):
    """Start a personal-time block (ending any running one), or end it with 0 minutes."""
    now = time.time()
    conn = connect_view()
    conn.execute("UPDATE personal_time SET ended_at=MIN(planned_end, ?) WHERE ended_at IS NULL", (now,))
    if minutes:
        conn.execute("INSERT INTO personal_time(start, planned_end) VALUES(?,?)",
                     (now, now + minutes * 60))
    conn.commit()
    conn.close()
    if dataset == "real":
        signals["personal"] = True


def icon_png(kind, name):
    """PNG icon for an app or website, cached; None when none can be found."""
    key = (kind, name)
    with icon_lock:
        hit = icon_cache.get(key)
        if hit and (hit[1] is not None or time.time() - hit[0] < ICON_MISS_RETRY_SECONDS):
            icon_cache.move_to_end(key)
            return hit[1]
        try:
            # HTTP threads have no Cocoa autorelease pool of their own.
            with objc.autorelease_pool():
                png = app_icon_png(name) if kind == "app" else favicon_png(name)
        except Exception:
            log.exception("icon lookup failed for %s %s", kind, name)
            png = None
        icon_cache[key] = (time.time(), png)
        icon_cache.move_to_end(key)
        while len(icon_cache) > ICON_CACHE_MAX:
            icon_cache.popitem(last=False)
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


def set_day_closed(reopen=False):
    """Close today, so later activity counts as after hours, or reopen it."""
    conn = connect_view()
    day = day_key(time.time())
    if reopen:
        conn.execute("UPDATE day_notes SET closed_at=NULL WHERE day=?", (day,))
    else:
        conn.execute("INSERT INTO day_notes(day, closed_at) VALUES(?,?) "
                     "ON CONFLICT(day) DO UPDATE SET closed_at=excluded.closed_at", (day, time.time()))
    conn.commit()
    conn.close()
    log.info("Day %s %s", day, "reopened" if reopen else "closed")
    if dataset == "real":
        signals["reload_closed"] = True
    return day


# The chat panel runs the user's own `claude` CLI in this folder with the user's login.
# --restricted ignores user, project and local settings (so saved permissions and hooks
# don't apply) and confines file reads to this folder. It also skips CLAUDE.md, so
# chat_prompt() includes it. Bash may only run the two helpers: tools/query.py
# (read-only SQL) and tools/todo.py (to-dos, close/reopen the day). Anything else is denied rather than
# prompted, since nobody can answer a prompt here.
CHAT_TOOLS = "Bash,Read,Grep,Glob"
CHAT_ALLOWED = "Bash(python3 tools/query.py *),Bash(python3 tools/todo.py *),Read,Grep,Glob"
CHAT_TIMEOUT_SECONDS = 300
CHAT_MAX_RUNNING = 2
CHAT_PING_SECONDS = 5                # lets a closed browser tab be noticed while Claude is quiet
chat_slots = threading.BoundedSemaphore(CHAT_MAX_RUNNING)
CHAT_PATH = os.pathsep.join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                             "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin")])


def chat_prompt():
    now = datetime.now()
    today = day_key(now.timestamp())
    week = week_start_of(parse_day(today))
    return (f"You are answering in a small chat panel on the worktracker dashboard. "
            f"It is {now:%A %Y-%m-%d %H:%M}; the current logical day is {today}, and this week "
            f"runs {week:%a %Y-%m-%d} to {week + timedelta(days=6):%a %Y-%m-%d}. "
            "The panel is narrow: keep answers short, and prefer compact tables over long lists. "
            "Read data with tools/query.py and manage to-dos or close/reopen the day with "
            "tools/todo.py (see CLAUDE.md); you cannot change anything else. For other changes, "
            "tell the user how to make them in the dashboard's Goals & settings.\n\n"
            + (CODE_DIR / "CLAUDE.md").read_text())


def drain(stream, sink, at_end=None):
    for line in stream:
        sink(line)
    if at_end:
        at_end()


def run_chat(message, session_id, write):
    """Stream one chat turn as NDJSON events: session, text, tool, block, ping, done, error."""
    claude = shutil.which("claude", path=CHAT_PATH)
    if not claude:
        write({"t": "error", "d": "The claude command was not found. Install Claude Code and "
                                  "log in once in a terminal, then try again."})
        return
    if not chat_slots.acquire(blocking=False):
        write({"t": "error", "d": "Another answer is still running. Try again when it finishes."})
        return
    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--restricted", "--tools", CHAT_TOOLS,
           "--allowedTools", CHAT_ALLOWED, "--permission-mode", "dontAsk",
           "--strict-mcp-config", "--append-system-prompt", chat_prompt()]
    if session_id:
        cmd += ["--resume", session_id]
    # A new session makes the CLI and anything it starts one process group to kill.
    proc = subprocess.Popen(cmd, cwd=CODE_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True,
                            env={**os.environ, "PATH": CHAT_PATH, "WORKTRACKER_HOME": str(view_home())})
    lines, err_tail = queue.Queue(), deque(maxlen=20)
    # Read both pipes on their own threads: a full stderr pipe must not stall the CLI.
    threading.Thread(target=drain, args=(proc.stdout, lines.put, lambda: lines.put(None)),
                     daemon=True).start()
    threading.Thread(target=drain, args=(proc.stderr, lambda l: err_tail.append(l.rstrip())),
                     daemon=True).start()
    deadline = time.monotonic() + CHAT_TIMEOUT_SECONDS
    texts, done = 0, False
    try:
        proc.stdin.write(message)
        proc.stdin.close()
        while True:
            if time.monotonic() > deadline:
                write({"t": "error", "d": f"Stopped after {CHAT_TIMEOUT_SECONDS // 60} minutes without finishing."})
                return
            try:
                line = lines.get(timeout=CHAT_PING_SECONDS)
            except queue.Empty:
                write({"t": "ping"})
                continue
            if line is None:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                write({"t": "session", "id": ev.get("session_id")})
            elif kind == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_start" and e["content_block"].get("type") == "text":
                    if texts:
                        write({"t": "block"})
                    texts += 1
                elif e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                    write({"t": "text", "d": e["delta"]["text"]})
            elif kind == "assistant":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        inp = c.get("input", {})
                        label = inp.get("description") or inp.get("command") or inp.get("pattern") or inp.get("file_path")
                        write({"t": "tool", "d": f"{c.get('name')}: {label}"[:120] if label else c.get("name")})
            elif kind == "result":
                done = True
                write({"t": "done", "error": bool(ev.get("is_error")),
                       "result": ev.get("result", ""), "id": ev.get("session_id")})
        proc.wait(timeout=10)
        if not done:
            reason = err_tail[-1] if err_tail else f"claude exited with code {proc.returncode}"
            write({"t": "error", "d": f"Claude stopped without finishing: {reason}"})
    except (BrokenPipeError, ConnectionResetError, OSError, subprocess.TimeoutExpired):
        pass
    finally:
        chat_slots.release()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()


class Handler(BaseHTTPRequestHandler):
    timeout = 30                     # a client that stops sending mid-request is dropped
    MAX_BODY = 20_000

    def log_message(self, *args):
        pass

    def _headers(self, code, ctype, cache="no-store", length=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        # The dashboard must not be framed by another page (clickjacking).
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        self._headers(code, ctype, cache, len(body))
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode())

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{bound_port}", f"localhost:{bound_port}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b'{"error":"forbidden"}')
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
        elif url.path in STATIC_FILES:
            name = url.path.rsplit("/", 1)[1]
            self._send(200, (STATIC_DIR / name).read_bytes(), STATIC_FILES[url.path], "max-age=3600")
        elif url.path == "/api/stats":
            raw = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                for k in ("from", "to"):
                    if raw.get(k):
                        parse_day(raw[k])
                self._json(stats(raw))
            except ValueError:
                self._send(400, b'{"error":"bad request"}')
        elif url.path == "/api/icon":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            kind = "app" if q.get("app") else "domain" if q.get("domain") else None
            png = icon_png(kind, q[kind]) if kind and len(q[kind]) <= ICON_NAME_MAX else None
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
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, b'{"error":"length required"}')
        if not 0 <= size <= self.MAX_BODY:
            return self._send(413, b'{"error":"request too large"}')
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            path = urlparse(self.path).path
            if path == "/api/chat":
                return self._chat(body["message"], body.get("session_id"))
            if path == "/api/config":
                apply_config_patch(body)
                self._json({"ok": True})
            elif path == "/api/close":
                self._json({"ok": True, "day": set_day_closed(reopen=body.get("reopen") is True)})
            elif path == "/api/todo":
                self._json({"ok": True, "todos": change_todo(
                    str(body["action"]), body.get("id"), body.get("text"), body.get("for"),
                    split=body.get("split") is True)})
            elif path == "/api/dataset":
                select_dataset(str(body.get("name")))
                self._json({"ok": True, "dataset": dataset})
            elif path == "/api/personal":
                set_personal_time(int(_num(body.get("minutes", 0), 0, 480)))
                self._json({"ok": True})
            elif path == "/api/test-alert":
                style = body.get("style")
                if style not in ALERT_STYLES:
                    raise ValueError("bad alert style")
                AppHelper.callAfter(tracker.show, style, "Test alert",
                                    f"This is how alerts look in the \"{style}\" style.")
                self._json({"ok": True})
            else:
                self._send(404, b'{"error":"not found"}')
        except (ValueError, TypeError, KeyError, AttributeError):
            self._send(400, b'{"error":"bad request"}')

    def _chat(self, message, session_id):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("empty message")
        if session_id is not None and str(uuid.UUID(str(session_id))) != str(session_id).lower():
            raise ValueError("bad session id")
        self._headers(200, "application/x-ndjson")

        def write(event):
            self.wfile.write((json.dumps(event) + "\n").encode())
            self.wfile.flush()

        run_chat(message.strip(), session_id, write)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A page closed or reloaded mid-response, or a client stalled; nothing to report.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    global cfg, tracker, bound_port
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "worktracker.log", maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    cfg = load_config()
    tracker = Tracker()
    # Bind before announcing; a taken port must not look like a working dashboard.
    # The port is fixed for the life of the process, even if config changes it.
    bound_port = cfg["dashboard_port"]
    try:
        server = Server(("127.0.0.1", bound_port), Handler)
    except OSError as e:
        log.error("Dashboard not started: port %s is unavailable (%s). Tracking continues; "
                  "set dashboard_port in config.json and restart.", bound_port, e)
    else:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Started. Dashboard: http://127.0.0.1:%s", bound_port)

    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    # An accessory app (no Dock icon) can show overlay windows and handle their clicks.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    interval = cfg["sample_seconds"]
    timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        interval, True, lambda _t: tracker.safe_tick())
    timer.setTolerance_(interval * 0.3)  # lets macOS coalesce wakeups
    app.run()


if __name__ == "__main__":
    main()
