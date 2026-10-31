# 🤖 Desk Buddy

Turn your spare phone into a cute desk robot that reacts to what your PC is doing.

**[Website & live demo](https://wasiulbahar.github.io/desk-buddy/)** · **[Every feature](https://wasiulbahar.github.io/desk-buddy/features.html)** · **[Download DeskBuddy.exe](https://github.com/wasiulbahar/desk-buddy/releases/latest/download/DeskBuddy.exe)**

## What it does

| When… | Desk Buddy… |
|---|---|
| You move the mouse | Follows your cursor with its eyes across every monitor |
| VS Code, a terminal, Figma, Office… | Narrow-eyed focus mode 🤓 |
| YouTube, Netflix, Reddit… | Gives you a suspicious purple squint 👀 |
| Spotify, YouTube or any player is playing | Dances to the beat and shows the song name 🎵 |
| A PDF, Google Docs, Wikipedia… is open | Puts on reading glasses and reads along 👓 |
| You're in a Zoom / Teams / Meet call | Goes quiet and mutes itself 🤫 |
| CPU above 85% | Turns orange and sweats 🥵 |
| New Gmail arrives | Shows the sender and subject 📧 |
| A Google Calendar event is coming up | Reminds you 10 minutes before and when it starts 📅 |
| Away for 2 / 10 minutes | Gets sleepy, then falls asleep 😴 |
| 50 minutes without a break | Reminds you to stretch and drink water 💧 |
| You stroke / poke / hold the screen | Purrs and sparkles / giggles (or sulks if overdone) / gets curious |
| You hold the screen for 2 seconds | Opens its toolbox: timer, clock, stopwatch, focus, breathe, dice ⏳ |
| You shake / tilt / lay down the phone | Gets dizzy / rolls its eyes like marbles / relaxes 📳 |
| You clap twice | Wakes up 👏 |
| You spend time together | Friendship grows from level 1 (shy) to 5 (best friends) 💞 |
| Nothing much happening | Sneezes, hiccups, hums, nods off and chases flies 🤧 |

It talks in soft "buddy-speak" (vowel sounds whose melody follows the feeling, never words), in one of five personality voices. Every buddy gets its own random name and personality. Optional **Cheeky mode** adds playful teasing at three levels.

## Install

1. Download **DeskBuddy.exe** from the [latest release](https://github.com/wasiulbahar/desk-buddy/releases/latest) and double-click it.
2. Click **Yes** on the one-time Windows prompt (lets your phone through the firewall on home/work networks).
3. A welcome tour opens: scan the QR code with your phone's camera and pick where sounds should play.

From then on it starts with Windows, quietly. Open **Desk Buddy** from the Start menu, desktop or tray to use the dashboard: switch features on or off, change its personality and voice, choose phone or PC sound, set up sensors, Gmail, Calendar and Cheeky mode. Uninstall from **Settings → Apps** or the dashboard.

> Windows may show "Windows protected your PC" because the app isn't code-signed. Click **More info → Run anyway**.

Requirements: Windows 10/11, and a phone on the same Wi-Fi.

### Sensors and microphone

Phones only share motion sensors and the mic with secure pages, so the dashboard's **Phone** page has a second QR code for an `https` link. Desk Buddy makes its own certificate, so the phone warns once ("not private"): tap **Advanced → Continue**. The mic only listens for claps, on the phone itself; nothing is recorded or sent.

### Gmail and Google Calendar (optional)

Dashboard → **Mail & calendar**. Gmail connects with a [Google App Password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification), and Calendar with your calendar's *Secret address in iCal format*. Both are encrypted with Windows DPAPI and stored only on your PC.

## How it works

`deskbuddy.py` reads the cursor, CPU, RAM, idle time, active window title, what's playing and the speaker level through Windows APIs, and streams them over your local network to `face.html`, which draws the robot and synthesises its voice with Web Audio. `dashboard.html` is the settings app (Edge app mode, PC only). When sound is set to the PC, the phone sends each sound's notes and the PC renders the same sound in Python. Nothing leaves your network except your own Gmail/Calendar checks.

## Build it yourself

Needs Python 3.10+ on Windows.

```bat
build.bat
```

This creates a virtual environment, installs the dependencies (PyInstaller, Pillow, pystray, qrcode, pycaw, sounddevice, winrt, icalendar, cryptography…) and produces `DeskBuddy.exe`.

For development, run `python deskbuddy.py`: it opens the dashboard, and edits to `face.html` / `dashboard.html` show up with a refresh.
