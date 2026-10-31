# 🤖 Desk Buddy

Turn your spare phone into a cute desk robot that reacts to what your PC is doing.

**[Website & live demo](https://wasiulbahar.github.io/desk-buddy/)** · **[Download DeskBuddy.exe](https://github.com/wasiulbahar/desk-buddy/releases/latest/download/DeskBuddy.exe)**

## What it does

| On your PC | Desk Buddy |
|---|---|
| You move the mouse | Eyes follow your cursor across every monitor |
| VS Code, a terminal, Figma, Office… | Narrow-eyed focus mode 🤓 |
| YouTube, Netflix, Reddit… | Suspicious purple squint 👀 |
| CPU above 85% | Turns orange and sweats 🥵 |
| Away for 2 / 10 minutes | Gets sleepy, then falls asleep 😴 |
| You come back | Wakes up happy to see you 👋 |
| 50 minutes without a break | Reminds you to stretch and drink water 💧 |
| Spotify, YouTube or any player is playing | Dances to the beat and shows the song name 🎵 |
| A PDF, Google Docs, Wikipedia… is open | Puts on reading glasses and reads along 👓 |
| You're in a Zoom / Teams / Meet call | Goes quiet and mutes itself 🤫 |
| New Gmail arrives | Shows the sender and subject 📧 |
| A Google Calendar event is coming up | Reminds you 10 minutes before and when it starts 📅 |
| You spend time together | Friendship grows from level 1 (shy) to 5 (best friends) 💞 |
| You stroke the screen | Purrs, eyes sparkle, leans into your finger 🤚 |
| You tap the phone | Giggles and bounces 💕 (poke it too much and it sulks until you pet it) |
| You hold a finger still | Gets curious about your finger 🤔 |
| You find its secret tickle spot | Laughs uncontrollably 😆 |
| You spin the mouse in circles | Gets dizzy, with spiral eyes 😵 |
| Music is playing | Dances to the beat 🎵 |
| First time each day | Stretches and says good morning ☀️ (or that it missed you after a few days) |
| After midnight | Gets drowsy and nudges you toward bed 🌙 |
| Nothing much happening | Sneezes, hiccups, hums, nods off and chases flies 🤧 |

It talks with little chirps and beeps rather than words. Tap 🔊 at the bottom to mute it.

Every buddy gets its own random name and personality (cheerful, shy, hyper, sleepy or grumpy-but-sweet). See **[every feature](https://wasiulbahar.github.io/desk-buddy/features.html)**.

### Gmail and Google Calendar (optional)

Right-click the tray robot → **Settings**. Gmail connects with a [Google App Password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification), and Calendar with your calendar's *Secret address in iCal format*. Both are encrypted with Windows DPAPI and stored only on your PC.

## Install

1. Download **DeskBuddy.exe** from the [latest release](https://github.com/wasiulbahar/desk-buddy/releases/latest) and double-click it.
2. Click **Yes** on the one-time Windows prompt (lets your phone through the firewall on home networks).
3. Scan the QR code that appears with your phone's camera. Tap the robot to go fullscreen.

It starts with Windows and lives in the system tray. Uninstall from **Settings → Apps** or the tray menu.

> Windows may show "Windows protected your PC" because the app isn't code-signed. Click **More info → Run anyway**.

Requirements: Windows 10/11, and a phone on the same Wi-Fi.

## How it works

`deskbuddy.py` reads cursor position, CPU, RAM, idle time and the active window title through the Win32 API, and streams them over your local network to `face.html`, which draws the robot on a canvas. Nothing leaves your network.

## Build it yourself

Needs Python 3.10+ on Windows.

```bat
build.bat
```

This creates a virtual environment, installs PyInstaller, Pillow, pystray and qrcode, and produces `DeskBuddy.exe`.

For development, run `python deskbuddy.py` and open the printed link on your phone. Edits to `face.html` show up with a refresh.
