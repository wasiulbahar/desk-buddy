"""The mini desktop buddy: a small pair of eyes that sits in a corner of your screen while no phone
is connected, so your buddy is still with you. It runs its own little window (tkinter) in a thread.

Stroke it with your mouse pointer to pet it, click to poke it, drag it to move it, right-click for more.
"""
import ctypes
import math
import random
import threading
import time

FEEL_LINES = {
    "clingy": ["Pet me?", "Hey... hey...", "Got a minute for me?"],
    "lonely": ["I miss you a little...", "...hi"],
    "sad": ["Nobody pets me anymore...", "*sniff*"],
    "annoyed": ["Hmph. Busy, huh?", "I exist too, you know"],
    "angry": ["Hmph!!", "Pet me. Now."],
    "bored": ["Bored... play with me?", "So... bored..."],
    "tired": ["Low battery... snack?", "So sleepy..."],
    "joyful": ["Best day ever!", "I like you a lot, you know"],
}
UPSET = ("clingy", "lonely", "sad", "annoyed", "angry")

# w, h, lid, tilt (+ angry / - worried), happy (^_^), color: a few of the phone's moods.
MOODS = {
    "neutral": (1, 1, 0, 0, 0, (90, 240, 255)),
    "happy": (1.05, 1, 0, 0, .55, (90, 240, 255)),
    "love": (1.05, 1, 0, 0, .55, (255, 110, 175)),
    "content": (1.05, .85, .15, 0, .5, (90, 240, 255)),
    "giggle": (1.1, .9, 0, 0, .62, (120, 250, 255)),
    "groove": (1.05, .95, 0, 0, .35, (120, 255, 200)),
    "focused": (1.05, .8, .2, .2, 0, (90, 240, 255)),
    "reading": (.92, .74, .12, 0, 0, (90, 240, 255)),
    "meeting": (.88, .82, .08, 0, 0, (150, 200, 255)),
    "watching": (1.06, 1.08, .04, 0, 0, (255, 228, 175)),
    "sleepy": (1, 1, .55, -.25, 0, (70, 170, 220)),
    "sleeping": (1.05, .08, 0, 0, 0, (60, 120, 200)),
    "stressed": (.9, 1.05, .1, -.5, 0, (255, 170, 70)),
    "joyful": (1.08, 1.02, 0, 0, .3, (110, 250, 240)),
    "clingy": (1.18, 1.22, 0, -.3, 0, (150, 220, 255)),
    "lonely": (.95, .95, .18, -.35, 0, (120, 180, 235)),
    "sad": (1, .85, .28, -.55, 0, (100, 160, 230)),
    "annoyed": (1, .7, .4, .25, 0, (230, 150, 110)),
    "angry": (1.05, .8, .25, .7, 0, (255, 80, 70)),
    "bored": (1, .62, .42, 0, 0, (140, 200, 220)),
    "tired": (1, .8, .45, -.2, 0, (90, 170, 215)),
}
BG, KEY = "#0b1116", "#010203"   # the panel, and the color Windows makes see-through


