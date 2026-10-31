"""Desk Buddy - a robot face for your phone that reacts to what your PC is doing.

As DeskBuddy.exe (built with build.bat): double-click it once and it installs itself,
starts with Windows, lives in the system tray, and shows a QR code for pairing a phone.

During development:  python deskbuddy.py
(needs: pip install pillow pystray qrcode pycaw winrt-Windows.Media.Control icalendar recurring-ical-events)

Windows only (reads PC activity through the Win32 API via ctypes).
"""
import array
import base64
import ctypes
import datetime
import email
import hashlib
import hmac
import html
import imaplib
import io
import ipaddress
import json
import math
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
import winreg
from collections import deque
from ctypes import wintypes
from email.header import decode_header, make_header
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brain

APP_NAME = "Desk Buddy"
VERSION = "2.4.2"
PORT = int(os.environ.get("DESKBUDDY_PORT", 8765))
HTTPS_PORT = PORT + 1     # the secure link, needed for the phone's motion sensors and microphone
FPS = 15                  # how often the phone gets cursor updates
SESSION_RESET_IDLE = 300  # seconds away from the PC that count as "took a break"

FROZEN = getattr(sys, "frozen", False)
RESOURCES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
INSTALL_DIR = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "DeskBuddy")
INSTALLED_EXE = os.path.join(INSTALL_DIR, "DeskBuddy.exe")
_PROGRAMS = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs")
STARTUP_LNK = os.path.join(_PROGRAMS, "Startup", "Desk Buddy.lnk")
START_MENU_LNK = os.path.join(_PROGRAMS, "Desk Buddy.lnk")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DeskBuddy"
FIREWALL_RULE = "DeskBuddy"
NO_WINDOW = 0x08000000
DATA_DIR = os.path.join(os.environ["APPDATA"], "DeskBuddy")   # settings and friendship progress
CERT_FILE = os.path.join(DATA_DIR, "phone-link.crt")
KEY_FILE = os.path.join(DATA_DIR, "phone-link.key")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ERROR_LOG = os.path.join(DATA_DIR, "error.log")


def log_error(where, exc):
    """Problems go to a small log in the settings folder (shown on the dashboard's About page)."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(ERROR_LOG) and os.path.getsize(ERROR_LOG) > 200_000:
            os.replace(ERROR_LOG, ERROR_LOG + ".old")
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {VERSION} {where} (args {sys.argv[1:]})\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


sys.excepthook = lambda t, e, tb: log_error("crash", e)
threading.excepthook = lambda a: log_error(f"thread {a.thread.name if a.thread else '?'}", a.exc_value)
shell32 = ctypes.windll.shell32
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]

# Make cursor coordinates match real pixels on scaled (125%/150%) displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------- reading the PC ----------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def cpu_times():
    idle, kern, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    return idle.value, kern.value, user.value


def ram_percent():
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.dwMemoryLoad


def idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    user32.GetLastInputInfo(ctypes.byref(info))
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def active_window_title():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.LoadCursorW.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


def active_process():
    """The program in front, like 'code.exe' (just its name: used to tell coding from chatting)."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(260), wintypes.DWORD(260)
        return os.path.basename(buf.value).lower() if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def is_fullscreen():
    """True when the front window covers its whole screen (games, fullscreen videos)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
        return False
    r, mi = wintypes.RECT(), MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


BUSY_CURSORS = {user32.LoadCursorW(None, 32514), user32.LoadCursorW(None, 32650)}   # the hourglass and "working in background"


def busy_cursor():
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    return bool(user32.GetCursorInfo(ctypes.byref(ci))) and ci.hCursor in BUSY_CURSORS


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def pc_power():
    """The laptop's battery, or None on a desktop PC."""
    s = SYSTEM_POWER_STATUS()
    kernel32.GetSystemPowerStatus(ctypes.byref(s))
    if s.BatteryFlag == 128 or s.BatteryLifePercent == 255:
        return None
    return {"percent": s.BatteryLifePercent, "charging": s.ACLineStatus == 1}


def cursor_position():
    """Cursor position across all monitors, scaled to -1..1 on each axis."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    return (pt.x - vx) / max(vw, 1) * 2 - 1, (pt.y - vy) / max(vh, 1) * 2 - 1


stats = {"cpu": 0, "ram": 0, "idle": 0, "title": "", "work": 0, "audio": 0,
         "media": None, "mail": None, "calendar": None, "buddy": None, "prefs": None, "ping": 0, "backspaces": 0,
         "beat": None, "mediaKind": None, "activity": None, "day": None, "typing": 0, "busy": False, "online": True,
         "pcBattery": None, "clip": None, "volume": None, "fullscreen": False, "cmd": None, "profile": None,
         "life": None, "heart": None, "catchup": None, "links": [], "pcs": None}


# ---------- saved files, and the people who use this PC ----------
# Each person has a profile folder: their buddy, settings, Google accounts, what it learned and their
# work history. A profile can have a password; its secrets are then encrypted with a key made from that
# password, so nobody else using this PC can read them.

PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
PROFILE_ROOT = os.path.join(DATA_DIR, "profiles")
PROFILE_FILES = ("config.json", "buddy.json", "learned.json", "activity.json")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except (OSError, ValueError):
        return dict(default)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(func, data):
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("Windows could not encrypt/decrypt the setting")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def windows_lock(data):
    """Encrypt with Windows (DPAPI): only this Windows user on this PC can read it back."""
    return base64.b64encode(_dpapi(ctypes.windll.crypt32.CryptProtectData, data)).decode()


def windows_unlock(b64):
    try:
        return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(b64))
    except (OSError, ValueError):
        return None


profile_key = None   # the active person's password-derived key (None: no password, Windows encryption only)


def protect(text):
    """Encrypt a secret (App Password, calendar link) for the active person."""
    if not text:
        return ""
    if profile_key:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        return "pw:" + base64.b64encode(nonce + AESGCM(profile_key).encrypt(nonce, text.encode(), None)).decode()
    return windows_lock(text.encode())


def unprotect(blob):
    try:
        if not blob:
            return ""
        if blob.startswith("pw:"):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            raw = base64.b64decode(blob[3:])
            return AESGCM(profile_key).decrypt(raw[:12], raw[12:], None).decode() if profile_key else ""
        data = windows_unlock(blob)
        return data.decode() if data else ""
    except Exception:
        return ""


def derive(password, salt, purpose):
    """Slow on purpose (200,000 rounds), so passwords can't be guessed quickly."""
    return hashlib.pbkdf2_hmac("sha256", f"{purpose}:{password}".encode(), base64.b64decode(salt), 200_000)


def check_password(entry, password):
    if not entry.get("check"):
        return True   # this person chose no password
    return hmac.compare_digest(base64.b64encode(derive(password, entry["salt"], "check")).decode(), entry["check"])


profiles = load_json(PROFILES_FILE, {"active": "", "list": [], "keys": {}})
if not profiles["list"]:   # first run, or upgrading from single-person Desk Buddy: that becomes the first profile
    first = os.path.join(PROFILE_ROOT, "p1")
    os.makedirs(first, exist_ok=True)
    for _name in PROFILE_FILES:
        if os.path.exists(os.path.join(DATA_DIR, _name)):
            shutil.move(os.path.join(DATA_DIR, _name), os.path.join(first, _name))
    profiles.update(active="p1", keys={}, list=[{"id": "p1", "name": "Me", "emoji": "🙂",
                                                "salt": base64.b64encode(os.urandom(16)).decode(), "check": ""}])
    save_json(PROFILES_FILE, profiles)


def profile_entry(pid):
    return next((p for p in profiles["list"] if p["id"] == pid), None)


DEFAULT_CONFIG = {"gmail_user": "", "gmail_password": "", "calendar_url": "", "onboarded": False,
                  "payday": 0, "birthday": ""}

# Everything the dashboard can switch on and off. Sent to the phone, so changes apply instantly.
DEFAULT_PREFS = {
    "music": True, "reading": True, "meeting": True, "suspicious": True, "stress": True, "quirks": True,
    "breaks": True, "breakMinutes": 50, "bedtime": True, "mailAlerts": True, "calendarAlerts": True,
    "soundOutput": "phone", "volume": 80, "cheeky": 0, "sensors": True, "mic": True,
    "movie": True, "greetings": True, "spin": True, "toolbox": True, "babble": True,
    "typing": True, "loading": True, "offline": True, "pcBattery": True, "clipboard": True, "volumeReact": True,
    "gaming": True, "patterns": True,
    "weather": True, "daylight": True, "seasons": True, "occasions": True, "money": True,
    "hEid": True, "hChristmas": True, "hDiwali": True, "hLunar": True,
    "needs": True, "catchup": True, "danceFx": True, "desktopBuddy": True,
}
PREF_CHOICES = {"soundOutput": ("phone", "pc", "both", "off")}
PREF_RANGES = {"breakMinutes": (10, 180), "volume": (0, 100), "cheeky": (0, 3)}
config, prefs, buddy = {}, {}, {}   # the active person's (filled in by load_profile)
CONFIG_FILE = BUDDY_FILE = ""


def update_prefs(changes):
    for key, value in changes.items():
        if key not in DEFAULT_PREFS:
            continue
        default = DEFAULT_PREFS[key]
        if isinstance(default, bool):
            value = bool(value)
        elif key in PREF_RANGES:
            low, high = PREF_RANGES[key]
            value = max(low, min(high, int(value)))
        elif value not in PREF_CHOICES.get(key, ()):
            continue
        prefs[key] = value
    config["prefs"] = prefs
    save_json(CONFIG_FILE, config)
    stats["prefs"] = dict(prefs)


services = {"gmail": "Not set up", "calendar": "Not set up"}   # status lines for the settings page
wake_mail, wake_calendar = threading.Event(), threading.Event()

# Friendship: points come from days together, time at the PC, petting and taking breaks.
PERSONALITIES = {"cheerful": "Cheerful 😊", "shy": "Shy 😳", "hyper": "Hyper ⚡",
                 "sleepy": "Sleepy 😴", "grumpy": "Grumpy-but-sweet 😤"}
GENDERS = {"boy": "Boy 👦", "girl": "Girl 👧", "neutral": "Just a robot 🤖"}
NAMES = {"boy": ["Bolt", "Pip", "Biscuit", "Pixel", "Bean", "Blip", "Rusty", "Ziggy"],
         "girl": ["Mochi", "Nori", "Zuzu", "Momo", "Luna", "Pixie", "Daisy", "Kiki"],
         "neutral": ["Sprout", "Tofu", "Beep", "Nova", "Orbit", "Sunny"]}
LEVELS = [0, 40, 120, 260, 480]          # points needed for friendship levels 1-5
POINTS = {"day": 10, "time": 1, "pet": 1, "break": 5}
DAILY_CAP = {"time": 30, "pet": 15, "break": 15}
bond_lock = threading.Lock()
learner = tracker = heart = None
profile_gen = 0   # goes up on every switch, so background checks (like Gmail) start fresh


PC_FILE = os.path.join(DATA_DIR, "pc.json")


def _pc_id():
    d = load_json(PC_FILE, {})
    if not d.get("id"):
        d = {"id": "pc" + os.urandom(4).hex()}
        save_json(PC_FILE, d)
    return d["id"]


PC_ID, PC_NAME = _pc_id(), socket.gethostname()   # this PC, for sync
OTHERS_FILE = ""


def born_buddy():
    gender = random.choice(list(GENDERS))
    return {"name": random.choice(NAMES[gender]), "gender": gender, "personality": random.choice(list(PERSONALITIES)),
            "points": 0, "met": datetime.date.today().isoformat(), "lastDay": "", "awayDays": 0, "todayDate": "", "today": {}}


