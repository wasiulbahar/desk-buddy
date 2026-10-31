"""Desk Buddy's learning: telling music from movies (and learning from your corrections), and
understanding your work pattern from which kind of app you use when.

Everything here is plain logic with no Windows calls, so it's easy to test. It only stores app
categories, times, and the titles/channels/words you've corrected; nothing leaves your PC.
"""
import datetime
import json
import math
import os
import re
import statistics
import threading
import time
from collections import deque

# ---------- music or movie? ----------

MUSIC_RE = re.compile(r"spotify|youtube music|music\.youtube|soundcloud|apple music|deezer|tidal|pandora|amazon music|bandcamp|"
                      r"audiomack|jiosaavn|gaana|wynk|official (audio|music video|video|lyric)|lyrics?\b|\(audio\)|\[audio\]| - topic\b|"
                      r"\bft\.|\bfeat\.|\bremix\b|\bacoustic\b|\bplaylist\b|\blo-?fi\b|\bsongs?\b|\balbum\b", re.I)
MOVIE_RE = re.compile(r"netflix|prime video|disney\+|hotstar|hulu|hbo|crunchyroll|apple tv|paramount\+|peacock|zee5|sonyliv|mubi|"
                      r"vlc media player|mpc-hc|films? & tv|movies? & tv|\btrailer\b|\bepisode\b|\bs\d{1,2}\s?e\d{1,2}\b|\bseason \d|"
                      r"full movie|\bmovie\b|\.(mkv|mp4|avi|mov)\b", re.I)
VIDEO_RE = re.compile(r"youtube|twitch|vimeo|dailymotion|bilibili", re.I)
KINDS = ("music", "movie", "ignore")
STOP = {"the", "and", "for", "with", "you", "your", "from", "this", "that", "official", "video", "youtube", "google", "chrome",
        "edge", "firefox", "microsoft", "new", "full", "best", "live", "part", "vol", "feat", "www", "com", "hd", "4k"}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())[:160]


def _words(s):
    return [w for w in re.findall(r"[a-z][a-z0-9']{2,}", (s or "").lower()) if w not in STOP][:30]


def _site(window_title):
    """'Song - YouTube - Google Chrome' -> 'youtube'."""
    parts = [p.strip().lower() for p in re.split(r" [-–|] ", window_title or "") if p.strip()]
    browsers = {"google chrome", "microsoft edge", "microsoft​ edge", "mozilla firefox", "opera", "brave"}
    parts = [p for p in parts if p not in browsers and not p.endswith("profile 1")]
    return parts[-1] if len(parts) > 1 else ""


