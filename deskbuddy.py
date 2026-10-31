"""Desk Buddy - a robot face for your phone that reacts to what your PC is doing.

As DeskBuddy.exe (built with build.bat): double-click it once and it installs itself,
starts with Windows, lives in the system tray, and shows a QR code for pairing a phone.

During development:  python deskbuddy.py
(needs: pip install pillow pystray qrcode pycaw winrt-Windows.Media.Control icalendar recurring-ical-events)

Windows only (reads PC activity through the Win32 API via ctypes).
"""
import base64
import ctypes
import datetime
import email
import html
import imaplib
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import winreg
from ctypes import wintypes
from email.header import decode_header, make_header
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "Desk Buddy"
VERSION = "1.2"
PORT = int(os.environ.get("DESKBUDDY_PORT", 8765))
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
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
BUDDY_FILE = os.path.join(DATA_DIR, "buddy.json")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
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


def cursor_position():
    """Cursor position across all monitors, scaled to -1..1 on each axis."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    return (pt.x - vx) / max(vw, 1) * 2 - 1, (pt.y - vy) / max(vh, 1) * 2 - 1


stats = {"cpu": 0, "ram": 0, "idle": 0, "title": "", "work": 0, "audio": 0,
         "media": None, "mail": None, "calendar": None, "buddy": None}


# ---------- saved files: settings (secrets encrypted by Windows) and friendship ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except (OSError, ValueError):
        return dict(default)


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
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


def protect(text):
    """Encrypt with Windows (DPAPI): only this Windows user on this PC can read it back."""
    return base64.b64encode(_dpapi(ctypes.windll.crypt32.CryptProtectData, text.encode())).decode() if text else ""


def unprotect(b64):
    try:
        return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(b64)).decode() if b64 else ""
    except (OSError, ValueError):
        return ""


config = load_json(CONFIG_FILE, {"gmail_user": "", "gmail_password": "", "calendar_url": ""})
services = {"gmail": "Not set up", "calendar": "Not set up"}   # status lines for the settings page
wake_mail, wake_calendar = threading.Event(), threading.Event()

# Friendship: points come from days together, time at the PC, petting and taking breaks.
PERSONALITIES = {"cheerful": "Cheerful 😊", "shy": "Shy 😳", "hyper": "Hyper ⚡",
                 "sleepy": "Sleepy 😴", "grumpy": "Grumpy-but-sweet 😤"}
NAMES = ["Pip", "Mochi", "Bolt", "Nori", "Biscuit", "Pixel", "Sprout", "Tofu", "Bean", "Zuzu", "Momo", "Blip"]
LEVELS = [0, 40, 120, 260, 480]          # points needed for friendship levels 1-5
POINTS = {"day": 10, "time": 1, "pet": 1, "break": 5}
DAILY_CAP = {"time": 30, "pet": 15, "break": 15}
buddy = load_json(BUDDY_FILE, {})
if buddy.get("personality") not in PERSONALITIES:   # first run: a brand-new buddy is born
    buddy = {"name": random.choice(NAMES), "personality": random.choice(list(PERSONALITIES)), "points": 0,
             "met": datetime.date.today().isoformat(), "lastDay": "", "awayDays": 0, "todayDate": "", "today": {}}
    save_json(BUDDY_FILE, buddy)
bond_lock = threading.Lock()


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
        save_json(BUDDY_FILE, buddy)


def note_active_day():
    today = datetime.date.today()
    if buddy["lastDay"] != today.isoformat():
        last = buddy["lastDay"]
        buddy["awayDays"] = (today - datetime.date.fromisoformat(last)).days if last else 0
        buddy["lastDay"] = today.isoformat()
        add_points("day")


def buddy_info():
    pts = buddy["points"]
    level = sum(pts >= t for t in LEVELS)
    nxt = LEVELS[level] if level < len(LEVELS) else None
    return {
        "name": buddy["name"], "personality": buddy["personality"], "level": level,
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
    last_uid, seq = None, 0
    while True:
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


def audio_loop():
    """How loud the PC's speakers are right now (0..1), so the robot can dance to music."""
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        comtypes.CoInitialize()
        dev = AudioUtilities.GetSpeakers()
        dev = getattr(dev, "_dev", dev)  # newer pycaw wraps the raw device
        meter = ctypes.cast(dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None),
                            ctypes.POINTER(IAudioMeterInformation))
    except Exception:
        return  # no speakers or pycaw missing: the robot just won't dance
    level = 0.0
    while True:
        try:
            # Fast attack, slow release, so beats show up as clear spikes.
            level = max(meter.GetPeakValue(), level * 0.8)
        except Exception:
            level = 0.0
        stats["audio"] = round(level, 3)
        time.sleep(1 / 60)