def load_profile(pid, key):
    """Make pid the active person: their buddy, accounts, settings, learning and history."""
    global CONFIG_FILE, BUDDY_FILE, learner, tracker, heart, profile_key, profile_gen
    if tracker:
        with tracker.lock:
            tracker._save()
    if heart:
        save_heart()   # keep the last person's buddy's feelings
    folder = os.path.join(PROFILE_ROOT, pid)
    CONFIG_FILE, BUDDY_FILE = os.path.join(folder, "config.json"), os.path.join(folder, "buddy.json")
    profile_key = key
    config.clear()
    config.update(load_json(CONFIG_FILE, DEFAULT_CONFIG))
    prefs.clear()
    prefs.update({**DEFAULT_PREFS, **config.get("prefs", {})})
    fresh = load_json(BUDDY_FILE, {})
    if fresh.get("personality") not in PERSONALITIES:   # a brand-new buddy is born
        fresh = born_buddy()
        save_json(BUDDY_FILE, fresh)
    fresh.setdefault("gender", "neutral")   # buddies born before genders existed
    with bond_lock:
        buddy.clear()
        buddy.update(fresh)
        buddy.setdefault("pcPoints", {PC_ID: buddy["points"]})   # friendship from before sync existed
        heart = brain.Heart(buddy.get("heart"), buddy["personality"])
    learner = brain.MediaLearner(os.path.join(folder, "learned.json"))
    tracker = brain.ActivityTracker(os.path.join(folder, "activity.json"))
    global OTHERS_FILE
    OTHERS_FILE = os.path.join(folder, "others.json")   # your other PCs' activity (sync)
    tracker.others = {pc: e.get("days", {}) for pc, e in load_json(OTHERS_FILE, {}).items()}
    if "sync_state" in globals():
        sync_state.update(peers={}, folderPeers={}, code=None, key=None, group=None)
        wake_sync.set()
    profiles["active"] = pid
    profiles["keys"] = {pid: windows_lock(key)} if key else {}   # so a restart doesn't ask again (it locks only on switching)
    save_json(PROFILES_FILE, profiles)
    profile_gen += 1
    entry = profile_entry(pid)
    stats.update(prefs=dict(prefs), mail=None, calendar=None, day=None, activity=None,
                 profile={"id": pid, "name": entry["name"], "emoji": entry["emoji"], "locked": bool(entry["check"]) and not key})
    services.update(gmail="Checking…" if config["gmail_user"] else "Not set up", calendar="Checking…" if config["calendar_url"] else "Not set up")
    wake_mail.set()
    wake_calendar.set()
    if "wake_life" in globals():
        wake_life.set()   # their own payday, birthday and holidays


def _startup_profile():
    pid = profiles["active"] if profile_entry(profiles["active"]) else profiles["list"][0]["id"]
    cached = profiles.get("keys", {}).get(pid)
    load_profile(pid, windows_unlock(cached) if cached else None)


_startup_profile()


def add_points(kind):
    with bond_lock:
        today = datetime.date.today().isoformat()
        if buddy.get("todayDate") != today:
            buddy["todayDate"], buddy["today"] = today, {}
        used = buddy["today"].get(kind, 0)
        if kind in DAILY_CAP and used >= DAILY_CAP[kind]:
            return
        buddy["today"][kind] = used + POINTS[kind]
        buddy["points"] += POINTS[kind]
        pts = buddy.setdefault("pcPoints", {})
        pts[PC_ID] = pts.get(PC_ID, 0) + POINTS[kind]
        if heart:
            buddy["heart"] = heart.save()
        save_json(BUDDY_FILE, buddy)


def save_heart():
    with bond_lock:
        buddy["heart"] = heart.save()
        save_json(BUDDY_FILE, buddy)


PET_KINDS = ("pet", "pat", "chin", "hug", "boop", "tickle", "highfive")


def give_care(kind):
    """Petting, treats and play from the phone or the desktop buddy. False: it's had enough of that treat today."""
    with bond_lock:
        ok = heart.care(kind, time.time())
    if ok and kind in PET_KINDS:
        add_points("pet")
    elif ok:
        save_heart()
    stats["heart"] = heart.info()
    return ok


def note_active_day():
    today = datetime.date.today()
    if buddy["lastDay"] != today.isoformat():
        last = buddy["lastDay"]
        buddy["awayDays"] = (today - datetime.date.fromisoformat(last)).days if last else 0
        buddy["lastDay"] = today.isoformat()
        if heart and buddy["awayDays"] >= 3:
            heart._add("love", -.25)   # it missed you; a little extra love fixes that
        add_points("day")


def buddy_info():
    pts = buddy["points"]
    level = sum(pts >= t for t in LEVELS)
    nxt = LEVELS[level] if level < len(LEVELS) else None
    return {
        "name": buddy["name"], "personality": buddy["personality"], "gender": buddy["gender"], "level": level,
        "progress": round((pts - LEVELS[level - 1]) / (nxt - LEVELS[level - 1]), 3) if nxt else 1,
        "days": (datetime.date.today() - datetime.date.fromisoformat(buddy["met"])).days,
        "awayDays": buddy["awayDays"],
    }


# ---------- what's playing (Spotify, YouTube, any app that shows in Windows' media controls) ----------

def media_loop():
    try:
        import asyncio
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    except Exception:
        return  # older Windows: the robot still dances to sound, just without song names

    async def watch():
        manager = await Manager.request_async()
        while True:
            info = None
            try:
                session = manager.get_current_session()
                if session:
                    props = await session.try_get_media_properties_async()
                    app = session.source_app_user_model_id.lower()
                    info = {
                        "title": props.title or "", "artist": props.artist or "",
                        "playing": int(session.get_playback_info().playback_status) == 4,  # 4 = Playing
                        "app": next((n for k, n in (("spotify", "Spotify"), ("chrome", "Chrome"), ("edge", "Edge"),
                                                    ("firefox", "Firefox"), ("308046b0af4a39cb", "Firefox"))
                                     if k in app), ""),
                    }
            except Exception:
                info = None
            stats["media"] = info
            stats["mediaKind"] = learner.classify(info, stats["title"])   # music, movie or ignore (and why)
            await asyncio.sleep(1)

    try:
        asyncio.run(watch())
    except Exception:
        pass


# ---------- Gmail (IMAP with a Google App Password) ----------

def decode(value):
    return str(make_header(decode_header(value or "")))


def check_gmail(user, password, last_uid):
    """Returns (unread count, newest new message or None, how many new, highest UID seen)."""
    with imaplib.IMAP4_SSL("imap.gmail.com", timeout=20) as m:
        m.login(user, password)
        m.select("INBOX", readonly=True)
        unread = [int(u) for u in m.uid("search", None, "UNSEEN")[1][0].split()]
        new = [u for u in unread if last_uid is not None and u > last_uid]
        latest = None
        if new:
            data = m.uid("fetch", str(max(new)), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")[1]
            header = email.message_from_bytes(data[0][1])
            name, addr = parseaddr(decode(header.get("From")))
            latest = {"from": name or addr, "subject": decode(header.get("Subject"))[:90]}
        status = m.status("INBOX", "(UIDNEXT)")[1][0].decode()
        top = int(status.split("UIDNEXT")[1].strip(" )")) - 1
        return len(unread), latest, len(new), top


def mail_loop():
    last_uid, seq, gen = None, 0, profile_gen
    while True:
        if gen != profile_gen:   # someone else's inbox now: start fresh
            last_uid, gen = None, profile_gen
        user, password = config["gmail_user"], unprotect(config["gmail_password"])
        if user and password:
            try:
                unread, latest, count, last_uid = check_gmail(user, password, last_uid)
                if latest:
                    seq += 1
                stats["mail"] = {"unread": unread, "latest": latest, "count": count, "seq": seq}
                services["gmail"] = f"Connected ✓ · {unread} unread"
            except imaplib.IMAP4.error:
                stats["mail"], last_uid = None, None
                services["gmail"] = "Login failed - check the email address and App Password"
            except Exception as e:
                services["gmail"] = f"Can't reach Gmail right now ({type(e).__name__})"
        else:
            stats["mail"], last_uid = None, None
            services["gmail"] = "Not set up"
        wake_mail.wait(60)
        wake_mail.clear()


# ---------- Google Calendar (its private iCal link) ----------

def parse_calendar(data, now=None):
    """Upcoming timed events in the next 24 hours, plus how many are left today."""
    import icalendar
    import recurring_ical_events
    now = now or datetime.datetime.now().astimezone()
    end_of_day = now.replace(hour=23, minute=59, second=59)
    events = []
    for ev in recurring_ical_events.of(icalendar.Calendar.from_ical(data)).between(
            now - datetime.timedelta(minutes=15), now + datetime.timedelta(hours=24)):
        start = ev.get("DTSTART").dt
        if not isinstance(start, datetime.datetime):
            continue  # all-day events have no start time to remind about
        if start.tzinfo is None:
            start = start.astimezone()
        events.append({"title": str(ev.get("SUMMARY", "Event"))[:80], "start": start.timestamp()})
    events.sort(key=lambda e: e["start"])
    today = sum(now.timestamp() <= e["start"] <= end_of_day.timestamp() for e in events)
    return {"events": events[:6], "today": today}


def calendar_loop():
    while True:
        url = unprotect(config["calendar_url"])
        if url:
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    stats["calendar"] = parse_calendar(r.read())
                services["calendar"] = f"Connected ✓ · {stats['calendar']['today']} more events today"
            except Exception as e:
                services["calendar"] = f"Couldn't read the calendar link ({type(e).__name__})"
        else:
            stats["calendar"] = None
            services["calendar"] = "Not set up"
        wake_calendar.wait(300)
        wake_calendar.clear()


# ---------- the buddy's voice through the PC speakers ----------
# The phone composes every sound as a short list of notes and noise bursts. When sound is set to
# "PC", it sends that list here and we synthesise the very same sound, so both outputs match.

SAMPLE_RATE = 24000
_voices, _voice_lock, _stream = [], threading.Lock(), None


def _osc(kind, phase):
    x = phase % 1.0
    if kind == "triangle":
        return 1 - 4 * abs(x - 0.5)
    if kind == "sawtooth":
        return 2 * x - 1
    if kind == "square":
        return 1.0 if x < 0.5 else -1.0
    return math.sin(2 * math.pi * x)


def _num(e, key, default, low, high):
    try:
        return min(max(float(e.get(key, default)), low), high)
    except (TypeError, ValueError):
        return default


def _biquad(kind, freq, q):
    """RBJ filter coefficients - the same filters Web Audio's BiquadFilterNode uses."""
    w0 = 2 * math.pi * min(freq, SAMPLE_RATE / 2.2) / SAMPLE_RATE
    alpha, cos = math.sin(w0) / (2 * q), math.cos(w0)
    if kind == "lowpass":
        b = ((1 - cos) / 2, 1 - cos, (1 - cos) / 2)
    else:  # band-pass, 0 dB peak
        b = (alpha, 0.0, -alpha)
    a0 = 1 + alpha
    return [x / a0 for x in b] + [-2 * cos / a0, (1 - alpha) / a0]


def _filter(samples, coeffs):
    b0, b1, b2, a1, a2 = coeffs
    x1 = x2 = y1 = y2 = 0.0
    out = []
    for x in samples:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
        out.append(y)
    return out


def _ramp(a, b, frac):
    """Exponential ramp from a to b, like Web Audio's exponentialRampToValueAtTime."""
    return a * (b / a) ** frac


def _source(e, dur, count):
    """The raw oscillator (pitch glide + vibrato) shared by tones and syllables."""
    f0, f1 = _num(e, "f0", 440, 20, 8000), _num(e, "f1", 440, 20, 8000)
    kind, vib = e.get("type") or e.get("wave") or "sine", _num(e, "vib", 0, 0, 40)
    phase, out = 0.0, []
    for i in range(count):
        t = i / SAMPLE_RATE
        f = _ramp(f0, f1, t / dur)
        if vib:
            f += f0 * 0.05 * math.sin(2 * math.pi * vib * t)
        phase += f / SAMPLE_RATE
        out.append(_osc(kind, phase))
    return out


def _room(samples, wet=0.22):
    """A small, soft room echo (Schroeder reverb), standing in for the phone's convolution reverb."""
    tail = samples + [0.0] * int(0.9 * SAMPLE_RATE)
    combs = [(int(SAMPLE_RATE * d), g) for d, g in ((0.0297, 0.77), (0.0371, 0.75), (0.0411, 0.73), (0.0437, 0.71))]
    mixed = [0.0] * len(tail)
    for delay, gain in combs:
        buf, low = [0.0] * delay, 0.0
        for i, x in enumerate(tail):
            y = buf[i % delay]
            low = y * 0.6 + low * 0.4   # damp the highs so the echo stays warm
            buf[i % delay] = x + low * gain
            mixed[i] += y
    for delay, g in ((int(SAMPLE_RATE * 0.005), 0.7), (int(SAMPLE_RATE * 0.0017), 0.7)):
        buf = [0.0] * delay
        for i, x in enumerate(mixed):
            y = buf[i % delay]
            buf[i % delay] = x + y * g
            mixed[i] = y - x * g
    return [d + m * wet * 0.35 for d, m in zip(tail, mixed)]


SYL_GAIN, SYL_Q = 4, 2


def vowel_loudness(f, f1, f2, wave):
    """Same as vowelLoudness() in face.html: evens out syllables whose pitch misses the vowel's resonances."""
    total = 0.0
    for k in range(1, 40):
        if k * f > 11000:
            break
        if wave == "sawtooth":
            amp = 2 / (math.pi * k)
        elif wave == "sine":
            amp = 1.0 if k == 1 else 0.0
        else:
            amp = 8 / (math.pi ** 2 * k * k) if k % 2 else 0.0
        if amp:
            h = lambda fc: 1 / math.sqrt(1 + SYL_Q ** 2 * (k * f / fc - fc / (k * f)) ** 2)
            total += amp * amp * (h(f1) + 0.6 * h(f2)) ** 2 / 2
    return math.sqrt(total)


def render_sound(events, volume):
    """Turn the phone's sound list into samples - mirrors playTone/playNoise/playSyl in face.html."""
    events = [e for e in events[:60] if isinstance(e, dict)]
    if not events:
        return []
    end = min(4.0, max(_num(e, "delay", 0, 0, 4) + _num(e, "dur", 0.1, 0.01, 2.5) for e in events) + 0.06)
    out = [0.0] * int(end * SAMPLE_RATE)
    for e in events:
        dur = _num(e, "dur", 0.1, 0.01, 2.5)
        vol = _num(e, "vol", 0.1, 0.0001, 0.5)
        start = int(_num(e, "delay", 0, 0, 4) * SAMPLE_RATE)
        count = max(min(int(dur * SAMPLE_RATE), len(out) - start), 0)
        kind = e.get("k")
        if kind == "noise":
            src = _filter([random.uniform(-1, 1) for _ in range(count)], _biquad("bandpass", _num(e, "freq", 1000, 50, 9000), 1))
            for i, y in enumerate(src):
                out[start + i] += y * _ramp(vol, 0.0001, i / count)
        elif kind == "syl":
            raw = _source(e, dur, count)
            f1, f2 = _num(e, "F1", 700, 100, 5000), _num(e, "F2", 1500, 100, 6000)
            voice = [a + 0.6 * b for a, b in zip(_filter(raw, _biquad("bandpass", f1, SYL_Q)), _filter(raw, _biquad("bandpass", f2, SYL_Q)))]
            breath = _num(e, "breath", 0, 0, 0.5)
            if breath:
                air = _filter([random.uniform(-1, 1) for _ in range(count)], _biquad("bandpass", f2, 2))
                voice = [v + a * breath * 6 for v, a in zip(voice, air)]
            pitch = (_num(e, "f0", 440, 20, 8000) + _num(e, "f1", 440, 20, 8000)) / 2
            wave = e.get("wave") or "triangle"
            peak = vol * SYL_GAIN * min(3, max(0.4, 0.3 / max(vowel_loudness(pitch, f1, f2, wave), 1e-6)))
            for i, y in enumerate(voice):
                t = i / SAMPLE_RATE
                if t < 0.015:
                    env = _ramp(0.0001, peak, t / 0.015)
                elif t < dur * 0.7:
                    env = _ramp(peak, peak * 0.5, (t - 0.015) / (dur * 0.7 - 0.015))
                else:
                    env = _ramp(peak * 0.5, 0.0001, (t - dur * 0.7) / (dur * 0.3))
                out[start + i] += y * env
        else:  # tone
            src = _source(e, dur, count)
            lp = _num(e, "lp", 0, 0, 12000)
            if lp:
                src = _filter(src, _biquad("lowpass", lp, 0.707))
            attack = _num(e, "att", 0, 0, 1) or min(0.03, dur / 3)
            trem = _num(e, "trem", 0, 0, 60)
            for i, y in enumerate(src):
                t = i / SAMPLE_RATE
                env = _ramp(0.0001, vol, t / attack) if t < attack else _ramp(vol, 0.0001, (t - attack) / (dur - attack))
                if trem:
                    env += vol * 0.8 * math.sin(2 * math.pi * trem * t)
                out[start + i] += y * env
    gain = volume / 100 * 0.9
    return [math.tanh(s * gain * 1.2) / 1.2 for s in _room(out)]   # tanh: gentle limiting, never harsh clipping


def _mix(outdata, frames, _time, _status):
    import sounddevice
    mixed = [0.0] * frames
    with _voice_lock:
        for voice in _voices:
            samples, pos = voice
            chunk = samples[pos:pos + frames]
            for i, s in enumerate(chunk):
                mixed[i] += s
            voice[1] = pos + frames
        _voices[:] = [v for v in _voices if v[1] < len(v[0])]
        idle = not _voices
    pcm = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in mixed))
    outdata[:] = pcm.tobytes()
    if idle:
        raise sounddevice.CallbackStop   # release the speakers until the next sound