class MediaLearner:
    """Classifies what's playing, and learns from corrections at three levels: this exact title,
    everything from this channel/artist/app, and which title words lean music or movie."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.titles, self.sources, self.words = data.get("titles", {}), data.get("sources", {}), data.get("words", {})

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"titles": self.titles, "sources": self.sources, "words": self.words}, f, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def source_of(media, window_title):
        if media and media.get("artist"):
            return _norm(media["artist"])
        if media and media.get("app"):
            return _norm(media["app"])
        return _site(window_title)

    def classify(self, media, window_title):
        """Returns {'kind': 'music'|'movie'|'ignore'|None, 'why': ..., 'source': ..., 'title': ...}."""
        title = _norm(media.get("title") if media else "") or _norm(window_title)
        source = self.source_of(media, window_title)
        out = {"title": (media or {}).get("title") or window_title or "", "source": source, "kind": None, "why": "guess"}
        if not media and not MOVIE_RE.search(window_title or "") and not MUSIC_RE.search(window_title or ""):
            return out
        with self.lock:
            if title in self.titles:
                return {**out, "kind": self.titles[title], "why": "title"}
            if source and source in self.sources:
                return {**out, "kind": self.sources[source], "why": "source"}
            votes = {k: 0.0 for k in KINDS}
            evidence = 0
            for w in set(_words(title)):
                counts = self.words.get(w)
                if not counts:
                    continue
                total = sum(counts.values())
                evidence += total
                for k in KINDS:
                    votes[k] += math.log((counts.get(k, 0) + 0.5) / (total - counts.get(k, 0) + 0.5))
            if evidence >= 2:
                best = max(votes, key=votes.get)
                if votes[best] >= 1.2:
                    return {**out, "kind": best, "why": "words"}
        text = f"{window_title} {title} {(media or {}).get('artist', '')} {(media or {}).get('app', '')}"
        music = (media or {}).get("app") == "Spotify" or MUSIC_RE.search(text)
        if not music and (MOVIE_RE.search(text) or VIDEO_RE.search(text)):
            return {**out, "kind": "movie"}
        return {**out, "kind": "music" if media else None}

    def teach(self, kind, scope, media, window_title):
        if kind not in KINDS:
            raise ValueError("kind must be music, movie or ignore")
        title = _norm(media.get("title") if media else "") or _norm(window_title)
        if not title:
            raise ValueError("Nothing is playing right now")
        with self.lock:
            self.titles[title] = kind
            source = self.source_of(media, window_title)
            if scope == "source" and source:
                self.sources[source] = kind
            for w in set(_words(title)):
                self.words.setdefault(w, {})
                self.words[w][kind] = self.words[w].get(kind, 0) + 1
            self._save()

    def forget(self, kind_of_rule, key):
        with self.lock:
            {"title": self.titles, "source": self.sources}.get(kind_of_rule, {}).pop(key, None)
            self._save()

    def rules(self):
        with self.lock:
            return {"titles": sorted(self.titles.items()), "sources": sorted(self.sources.items()),
                    "words": len(self.words)}


# ---------- what are you doing, and what's your pattern? ----------

READ = r"\.(pdf|docx?|odt|rtf|epub|mobi|pptx?)\b| - word$|google docs|acrobat|foxit|sumatra|okular|kindle|wikipedia|medium\.com|arxiv|substack|scribd"
ACTIVITIES = [   # checked in order against "process.exe window title"
    ("meeting", "Meeting", "📞", r"zoom\.exe|zoom meeting|meeting \| microsoft teams|call \| microsoft teams|google meet|meet\.google|webex|skype"),
    ("coding", "Coding", "💻", r"\bcode\.exe|devenv|pycharm|idea64|webstorm|android studio|sublime_text|notepad\+\+|vim|windowsterminal|"
                              r"powershell|\bcmd\.exe|\bwt\.exe|github|gitlab|stack overflow|cursor\.exe|jupyter|localhost:|\bclaude\b"),
    ("writing", "Writing", "✍️", r"winword|notion|obsidian|onenote|evernote|google keep"),
    ("reading", "Reading", "📖", READ),
    ("sheets", "Sheets & slides", "📊", r"excel|google sheets|powerpnt|powerpoint|google slides"),
    ("design", "Design", "🎨", r"figma|photoshop|illustrator|canva|blender|inkscape|gimp|premiere|afterfx|after effects|davinci|lightroom|audacity"),
    ("email", "Email", "📧", r"outlook|gmail|mail\.google|thunderbird|hxoutlook| - mail\b"),
    ("chat", "Chat", "💬", r"whatsapp|telegram|discord|slack|messenger|signal|ms-teams|microsoft teams"),
    ("social", "Social media", "📱", r"reddit|instagram|facebook|twitter|\bx\.com|tiktok|linkedin|9gag|pinterest|threads"),
    ("video", "Watching", "🍿", r"netflix|prime video|disney\+|youtube|twitch|hulu|vlc|crunchyroll|hotstar"),
    ("gaming", "Gaming", "🎮", r"steam|epicgames|riotclient|battle\.net|minecraft|roblox|valorant|league of legends"),
    ("browsing", "Browsing", "🌐", r"chrome\.exe|msedge\.exe|firefox\.exe|opera|brave"),
    ("files", "Files", "📁", r"explorer\.exe"),
]
INFO = {k: (label, emoji) for k, label, emoji, _ in ACTIVITIES}
INFO["other"] = ("Other", "🧩")
INFO["music"] = ("Music", "🎵")
PRODUCTIVE = {"coding", "reading", "writing", "sheets", "design", "email", "meeting"}
_COMPILED = [(k, re.compile(rx, re.I)) for k, _, _, rx in ACTIVITIES]


def categorize(process, title, media_kind=None, fullscreen=False):
    text = f"{process} {title}"
    if _COMPILED[0][1].search(text):
        return "meeting"
    if media_kind == "movie":
        return "video"
    browsers = re.search(r"chrome|msedge|firefox|opera|brave|vlc|explorer", process or "", re.I)
    if fullscreen and process and not browsers:
        return "gaming"
    for key, rx in _COMPILED[1:]:
        if rx.search(text):
            return key
    return "other"


class ActivityTracker:
    """Called once a second. Keeps per-day totals for each kind of activity, the hours you were
    active, when you started and stopped, and your longest focus stretch; works out your patterns."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.days = json.load(f).get("days", {})
        except (OSError, ValueError):
            self.days = {}
        self.session_key, self.session_start = None, time.time()
        self.pending, self.pending_since = None, 0.0
        self.switches = deque()      # recent changes between kinds of activity
        self.streak, self.off_task = 0, 0
        self.last_save = 0.0

    def _day(self, d):
        return self.days.setdefault(d, {"acts": {}, "hours": [{} for _ in range(24)], "first": None, "last": None, "longest": 0})

    def tick(self, now, active, key):
        dt = datetime.datetime.fromtimestamp(now)
        date, minute = dt.date().isoformat(), dt.hour * 60 + dt.minute
        with self.lock:
            if active:
                day = self._day(date)
                day["acts"][key] = day["acts"].get(key, 0) + 1
                hour = day["hours"][dt.hour]
                hour[key] = hour.get(key, 0) + 1
                if day["first"] is None:
                    day["first"] = minute
                day["last"] = minute
                # focus stretch: productive time, allowing up to a minute of something else
                if key in PRODUCTIVE:
                    self.streak += 1 + self.off_task
                    self.off_task = 0
                else:
                    self.off_task += 1
                    if self.off_task > 60:
                        self.streak, self.off_task = 0, 0
                day["longest"] = max(day["longest"], self.streak)
                # the current session: only switch when something new lasts 20+ seconds
                if key != self.session_key:
                    if key != self.pending:
                        self.pending, self.pending_since = key, now
                        self.switches.append(now)
                    elif now - self.pending_since >= 20 or self.session_key is None:
                        self.session_key, self.session_start, self.pending = key, self.pending_since, None
                else:
                    self.pending = None
            else:
                self.streak, self.off_task = 0, 0
            while self.switches and now - self.switches[0] > 600:
                self.switches.popleft()
            if now - self.last_save > 60:
                self.last_save = now
                self._save()

    def _save(self):
        cutoff = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
        self.days = {d: v for d, v in self.days.items() if d >= cutoff}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"days": self.days}, f)
        os.replace(tmp, self.path)

    def now(self, now):
        key = self.session_key or "other"
        label, emoji = INFO.get(key, INFO["other"])
        return {"key": key, "label": label, "emoji": emoji, "minutes": round((now - self.session_start) / 60, 1),
                "switches10": len(self.switches), "productive": key in PRODUCTIVE, "streakMin": round(self.streak / 60, 1)}

    def summary(self, today=None):
        """Today's numbers plus your usual pattern, for the phone and the dashboard."""
        today = today or datetime.date.today().isoformat()
        with self.lock:
            day = self.days.get(today, {"acts": {}, "hours": [{} for _ in range(24)], "first": None, "last": None, "longest": 0})
            past = [v for d, v in sorted(self.days.items()) if d < today][-14:]
            week = [v for d, v in sorted(self.days.items()) if d > (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()]
            acts = sorted(day["acts"].items(), key=lambda kv: -kv[1])
            starts = [v["first"] for v in past if v.get("first") is not None]
            ends = [v["last"] for v in past if v.get("last") is not None]
            hour_focus = [0.0] * 24
            for v in past:
                for h, counts in enumerate(v.get("hours", [])):
                    hour_focus[h] += sum(s for k, s in counts.items() if k in PRODUCTIVE) / max(len(past), 1)
            week_acts = {}
            for v in week:
                for k, s in v["acts"].items():
                    week_acts[k] = week_acts.get(k, 0) + s
            timeline = []
            for counts in day["hours"]:
                top = max(counts.items(), key=lambda kv: kv[1]) if counts else None
                timeline.append([top[0], round(sum(counts.values()) / 60)] if top else None)
        mins = lambda s: round(s / 60)
        label = lambda k: [k, *INFO.get(k, INFO["other"])]
        return {
            "activeMin": mins(sum(day["acts"].values())),
            "productiveMin": mins(sum(s for k, s in day["acts"].items() if k in PRODUCTIVE)),
            "breakdown": [label(k) + [mins(s)] for k, s in acts if s >= 30][:8],
            "first": day["first"], "last": day["last"], "longestFocusMin": mins(day["longest"]),
            "usualStart": round(statistics.median(starts)) if len(starts) >= 3 else None,
            "usualEnd": round(statistics.median(ends)) if len(ends) >= 3 else None,
            "bestHours": [h for h, s in sorted(enumerate(hour_focus), key=lambda kv: -kv[1])[:3] if s > 300],
            "daysTracked": len(past) + (1 if day["acts"] else 0),
            "weekAvgMin": mins(sum(sum(v["acts"].values()) for v in week) / max(len(week), 1)),
            "week": [label(k) + [mins(s)] for k, s in sorted(week_acts.items(), key=lambda kv: -kv[1]) if s >= 60][:8],
            "timeline": timeline,
        }


# ---------- the world around you: weather, season, holidays, payday ----------
# Festival dates that follow the moon, from published calendars (they can shift by a day locally).
LUNAR_DATES = {
    "eid_fitr": {2026: "03-20", 2027: "03-10", 2028: "02-27", 2029: "02-15", 2030: "02-05", 2031: "01-25"},
    "eid_adha": {2026: "05-27", 2027: "05-17", 2028: "05-05", 2029: "04-24", 2030: "04-13", 2031: "04-02"},
    "diwali": {2026: "11-08", 2027: "10-29", 2028: "10-17", 2029: "11-05", 2030: "10-26", 2031: "11-14"},
    "lunar_ny": {2026: "02-17", 2027: "02-06", 2028: "01-26", 2029: "02-13", 2030: "02-03", 2031: "01-23"},
}
WEATHER_KINDS = [((0,), "clear"), ((1, 2), "partly"), ((3,), "cloudy"), ((45, 48), "fog"), ((51, 53, 55, 56, 57), "drizzle"),
                 ((61, 63, 65, 66, 67, 80, 81, 82), "rain"), ((71, 73, 75, 77, 85, 86), "snow"), ((95, 96, 99), "storm")]


def weather_kind(code):
    return next((kind for codes, kind in WEATHER_KINDS if code in codes), "cloudy")


def _near(today, key, days_before=0, days_after=0):
    md = LUNAR_DATES[key].get(today.year)
    if not md:
        return False
    day = datetime.date(today.year, int(md[:2]), int(md[3:]))
    return -days_before <= (today - day).days <= days_after


def occasions(today, prefs, birthday):
    out = []
    add = lambda key, label, emoji, greeting: out.append({"key": key, "label": label, "emoji": emoji, "greeting": greeting})
    md = today.strftime("%m-%d")
    if birthday and md == birthday:
        add("birthday", "Your birthday", "🎂", "Happy birthday!! 🎂🎉")
    if md == "01-01":
        add("newyear", "New Year", "🎉", "Happy New Year! 🎉")
    if md == "12-31":
        add("newyeareve", "New Year's Eve", "🥂", "Last day of the year! 🥂")
    if md == "10-31":
        add("halloween", "Halloween", "🎃", "Happy Halloween! 🎃")
    if prefs.get("hChristmas") and "12-24" <= md <= "12-26":
        add("christmas", "Christmas", "🎄", "Merry Christmas! 🎄")
    if prefs.get("hEid") and (_near(today, "eid_fitr", 0, 2) or _near(today, "eid_adha", 0, 3)):
        add("eid", "Eid", "🌙", "Eid Mubarak! 🌙✨")
    if prefs.get("hDiwali") and _near(today, "diwali", 1, 1):
        add("diwali", "Diwali", "🪔", "Happy Diwali! 🪔✨")
    if prefs.get("hLunar") and _near(today, "lunar_ny", 0, 2):
        add("lunar", "Lunar New Year", "🧧", "Happy Lunar New Year! 🧧")
    return out


def season(today, latitude):
    if latitude is not None and abs(latitude) < 15:
        return "tropical"
    north = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
             6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}[today.month]
    if latitude is not None and latitude < 0:
        north = {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}[north]
    return north