def stats_loop():
    prev = cpu_times()
    session_start = time.time()
    active = 0
    while True:
        time.sleep(1)
        cur = cpu_times()
        d_idle, d_kern, d_user = (c - p for c, p in zip(cur, prev))
        prev = cur
        total = d_kern + d_user  # kernel time already includes idle time
        idle = idle_seconds()
        if idle > SESSION_RESET_IDLE:
            session_start = time.time()
        if idle < 60:   # you're here: that's time spent together
            note_active_day()
            active += 1
            if active % 600 == 0:
                add_points("time")
        stats["buddy"] = buddy_info()
        stats.update(
            cpu=round(100 * (1 - d_idle / total)) if total else 0,
            ram=ram_percent(),
            idle=round(idle),
            title=active_window_title(),
            work=round((time.time() - session_start) / 60, 1),
        )


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the Wi-Fi/LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def phone_url():
    return f"http://{lan_ip()}:{PORT}"


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

PAIR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect a phone - Desk Buddy</title>
<link rel="icon" href="/icon-192.png">
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #05080b;
         color: #d8f9ff; font-family: system-ui, "Segoe UI", sans-serif; }
  main { text-align: center; padding: 24px; max-width: 520px; }
  h1 { font-size: 28px; margin: 0 0 6px; color: #5af0ff; }
  .qr { background: #fff; border-radius: 18px; padding: 18px; width: 280px; height: 280px;
        margin: 24px auto; box-shadow: 0 0 40px rgba(90,240,255,.35); }
  .qr svg { width: 100%; height: 100%; }
  code { font-size: 20px; color: #5af0ff; }
  #status { margin: 18px 0; font-size: 18px; }
  .ok { color: #7dff9b; }
  ol { text-align: left; line-height: 1.7; color: #9fb9c0; }
</style></head>
<body><main>
  <h1>Connect your phone</h1>
  <div>Scan this with your phone's camera</div>
  <div class="qr">__QR__</div>
  <div>or type <code>__URL__</code></div>
  <div id="status">Waiting for a phone...</div>
  <ol>
    <li>Phone and PC must be on the <b>same Wi-Fi</b>.</li>
    <li>Tap the robot once to make it fullscreen.</li>
    <li>Optional: in the phone's browser menu, choose <b>Add to Home screen</b>.</li>
  </ol>
  <p><a href="/settings" style="color:#5af0ff">Settings: Gmail, Calendar and your buddy →</a></p>
</main>
<script>
  setInterval(async () => {
    try {
      const s = await fetch('/status').then(r => r.json());
      const el = document.getElementById('status');
      el.textContent = s.phones ? 'Phone connected! You can close this page.' : 'Waiting for a phone...';
      el.className = s.phones ? 'ok' : '';
    } catch (e) {}
  }, 1000);
</script>
</body></html>"""


def pair_page():
    import qrcode
    import qrcode.image.svg
    url = phone_url()
    buf = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=0).save(buf)
    svg = buf.getvalue().decode()
    svg = svg[svg.index("<svg"):]  # drop the XML header so it can sit inside the page
    return PAIR_PAGE.replace("__QR__", svg).replace("__URL__", url).encode()


SETTINGS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings - Desk Buddy</title>
<link rel="icon" href="/icon-192.png">
<style>
  body { margin: 0; background: #05080b; color: #d8eef2; font-family: system-ui, "Segoe UI", sans-serif; line-height: 1.55; }
  main { max-width: 640px; margin: 0 auto; padding: 32px 20px 60px; }
  h1 { color: #5af0ff; margin: 0 0 4px; font-size: 28px; }
  h2 { font-size: 19px; margin: 0 0 4px; }
  .muted { color: #8aa3ab; font-size: 14px; }
  .card { background: #0b1218; border: 1px solid #17252e; border-radius: 16px; padding: 20px; margin-top: 18px; }
  label { display: block; font-size: 14px; color: #8aa3ab; margin: 14px 0 4px; }
  input { width: 100%; box-sizing: border-box; font: inherit; color: #d8eef2; background: #05080b;
          border: 1px solid #23343f; border-radius: 10px; padding: 10px 12px; }
  input:focus { outline: none; border-color: #5af0ff; }
  button { font: inherit; font-weight: 600; border: 0; border-radius: 10px; padding: 10px 18px; cursor: pointer;
           background: #5af0ff; color: #00262c; margin-top: 16px; }
  button.ghost { background: transparent; color: #8aa3ab; border: 1px solid #23343f; margin-left: 8px; }
  .status { margin-top: 12px; font-size: 14px; padding: 8px 12px; border-radius: 10px; background: #05080b; }
  .ok { color: #7dff9b; }
  .bar { height: 8px; border-radius: 4px; background: #17252e; overflow: hidden; margin: 8px 0 4px; }
  .bar i { display: block; height: 100%; background: #5af0ff; }
  ol { padding-left: 20px; margin: 8px 0 0; color: #b4c8ce; font-size: 14px; }
  a { color: #5af0ff; }
  .saved { background: #10351f; color: #7dff9b; border-radius: 10px; padding: 10px 14px; margin-top: 18px; }
</style></head>
<body><main>
  <h1>Desk Buddy settings</h1>
  <div class="muted">Only this PC can open this page. Passwords and links are encrypted by Windows and never leave your PC.</div>
  __SAVED__
  <form method="post" action="/settings">
  <div class="card">
    <h2>Your buddy</h2>
    <label for="name">Name</label>
    <input id="name" name="name" maxlength="20" value="__NAME__">
    <p class="muted" style="margin:12px 0 0">Personality: <b>__PERSONALITY__</b> · Together for <b>__DAYS__</b></p>
    <div class="bar"><i style="width:__PROGRESS__%"></i></div>
    <div class="muted">Friendship level <b>__LEVEL__</b> of 5. It grows with days together, petting, and taking your breaks.</div>
  </div>

  <div class="card">
    <h2>📧 Gmail</h2>
    <div class="muted">Your buddy tells you when new mail arrives (sender and subject only) and shows how many are unread.</div>
    <ol>
      <li>Turn on <a href="https://myaccount.google.com/signinoptions/twosv" target="_blank" rel="noopener">2-Step Verification</a> for your Google account, if it isn't already on.</li>
      <li>Open <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">App passwords</a>, type <b>Desk Buddy</b> as the name, and click <b>Create</b>.</li>
      <li>Paste the 16-letter password below. (It only lets Desk Buddy read mail, and you can delete it any time.)</li>
    </ol>
    <label for="gu">Gmail address</label>
    <input id="gu" name="gmail_user" type="email" autocomplete="off" value="__GMAIL_USER__" placeholder="you@gmail.com">
    <label for="gp">App password</label>
    <input id="gp" name="gmail_password" type="password" autocomplete="new-password" placeholder="__GMAIL_PW_HINT__">
    <div class="status" id="gmail">__GMAIL_STATUS__</div>
  </div>

  <div class="card">
    <h2>📅 Google Calendar</h2>
    <div class="muted">Your buddy reminds you 10 minutes before each event and again when it starts.</div>
    <ol>
      <li>Open <a href="https://calendar.google.com/calendar/r/settings" target="_blank" rel="noopener">Google Calendar settings</a>.</li>
      <li>On the left, click your calendar's name, then scroll to <b>Integrate calendar</b>.</li>
      <li>Copy <b>Secret address in iCal format</b> and paste it below.</li>
    </ol>
    <label for="cu">Secret iCal address</label>
    <input id="cu" name="calendar_url" type="password" autocomplete="off" placeholder="__CAL_HINT__">
    <div class="status" id="calendar">__CAL_STATUS__</div>
  </div>

  <button name="action" value="save">Save</button>
  <button class="ghost" name="action" value="forget_gmail">Disconnect Gmail</button>
  <button class="ghost" name="action" value="forget_calendar">Disconnect Calendar</button>
  </form>
</main>
<script>
  setInterval(async () => {
    try {
      const s = await fetch('/status').then(r => r.json());
      for (const k of ['gmail', 'calendar']) {
        const el = document.getElementById(k);
        el.textContent = s[k]; el.className = 'status' + (s[k].includes('✓') ? ' ok' : '');
      }
    } catch (e) {}
  }, 2000);
</script>
</body></html>"""


def settings_page(saved=""):
    info = buddy_info()
    e = lambda s: html.escape(str(s), quote=True)
    days = info["days"]
    return (SETTINGS_PAGE
            .replace("__SAVED__", f'<div class="saved">{e(saved)}</div>' if saved else "")
            .replace("__NAME__", e(info["name"]))
            .replace("__PERSONALITY__", e(PERSONALITIES[info["personality"]]))
            .replace("__DAYS__", "today" if days == 0 else f"{days} day{'s' if days != 1 else ''}")
            .replace("__PROGRESS__", str(round((info["level"] - 1 + info["progress"]) / 5 * 100)))
            .replace("__LEVEL__", str(info["level"]))
            .replace("__GMAIL_USER__", e(config["gmail_user"]))
            .replace("__GMAIL_PW_HINT__", "Saved - leave empty to keep it" if config["gmail_password"] else "xxxx xxxx xxxx xxxx")
            .replace("__GMAIL_STATUS__", e(services["gmail"]))
            .replace("__CAL_HINT__", "Saved - leave empty to keep it" if config["calendar_url"] else "https://calendar.google.com/calendar/ical/...")
            .replace("__CAL_STATUS__", e(services["calendar"]))).encode()


def save_settings(form):
    action = form.get("action", "save")
    if action == "forget_gmail":
        config.update(gmail_user="", gmail_password="")
        msg = "Gmail disconnected."
    elif action == "forget_calendar":
        config["calendar_url"] = ""
        msg = "Calendar disconnected."
    else:
        name = form.get("name", "").strip()[:20]
        if name:
            with bond_lock:
                buddy["name"] = name
                save_json(BUDDY_FILE, buddy)
        config["gmail_user"] = form.get("gmail_user", "").strip()
        if form.get("gmail_password", "").strip():
            config["gmail_password"] = protect(form["gmail_password"].replace(" ", ""))
        url = form.get("calendar_url", "").strip()
        if url:
            if not url.startswith("https://"):
                return "That calendar address doesn't look right: it should start with https://"
            config["calendar_url"] = protect(url)
        msg = "Saved! Checking your accounts now…"
    save_json(CONFIG_FILE, config)
    services["gmail"] = services["calendar"] = "Checking…"
    wake_mail.set()
    wake_calendar.set()
    return msg


# ---------- web server ----------

phones = 0
tray = None
_last_notice = 0.0


def phone_connected(delta):
    global phones, _last_notice
    phones += delta
    if delta > 0 and phones == 1 and tray and time.time() - _last_notice > 60:
        _last_notice = time.time()
        try:
            tray.notify("Your phone is connected 🤖", APP_NAME)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    static = {}

    def send_body(self, kind, body, cache=True):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        local = self.client_address[0] == "127.0.0.1"
        if path in self.static:
            self.send_body(*self.static[path])
        elif path in ("/", "/index.html"):
            # Read on every request so face.html can be tweaked during development with just a refresh.
            with open(os.path.join(RESOURCES, "face.html"), "rb") as f:
                self.send_body("text/html; charset=utf-8", f.read(), cache=False)
        elif path == "/pair":
            self.send_body("text/html; charset=utf-8", pair_page(), cache=False)
        elif path == "/status":
            status = dict(phones=phones, **(services if local else {}))
            self.send_body("application/json", json.dumps(status).encode(), cache=False)
        elif path == "/settings" and local:
            self.send_body("text/html; charset=utf-8", settings_page(), cache=False)
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
        body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20000)).decode("utf-8", "replace")
        if path == "/bond":
            # The phone reports petting and breaks taken; the PC keeps the friendship score.
            kind = body.strip()
            if kind in ("pet", "break"):
                add_points(kind)
            self.send_body("application/json", json.dumps(buddy_info()).encode(), cache=False)
        elif path == "/settings" and self.client_address[0] == "127.0.0.1" and self.from_settings_page():
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
            self.send_body("text/html; charset=utf-8", settings_page(saved=save_settings(form)), cache=False)
        else:
            self.send_error(403)

    def from_settings_page(self):
        """Blocks other websites in your browser from changing settings behind your back."""
        source = self.headers.get("Origin") or self.headers.get("Referer") or ""
        return any(source.startswith(f"http://{host}:{PORT}") for host in ("localhost", "127.0.0.1"))

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        phone_connected(+1)
        try:
            while True:
                x, y = cursor_position()
                msg = dict(stats, x=round(x, 3), y=round(y, 3))
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
                time.sleep(1 / FPS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            phone_connected(-1)

    def log_message(self, *args):
        pass


def port_in_use():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def open_pair_page():
    webbrowser.open(f"http://localhost:{PORT}/pair")


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
    if os.path.normcase(exe) in os.path.normcase(shown):
        return
    # Only home/work ("private") networks - the robot never listens on public Wi-Fi.
    run_as_admin("cmd.exe",
                 f"/c netsh advfirewall firewall delete rule name={FIREWALL_RULE} >nul & "
                 f"netsh advfirewall firewall add rule name={FIREWALL_RULE} dir=in action=allow "
                 f'protocol=TCP localport={PORT} program="{exe}" profile=private,domain enable=yes')


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


def stop_running_copy():
    if not port_in_use():
        return
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/quit", timeout=3).read()
    except Exception:
        pass
    for _ in range(50):
        if not port_in_use():
            break
        time.sleep(0.1)
    time.sleep(0.5)  # give Windows a moment to release the .exe file


def launch_detached(args):
    env = os.environ.copy()
    # A one-file .exe starting itself must not reuse this process's temporary unpack folder.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop("_MEIPASS2", None)
    subprocess.Popen(args, env=env, cwd=INSTALL_DIR, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP


def install():
    stop_running_copy()
    os.makedirs(INSTALL_DIR, exist_ok=True)
    for attempt in range(10):
        try:
            shutil.copy2(sys.executable, INSTALLED_EXE)
            break
        except PermissionError:
            time.sleep(0.5)
    make_shortcut(STARTUP_LNK, INSTALLED_EXE)
    make_shortcut(START_MENU_LNK, INSTALLED_EXE, "--pair")
    register_in_apps_list()
    allow_through_firewall(INSTALLED_EXE)
    launch_detached([INSTALLED_EXE, "--pair"])


def uninstall():
    stop_running_copy()
    for lnk in (STARTUP_LNK, START_MENU_LNK):
        if os.path.exists(lnk):
            os.remove(lnk)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    run_as_admin("netsh", f"advfirewall firewall delete rule name={FIREWALL_RULE}")
    shutil.rmtree(DATA_DIR, ignore_errors=True)   # settings, saved passwords and friendship progress
    user32.MessageBoxW(None, "Desk Buddy has been removed. Bye! 👋", APP_NAME, 0x40)
    # This .exe can't delete itself while running, so a hidden helper removes the folder once it exits.
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "{INSTALL_DIR}"',
                     cwd=os.environ.get("TEMP", "C:\\"), creationflags=NO_WINDOW)


# ---------- the running app ----------

def quit_app():
    if tray:
        tray.stop()
    else:
        os._exit(0)


def toggle_startup():
    if os.path.exists(STARTUP_LNK):
        os.remove(STARTUP_LNK)
    else:
        make_shortcut(STARTUP_LNK, INSTALLED_EXE)


def uninstall_from_tray():
    if user32.MessageBoxW(None, "Remove Desk Buddy from this PC?", APP_NAME, 0x24) == 6:  # Yes
        launch_detached([INSTALLED_EXE, "--uninstall"])


def run_app(show_pair):
    global tray
    Handler.static = {
        "/manifest.webmanifest": ("application/manifest+json", MANIFEST),
        "/sw.js": ("text/javascript", SERVICE_WORKER),
        "/icon-192.png": ("image/png", png_bytes(robot_image(192))),
        "/icon-512.png": ("image/png", png_bytes(robot_image(512))),
    }
    for loop in (stats_loop, audio_loop, media_loop, mail_loop, calendar_loop):
        threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if show_pair:
        open_pair_page()

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
        item("Connect a phone (QR code)", lambda: open_pair_page(), default=True),
        item("Settings (Gmail, Calendar, buddy)", lambda: webbrowser.open(f"http://localhost:{PORT}/settings")),
        item("Start with Windows", lambda: toggle_startup(),
             checked=lambda _: os.path.exists(STARTUP_LNK), visible=FROZEN),
        pystray.Menu.SEPARATOR,
        item("Uninstall", lambda: uninstall_from_tray(), visible=FROZEN),
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
        open_pair_page()  # already running - just show how to connect
    else:
        run_app(show_pair="--pair" in args or not FROZEN)


if __name__ == "__main__":
    main()