def play_on_pc(events):
    global _stream
    samples = render_sound(events, prefs["volume"])
    if not samples:
        return
    try:
        import sounddevice
        with _voice_lock:
            _voices.append([samples, 0])
        if _stream is None:
            _stream = sounddevice.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                                  blocksize=1200, callback=_mix)
        if not _stream.active:
            try:
                _stream.stop()
            except Exception:
                pass
            _stream.start()
    except Exception:
        pass   # no speakers available: stay silent

CHIRP = [   # "hi-hi!" in buddy-speak, for the dashboard's Test on PC button
    {"k": "syl", "f0": 470, "f1": 500, "dur": .12, "vol": .075, "F1": 900, "F2": 1500, "wave": "triangle", "breath": .02},
    {"k": "syl", "f0": 560, "f1": 640, "dur": .19, "vol": .075, "F1": 600, "F2": 2200, "wave": "triangle", "breath": .02, "delay": .16},
]


class BeatTracker:
    """Finds the beat in the speaker level: spots sudden rises (onsets), works out the tempo from the
    gaps between them, and keeps a steady beat grid locked onto them, so the phone can dance in time."""

    def __init__(self):
        self.prev = self.flux_avg = 0.0
        self.onsets, self.last_onset, self.period, self.anchor = [], 0.0, 0.0, 0.0
        # The shape of the song, for different dance moves: loudness now / lately / over the last
        # few seconds, big hits (kicks), build-ups and drops.
        self.fast = self.mid = self.long = self.loudest = 0.0
        self.last_hit = self.last_drop = self.built_at = self.calm_at = self.music_at = 0.0

    def feed(self, level, now):
        flux = max(0.0, level - self.prev)
        self.prev = level
        self.flux_avg = self.flux_avg * 0.97 + flux * 0.03
        if level > 0.01 and now - self.last_onset > 4:   # music (re)starting: forget the old song's shape
            self.fast = self.mid = self.long = level * 0.5
            self.music_at, self.loudest = now, 0.0
        self.fast = self.fast * 0.98 + level * 0.02           # ~0.5 s
        self.mid = self.mid * 0.995 + level * 0.005           # ~2 s
        self.long = self.long * 0.99875 + level * 0.00125     # ~8 s
        self.loudest = max(self.mid, self.loudest * 0.9998)   # the song's loud parts, fading over a minute
        settled = now - self.music_at > 10
        if settled and self.build() > 0.35:
            self.built_at = now
        if settled and self.long > 0.02 and self.fast < self.long * 0.45:
            self.calm_at = now
        if settled and now - self.last_drop > 15 and (
                (now - self.calm_at < 4 and self.fast > self.long * 1.5) or (now - self.built_at < 3 and self.fast > self.long * 2.2)):
            self.last_drop = now   # the drop!
        if flux > self.flux_avg * 6 + 0.05:
            self.last_hit = now
        if flux > self.flux_avg * 3 + 0.02 and now - self.last_onset > 0.2:
            self.last_onset = now
            self.onsets = [t for t in self.onsets if now - t < 10] + [now]
            self._tempo()
            self._lock(now)
        if now - self.last_onset > 4:
            self.period = 0.0   # the music stopped

    def _tempo(self):
        if len(self.onsets) < 6:
            return
        gaps = [b - a for i, a in enumerate(self.onsets) for b in self.onsets[i + 1:i + 5]]
        best, best_score = 0.0, 0.0
        for bpm in range(70, 181, 2):   # try every tempo; the one that explains most gaps wins
            p = 60 / bpm
            score = 0.0
            for g in gaps:
                k = round(g / p)
                if 1 <= k <= 4:
                    score += max(0.0, 1 - abs(g - k * p) / (0.08 * k)) / k
            score *= math.exp(-0.5 * ((bpm - 120) / 50) ** 2)   # most songs sit around 90-150 bpm
            if score > best_score:
                best, best_score = p, score
        if best:
            self.period = self.period * 0.7 + best * 0.3 if self.period and abs(best - self.period) < 0.1 else best

    def _lock(self, now):
        if not self.period:
            self.anchor = now
            return
        k = round((now - self.anchor) / self.period)
        err = now - (self.anchor + k * self.period)
        if abs(err) < self.period * 0.3:
            self.anchor += err * 0.3   # nudge the grid toward the beat we just heard
        else:
            self.anchor = now

    def build(self):
        return min(1.0, max(0.0, (self.mid - self.long) / self.long * 2)) if self.long > 0.02 and self.fast > self.long * 0.6 else 0.0

    def state(self, now):
        if not self.period:
            return None
        recent = sum(1 for t in self.onsets if now - t < 4)
        return {"bpm": round(60 / self.period, 1), "t0": int(self.anchor * 1000),
                "energy": round(self.mid / self.loudest, 2) if self.loudest else 0, "build": round(self.build(), 2) if now - self.music_at > 10 else 0,
                "busy": round(recent * self.period / 4, 2),            # hits per beat
                "calm": now - self.calm_at < 0.5,                          # a quiet break in the song
                "hit": int(self.last_hit * 1000), "drop": int(self.last_drop * 1000)}


def audio_loop():
    """How loud the PC's speakers are right now (0..1), and where the beat is, so the robot can dance."""
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        comtypes.CoInitialize()
        dev = AudioUtilities.GetSpeakers()
        dev = getattr(dev, "_dev", dev)  # newer pycaw wraps the raw device
        meter = ctypes.cast(dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None),
                            ctypes.POINTER(IAudioMeterInformation))
        volume = getattr(AudioUtilities.GetSpeakers(), "EndpointVolume", None)   # for "muted" / "too loud!"
    except Exception:
        return  # no speakers or pycaw missing: the robot just won't dance
    level, beats, tick = 0.0, BeatTracker(), 0
    while True:
        try:
            peak = meter.GetPeakValue()
        except Exception:
            peak = 0.0
        if volume is not None and tick % 50 == 0:
            try:
                stats["volume"] = {"level": round(volume.GetMasterVolumeLevelScalar(), 2), "muted": bool(volume.GetMute())}
            except Exception:
                volume = None
        level = max(peak, level * 0.8)   # fast attack, slow release, so beats show up as clear spikes
        beats.feed(peak, time.time())
        tick += 1
        if tick % 2 == 0:
            stats["audio"] = round(level, 3)
            stats["beat"] = beats.state(time.time())
        time.sleep(1 / 100)


work_session = {"start": time.time()}   # the phone can reset this ("I just took a break")


# Its heart keeps beating while the phone is off: needs change all day, and it remembers what you
# did so it can catch up when you open the phone again.
away = {"since": None, "acts": {}, "songs": set(), "mail": 0}
heart_clock = {"tick": time.time(), "saved": time.time()}


def feel(now, here, key, media):
    if now - heart_clock["tick"] >= 15:
        hours = min(now - heart_clock["tick"], 3600) / 3600
        heart_clock["tick"] = now
        if prefs.get("needs", True):
            with bond_lock:
                heart.tick(hours, here, key, datetime.datetime.now().hour)
        if now - heart_clock["saved"] > 300:
            heart_clock["saved"] = now
            save_heart()
    stats["heart"] = heart.info() if prefs.get("needs", True) else None
    today = lambda: dict(tracker.days.get(datetime.date.today().isoformat(), {}).get("acts", {}))
    mail_seq = (stats["mail"] or {}).get("seq", 0)
    if phones == 0:
        if away["since"] is None:
            away.update(since=now, acts=today(), songs=set(), mail=mail_seq)
        elif key == "music" and media.get("title"):
            away["songs"].add(media["title"])
    elif away["since"] is not None:
        minutes = (now - away["since"]) / 60
        if minutes >= 15:
            report = brain.catch_up(away["acts"], today(), minutes)
            report.update(id=int(now), songs=len(away["songs"]), song=next(iter(away["songs"]), ""),
                          mails=max(0, mail_seq - away["mail"]))
            stats["catchup"] = report
        away["since"] = None


