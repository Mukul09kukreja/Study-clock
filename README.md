# 🔒 StudyLock

> **A vibe coding project** — built entirely through conversational AI-assisted development, iterated in real time with zero boilerplate and zero templates. Just vibes, prompts, and Python.

A developer-focused focus timer with a floating HUD overlay, Pomodoro mode, app blocking, and a clean dark terminal aesthetic. Built with Python + Flask. No Electron. No npm. Just one file.

---

## ✨ Features

- **Free Timer** — pick any duration from 1–120 minutes via quick presets or a custom slider
- **Pomodoro Mode** — configurable focus / short break / long break / sessions with auto-advance
- **Floating HUD** — a compact 280×320 overlay window you can pin above your code editor
- **Real-time SSE** — HUD updates via Server-Sent Events (no polling lag, falls back gracefully)
- **Focus Lock Screen** — full-page blur overlay blocks the browser during focus sessions
- **App Blocker** — force-kills distracting apps (Spotify, Discord, etc.) when focus starts
- **Pause / Resume / Restart** — controls available both on the main panel and on the lock screen
- **Session Stats** — daily sessions, focus time, and streak tracked and saved to JSON
- **Session History** — sparkbar log of every focus block completed today
- **3 Themes** — Dark (GitHub-style), Warm (terminal amber), Cyber (neon grid)
- **Single file** — the entire app is `studylock.py`. No build step, no dependencies beyond Flask.

---

## 🚀 Quick Start

### Using a virtual environment (recommended)

```bash
# 1. Clone or download the project
git clone https://github.com/yourname/studylock.git
cd studylock

# 2. Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python studylock.py
```

### Or just install directly

```bash
pip install -r requirements.txt
python studylock.py
```

Your browser opens automatically at `http://127.0.0.1:5050`.

> **Tip:** Keep the virtual environment activated whenever you run StudyLock. To deactivate it later, just run `deactivate`.

---

## 🖥️ The Floating HUD

The HUD is the main reason this exists. You shouldn't have to alt-tab to check your timer while coding.

**Launch it:**
1. Click **⊞ Launch HUD** in the control panel
2. A small `280×320` browser window opens at `http://127.0.0.1:5050/hud`
3. Pin it above your editor using your OS:

| OS | How to always-on-top |
|---|---|
| **Windows** | Right-click title bar → "Always on top" · or use [PowerToys](https://learn.microsoft.com/en-us/windows/powertoys/) |
| **Linux (GNOME)** | Right-click title bar → "Always on Top" |
| **Linux (i3/bspwm)** | Mark window floating, then sticky |
| **macOS** | Use [Afloat](https://github.com/rwu823/afloat) or [Mango5Star](https://www.mangonapps.com/) |

Or just snap the HUD window into a corner of your monitor next to your editor. It's tiny enough to live alongside VS Code, Neovim, or a terminal.

The HUD shows:
- Phase badge (focus / short break / long break / idle)
- Large countdown timer
- Progress bar
- Pomodoro session dots
- "Next up" info (e.g. `next: 5m break · 3 sessions left`)
- Pause / Resume, Restart, Stop controls

---

## ⏱️ Modes

### Free Timer
Set any duration and go. Good for deep work blocks, reading, or debugging sessions without the Pomodoro structure.

### Pomodoro
Classic technique with fully configurable intervals:

| Setting | Default | Range |
|---|---|---|
| Focus | 25 min | 1–90 |
| Short break | 5 min | 1–30 |
| Long break | 15 min | 1–90 |
| Sessions | 4 | 1–12 |

A long break fires every 4th completed focus round. Sessions auto-advance — focus → break → focus → ... → done.

---

## 🚫 App Blocker

Apps entered in the blocker panel are force-killed when a focus session starts and unblocked during breaks.

Use the exact process name for your OS:

```
Windows  →  Spotify.exe   Discord.exe   chrome.exe
macOS    →  Spotify        Discord       Google Chrome
Linux    →  spotify        discord       chromium
```

---

## 🔌 API

The Flask backend exposes a clean REST API. You can control the timer from scripts, Alfred workflows, shell aliases, or any HTTP client.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/snapshot` | Full timer state as JSON |
| `POST` | `/api/start` | Start a new session |
| `POST` | `/api/pause` | Pause current session |
| `POST` | `/api/resume` | Resume paused session |
| `POST` | `/api/restart-phase` | Restart current phase from zero |
| `POST` | `/api/reset` | Full stop and reset to idle |
| `POST` | `/api/mode` | `{"mode": "free" \| "pomodoro"}` |
| `POST` | `/api/free-dur` | `{"minutes": 45}` |
| `POST` | `/api/cfg` | `{"focus":25, "short":5, "long":15, "sessions":4}` |
| `POST` | `/api/blocked` | `{"apps": ["Spotify", "Discord"]}` |
| `POST` | `/api/stats/reset` | Clear today's stats |
| `GET` | `/api/events` | SSE stream for real-time updates |
| `POST` | `/api/quit` | Shut down the server |
| `GET` | `/hud` | Floating HUD page |

### Example — start a 45-minute session from the terminal:

```bash
curl -s -X POST http://127.0.0.1:5050/api/free-dur \
  -H "Content-Type: application/json" -d '{"minutes": 45}'

curl -s -X POST http://127.0.0.1:5050/api/start
```

### Example — check remaining time:

```bash
curl -s http://127.0.0.1:5050/api/snapshot | python3 -m json.tool | grep remaining
```

---

## 📁 File Structure

```
studylock/
├── studylock.py           ← entire app (backend + both frontends)
├── requirements.txt       ← pip dependencies
├── LICENSE                ← MIT license
├── README.md
├── venv/                  ← virtual environment (not committed to git)
└── studylock_stats.json   ← auto-created at runtime, daily stats
```

> Add `venv/` and `studylock_stats.json` to your `.gitignore`:
> ```
> venv/
> studylock_stats.json
> ```

---

## 🛠️ Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.8+ · Flask · threading |
| Real-time | Server-Sent Events (SSE) |
| Frontend | Vanilla HTML / CSS / JS |
| Font | JetBrains Mono |
| Persistence | JSON file (no database) |
| App blocking | `subprocess` → `taskkill` / `pkill` |
| Dependencies | `requirements.txt` — one package: `flask` |
| Environment | `venv` — standard Python virtual environment |

---

## 💡 About This Project

**StudyLock is a vibe coding project.**

It was built entirely through conversational AI-assisted development — no scaffolding, no templates, no pre-planned architecture. Each feature was iterated live through prompts: the timer engine, the SSE real-time layer, the floating HUD, the lock screen, the app blocker, the themes. Every bug fix and redesign happened in conversation.

The goal was to see how far you can push a single-file Python app just by describing what you want and iterating on the result. Turns out: pretty far.

If you want to extend it — add a webhook, build a CLI wrapper, wire it to a Raycast extension, or hook it into your `~/.zshrc` — the API is right there at `localhost:5050`.

---

## 📄 License

This project is licensed under the **MIT License** — see the [`LICENSE`](LICENSE) file for details.

```
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