class MiniBuddy:
    def __init__(self, get_state, care, open_app, set_pref, save_pos, pos):
        self.get_state, self.care, self.open_app, self.set_pref, self.save_pos = get_state, care, open_app, set_pref, save_pos
        self.pos = pos
        self.override = None          # (mood, until)
        self.cur = list(MOODS["neutral"][:5]) + [list(MOODS["neutral"][5])]
        self.look = [0.0, 0.0]
        self.blink_at, self.next_blink = 0.0, time.time() + 3
        self.bounce = self.bounce_v = 0.0
        self.pet_dist, self.last_care, self.last_hover = 0.0, 0.0, None
        self.drag = None
        self.next_line = time.time() + 60
        self.shown = False
        self.said_hi = ""

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    # ---------- window ----------
    def _run(self):
        try:
            import tkinter as tk
        except ImportError:
            return
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem() / 96
        except Exception:
            dpi = 1.0
        self.tk, self.k = tk, dpi
        self.W, self.H = int(150 * dpi), int(84 * dpi)
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        cv = self.cv = tk.Canvas(root, width=self.W, height=self.H, bg=KEY, highlightthickness=0, cursor="hand2")
        cv.pack()
        x, y = self._place()
        root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        # A little speech bubble above it.
        self.tip = tk.Toplevel(root)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip_label = tk.Label(self.tip, bg="#16242c", fg="#e8f7ff", font=("Segoe UI", 10), padx=10, pady=5)
        self.tip_label.pack()
        self.tip_until = 0.0
        self.menu = tk.Menu(root, tearoff=0)
        for label, kind in (("Give a cookie", "cookie"), ("Give ice cream", "icecream"), ("Give a battery", "battery")):
            self.menu.add_command(label=label, command=lambda k=kind: self._treat(k))
        self.menu.add_separator()
        self.menu.add_command(label="Open Desk Buddy", command=self.open_app)
        self.menu.add_command(label="Hide the mini buddy", command=self._hide_for_good)
        cv.bind("<Motion>", self._hover)
        cv.bind("<Leave>", lambda e: setattr(self, "last_hover", None))
        cv.bind("<ButtonPress-1>", self._press)
        cv.bind("<B1-Motion>", self._drag)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Double-Button-1>", lambda e: self.open_app())
        cv.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self._tick()
        root.mainloop()

    def _place(self):
        if self.pos:
            return self.pos
        try:
            import ctypes.wintypes
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)   # the work area, above the taskbar
            return rect.right - self.W - int(24 * self.k), rect.bottom - self.H - int(16 * self.k)
        except Exception:
            return 100, 100

    def say(self, text, seconds=3.5):
        self.tip_label.config(text=text)
        self.tip.update_idletasks()
        tw, th = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
        x = self.root.winfo_x() + self.W // 2 - tw // 2
        y = self.root.winfo_y() - th - int(6 * self.k)
        self.tip.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.tip.deiconify()
        self.tip_until = time.time() + seconds

    def set_override(self, mood, seconds):
        self.override = (mood, time.time() + seconds)

    # ---------- mouse ----------
    def _hover(self, e):
        """Stroking it with the pointer (no button) is petting."""
        if self.drag:
            return
        if self.last_hover:
            d = math.hypot(e.x - self.last_hover[0], e.y - self.last_hover[1])
            self.pet_dist += d
            feeling = (self.state.get("heart") or {}).get("feeling", "okay")
            need = {"angry": 1800, "annoyed": 1100, "sad": 700}.get(feeling, 250) * self.k
            if self.pet_dist > need:
                self.set_override("content", .8)
                if time.time() - self.last_care > 3.5:
                    self.last_care = time.time()
                    threading.Thread(target=self.care, args=("pet",), daemon=True).start()
            elif feeling in UPSET:
                self.set_override(feeling, .8)
        self.last_hover = (e.x, e.y)

    def _press(self, e):
        self.drag = {"x": e.x_root, "y": e.y_root, "wx": self.root.winfo_x(), "wy": self.root.winfo_y(), "moved": False}

    def _drag(self, e):
        d = self.drag
        if not d:
            return
        dx, dy = e.x_root - d["x"], e.y_root - d["y"]
        if abs(dx) + abs(dy) > 4:
            d["moved"] = True
        if d["moved"]:
            self.root.geometry(f"+{d['wx'] + dx}+{d['wy'] + dy}")

    def _release(self, e):
        d, self.drag = self.drag, None
        if d and d["moved"]:
            self.save_pos(self.root.winfo_x(), self.root.winfo_y())
        else:   # a poke
            feeling = (self.state.get("heart") or {}).get("feeling", "okay")
            if feeling in ("angry", "annoyed"):
                self.set_override(feeling, 1.2)
                self.say("Don't poke me. Pet me!" if feeling == "angry" else "Hmph.", 2)
            else:
                self.set_override(random.choice(["giggle", "giggle", "love"]), 1.2)
                self.bounce_v = -60
        self.pet_dist = 0

    def _treat(self, kind):
        ok = self.care(kind)
        if ok:
            self.set_override("love", 2)
            self.bounce_v = -70
            self.say({"cookie": "Yum! Crunchy!", "icecream": "Brain freeze! So good", "battery": "Charging... much better!"}[kind])
        else:
            self.say("I'm full! Maybe tomorrow")

    def _hide_for_good(self):
        self.set_pref("desktopBuddy", False)
        self.root.withdraw()
        self.tip.withdraw()
        self.shown = False

    # ---------- what it feels and does ----------
    def _mood(self, s, now):
        if self.override and now < self.override[1]:
            return self.override[0]
        idle, act = s.get("idle", 0), (s.get("activity") or {}).get("key", "")
        music = s.get("musicPlaying")
        if idle > 600 and not music:
            return "sleeping"
        if idle > 120 and not music:
            return "sleepy"
        if act == "meeting":
            return "meeting"
        if s.get("cpu", 0) > 85:
            return "stressed"
        if s.get("moviePlaying"):
            return "watching"
        if music:
            return "groove"
        feeling = (s.get("heart") or {}).get("feeling", "okay")
        if act in ("coding", "writing", "sheets", "design", "email"):
            return "focused"
        if act == "reading":
            return "reading"
        return feeling if feeling != "okay" else "neutral"

    def _tick(self):
        now = time.time()
        try:
            self.state = s = self.get_state()
            if s.get("show") != self.shown:
                self.shown = s.get("show")
                if self.shown:
                    self.root.deiconify()
                    self.root.attributes("-topmost", True)
                    today = time.strftime("%Y-%m-%d")
                    if self.said_hi != today:
                        self.said_hi = today
                        self.say("Hi! I'll keep you company here")
                else:
                    self.root.withdraw()
                    self.tip.withdraw()
            if self.shown:
                self._think(s, now)
                self._draw(s, now)
            if self.tip_until and now > self.tip_until:
                self.tip.withdraw()
                self.tip_until = 0
        except Exception:
            pass
        self.root.after(33, self._tick)

    def _think(self, s, now):
        feeling = (s.get("heart") or {}).get("feeling", "okay")
        if now > self.next_line and s.get("idle", 0) < 60:
            self.next_line = now + random.uniform(240, 480)
            if feeling in FEEL_LINES:
                self.say(random.choice(FEEL_LINES[feeling]))
                if feeling == "clingy":
                    self.bounce_v = -60

    def _draw(self, s, now):
        mood = self._mood(s, now)
        target = MOODS.get(mood, MOODS["neutral"])
        k = .25
        for i in range(5):
            self.cur[i] += (target[i] - self.cur[i]) * k
        for i in range(3):
            self.cur[5][i] += (target[5][i] - self.cur[5][i]) * k
        w, h, lid, tilt, happy = self.cur[:5]
        color = "#%02x%02x%02x" % tuple(int(c) for c in self.cur[5])
        # look at the mouse pointer (anywhere on screen), or wander
        tx, ty = s.get("x", 0), s.get("y", 0)
        if mood in ("sleeping", "sleepy"):
            tx, ty = 0, .3
        elif mood in ("sad", "angry", "annoyed"):
            tx, ty = (.8 if mood != "sad" else .2), (.4 if mood == "sad" else 0)
        self.look[0] += (max(-1, min(1, tx)) - self.look[0]) * .15
        self.look[1] += (max(-1, min(1, ty)) - self.look[1]) * .15
        # blink and bounce (and dance to the beat)
        if now > self.next_blink and mood != "sleeping":
            self.blink_at, self.next_blink = now, now + random.uniform(2, 6)
        bt = (now - self.blink_at) / .16
        blink = 1 - .92 * math.sin(bt * math.pi) if bt < 1 else 1
        self.bounce_v += (-140 * self.bounce - 9 * self.bounce_v) * .033
        self.bounce += self.bounce_v * .033
        dance_x = dance_y = 0
        beat = s.get("beat")
        if mood == "groove" and beat:
            t = (now * 1000 - beat["t0"]) / (60000 / beat["bpm"])
            phase = t - math.floor(t)
            dance_x = math.sin(math.pi * t) * 6 * self.k
            dance_y = -abs(math.sin(math.pi * phase)) * 5 * self.k
        cv, K = self.cv, self.k
        cv.delete("all")
        r = 18 * K
        x0, y0, x1, y1 = 2, 2, self.W - 2, self.H - 2   # the dark rounded panel
        cv.create_polygon(x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
                          x0, y1 - r, x0, y0 + r, x0, y0, smooth=True, fill=BG, outline="#1d2c35")
        cx = self.W / 2 + self.look[0] * 10 * K + dance_x
        cy = self.H / 2 + self.look[1] * 6 * K + self.bounce + dance_y
        gap, ew, eh = 30 * K, 34 * K * w, max(44 * K * h * blink, 3 * K)
        for side in (-1, 1):
            ex = cx + side * gap
            l, t, rr, b = ex - ew / 2, cy - eh / 2, ex + ew / 2, cy + eh / 2
            rad = min(ew, eh) * .3
            cv.create_polygon(l + rad, t, rr - rad, t, rr, t, rr, t + rad, rr, b - rad, rr, b, rr - rad, b, l + rad, b, l, b,
                              l, b - rad, l, t + rad, l, t, smooth=True, fill=color, outline="")
            inner, outer = lid + max(0, tilt) * .5, lid + max(0, -tilt) * .5
            left_drop, right_drop = (outer, inner) if side < 0 else (inner, outer)
            if left_drop > .01 or right_drop > .01:   # the top lid: slanted when angry or worried
                cv.create_polygon(l - 2, t - 2, rr + 2, t - 2, rr + 2, t + right_drop * eh, l - 2, t + left_drop * eh, fill=BG, outline="")
            if happy > .02:   # ^_^
                rad2 = ew * .75
                cv.create_oval(ex - rad2, b - happy * eh, ex + rad2, b - happy * eh + rad2 * 2, fill=BG, outline="")
            if mood == "sad" and int(now * 2) % 3 == 0:   # a tear
                cv.create_oval(ex + side * ew * .25 - 3 * K, b + 2 * K, ex + side * ew * .25 + 3 * K, b + 9 * K, fill="#82c8ff", outline="")
        if mood == "sleeping":
            cv.create_text(self.W - 16 * K, 16 * K - (now * 8 % 8) * K, text="z", fill=color, font=("Segoe UI", 10, "bold"))