def stats_loop():
    prev = cpu_times()
    active = 0
    while True:
        time.sleep(1)
        cur = cpu_times()
        d_idle, d_kern, d_user = (c - p for c, p in zip(cur, prev))
        prev = cur
        total = d_kern + d_user  # kernel time already includes idle time
        idle = idle_seconds()
        if idle > SESSION_RESET_IDLE:
            work_session["start"] = time.time()
        if idle < 60:   # you're here: that's time spent together
            if idle < 2 and time.time() - last_here["at"] > 5:
                hello_now.set()   # back at this PC: let your other PCs (and the phone) know at once
            last_here["at"] = time.time()
            note_active_day()
            active += 1
            if active % 600 == 0:
                add_points("time")
        stats["buddy"] = buddy_info()
        title, now = active_window_title(), time.time()
        stats.update(
            cpu=round(100 * (1 - d_idle / total)) if total else 0,
            ram=ram_percent(),
            idle=round(idle),
            title=title,
            work=round((now - work_session["start"]) / 60, 1),
            pcBattery=pc_power(),
        )
        # What you're doing (by kind of app), for your work pattern.
        media, kind = stats["media"] or {}, (stats["mediaKind"] or {}).get("kind")
        playing = kind if media.get("playing") else None
        full = is_fullscreen()
        key = brain.categorize(active_process(), title, playing, full)
        if key == "browsing" and playing == "music":
            key = "music"
        tracker.tick(now, idle < 60 or playing == "movie", key)   # watching a film counts, even hands-off
        screen["fullscreen"] = full
        stats["activity"], stats["fullscreen"] = tracker.now(now), full and key == "gaming"
        feel(now, idle < 60 or playing == "movie", key, media)
        stats["pcs"] = pc_choices()
        if int(now) % 15 == 0 or stats["day"] is None:
            day = tracker.summary()
            stats["day"] = {k: day[k] for k in ("activeMin", "productiveMin", "breakdown", "first", "usualStart", "usualEnd",
                                                "longestFocusMin", "daysTracked")}


def signals_loop():
    """Little things worth a reaction: the busy (loading) cursor, copying, and screenshots."""
    last_seq, clips, busy_since = user32.GetClipboardSequenceNumber(), 0, 0.0
    while True:
        now = time.time()
        busy_since = (busy_since or now) if prefs["loading"] and busy_cursor() else 0.0
        stats["busy"] = bool(busy_since and now - busy_since > 0.4)
        seq = user32.GetClipboardSequenceNumber()
        if seq != last_seq:
            last_seq = seq
            if prefs["clipboard"]:
                clips += 1
                image = user32.IsClipboardFormatAvailable(8) or user32.IsClipboardFormatAvailable(2)   # CF_DIB / CF_BITMAP
                stats["clip"] = {"n": clips, "kind": "image" if image else "copy"}
        time.sleep(0.25)


def net_loop():
    """Is the internet up? (A quick connection attempt to well-known servers; nothing is sent.)"""
    while True:
        ok = False
        if prefs["offline"]:
            for host in (("1.1.1.1", 443), ("8.8.8.8", 443), ("www.msftconnecttest.com", 80)):
                try:
                    socket.create_connection(host, timeout=3).close()
                    ok = True
                    break
                except OSError:
                    pass
        stats["online"] = ok or not prefs["offline"]
        time.sleep(10 if ok else 5)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the Wi-Fi/LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()



# ---------- the ways your phone can reach this PC: Wi-Fi, or the phone's own USB / Bluetooth tethering ----------
# Turning on USB or Bluetooth tethering on the phone makes a little private network between the two.
# The phone hears about every address the PC has, and switches by itself when one stops working.

links = []   # [{"ip", "net", "kind": wifi/lan/cable/bluetooth, "public"}]
TETHER_RE = re.compile(r"remote ndis|rndis|apple mobile device|usb.*(ethernet|ncm)|android|\bncm\b", re.I)
VIRTUAL_RE = re.compile(r"hyper-v|vmware|virtualbox|vethernet|wsl|tap-|wintun|wireguard|warp|vpn|loopback|npcap|zerotier|tailscale|hamachi", re.I)
LINKS_PS = r"""
$o = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object {
    $a = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue
    $p = Get-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue
    [pscustomobject]@{ ip = $_.IPAddress; prefix = $_.PrefixLength; desc = [string]$a.InterfaceDescription;
                       media = [string]$a.PhysicalMediaType; category = [string]$p.NetworkCategory } }
ConvertTo-Json -Compress -InputObject @($o)
"""


def find_links():
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", LINKS_PS],
                         capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30).stdout
    data = json.loads(out or "[]")
    found = []
    for d in data if isinstance(data, list) else [data]:
        desc = f"{d.get('desc', '')} {d.get('media', '')}"
        if not d.get("ip") or VIRTUAL_RE.search(desc):
            continue
        kind = ("cable" if TETHER_RE.search(desc) else "bluetooth" if "bluetooth" in desc.lower()
                else "wifi" if re.search(r"wi-?fi|wireless|802\.11|wlan", desc, re.I) else "lan")
        found.append({"ip": d["ip"], "net": str(ipaddress.ip_network(f"{d['ip']}/{d.get('prefix', 24)}", strict=False)),
                      "kind": kind, "public": d.get("category") == "Public"})
    order = {"wifi": 0, "lan": 1, "cable": 2, "bluetooth": 3}
    return sorted(found, key=lambda l: order[l["kind"]])


def links_loop():
    while True:
        try:
            links[:] = find_links()
            stats["links"] = [{"ip": l["ip"], "kind": l["kind"]} for l in links if not blocked_link(l)]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(20)


def blocked_link(link):
    """Public networks (cafes, airports) are never served, except your own phone's cable or Bluetooth link."""
    return link["public"] and link["kind"] not in ("cable", "bluetooth")


def blocked_client(ip):
    if ip.startswith("127."):
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return any(blocked_link(l) for l in links if addr in ipaddress.ip_network(l["net"]))
    except ValueError:
        return False


def own_origin(origin):
    """Is this one of Desk Buddy's own pages, reached at another of this PC's addresses?"""
    try:
        u = urllib.parse.urlsplit(origin)
        port = u.port
    except ValueError:
        return False
    hosts = {l["ip"] for l in links} | {lan_ip(), "localhost", "127.0.0.1"} | {p["ip"] for p in sync_state["peers"].values()}
    return u.scheme in ("http", "https") and u.hostname in hosts and port in (PORT, HTTPS_PORT)

def phone_url():
    return f"http://{lan_ip()}:{PORT}"


def secure_phone_url():
    return f"https://{lan_ip()}:{HTTPS_PORT}"


def ensure_certificate():
    """A self-made certificate for the secure link. Phones show a one-time warning for it (normal on
    your own Wi-Fi); after that they allow motion sensors and the mic. Remade if the PC's address changes."""
    ip = lan_ip()
    try:
        from cryptography import x509
        if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
            with open(CERT_FILE, "rb") as f:
                san = x509.load_pem_x509_certificate(f.read()).extensions.get_extension_for_class(x509.SubjectAlternativeName)
            if ipaddress.ip_address(ip) in san.value.get_values_for_type(x509.IPAddress):
                return True
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Desk Buddy")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName("localhost")]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .sign(key, hashes.SHA256()))
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(KEY_FILE, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with open(CERT_FILE, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return True
    except Exception:
        return False


class SecureServer(ThreadingHTTPServer):
    """The same web server over https. The TLS handshake happens in each request's own thread,
    so a phone that's still deciding about the certificate warning can't hold anything up."""

    def get_request(self):
        sock, addr = super().get_request()
        return self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass   # phones refusing the self-made certificate are expected, not errors


def start_secure_server():
    if not ensure_certificate():
        return
    try:
        server = SecureServer(("0.0.0.0", HTTPS_PORT), Handler)
        server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.tls.load_cert_chain(CERT_FILE, KEY_FILE)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        pass


# What the phone reports it was allowed to use (shown in the dashboard).
phone_caps = {"secure": False, "motion": "off", "mic": "off", "at": 0}


LOCATION_FILE = os.path.join(DATA_DIR, "location.json")
wake_life = threading.Event()


def web_context():
    """Some Windows installs carry expired root certificates, so prefer certifi's fresh bundle."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"DeskBuddy/{VERSION}"})
    with urllib.request.urlopen(req, timeout=15, context=web_context()) as r:
        return json.loads(r.read().decode())


def find_location(city=None):
    """Your city: typed in the dashboard, or guessed once a day from your internet connection (ipwho.is)."""
    if city:
        hit = (fetch_json("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(city)).get("results") or [None])[0]
        if not hit:
            raise ValueError(f"Couldn't find {city}")
        return {"lat": hit["latitude"], "lon": hit["longitude"], "city": hit["name"], "manual": True, "at": time.time()}
    data = fetch_json("https://ipwho.is/")
    if not data.get("success"):
        return None
    return {"lat": data["latitude"], "lon": data["longitude"], "city": data.get("city", ""), "manual": False, "at": time.time()}


def fetch_weather(loc):
    """Current weather from Open-Meteo (free, no account): only your city's coordinates are sent."""
    w = fetch_json(f"https://api.open-meteo.com/v1/forecast?latitude={loc['lat']}&longitude={loc['lon']}"
                   "&current=temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m"
                   "&daily=sunrise,sunset&timezone=auto&forecast_days=1")
    cur, daily = w["current"], w["daily"]
    minutes = lambda iso: int(iso[11:13]) * 60 + int(iso[14:16])
    return {"kind": brain.weather_kind(cur["weather_code"]), "code": cur["weather_code"], "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]), "wind": round(cur["wind_speed_10m"]), "isDay": bool(cur["is_day"]),
            "sunrise": minutes(daily["sunrise"][0]), "sunset": minutes(daily["sunset"][0]), "at": time.time()}


def life_loop():
    """Weather, season, holidays and the money cycle, so the buddy can feel the day with you."""
    loc, weather = load_json(LOCATION_FILE, {}) or None, None
    while True:
        try:
            if prefs["weather"]:
                if not loc or (not loc.get("manual") and time.time() - loc.get("at", 0) > 86400):
                    loc = find_location() or loc
                    if loc:
                        save_json(LOCATION_FILE, loc)
                if loc and (not weather or time.time() - weather["at"] > 1200):
                    weather = fetch_weather(loc)
        except Exception:
            pass   # offline: keep what we had
        if os.path.exists(LOCATION_FILE):
            loc = load_json(LOCATION_FILE, {}) or loc   # the dashboard may have changed the city
        stats["life"] = brain.life_context(datetime.date.today(), prefs, config, loc, weather)
        if wake_life.wait(60):
            wake_life.clear()
            if loc and time.time() - loc.get("at", 0) < 5:
                weather = None   # a new city: fetch its weather now


def api_life(data):
    """Payday, birthday and city (from the dashboard's Life page)."""
    if "payday" in data:
        day = int(data["payday"] or 0)
        config["payday"] = 99 if day >= 99 else max(0, min(31, day))
    if "birthday" in data:
        bday = str(data["birthday"] or "").strip()
        if bday and not re.fullmatch(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", bday):
            raise ValueError("Birthday should look like MM-DD, e.g. 03-27")
        config["birthday"] = bday
    save_json(CONFIG_FILE, config)
    if "city" in data:
        city = str(data["city"] or "").strip()
        try:
            save_json(LOCATION_FILE, find_location(city) if city else (find_location() or {}))
        except OSError:
            raise ValueError("Couldn't look up that city right now")
    wake_life.set()
    return "Saved"


TYPING_KEYS = [0x20] + list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + [0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xDE]


def keys_loop():
    """Counts key presses as plain numbers, never which key and never any text: Backspace for Cheeky
    mode's typo jokes (Spicy only), and letters/numbers/space for the typing face (presses per second)."""
    count, was_back, held, presses = 0, False, set(), deque()
    while True:
        typing_on, spicy = prefs["typing"], prefs["cheeky"] >= 3
        if not typing_on and not spicy:
            stats["typing"] = 0
            time.sleep(1)
            continue
        now = time.time()
        if spicy:
            down = bool(user32.GetAsyncKeyState(0x08) & 0x8000)   # VK_BACK
            if down and not was_back:
                count += 1
                stats["backspaces"] = count
            was_back = down
        if typing_on:
            down_now = {k for k in TYPING_KEYS if user32.GetAsyncKeyState(k) & 0x8000}
            presses.extend(now for _ in down_now - held)
            held = down_now
            while presses and now - presses[0] > 3:
                presses.popleft()
            stats["typing"] = round(len(presses) / 3, 1)
        time.sleep(0.03)


# ---------- icons, app manifest, pairing page ----------

def robot_image(n, eye_scale=1.0):
    """The robot's two glowing eyes on black - used for the tray, the .exe and the phone app icon."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (n, n), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    w, h, r = 0.17 * n * eye_scale, 0.25 * n * eye_scale, 0.06 * n * eye_scale
    for cx in (0.5 * n - 0.15 * n * eye_scale, 0.5 * n + 0.15 * n * eye_scale):
        draw.rounded_rectangle([cx - w / 2, n / 2 - h / 2, cx + w / 2, n / 2 + h / 2],
                               radius=r, fill=(90, 240, 255, 255))
    return img


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


MANIFEST = json.dumps({
    "name": APP_NAME,
    "short_name": APP_NAME,
    "start_url": "/",
    "scope": "/",
    "display": "fullscreen",
    "orientation": "landscape",
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [
        {"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": p}
        for n in (192, 512) for p in ("any", "maskable")
    ],
}).encode()

# Phones only offer "Install app" when the page has a service worker with a real fetch handler
# (Chrome ignores an empty one). It just passes requests through to the PC, so nothing is cached.
SERVICE_WORKER = b"""
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (new URL(e.request.url).pathname === '/events') return;  // leave the live stream alone
  e.respondWith(fetch(e.request));
});
"""

def qr_svg(text):
    import qrcode
    import qrcode.image.svg
    buf = io.BytesIO()
    qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=0).save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]  # drop the XML header so it can sit inside the page


# ---------- one buddy on all your PCs: sync through a shared folder and/or the same network ----------
# A sync code (like MOCHI-7K2P-91QX) ties your PCs together and encrypts everything that's shared. Over the
# same network the PCs find each other and swap directly; through a synced folder (Google Drive, OneDrive,
# Dropbox) each PC leaves its own encrypted file. What's shared: the buddy (friendship from every PC adds
# up), its feelings (they follow you to the PC you used last), what it learned about music/movies, and your
# activity. Gmail and Calendar logins stay on each PC.

SYNC_PORT = 8769
CODE_WORDS = ["MOCHI", "PIXEL", "NOVA", "ORBIT", "BEAN", "ZIGGY", "LUNA", "TOFU", "BLIP", "DAISY", "SPROUT", "KIKI", "BOLT", "NORI"]
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L mix-ups
sync_state = {"peers": {}, "folderAt": 0, "folderPeers": {}, "error": "", "code": None, "key": None, "group": None}
wake_sync = threading.Event()
last_here = {"at": time.time()}   # the last time you used this PC


def new_sync_code():
    part = lambda: "".join(random.SystemRandom().choice(CODE_CHARS) for _ in range(4))
    return f"{random.SystemRandom().choice(CODE_WORDS)}-{part()}-{part()}"


def sync_keys():
    """The encryption key and the group name, from the active person's sync code (cached)."""
    s = config.get("sync") or {}
    code = unprotect(s["code"]) if s.get("code") else ""
    if code != sync_state["code"]:
        norm = re.sub(r"[^A-Z0-9]", "", code.upper()).encode()
        key = hashlib.pbkdf2_hmac("sha256", norm, b"deskbuddy-sync", 200_000) if norm else None
        sync_state.update(code=code, key=key, group=hashlib.sha256(b"group" + key).hexdigest()[:16] if key else None)
    return sync_state["key"], sync_state["group"]


def seal(obj, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, json.dumps(obj).encode(), b"deskbuddy-sync")).decode()