def _payday_in(year, month, day):
    last = (datetime.date(year + month // 12, month % 12 + 1, 1) - datetime.timedelta(days=1)).day
    return datetime.date(year, month, last if day >= 99 else min(day, last))


def money(today, payday):
    """Where you are in your pay cycle: 'payday', 'flush' (just paid), 'tight' (the last stretch) or 'normal'."""
    if not payday:
        return None
    this = _payday_in(today.year, today.month, payday)
    if this > today:
        prev_m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        last, nxt = _payday_in(*prev_m, payday), this
    else:
        next_m = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        last, nxt = this, _payday_in(*next_m, payday)
    since, until = (today - last).days, (nxt - today).days
    phase = "payday" if since == 0 else "flush" if since <= 4 else "tight" if until <= 4 else "normal"
    return {"phase": phase, "daysToPayday": until, "daysSincePayday": since}


def life_context(today, prefs, config, location, weather):
    lat = location.get("lat") if location else None
    return {
        "weather": weather if prefs.get("weather") else None,
        "city": location.get("city") if location else None,
        "season": season(today, lat) if prefs.get("seasons") else None,
        "occasions": occasions(today, prefs, config.get("birthday")) if prefs.get("occasions") else [],
        "money": money(today, int(config.get("payday") or 0)) if prefs.get("money") else None,
    }
