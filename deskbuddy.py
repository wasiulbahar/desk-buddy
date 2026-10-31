"""Desk Buddy - a robot face for your phone that reacts to what your PC is doing.

As DeskBuddy.exe (built with build.bat): double-click it once and it installs itself,
starts with Windows, lives in the system tray, and shows a QR code for pairing a phone.

During development:  python deskbuddy.py   (needs: pip install pillow pystray qrcode)

Windows only (reads PC activity through the Win32 API via ctypes).
"""
import ctypes
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import winreg
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "Desk Buddy"
VERSION = "1.0"
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

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

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


stats = {"cpu": 0, "ram": 0, "idle": 0, "title": "", "work": 0}


def stats_loop():
    prev = cpu_times()
    session_start = time.time()
    while True:
        time.sleep(1)
        cur = cpu_times()
        d_idle, d_kern, d_user = (c - p for c, p in zip(cur, prev))
        prev = cur
        total = d_kern + d_user  # kernel time already includes idle time
        idle = idle_seconds()
        if idle > SESSION_RESET_IDLE:
            session_start = time.time()
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
            self.send_body("application/json", json.dumps({"phones": phones}).encode(), cache=False)
        elif path == "/quit" and local:
            # Used by a newer DeskBuddy.exe to replace this one when updating.
            self.send_body("text/plain", b"bye")
            threading.Thread(target=quit_app, daemon=True).start()
        elif path == "/events":
            self.stream_events()
        else:
            self.send_error(404)

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
    threading.Thread(target=stats_loop, daemon=True).start()
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