def unseal(text, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(text)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], b"deskbuddy-sync"))


def make_bundle():
    with bond_lock:
        b = {k: buddy.get(k) for k in ("name", "personality", "gender", "met", "lastDay", "edited", "epoch", "pcPoints")}
        b["heart"] = heart.save()
    others = load_json(OTHERS_FILE, {})
    activity = {**others, PC_ID: {"name": PC_NAME, "at": time.time(), "days": tracker.recent(14)}}
    return {"v": 1, "pc": PC_ID, "pcName": PC_NAME, "at": time.time(), "activeAt": last_here["at"],
            "addrs": own_addresses(), "port": PORT,
            "pin": pin(), "pinAt": (config.get("sync") or {}).get("pinAt", 0),
            "buddy": b, "learned": learner.data(), "activity": activity}


def merge_bundle(r):
    """Fold another PC's copy into ours. Safe to repeat: every rule gives the same result twice."""
    if not isinstance(r, dict) or r.get("pc") == PC_ID:
        return
    rb = r.get("buddy") or {}
    with bond_lock:
        if (config.get("sync") or {}).get("adopt") and rb.get("personality") in PERSONALITIES:
            # Just joined: this PC takes on the buddy you already have (it keeps the friendship it earned here).
            buddy.update(name=str(rb.get("name") or buddy["name"])[:20], personality=rb["personality"],
                         gender=rb.get("gender") if rb.get("gender") in GENDERS else buddy["gender"],
                         edited=rb.get("edited") or time.time(), epoch=rb.get("epoch") or 0, met=rb.get("met") or buddy["met"])
            heart.personality = buddy["personality"]
            if isinstance(rb.get("heart"), dict):
                heart.adopt(rb["heart"])
            config["sync"]["adopt"] = False
            save_json(CONFIG_FILE, config)
        if (rb.get("epoch") or 0) > (buddy.get("epoch") or 0):   # the friendship was restarted over there
            buddy.update(epoch=rb["epoch"], met=rb.get("met") or buddy["met"], pcPoints={})
        if (rb.get("epoch") or 0) == (buddy.get("epoch") or 0):
            pts = buddy.setdefault("pcPoints", {})
            for pc, n in (rb.get("pcPoints") or {}).items():   # each PC only ever adds to its own count
                if pc != PC_ID and isinstance(n, (int, float)):
                    pts[pc] = max(pts.get(pc, 0), n)
            buddy["points"] = sum(pts.values())
        if (rb.get("edited") or 0) > (buddy.get("edited") or 0):   # renamed / new personality over there
            if rb.get("personality") in PERSONALITIES and rb.get("gender") in GENDERS and rb.get("name"):
                buddy.update(name=str(rb["name"])[:20], personality=rb["personality"], gender=rb["gender"], edited=rb["edited"])
                heart.personality = buddy["personality"]
        if rb.get("met") and rb["met"] < buddy["met"]:
            buddy["met"] = rb["met"]
        if (rb.get("lastDay") or "") > (buddy.get("lastDay") or ""):
            buddy["lastDay"] = rb["lastDay"]
        if (r.get("activeAt") or 0) > last_here["at"] and isinstance(rb.get("heart"), dict):
            heart.adopt(rb["heart"])   # you were over there more recently: it brings its feelings along
        buddy["heart"] = heart.save()
        save_json(BUDDY_FILE, buddy)
    if (r.get("pinAt") or 0) > (config.get("sync") or {}).get("pinAt", 0):   # which PC the phone shows, chosen over there
        set_pin(r.get("pin") or "", r["pinAt"], tell_others=False)
    learner.merge(r.get("learned"))
    others = load_json(OTHERS_FILE, {})
    for pc, entry in (r.get("activity") or {}).items():
        if pc != PC_ID and isinstance(entry, dict) and entry.get("at", 0) > others.get(pc, {}).get("at", 0):
            others[pc] = entry
    save_json(OTHERS_FILE, others)
    tracker.others = {pc: e.get("days", {}) for pc, e in others.items()}
    stats["buddy"] = buddy_info()


def synced_folders():
    """Folders that sync to the cloud on their own: Google Drive for desktop, OneDrive, Dropbox."""
    home, found = os.path.expanduser("~"), []
    drive = [f"{c}:\\My Drive" for c in "DEFGHIJKLMNOPQRSTUVWXYZ"] + [os.path.join(home, "My Drive"), os.path.join(home, "Google Drive")]
    for path in drive:
        if os.path.isdir(path):
            found.append({"name": "Google Drive", "path": path})
            break
    if os.environ.get("OneDrive") and os.path.isdir(os.environ["OneDrive"]):
        found.append({"name": "OneDrive", "path": os.environ["OneDrive"]})
    for info in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Dropbox", "info.json"), os.path.join(os.environ.get("APPDATA", ""), "Dropbox", "info.json")):
        path = (load_json(info, {}).get("personal") or {}).get("path")
        if path and os.path.isdir(path):
            found.append({"name": "Dropbox", "path": path})
            break
    return found


def folder_sync(key, group):
    root = (config.get("sync") or {}).get("folder")
    if not root or not os.path.isdir(root):
        return
    folder = os.path.join(root, "Desk Buddy", group)
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):
        if name.endswith(".dbsync") and name != PC_ID + ".dbsync":
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    r = unseal(f.read(), key)
                merge_bundle(r)
                k = known_peers().setdefault(r["pc"], {"name": r.get("pcName", "?"), "port": r.get("port", PORT)})
                if k.get("ips") != r.get("addrs"):   # its addresses, to check in directly when you're on the same network
                    k["ips"] = [ip for ip in r.get("addrs") or [] if isinstance(ip, str)][:6]
                    save_json(CONFIG_FILE, config)
                sync_state["folderPeers"][r["pc"]] = {"name": r.get("pcName", "?"), "at": r.get("at", 0)}
            except Exception:
                continue   # half-downloaded, or from an old code
    tmp = os.path.join(folder, PC_ID + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(seal(make_bundle(), key))
    os.replace(tmp, os.path.join(folder, PC_ID + ".dbsync"))
    sync_state["folderAt"] = time.time()


def sync_loop():
    while True:
        try:
            key, group = sync_keys()
            if key:
                folder_sync(key, group)
            sync_state["error"] = ""
        except Exception as e:
            sync_state["error"] = f"Couldn't use the sync folder ({e.__class__.__name__})"
        wake_sync.wait(60)
        wake_sync.clear()


def sync_with_peer(peer):
    key, _ = sync_keys()
    try:
        req = urllib.request.Request(f"http://{peer['ip']}:{peer['port']}/sync", data=seal(make_bundle(), key).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            back = unseal(r.read().decode(), key)
        merge_bundle(back)
        peer["synced"] = time.time()
        note_peer(back.get("pc"), peer["ip"], back.get("port"), back.get("pcName"), back.get("activeAt"))
    except Exception:
        pass


def lan_sync_loop():
    """Says hello on the local network every 20 s; PCs with the same sync code swap their copies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("0.0.0.0", SYNC_PORT))
    except OSError:
        return
    sock.settimeout(1)
    next_hello = 0
    while True:
        now = time.time()
        key, group = sync_keys()
        on = key and (config.get("sync") or {}).get("lan", True)
        if on and (now > next_hello or hello_now.is_set()):
            next_hello = now + 20
            hello_now.clear()
            hello = json.dumps({"app": "deskbuddy", "g": group, "pc": PC_ID, "name": PC_NAME, "port": PORT,
                                "active": last_here["at"]}).encode()
            targets = {"255.255.255.255"} | {str(ipaddress.ip_network(l["net"]).broadcast_address) for l in links if not l["public"]}
            for t in targets:
                try:
                    sock.sendto(hello, (t, SYNC_PORT))
                except OSError:
                    pass
        try:
            data, (ip, _) = sock.recvfrom(4096)
            msg = json.loads(data)
        except (socket.timeout, ValueError, OSError):
            continue
        if not on or msg.get("app") != "deskbuddy" or msg.get("g") != group or msg.get("pc") == PC_ID or blocked_client(ip):
            continue
        note_peer(msg["pc"], ip, msg.get("port"), msg.get("name"), msg.get("active"))


# ---------- the phone follows you to your other PC (it stays connected here; this PC relays) ----------
# When you move to another of your PCs on this network, this PC passes that PC's live feed on to the
# phone, and passes your pets, sounds and taps back. The phone never changes address, so nothing to
# reconnect, no certificate to accept again. Touch this PC and it switches back at once.

relay = {"peer": None, "msg": None, "at": 0.0, "gen": 0}
hello_now = threading.Event()   # you just came back to this PC: tell the others right away

# ---------- finding your other PCs, and which one the phone shows ----------
# Many Wi-Fi routers block broadcasts between devices, so PCs don't rely on them alone: once two PCs
# have met (a broadcast, a direct sync, or the shared folder), each remembers the other's address and
# checks in directly every few seconds.

def known_peers():
    return (config.get("sync") or {}).setdefault("known", {})


def note_peer(pc, ip, port, name, active=None):
    if not pc or pc == PC_ID or not ip:
        return None
    now = time.time()
    peer = sync_state["peers"].setdefault(pc, {"synced": 0})
    peer.update(name=str(name or "?")[:40], ip=ip, port=int(port or PORT), seen=now)
    if active:
        peer["active"] = max(peer.get("active", 0), float(active))
    known = known_peers()
    if known.get(pc, {}).get("ip") != ip or known[pc].get("name") != peer["name"]:
        known[pc] = {"ip": ip, "port": peer["port"], "name": peer["name"], "ips": known.get(pc, {}).get("ips", [])}
        save_json(CONFIG_FILE, config)
    if now - peer["synced"] > 60:
        peer["synced"] = now
        threading.Thread(target=sync_with_peer, args=(peer,), daemon=True).start()
    return peer


def own_addresses():
    return [l["ip"] for l in links if not l["public"]] or [lan_ip()]


def peer_poll_loop():
    """Checks in with each PC you've synced with, every few seconds: is it on, and when was it last used?"""
    while True:
        key, group = sync_keys()
        if key:
            for pc, k in list(known_peers().items()):
                for ip in dict.fromkeys([k.get("ip")] + k.get("ips", [])):
                    if not ip:
                        continue
                    try:
                        with urllib.request.urlopen(f"http://{ip}:{k.get('port', PORT)}/hello?g={group}", timeout=2) as r:
                            h = json.loads(r.read())
                        if h.get("pc") == pc:
                            note_peer(pc, ip, h.get("port"), h.get("name"), h.get("active"))
                            break
                    except Exception:
                        continue
        time.sleep(4)


def pin():
    """Which PC the phone shows: "" = the one you're using (automatic), or a PC's id."""
    return (config.get("sync") or {}).get("pin", "")


def set_pin(value, at=None, tell_others=True):
    s = config.setdefault("sync", {})
    s.update(pin=value or "", pinAt=at or time.time())
    save_json(CONFIG_FILE, config)
    if tell_others:
        key, _ = sync_keys()
        body = seal({"pin": s["pin"], "at": s["pinAt"]}, key).encode() if key else None
        def tell(p):
            try:
                urllib.request.urlopen(urllib.request.Request(f"http://{p['ip']}:{p['port']}/pin", data=body, method="POST"), timeout=3).read()
            except Exception:
                pass   # it's off; it'll hear about it next time they sync

        for p in list(sync_state["peers"].values()) if body else []:
            threading.Thread(target=tell, args=(p,), daemon=True).start()


def pc_choices():
    """For the phone and the dashboard: your PCs, which one the phone shows, and whether it's chosen by hand."""
    key, _ = sync_keys()
    if not key or not known_peers():
        return None
    now = time.time()
    showing = relay["peer"]["name"] if relayed() else PC_NAME
    pcs = [{"id": PC_ID, "name": PC_NAME, "here": True, "ok": True}]
    pcs += [{"id": pc, "name": k.get("name", "?"), "here": False,
             "ok": now - sync_state["peers"].get(pc, {}).get("seen", 0) < 15} for pc, k in known_peers().items()]
    return {"list": pcs, "pin": pin(), "showing": showing}


def follow_target(now):
    if not sync_state["key"]:
        return None
    chosen = pin()
    if chosen:   # you picked a PC: show that one whatever you're doing (if it's on)
        p = sync_state["peers"].get(chosen)
        return p if p and now - p.get("seen", 0) < 15 and now - p.get("failAt", 0) > 10 else None
    if stats["idle"] < 3:
        return None   # you're using this PC
    best, best_act = None, last_here["at"] + 2
    for p in list(sync_state["peers"].values()):
        if now - p.get("seen", 0) > 15 or now - p.get("failAt", 0) < 30:
            continue
        act = p.get("active", 0)
        if p is relay["peer"] and relay["msg"] and now - relay["at"] < 5:
            act = max(act, now - relay["msg"].get("idle", 0))   # live from its feed
        if act > best_act:
            best, best_act = p, act
    if best is not None and (stats["idle"] >= 8 or best is relay["peer"]):
        return best
    return None


def relay_reader(peer, gen):
    try:
        req = urllib.request.Request(f"http://{peer['ip']}:{peer['port']}/events?relay=1")
        with urllib.request.urlopen(req, timeout=10) as r:
            for line in r:
                if relay["gen"] != gen:
                    return
                if line.startswith(b"data: "):
                    relay["msg"], relay["at"] = json.loads(line[6:]), time.time()
    except Exception:
        pass
    if relay["gen"] == gen:   # couldn't reach it: stay here for a bit
        peer["failAt"] = time.time()
        relay.update(peer=None, msg=None, gen=gen + 1)


def relay_loop():
    while True:
        target = follow_target(time.time()) if phones else None
        if target is not relay["peer"]:
            relay.update(peer=target, msg=None, gen=relay["gen"] + 1)
            if target:
                threading.Thread(target=relay_reader, args=(target, relay["gen"]), daemon=True).start()
        time.sleep(0.3)


def relayed():
    """The other PC's latest frame, if the phone is following you there right now."""
    p = relay["peer"]
    return relay["msg"] if p and relay["msg"] and time.time() - relay["at"] < 3 else None


def sync_status():
    key, _ = sync_keys()
    s, now = config.get("sync") or {}, time.time()
    peers = [{"name": p["name"], "via": "same network", "ago": round(now - p.get("synced", 0)) if p.get("synced") else None}
             for p in sync_state["peers"].values() if now - p.get("seen", 0) < 120]
    peers += [{"name": p["name"], "via": "synced folder", "ago": round(now - p["at"])} for p in sync_state["folderPeers"].values()]
    return {"on": bool(key), "lan": s.get("lan", True), "pcs": pc_choices(), "folder": s.get("folder", ""), "peers": peers, "error": sync_state["error"],
            "following": relay["peer"]["name"] if relayed() else None,
            "folderAt": sync_state["folderAt"], "pcName": PC_NAME}


def api_sync(data):
    action = data.get("action")
    s = config.setdefault("sync", {})
    if action in ("start", "join"):
        code = new_sync_code() if action == "start" else str(data.get("code", "")).strip().upper()
        if len(re.sub(r"[^A-Z0-9]", "", code)) < 10:
            raise ValueError("That code looks too short. It's on the other PC: Desk Buddy > Sync")
        folders = synced_folders()
        s.update(code=protect(code), lan=True, folder=s.get("folder") or (folders[0]["path"] if folders else ""),
                 adopt=action == "join")
        msg = "Sync is on! Type this code on your other PC: " + code if action == "start" else "Joined! Syncing now…"
    elif action == "folder":
        path = str(data.get("path", "")).strip().strip('"')
        if path and not os.path.isdir(path):
            raise ValueError("That folder doesn't exist")
        s["folder"] = path
        msg = "Saved"
    elif action == "lan":
        s["lan"] = bool(data.get("on"))
        msg = "Saved"
    elif action == "pin":
        choice = str(data.get("pin", ""))
        if choice not in ("", PC_ID) and choice not in known_peers():
            raise ValueError("That PC isn't one of yours")
        set_pin(choice)
        return ("The phone follows you to whichever PC you're using" if not choice else
                "The phone now shows this PC" if choice == PC_ID else f"The phone now shows {known_peers()[choice].get('name', 'that PC')}")
    elif action == "stop":
        config["sync"] = {}
        sync_state.update(peers={}, folderPeers={})
        msg = "Sync is off on this PC"
    else:
        raise ValueError("Unknown action")
    save_json(CONFIG_FILE, config)
    wake_sync.set()
    return msg


def sync_code_text():
    key, _ = sync_keys()
    return sync_state["code"] if key else ""

# ---------- the dashboard's API (only answers this PC) ----------

phone_tools = {"state": None, "at": 0}   # the phone's timer/stopwatch/focus, so the dashboard can show them


def live_status():
    now = time.time()
    caps = phone_caps if now - phone_caps["at"] < 120 else None
    media = stats["media"] or {}
    return {"phones": phones, "buddy": buddy_info(), "phoneCaps": caps, **services,
            "tools": phone_tools["state"] if now - phone_tools["at"] < 5 else None,
            "nowPlaying": {**(stats["mediaKind"] or {}), "playing": bool(media.get("playing"))} if media else None,
            "activity": stats["activity"], "profile": stats["profile"], "profiles": profile_list(), "life": stats["life"],
            "heart": stats["heart"], "sync": sync_status(), "links": stats["links"]}


def teach_media(data):
    """You corrected music vs movie: remember it for this title, or everything from this channel/app."""
    learner.teach(data.get("kind"), "source" if data.get("scope") == "source" else "title", stats["media"], stats["title"])
    stats["mediaKind"] = learner.classify(stats["media"], stats["title"])
    return {"music": "Got it: that's music 🎵", "movie": "Got it: that's a movie 🍿", "ignore": "Got it: I'll leave that one alone"}[data["kind"]]


def api_media(data):
    if data.get("forget"):
        learner.forget(data["forget"], str(data.get("key", "")))
        stats["mediaKind"] = learner.classify(stats["media"], stats["title"])
        return "Forgotten"
    return teach_media(data)


TOOL_ACTIONS = {"timer.start", "timer.pause", "timer.resume", "timer.cancel", "watch.start", "watch.stop", "watch.reset",
                "pomo.start", "pomo.pause", "pomo.resume", "pomo.skip", "pomo.stop", "breathe.start", "dice.roll",
                "coin.flip", "clock.show", "tool.close"}


def api_tool(data):
    """Run a tool on the phone from the dashboard (the phone does the timing, shows it and rings)."""
    if data.get("action") not in TOOL_ACTIONS:
        raise ValueError("Unknown tool action")
    if not phones:
        raise ValueError("Your phone isn't connected right now")
    # A short queue (not a single slot), so two quick clicks both reach the phone.
    queue = stats["cmd"] or []
    n = queue[-1]["n"] + 1 if queue else 1
    stats["cmd"] = (queue + [{"n": n, "action": data["action"], "minutes": int(data.get("minutes") or 0),
                              "show": bool(data.get("show", True))}])[-10:]
    return "Sent to your phone"


def profile_list():
    return [{"id": p["id"], "name": p["name"], "emoji": p["emoji"], "hasPassword": bool(p["check"])} for p in profiles["list"]]


def api_profile(data):
    """Switch, add, rename, change the password of, or delete a person's profile."""
    action, active = data.get("action"), profile_entry(profiles["active"])
    password = str(data.get("password", ""))
    if action == "switch":
        entry = profile_entry(data.get("id"))
        if not entry:
            raise ValueError("That profile doesn't exist")
        if not check_password(entry, password):
            time.sleep(1)   # slows down guessing
            raise ValueError("Wrong password")
        load_profile(entry["id"], derive(password, entry["salt"], "key") if entry["check"] else None)
        work_session["start"] = time.time()
        return f"Hi {entry['name']}! 👋"
    if action == "add":
        name = str(data.get("name", "")).strip()[:24]
        if not name:
            raise ValueError("Give the new profile a name")
        if len(password) < 4:
            raise ValueError("Use a password of at least 4 characters")
        salt = base64.b64encode(os.urandom(16)).decode()
        entry = {"id": "p" + os.urandom(3).hex(), "name": name, "emoji": str(data.get("emoji") or "🙂")[:4],
                 "salt": salt, "check": base64.b64encode(derive(password, salt, "check")).decode()}
        profiles["list"].append(entry)
        load_profile(entry["id"], derive(password, salt, "key"))
        work_session["start"] = time.time()
        return f"Welcome, {name}! Meet your new buddy 🤖"
    if action == "rename":
        active["name"] = str(data.get("name", "")).strip()[:24] or active["name"]
        active["emoji"] = str(data.get("emoji") or active["emoji"])[:4]
        save_json(PROFILES_FILE, profiles)
        stats["profile"] = {**(stats["profile"] or {}), "name": active["name"], "emoji": active["emoji"]}
        return "Saved"
    if action == "password":
        global profile_key
        if not check_password(active, str(data.get("old", ""))):
            time.sleep(1)
            raise ValueError("Your current password isn't right")
        new = str(data.get("new", ""))
        if new and len(new) < 4:
            raise ValueError("Use a password of at least 4 characters")
        secrets = {k: unprotect(config[k]) for k in ("gmail_password", "calendar_url")}   # re-lock them with the new password
        active["salt"] = base64.b64encode(os.urandom(16)).decode()
        active["check"] = base64.b64encode(derive(new, active["salt"], "check")).decode() if new else ""
        profile_key = derive(new, active["salt"], "key") if new else None
        for k, v in secrets.items():
            config[k] = protect(v)
        save_json(CONFIG_FILE, config)
        profiles["keys"] = {active["id"]: windows_lock(profile_key)} if profile_key else {}
        save_json(PROFILES_FILE, profiles)
        return "Password changed" if new else "Password removed"
    if action == "delete":
        entry = profile_entry(data.get("id"))
        if not entry or len(profiles["list"]) < 2:
            raise ValueError("You can't delete the only profile")
        if not check_password(entry, password):
            time.sleep(1)
            raise ValueError("Wrong password")
        profiles["list"].remove(entry)
        shutil.rmtree(os.path.join(PROFILE_ROOT, entry["id"]), ignore_errors=True)
        if entry["id"] == profiles["active"]:   # deleted yourself: the next profile takes over, locked until unlocked
            nxt = profiles["list"][0]
            load_profile(nxt["id"], None)
        save_json(PROFILES_FILE, profiles)
        return f"{entry['name']}'s profile was deleted"
    raise ValueError("Unknown profile action")


def api_insights():
    return {"summary": tracker.summary(), "now": stats["activity"], "learned": learner.rules(),
            "categories": {k: v for k, v in brain.INFO.items()}, "productive": sorted(brain.PRODUCTIVE)}


def api_state():
    url = phone_url()
    return {
        **live_status(), "version": VERSION, "installed": FROZEN, "onboarded": config["onboarded"],
        "personalities": PERSONALITIES, "genders": GENDERS, "prefs": prefs, "phoneUrl": url, "qr": qr_svg(url),
        "secureUrl": secure_phone_url(), "secureQr": qr_svg(secure_phone_url()),
        "gmailUser": config["gmail_user"], "gmailSaved": bool(config["gmail_password"]),
        "calendarSaved": bool(config["calendar_url"]), "startup": os.path.exists(STARTUP_LNK),
        "payday": config.get("payday", 0), "birthday": config.get("birthday", ""),
        "syncCode": sync_code_text(), "folders": synced_folders(),
        "errorLog": open(ERROR_LOG, encoding="utf-8", errors="replace").read()[-4000:] if os.path.exists(ERROR_LOG) else "",
        "linkQr": {l["ip"]: qr_svg(f"http://{l['ip']}:{PORT}") for l in stats["links"]},
    }


def api_buddy(data):
    with bond_lock:
        name = str(data.get("name", "")).strip()[:20]
        if name:
            buddy["name"] = name
        if data.get("gender") in GENDERS:
            buddy["gender"] = data["gender"]
        if data.get("personality") in PERSONALITIES:
            buddy["personality"] = heart.personality = data["personality"]
        if name or data.get("gender") in GENDERS or data.get("personality") in PERSONALITIES:
            buddy["edited"] = time.time()   # newest change wins on your other PCs
        if data.get("reset"):
            buddy.update(points=0, met=datetime.date.today().isoformat(), todayDate="", today={}, awayDays=0,
                         pcPoints={}, epoch=time.time())
            heart.__init__({}, buddy["personality"])
        if data.get("care") in brain.CARE:   # the dashboard's "give it some love" buttons
            heart.care(data["care"], time.time())
        buddy["heart"] = heart.save()
        save_json(BUDDY_FILE, buddy)
    stats["heart"] = heart.info()
    return "Saved"


def api_accounts(data):
    action = data.get("action", "save")
    if action == "forget_gmail":
        config.update(gmail_user="", gmail_password="")
        msg = "Gmail disconnected"
    elif action == "forget_calendar":
        config["calendar_url"] = ""
        msg = "Calendar disconnected"
    else:
        url = str(data.get("calendar_url", "")).strip()
        if url and not url.startswith("https://"):
            raise ValueError("That calendar address should start with https://")
        if url:
            config["calendar_url"] = protect(url)
        if "gmail_user" in data:
            config["gmail_user"] = str(data["gmail_user"]).strip()
        password = str(data.get("gmail_password", "")).replace(" ", "")
        if password:
            config["gmail_password"] = protect(password)
        msg = "Saved! Checking your accounts…"
    save_json(CONFIG_FILE, config)
    services["gmail"] = services["calendar"] = "Checking…"
    wake_mail.set()
    wake_calendar.set()
    return msg


def api_startup(data):
    if FROZEN:
        if data.get("on") and not os.path.exists(STARTUP_LNK):
            make_shortcut(STARTUP_LNK, INSTALLED_EXE, "--startup")
        elif not data.get("on") and os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
    return "Saved"


def api_test_sound(data):
    if data.get("where") == "pc":
        threading.Thread(target=play_on_pc, args=(CHIRP,), daemon=True).start()
    else:
        stats["ping"] += 1   # the phone sees this in its stream and chirps
    return "Playing"


def api_onboarded(_data):
    config["onboarded"] = True
    save_json(CONFIG_FILE, config)
    return "Saved"


def api_uninstall(_data):
    if FROZEN:
        launch_detached([INSTALLED_EXE, "--uninstall"])
    return "Uninstalling"


API = {
    "prefs": lambda d: (update_prefs(d), "Saved")[1], "buddy": api_buddy, "accounts": api_accounts, "sync": api_sync,
    "startup": api_startup, "test-sound": api_test_sound, "onboarded": api_onboarded, "uninstall": api_uninstall,
    "media": api_media, "tool": api_tool, "profile": api_profile, "life": lambda d: api_life(d),
}


# ---------- web server ----------

phones = 0
tray = None
_last_notice = 0.0


def phone_connected(delta, notify=True):
    global phones, _last_notice
    phones += delta
    if notify and delta > 0 and phones == 1 and tray and time.time() - _last_notice > 60:
        _last_notice = time.time()
        try:
            tray.notify("Your phone is connected 🤖", APP_NAME)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    static = {}

    def cors(self):
        # The phone may reach the PC at another address (cable, Bluetooth); allow only Desk Buddy's own pages.
        origin = self.headers.get("Origin")
        if origin and own_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def send_body(self, kind, body, cache=True):
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        local = self.client_address[0] == "127.0.0.1"
        if blocked_client(self.client_address[0]):
            return self.send_error(403)
        if path in self.static:
            self.send_body(*self.static[path])
        elif path in ("/", "/index.html"):
            # Read on every request so face.html can be tweaked during development with just a refresh.
            with open(os.path.join(RESOURCES, "face.html"), "rb") as f:
                self.send_body("text/html; charset=utf-8", f.read(), cache=False)
        elif path == "/app" and local:
            with open(os.path.join(RESOURCES, "dashboard.html"), "rb") as f:
                self.send_body("text/html; charset=utf-8", f.read(), cache=False)
        elif path in ("/pair", "/settings") and local:   # old links from earlier versions
            self.send_response(302)
            self.send_header("Location", "/app#" + ("phone" if path == "/pair" else "accounts"))
            self.end_headers()
        elif path == "/hello":   # another of your PCs checking in (same sync code only)
            key, group = sync_keys()
            if not key or urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("g", [""])[0] != group:
                return self.send_error(404)
            self.send_body("application/json", json.dumps({"pc": PC_ID, "name": PC_NAME, "port": PORT, "active": last_here["at"]}).encode(), cache=False)
        elif path == "/status":
            status = live_status() if local else {"phones": phones}
            self.send_body("application/json", json.dumps(status).encode(), cache=False)
        elif path == "/api/state" and local:
            self.send_body("application/json", json.dumps(api_state()).encode(), cache=False)
        elif path == "/api/insights" and local:
            self.send_body("application/json", json.dumps(api_insights()).encode(), cache=False)
        elif path == "/quit" and local:
            # Used by a newer DeskBuddy.exe to replace this one when updating.
            self.send_body("text/plain", b"bye")
            threading.Thread(target=quit_app, daemon=True).start()
        elif path == "/events":
            self.stream_events()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if blocked_client(self.client_address[0]):
            return self.send_error(403)
        limit = 4_000_000 if path == "/sync" else 20000
        body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), limit)).decode("utf-8", "replace")
        if path in ("/bond", "/sound", "/work-reset", "/media-correct") and relayed() and self.forward(path, body):
            return
        if path == "/sync":   # another of your PCs, with the same sync code
            key, _ = sync_keys()
            try:
                r = unseal(body, key)
            except Exception:
                return self.send_error(403)
            merge_bundle(r)
            note_peer(r.get("pc"), self.client_address[0], r.get("port"), r.get("pcName"), r.get("activeAt"))
            self.send_body("text/plain", seal(make_bundle(), key).encode(), cache=False)
        elif path == "/pin":   # you chose on another of your PCs which one the phone shows
            key, _ = sync_keys()
            try:
                p = unseal(body, key)
            except Exception:
                return self.send_error(403)
            if p.get("at", 0) > (config.get("sync") or {}).get("pinAt", 0):
                set_pin(p.get("pin", ""), p["at"], tell_others=False)
            self.send_body("application/json", b"{}", cache=False)
        elif path == "/follow":   # chosen on the phone
            try:
                choice = str(json.loads(body).get("pin", ""))
            except (ValueError, AttributeError):
                choice = None
            if choice is not None and (choice in ("", PC_ID) or choice in known_peers()):
                set_pin(choice)
            self.send_body("application/json", json.dumps({"ok": choice is not None}).encode(), cache=False)
        elif path == "/bond":
            # The phone reports petting and breaks taken; the PC keeps the friendship score.
            kind, ok = body.strip()[:20], True
            if kind == "break":
                add_points(kind)
            elif kind in brain.CARE:
                ok = give_care(kind)
            self.send_body("application/json", json.dumps({**buddy_info(), "ok": ok, "heart": stats["heart"]}).encode(), cache=False)
        elif path == "/media-correct":   # "that's music / a movie / neither" from the phone
            try:
                result = {"ok": True, "message": teach_media(json.loads(body))}
            except (ValueError, KeyError, AttributeError) as e:
                result = {"ok": False, "message": str(e)}
            self.send_body("application/json", json.dumps(result).encode(), cache=False)
        elif path == "/work-reset":   # tapped "I took a break" on the phone
            work_session["start"] = time.time()
            stats["work"] = 0
            self.send_body("application/json", b"{}", cache=False)
        elif path == "/phone-status":
            try:
                data = json.loads(body)
                if "tools" in data:   # the phone's running timers, for the dashboard's Tools page
                    phone_tools.update(state=data["tools"] if isinstance(data["tools"], dict) else None, at=time.time())
                else:
                    phone_caps.update(secure=bool(data.get("secure")), motion=str(data.get("motion", "off"))[:12],
                                      mic=str(data.get("mic", "off"))[:12], at=time.time())
            except (ValueError, AttributeError):
                pass
            self.send_body("application/json", b"{}", cache=False)
        elif path == "/sound":
            # The phone asks the PC to make the sound (when sound is set to PC or both).
            if prefs["soundOutput"] in ("pc", "both"):
                try:
                    events = json.loads(body).get("events", [])
                    threading.Thread(target=play_on_pc, args=(events,), daemon=True).start()
                except (ValueError, AttributeError):
                    pass
            self.send_body("application/json", b"{}", cache=False)
        elif path.startswith("/api/") and path[5:] in API and self.from_dashboard():
            try:
                data = json.loads(body or "{}")
                result = {"ok": True, "message": API[path[5:]](data if isinstance(data, dict) else {})}
            except ValueError as e:
                result = {"ok": False, "message": str(e)}
            self.send_body("application/json", json.dumps(result).encode(), cache=False)
        else:
            self.send_error(403)

    def from_dashboard(self):
        """Only the dashboard on this PC may change settings - not the network, not other websites."""
        source = self.headers.get("Origin") or ""
        return (self.client_address[0] == "127.0.0.1"
                and self.headers.get("Content-Type", "").startswith("application/json")
                and any(source == f"http://{host}:{PORT}" for host in ("localhost", "127.0.0.1")))

    def stream_events(self):
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        from_peer = "relay=1" in self.path   # another of your PCs passing this on to the phone: never relay back
        phone_connected(+1, notify=not from_peer)
        try:
            while True:
                other = None if from_peer else relayed()
                if other:   # you're on your other PC: show the phone that one
                    msg = dict(other, via=relay["peer"]["name"], links=stats["links"], profile=stats["profile"], pcs=stats["pcs"])
                else:
                    x, y = cursor_position()
                    msg = dict(stats, x=round(x, 3), y=round(y, 3), now=int(time.time() * 1000), via=None)   # "now": lets the phone line up with the beat
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
                time.sleep(1 / FPS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            phone_connected(-1)

    def forward(self, path, body):
        """The phone is following you on another PC: hand its pet / sound / tap to that PC."""
        p = relay["peer"]
        try:
            req = urllib.request.Request(f"http://{p['ip']}:{p['port']}{path}", data=body.encode(), method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                data, kind = r.read(), r.headers.get("Content-Type", "application/json")
        except Exception:
            return False
        self.send_body(kind, data, cache=False)
        return True

    def log_message(self, *args):
        pass


def port_in_use():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        path = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if base and os.path.exists(path):
            return path
    return None


def open_dashboard(page=""):
    """Opens the dashboard as its own app window (Edge's app mode: no tabs or address bar)."""
    url = f"http://localhost:{PORT}/app" + (f"#{page}" if page else "")
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1180,820"], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url)


def desktop_lnk():
    buf = ctypes.create_unicode_buffer(260)
    shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)   # the Desktop folder (even if OneDrive moved it)
    return os.path.join(buf.value or os.path.join(os.environ["USERPROFILE"], "Desktop"), "Desk Buddy.lnk")


# ---------- install / uninstall ----------

class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def run_as_admin(exe, params):
    """Run a command with a Windows admin (UAC) prompt and wait for it. False if the user says no."""
    info = SHELLEXECUTEINFO(cbSize=ctypes.sizeof(SHELLEXECUTEINFO), fMask=0x40,  # SEE_MASK_NOCLOSEPROCESS
                            lpVerb="runas", lpFile=exe, lpParameters=params, nShow=0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return False
    kernel32.WaitForSingleObject(info.hProcess, 60000)
    kernel32.CloseHandle(info.hProcess)
    return True


def allow_through_firewall(exe):
    # Already allowed (e.g. installing an update): skip the admin prompt.
    shown = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}", "verbose"],
                           capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    cable_rule, sync_rule = FIREWALL_RULE + "Cable", FIREWALL_RULE + "Sync"
    has = lambda rule: rule in subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
                                              capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    if os.path.normcase(exe) in os.path.normcase(shown) and str(HTTPS_PORT) in shown and has(cable_rule) and has(sync_rule):
        return
    # Only home/work ("private") networks - the robot never listens on public Wi-Fi. The second rule is for
    # the phone's own USB / Bluetooth tethering link, which Windows calls "public": wired-type links only, and
    # only from that link itself (the app also checks it really is a phone's tethering adapter).
    run_as_admin("cmd.exe",
                 f"/c netsh advfirewall firewall delete rule name={FIREWALL_RULE} >nul & "
                 f"netsh advfirewall firewall delete rule name={cable_rule} >nul & "
                 f"netsh advfirewall firewall delete rule name={sync_rule} >nul & "
                 f"netsh advfirewall firewall add rule name={sync_rule} dir=in action=allow "
                 f'protocol=UDP localport={SYNC_PORT} program="{exe}" profile=private,domain enable=yes & '
                 f"netsh advfirewall firewall add rule name={FIREWALL_RULE} dir=in action=allow "
                 f'protocol=TCP localport={PORT},{HTTPS_PORT} program="{exe}" profile=private,domain enable=yes & '
                 f"netsh advfirewall firewall add rule name={cable_rule} dir=in action=allow "
                 f'protocol=TCP localport={PORT},{HTTPS_PORT} program="{exe}" profile=public interfacetype=lan remoteip=localsubnet enable=yes')


def make_shortcut(lnk, target, args=""):
    q = lambda s: s.replace("'", "''")  # PowerShell single-quote escaping
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
          f"$s.TargetPath='{q(target)}';$s.Arguments='{q(args)}';"
          f"$s.WorkingDirectory='{q(os.path.dirname(target))}';$s.Description='{APP_NAME}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], creationflags=NO_WINDOW)


def register_in_apps_list():
    """Shows Desk Buddy in Windows Settings > Apps, with a working Uninstall button."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, value in {
            "DisplayName": APP_NAME,
            "DisplayVersion": VERSION,
            "Publisher": APP_NAME,
            "DisplayIcon": INSTALLED_EXE,
            "InstallLocation": INSTALL_DIR,
            "UninstallString": f'"{INSTALLED_EXE}" --uninstall',
        }.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def running_copies():
    """The installed Desk Buddy, if it's running (never this process or the launcher that started it)."""
    import psutil
    me, found = {os.getpid(), os.getppid()}, []
    for p in psutil.process_iter(["pid", "exe"]):
        try:
            if p.info["pid"] not in me and p.info["exe"] and os.path.normcase(p.info["exe"]) == os.path.normcase(INSTALLED_EXE):
                found.append(p)
        except Exception:
            continue
    return found


def stop_running_copy():
    """Ask the running Desk Buddy to quit; if it doesn't within 10 seconds, stop it."""
    import psutil
    if port_in_use():
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/quit", timeout=3).read()
        except Exception:
            pass
    deadline = time.time() + 10
    while time.time() < deadline and (port_in_use() or running_copies()):
        time.sleep(0.2)
    left = running_copies()
    for p in left:
        try:
            p.kill()
        except Exception:
            pass
    psutil.wait_procs(left, timeout=5)
    time.sleep(0.5)  # give Windows a moment to release the .exe file


def launch_detached(args):
    env = os.environ.copy()
    # A one-file .exe starting itself must not reuse this process's temporary unpack folder.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop("_MEIPASS2", None)
    subprocess.Popen(args, env=env, cwd=INSTALL_DIR, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP


def install():
    try:
        _install()
    except Exception as e:
        log_error("install", e)
        user32.MessageBoxW(None, f"Desk Buddy couldn't finish installing:\n{e}\n\nPlease double-click DeskBuddy.exe again.", APP_NAME, 0x30)


def _install():
    stop_running_copy()
    os.makedirs(INSTALL_DIR, exist_ok=True)
    fresh = INSTALLED_EXE + ".new"
    shutil.copy2(sys.executable, fresh)
    for attempt in range(40):   # Windows (or antivirus) can hold on to the old file for a few seconds
        try:
            os.replace(fresh, INSTALLED_EXE)
            break
        except PermissionError:
            time.sleep(0.5)
    else:
        raise RuntimeError("the old Desk Buddy is still running. Right-click the robot by the clock, choose Quit, and try again.")
    make_shortcut(STARTUP_LNK, INSTALLED_EXE, "--startup")   # quietly in the tray at every login
    make_shortcut(START_MENU_LNK, INSTALLED_EXE, "--open")   # these open the dashboard
    make_shortcut(desktop_lnk(), INSTALLED_EXE, "--open")
    register_in_apps_list()
    allow_through_firewall(INSTALLED_EXE)
    launch_detached([INSTALLED_EXE, "--welcome"])


def uninstall():
    stop_running_copy()
    for lnk in (STARTUP_LNK, START_MENU_LNK, desktop_lnk()):
        if os.path.exists(lnk):
            os.remove(lnk)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    run_as_admin("cmd.exe", f"/c netsh advfirewall firewall delete rule name={FIREWALL_RULE} & "
                            f"netsh advfirewall firewall delete rule name={FIREWALL_RULE}Cable & "
                            f"netsh advfirewall firewall delete rule name={FIREWALL_RULE}Sync")
    for _ in range(10):   # settings, saved passwords and friendship progress
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        if not os.path.exists(DATA_DIR):
            break
        time.sleep(0.5)
    # This .exe can't delete itself while running, so a hidden helper waits for it to exit, then removes the folder.
    pids = ",".join(str(p) for p in {os.getpid(), os.getppid()})
    folder = INSTALL_DIR.replace("'", "''")
    ps = (f"Wait-Process -Id {pids} -Timeout 120 -ErrorAction SilentlyContinue; Start-Sleep 1; "
          f"foreach ($i in 1..15) {{ Remove-Item -LiteralPath '{folder}' -Recurse -Force -ErrorAction SilentlyContinue; "
          f"if (-not (Test-Path -LiteralPath '{folder}')) {{ break }}; Start-Sleep 1 }}")
    helper = ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]
    try:   # break away from whatever started us, so the helper outlives it
        subprocess.Popen(helper, cwd=os.environ.get("TEMP", "C:\\"), creationflags=NO_WINDOW | 0x00000200 | 0x01000000)
    except OSError:
        subprocess.Popen(helper, cwd=os.environ.get("TEMP", "C:\\"), creationflags=NO_WINDOW | 0x00000200)
    user32.MessageBoxW(None, "Desk Buddy has been removed. Bye! 👋", APP_NAME, 0x40)


# ---------- the running app ----------

def quit_app():
    if tray:
        tray.stop()
    else:
        os._exit(0)


screen = {"fullscreen": False}


def start_mini_buddy():
    """The little eyes in the corner of your screen while no phone is connected."""
    import minibuddy

    def state():
        media, kind = stats["media"] or {}, (stats["mediaKind"] or {}).get("kind")
        x, y = cursor_position()
        return {"idle": stats["idle"], "activity": stats["activity"], "cpu": stats["cpu"], "heart": stats["heart"],
                "beat": stats["beat"], "x": x, "y": y,
                "musicPlaying": bool(media.get("playing")) and kind == "music",
                "moviePlaying": bool(media.get("playing")) and kind == "movie",
                "show": bool(prefs.get("desktopBuddy", True)) and phones == 0 and not screen["fullscreen"]}

    def save_pos(x, y):
        config["mini_pos"] = [x, y]
        save_json(CONFIG_FILE, config)

    pos = config.get("mini_pos")
    minibuddy.MiniBuddy(state, give_care, open_dashboard, lambda k, v: update_prefs({k: v}), save_pos,
                        tuple(pos) if isinstance(pos, list) and len(pos) == 2 else None).start()


def run_app(args):
    global tray
    Handler.static = {
        "/manifest.webmanifest": ("application/manifest+json", MANIFEST),
        "/sw.js": ("text/javascript", SERVICE_WORKER),
        "/icon-192.png": ("image/png", png_bytes(robot_image(192))),
        "/icon-512.png": ("image/png", png_bytes(robot_image(512))),
    }
    for attempt in range(30):   # just updated: the old copy may still be letting go of the port
        try:
            server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    else:
        if port_in_use():
            open_dashboard()   # another Desk Buddy got there first
        return
    for loop in (stats_loop, audio_loop, media_loop, mail_loop, calendar_loop, keys_loop, signals_loop, net_loop, life_loop,
                 links_loop, sync_loop, lan_sync_loop, peer_poll_loop, relay_loop, start_secure_server):
        threading.Thread(target=loop, daemon=True).start()
    start_mini_buddy()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # At login it starts quietly; the dashboard only opens when asked (or until setup is finished).
    if "--startup" not in args or not config["onboarded"]:
        open_dashboard()

    try:
        import pystray
    except ImportError:  # plain `python deskbuddy.py` without pystray: run in the console instead
        print(f"\n  Desk Buddy is awake!  Phone link: {phone_url()}   (Ctrl+C to stop)\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return

    item = pystray.MenuItem
    tray = pystray.Icon("DeskBuddy", robot_image(64, eye_scale=1.5), APP_NAME, pystray.Menu(
        item("Open Desk Buddy", lambda: open_dashboard(), default=True),
        item("Connect a phone", lambda: open_dashboard("phone")),
        item("Show the phone this PC", lambda: set_pin(PC_ID), checked=lambda i: pin() == PC_ID, visible=lambda i: bool(pc_choices())),
        item("Phone follows me", lambda: set_pin(""), checked=lambda i: pin() == "", visible=lambda i: bool(pc_choices())),
        pystray.Menu.SEPARATOR,
        item("Quit", lambda: quit_app()),
    ))
    tray.run()


def main():
    args = sys.argv[1:]
    if "--uninstall" in args:
        uninstall()
    elif FROZEN and os.path.normcase(sys.executable) != os.path.normcase(INSTALLED_EXE):
        install()  # first double-click (or an update): copy into place and start from there
    elif port_in_use():
        if "--startup" not in args:
            open_dashboard()  # already running: just bring up the dashboard
    else:
        run_app(args)


if __name__ == "__main__":
    main()
