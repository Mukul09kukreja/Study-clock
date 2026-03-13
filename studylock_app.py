"""
StudyLock v3 — Developer Focus Timer
=====================================
Flask backend · Main panel · Floating HUD overlay

HOW THE OVERLAY WORKS
─────────────────────
Run `python studylock.py` → browser opens the main panel.
Click "Launch HUD" → opens a tiny always-on-top floating window
(use your OS window manager to pin it above other apps).
The HUD polls /api/snapshot every second and shows time + controls.
You can close the main panel and keep only the HUD running.

USAGE
─────
  pip install flask
  python studylock.py

  Then in your browser: open the HUD window and use your OS to
  set it "always on top" (right-click title bar on Windows/Linux,
  or use a tool like afloat on macOS).

  The HUD URL is: http://127.0.0.1:5050/hud
"""

import threading, webbrowser, json, time, os, signal, sys, platform, subprocess
from datetime import datetime, date
from flask import Flask, jsonify, request, render_template_string, Response

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  TIMER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class Timer:
    def __init__(self):
        self.mode    = "free"    # free | pomodoro
        self.phase   = "idle"   # idle | focus | short_break | long_break
        self.running = False

        # Free
        self.free_dur     = 25 * 60
        self.free_elapsed = 0

        # Pomodoro config
        self.cfg = dict(focus=25, short=5, long=15, sessions=4)

        # Pomodoro runtime
        self.pomo_session  = 0
        self.pomo_elapsed  = 0
        self.pomo_phase_s  = 25 * 60   # current phase duration in seconds

        # Threading
        self._lock    = threading.Lock()
        self._evt     = threading.Event()   # set = ticking
        self._thread  = None

        # Stats
        self._sf    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studylock_stats.json")
        self.stats  = self._load_stats()

        # Blocked apps
        self.blocked = []

        # Notifications (consumed per snapshot)
        self._notifs = []

        # SSE subscribers  {id: queue}
        self._subs  = {}
        self._sub_lock = threading.Lock()

    # ── stats ────────────────────────────────────────────────────────────────
    def _load_stats(self):
        today = str(date.today())
        if os.path.exists(self._sf):
            try:
                d = json.load(open(self._sf))
                if d.get("date") == today:
                    return d
            except: pass
        return {"date": today, "sessions": 0, "focus_min": 0, "streak": 0, "history": []}

    def _save(self):
        try: json.dump(self.stats, open(self._sf, "w"), indent=2)
        except: pass

    # ── tick loop ────────────────────────────────────────────────────────────
    def _loop(self):
        while True:
            time.sleep(1)
            self._evt.wait()
            with self._lock:
                if not self.running: continue
                if self.mode == "free":
                    self.free_elapsed = min(self.free_elapsed + 1, self.free_dur)
                    if self.free_elapsed >= self.free_dur:
                        self._finish_free()
                else:
                    self.pomo_elapsed = min(self.pomo_elapsed + 1, self.pomo_phase_s)
                    if self.pomo_elapsed >= self.pomo_phase_s:
                        self._advance_pomo()
            self._broadcast()

    def _finish_free(self):
        mins = self.free_dur // 60
        self.running = False; self._evt.clear(); self.phase = "idle"
        self.stats["sessions"] += 1; self.stats["focus_min"] += mins
        self.stats["streak"]   += 1
        self.stats["history"].append({"type":"free","min":mins,"at":datetime.now().isoformat()})
        self._save()
        self._notif("complete", f"✓ {mins}m session done")
        self._kill_blocked()

    def _advance_pomo(self):
        if self.phase == "focus":
            self.pomo_session += 1
            self.stats["sessions"] += 1; self.stats["focus_min"] += self.cfg["focus"]
            self.stats["history"].append({"type":"focus","min":self.cfg["focus"],
                                          "sess":self.pomo_session,"at":datetime.now().isoformat()})
            self._save()
            if self.pomo_session >= self.cfg["sessions"]:
                self.stats["streak"] += 1; self._save()
                self.running = False; self._evt.clear()
                self.phase = "idle"; self.pomo_session = 0; self.pomo_elapsed = 0
                self._notif("complete", "✓ All sessions done!")
                self._kill_blocked()
            elif self.pomo_session % 4 == 0:
                self._start_break("long_break", self.cfg["long"])
            else:
                self._start_break("short_break", self.cfg["short"])
        elif self.phase in ("short_break","long_break"):
            self.phase = "focus"; self.pomo_elapsed = 0
            self.pomo_phase_s = self.cfg["focus"] * 60
            self._notif("focus", "↩ Break over — back to work")
            self._spawn_blocked()

    def _start_break(self, phase, mins):
        self.phase = phase; self.pomo_elapsed = 0
        self.pomo_phase_s = mins * 60
        self._notif("break", f"↻ {phase.replace('_',' ')} — {mins}m")
        self._kill_blocked()

    # ── SSE broadcast ────────────────────────────────────────────────────────
    def _broadcast(self):
        data = json.dumps(self._snap_raw())
        with self._sub_lock:
            dead = []
            for sid, q in self._subs.items():
                try: q.put_nowait(data)
                except: dead.append(sid)
            for sid in dead: del self._subs[sid]

    def subscribe(self, sid):
        import queue
        q = queue.Queue(maxsize=10)
        with self._sub_lock: self._subs[sid] = q
        return q

    def unsubscribe(self, sid):
        with self._sub_lock: self._subs.pop(sid, None)

    # ── controls ─────────────────────────────────────────────────────────────
    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def start(self):
        with self._lock:
            if self.running: return
            if self.mode == "free":
                self.free_elapsed = 0; self.phase = "focus"
            else:
                if self.phase == "idle":
                    self.pomo_session = 0; self.pomo_elapsed = 0
                    self.pomo_phase_s = self.cfg["focus"] * 60; self.phase = "focus"
            self.running = True; self._evt.set()
            if self.phase == "focus": self._spawn_blocked()
        self._ensure_thread()
        self._broadcast()

    def pause(self):
        with self._lock:
            self.running = False; self._evt.clear()
            self._kill_blocked()
        self._broadcast()

    def resume(self):
        with self._lock:
            if self.phase == "idle": return
            self.running = True; self._evt.set()
            if self.phase == "focus": self._spawn_blocked()
        self._broadcast()

    def restart_phase(self):
        with self._lock:
            if self.mode == "free": self.free_elapsed = 0
            else: self.pomo_elapsed = 0
        self._broadcast()

    def reset(self):
        with self._lock:
            self.running = False; self._evt.clear()
            self.phase = "idle"; self.free_elapsed = 0
            self.pomo_elapsed = 0; self.pomo_session = 0
            self.pomo_phase_s = self.cfg["focus"] * 60
            self._kill_blocked()
        self._broadcast()

    def set_mode(self, mode):
        with self._lock:
            if self.running: return False
            self.mode = mode; self.phase = "idle"
            self.free_elapsed = 0; self.pomo_elapsed = 0
            self.pomo_session = 0
            self.pomo_phase_s = self.cfg["focus"] * 60
            return True

    def set_free_dur(self, minutes):
        with self._lock:
            if not self.running:
                self.free_dur = max(1, int(minutes)) * 60
                self.free_elapsed = 0

    def set_cfg(self, **kw):
        with self._lock:
            if self.running: return False
            if "focus"    in kw: self.cfg["focus"]    = max(1, min(90,  int(kw["focus"])))
            if "short"    in kw: self.cfg["short"]    = max(1, min(30,  int(kw["short"])))
            if "long"     in kw: self.cfg["long"]     = max(1, min(90,  int(kw["long"])))
            if "sessions" in kw: self.cfg["sessions"] = max(1, min(12,  int(kw["sessions"])))
            self.pomo_phase_s = self.cfg["focus"] * 60
            return True

    def set_blocked(self, apps):
        with self._lock:
            self.blocked = [a.strip() for a in apps if a.strip()]

    def reset_stats(self):
        with self._lock:
            self.stats = {"date":str(date.today()),"sessions":0,"focus_min":0,"streak":0,"history":[]}
            self._save()

    # ── app blocking ─────────────────────────────────────────────────────────
    def _kill_blocked(self):
        if not self.blocked: return
        for name in self.blocked:
            try:
                if platform.system() == "Windows":
                    subprocess.run(["taskkill","/F","/IM",name], capture_output=True)
                else:
                    subprocess.run(["pkill","-x",name], capture_output=True)
            except: pass

    def _spawn_blocked(self): pass   # user reopens apps themselves

    # ── notifications ─────────────────────────────────────────────────────────
    def _notif(self, kind, msg): self._notifs.append({"type":kind,"msg":msg})
    def _pop_notifs(self):
        n = self._notifs[:]; self._notifs.clear(); return n

    # ── snapshot ──────────────────────────────────────────────────────────────
    def _snap_raw(self):
        if self.mode == "free":
            total = self.free_dur; elapsed = self.free_elapsed
        else:
            total = self.pomo_phase_s; elapsed = self.pomo_elapsed
        remaining = max(0, total - elapsed)
        return {
            "mode": self.mode, "phase": self.phase,
            "running": self.running,
            "show_lock": self.phase == "focus",
            "remaining": remaining, "total": total, "elapsed": elapsed,
            "progress": elapsed / total if total > 0 else 0,
            "free_dur_min": self.free_dur // 60,
            "cfg": dict(self.cfg),
            "pomo_session": self.pomo_session,
            "stats": self.stats,
            "blocked": self.blocked,
            "notifs": self._pop_notifs(),
        }

    def snapshot(self):
        with self._lock: return self._snap_raw()


# ── singleton ─────────────────────────────────────────────────────────────────
T = Timer()


# ══════════════════════════════════════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def snap(): return jsonify(T.snapshot())

@app.route("/api/snapshot")
def api_snap(): return snap()

@app.route("/api/start",         methods=["POST"])
def api_start():    T.start();         return snap()
@app.route("/api/pause",         methods=["POST"])
def api_pause():    T.pause();         return snap()
@app.route("/api/resume",        methods=["POST"])
def api_resume():   T.resume();        return snap()
@app.route("/api/restart-phase", methods=["POST"])
def api_restart():  T.restart_phase(); return snap()
@app.route("/api/reset",         methods=["POST"])
def api_reset():    T.reset();         return snap()

@app.route("/api/mode", methods=["POST"])
def api_mode():
    T.set_mode((request.json or {}).get("mode","free")); return snap()

@app.route("/api/free-dur", methods=["POST"])
def api_free_dur():
    T.set_free_dur((request.json or {}).get("minutes",25)); return snap()

@app.route("/api/cfg", methods=["POST"])
def api_cfg():
    d = request.json or {}
    T.set_cfg(**{k:d[k] for k in ("focus","short","long","sessions") if k in d})
    return snap()

@app.route("/api/blocked", methods=["POST"])
def api_blocked():
    T.set_blocked((request.json or {}).get("apps",[])); return snap()

@app.route("/api/stats/reset", methods=["POST"])
def api_stats_reset(): T.reset_stats(); return snap()

@app.route("/api/quit", methods=["POST"])
def api_quit():
    threading.Thread(target=lambda:(time.sleep(0.3),os.kill(os.getpid(),signal.SIGTERM)),daemon=True).start()
    return jsonify({"ok":True})

# SSE — real-time push to HUD
@app.route("/api/events")
def api_events():
    import uuid, queue
    sid = str(uuid.uuid4())
    q   = T.subscribe(sid)
    def gen():
        try:
            # send current state immediately
            yield f"data: {json.dumps(T.snapshot())}\n\n"
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"   # keepalive
        finally:
            T.unsubscribe(sid)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/")
def index(): return render_template_string(MAIN_HTML)

@app.route("/hud")
def hud(): return render_template_string(HUD_HTML)


# ══════════════════════════════════════════════════════════════════════════════
#  HUD HTML  — the floating always-on-top overlay
# ══════════════════════════════════════════════════════════════════════════════

HUD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>StudyLock HUD</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--border:#21262d;--acc:#58a6ff;
  --grn:#3fb950;--red:#f85149;--yel:#d29922;
  --txt:#e6edf3;--mut:#8b949e;--sur:#161b22;
}

html,body{width:100%;height:100%;overflow:hidden;user-select:none}

body{
  font-family:'JetBrains Mono',monospace;
  background:var(--bg);
  color:var(--txt);
  border:1px solid var(--border);
  border-radius:10px;
  overflow:hidden;
  cursor:default;
}

/* Drag region - whole body is draggable via title-bar area */
.drag{
  background:var(--sur);
  border-bottom:1px solid var(--border);
  padding:6px 10px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  cursor:move;
  -webkit-app-region:drag;
}
.drag-title{
  font-size:.6rem;font-weight:700;
  letter-spacing:.2em;text-transform:uppercase;
  color:var(--mut);display:flex;align-items:center;gap:6px;
}
.drag-title .dot{
  width:6px;height:6px;border-radius:50%;
  background:var(--mut);transition:background .3s;
}
.drag-title .dot.run{background:var(--grn);box-shadow:0 0 6px var(--grn)}
.drag-title .dot.paused{background:var(--yel)}
.drag-title .dot.break{background:var(--acc);box-shadow:0 0 6px var(--acc)}
.close-btn{
  width:18px;height:18px;border-radius:50%;
  border:none;background:#f85149;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  font-size:.55rem;color:transparent;transition:color .15s;
  -webkit-app-region:no-drag;flex-shrink:0;
}
.close-btn:hover{color:#fff}

/* Main content */
.body{padding:12px 14px;display:flex;flex-direction:column;gap:10px}

/* Phase badge */
.phase-row{display:flex;align-items:center;justify-content:space-between}
.phase-badge{
  font-size:.55rem;font-weight:700;letter-spacing:.18em;
  text-transform:uppercase;padding:3px 8px;
  border-radius:4px;border:1px solid currentColor;
}
.phase-badge.focus {color:var(--red);border-color:rgba(248,81,73,.35);background:rgba(248,81,73,.08)}
.phase-badge.break {color:var(--grn);border-color:rgba(63,185,80,.35);background:rgba(63,185,80,.08)}
.phase-badge.idle  {color:var(--mut);border-color:rgba(139,148,158,.25);background:transparent}
.sess-badge{font-size:.55rem;color:var(--mut);letter-spacing:.06em}

/* Timer */
.timer-row{display:flex;align-items:baseline;gap:6px}
.timer{
  font-size:2.6rem;font-weight:700;
  color:var(--txt);letter-spacing:-.03em;
  line-height:1;font-variant-numeric:tabular-nums;
  transition:color .3s;
}
.timer.focus{color:var(--txt)}
.timer.break{color:var(--grn)}
.timer.paused{color:var(--yel);animation:flicker 2s ease-in-out infinite}
@keyframes flicker{0%,100%{opacity:1}50%{opacity:.6}}

/* Progress bar */
.prog-wrap{
  height:2px;background:var(--border);border-radius:2px;overflow:hidden;position:relative;
}
.prog-fill{
  height:100%;border-radius:2px;
  transition:width .95s linear,background .5s;
}
.prog-fill.focus{background:var(--red)}
.prog-fill.break{background:var(--grn)}
.prog-fill.idle {background:var(--acc)}

/* Pomo dots */
.dots{display:flex;gap:5px;align-items:center;min-height:10px}
.d{width:7px;height:7px;border-radius:50%;border:1px solid var(--mut);transition:all .3s}
.d.done{background:var(--acc);border-color:var(--acc);box-shadow:0 0 4px var(--acc)}
.d.cur{background:var(--red);border-color:var(--red);animation:d-pulse 1.4s ease-in-out infinite}
@keyframes d-pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.6)}}

/* Controls */
.ctrls{display:flex;gap:6px}
.cb{
  flex:1;padding:7px 4px;border-radius:6px;
  border:1px solid var(--border);background:var(--sur);
  color:var(--mut);font-family:'JetBrains Mono',monospace;
  font-size:.62rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;cursor:pointer;
  transition:all .15s;display:flex;align-items:center;
  justify-content:center;gap:4px;white-space:nowrap;
}
.cb:hover{border-color:var(--acc);color:var(--acc);background:rgba(88,166,255,.06)}
.cb.primary{border-color:var(--acc);background:rgba(88,166,255,.12);color:var(--acc)}
.cb.primary:hover{background:rgba(88,166,255,.22)}
.cb.danger:hover{border-color:var(--red);color:var(--red);background:rgba(248,81,73,.08)}
.cb.success{border-color:var(--grn);background:rgba(63,185,80,.12);color:var(--grn)}
.cb.success:hover{background:rgba(63,185,80,.22)}

/* Next up info */
.next-info{
  font-size:.58rem;color:var(--mut);letter-spacing:.06em;
  padding:6px 8px;background:rgba(255,255,255,.02);
  border-radius:5px;border:1px solid var(--border);
  line-height:1.6;
}
.next-info span{color:var(--txt)}

/* Notif flash */
.notif{
  font-size:.6rem;color:var(--grn);letter-spacing:.06em;
  padding:5px 8px;background:rgba(63,185,80,.08);
  border:1px solid rgba(63,185,80,.25);border-radius:5px;
  opacity:0;transition:opacity .3s;text-align:center;
  display:none;
}
.notif.show{opacity:1;display:block}

/* Open main link */
.open-main{
  font-size:.55rem;color:var(--mut);text-align:center;
  text-decoration:none;letter-spacing:.06em;padding:4px;
  transition:color .2s;cursor:pointer;background:none;border:none;
  font-family:'JetBrains Mono',monospace;width:100%;
}
.open-main:hover{color:var(--acc)}

/* Theme variants */
body.zen{
  --bg:#1a1612;--border:#2d261e;--acc:#c9956c;
  --grn:#7cb87c;--red:#c47a5a;--yel:#c9956c;--sur:#211c17;
  font-family:'JetBrains Mono',monospace;
}
body.cyber{
  --bg:#020812;--border:#0a1f3a;--acc:#00d4ff;
  --grn:#00ff88;--red:#ff3366;--yel:#ffaa00;--sur:#041020;
}
body.cyber .timer{text-shadow:0 0 20px var(--txt)}
body.cyber .prog-fill.focus{box-shadow:0 0 8px var(--red)}
body.cyber .prog-fill.break{box-shadow:0 0 8px var(--grn)}
</style>
</head>
<body>

<!-- Title bar (drag handle) -->
<div class="drag">
  <div class="drag-title">
    <div class="dot" id="dot"></div>
    studylock · hud
  </div>
  <button class="close-btn" onclick="window.close()" title="Close HUD">✕</button>
</div>

<!-- Body -->
<div class="body">
  <!-- Phase + session -->
  <div class="phase-row">
    <div class="phase-badge idle" id="phaseBadge">idle</div>
    <div class="sess-badge" id="sessBadge"></div>
  </div>

  <!-- Timer -->
  <div class="timer-row">
    <div class="timer" id="timer">25:00</div>
  </div>

  <!-- Progress -->
  <div class="prog-wrap">
    <div class="prog-fill idle" id="progFill" style="width:100%"></div>
  </div>

  <!-- Pomo dots -->
  <div class="dots" id="dots"></div>

  <!-- Notification flash -->
  <div class="notif" id="notif"></div>

  <!-- Controls -->
  <div class="ctrls" id="ctrls">
    <button class="cb primary" id="playBtn" onclick="handlePlay()">▶ Start</button>
    <button class="cb"         id="restartBtn" onclick="handleRestart()">↺</button>
    <button class="cb danger"  id="resetBtn"   onclick="handleReset()">■ Stop</button>
  </div>

  <!-- Next phase info -->
  <div class="next-info" id="nextInfo" style="display:none"></div>

  <!-- Open main panel -->
  <button class="open-main" onclick="window.open('/','studylock_main','width=760,height=900,left=100,top=50')">
    ↗ open control panel
  </button>
</div>

<script>
let state = null;
let notifTimer;

// ── SSE real-time updates ────────────────────────────────────────────────────
const es = new EventSource('/api/events');
es.onmessage = e => { applySnap(JSON.parse(e.data)); };
es.onerror   = () => { setTimeout(fetchPoll, 2000); };   // fallback poll on error

function fetchPoll() {
  fetch('/api/snapshot').then(r=>r.json()).then(applySnap).catch(()=>{});
}

// ── Apply state ───────────────────────────────────────────────────────────────
function applySnap(s) {
  state = s;
  const ph     = s.phase;
  const run    = s.running;
  const isBreak= ph==='short_break'||ph==='long_break';
  const isFocus= ph==='focus';
  const isIdle = ph==='idle';
  const prog   = s.total > 0 ? (s.remaining/s.total) : 1;

  // Timer
  const timerEl = document.getElementById('timer');
  timerEl.textContent = fmt(s.remaining);
  timerEl.className = 'timer ' + (isBreak?'break': !run&&!isIdle?'paused':'focus');

  // Phase badge
  const badge = document.getElementById('phaseBadge');
  const labels= {idle:'idle',focus:'focus',short_break:'short break',long_break:'long break'};
  badge.textContent = labels[ph]||ph;
  badge.className   = 'phase-badge '+(isBreak?'break':isFocus?'focus':'idle');

  // Session badge
  const sb = document.getElementById('sessBadge');
  if (s.mode==='pomodoro' && !isIdle) {
    sb.textContent = `${s.pomo_session}/${s.cfg.sessions} done`;
  } else sb.textContent = s.mode==='free' ? s.free_dur_min+'m free' : s.mode;

  // Progress bar
  const pf = document.getElementById('progFill');
  pf.style.width    = (prog*100)+'%';
  pf.className      = 'prog-fill '+(isBreak?'break':isFocus?'focus':'idle');

  // Status dot
  const dot = document.getElementById('dot');
  dot.className = 'dot'+(run&&isFocus?' run':run&&isBreak?' break':!run&&!isIdle?' paused':'');

  // Pomo dots
  const dotsEl = document.getElementById('dots');
  if (s.mode==='pomodoro') {
    dotsEl.innerHTML = Array.from({length:s.cfg.sessions},(_,i)=>{
      let c='d';
      if(i<s.pomo_session) c+=' done';
      if(i===s.pomo_session && isFocus) c+=' cur';
      return `<div class="${c}"></div>`;
    }).join('');
  } else dotsEl.innerHTML='';

  // Play button
  const pb = document.getElementById('playBtn');
  if (isIdle)     { pb.textContent='▶ Start';  pb.className='cb primary'; }
  else if (run)   { pb.textContent='⏸ Pause';  pb.className='cb'; }
  else            { pb.textContent='▶ Resume'; pb.className='cb success'; }

  // Next info
  const ni = document.getElementById('nextInfo');
  if (!isIdle && s.mode==='pomodoro') {
    const left = s.cfg.sessions - s.pomo_session;
    const nextBreak = s.pomo_session>0 && s.pomo_session%4===0 ? 'long' : 'short';
    if (isFocus) {
      ni.innerHTML = `next: <span>${s.cfg.short}m break</span> · ${left} session${left!==1?'s':''} left`;
    } else {
      ni.innerHTML = `next: <span>${s.cfg.focus}m focus</span> · session ${s.pomo_session+1} of ${s.cfg.sessions}`;
    }
    ni.style.display='';
  } else if (!isIdle && s.mode==='free') {
    const pct = Math.round(s.progress*100);
    ni.innerHTML = `<span>${pct}%</span> complete · ${fmt(s.elapsed)} elapsed`;
    ni.style.display='';
  } else ni.style.display='none';

  // Notifications
  (s.notifs||[]).forEach(n=>flash(n.msg));
}

function flash(msg) {
  const el=document.getElementById('notif');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(notifTimer);
  notifTimer=setTimeout(()=>el.classList.remove('show'),3500);
}

// ── Controls ──────────────────────────────────────────────────────────────────
function handlePlay() {
  if (!state) return;
  const ep = state.phase==='idle' ? '/api/start' : state.running ? '/api/pause' : '/api/resume';
  post(ep);
}
function handleRestart() { post('/api/restart-phase'); }
function handleReset()   { post('/api/reset'); }

function post(url) {
  fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(applySnap).catch(()=>{});
}

function fmt(s) {
  return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
}

// Apply theme from URL param or localStorage
const theme = new URLSearchParams(location.search).get('theme') ||
              localStorage.getItem('slTheme') || 'dark';
if (theme !== 'dark') document.body.classList.add(theme);
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PANEL HTML
# ══════════════════════════════════════════════════════════════════════════════

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StudyLock — Control Panel</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,700;1,300&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--sur:#161b22;--sur2:#21262d;
  --border:#30363d;--border2:#21262d;
  --acc:#58a6ff;--grn:#3fb950;--red:#f85149;
  --yel:#d29922;--pur:#bc8cff;
  --txt:#e6edf3;--txt2:#c9d1d9;--mut:#8b949e;
  --rad:8px;
  --fd:'JetBrains Mono',monospace;
  --fb:'JetBrains Mono',monospace;
}
body.zen{
  --bg:#100e0c;--sur:#1a1612;--sur2:#251e18;
  --border:#332a22;--border2:#241d18;
  --acc:#c9956c;--grn:#7cb87c;--red:#c47a5a;
  --yel:#c9956c;--pur:#9c7acc;
  --txt:#f0e6d8;--txt2:#d4c4b0;--mut:#8a7060;
}
body.cyber{
  --bg:#020812;--sur:#041020;--sur2:#061828;
  --border:#0a2040;--border2:#061830;
  --acc:#00d4ff;--grn:#00ff88;--red:#ff3366;
  --yel:#ffaa00;--pur:#aa44ff;
  --txt:#c0e8ff;--txt2:#90c8e8;--mut:#2a5878;
}
html{height:100%}
body{
  font-family:var(--fb);background:var(--bg);color:var(--txt);
  min-height:100vh;font-size:13px;line-height:1.5;
}

/* ── BG ── */
.bg{position:fixed;inset:0;pointer-events:none;z-index:0}
body:not(.zen):not(.cyber) .bg{
  background:
    radial-gradient(ellipse 50% 30% at 50% 0%,rgba(88,166,255,.04) 0%,transparent 70%),
    repeating-linear-gradient(0deg,transparent,transparent 24px,rgba(255,255,255,.012) 24px,rgba(255,255,255,.012) 25px);
}
body.cyber .bg{
  background:
    radial-gradient(ellipse 60% 40% at 50% 0%,rgba(0,212,255,.06) 0%,transparent 70%),
    repeating-linear-gradient(0deg,transparent,transparent 24px,rgba(0,212,255,.018) 24px,rgba(0,212,255,.018) 25px),
    repeating-linear-gradient(90deg,transparent,transparent 80px,rgba(0,212,255,.01) 80px,rgba(0,212,255,.01) 81px);
}

/* ── LAYOUT ── */
.wrap{position:relative;z-index:1;max-width:720px;margin:0 auto;padding:32px 24px 60px;display:flex;flex-direction:column;gap:20px}

/* ── TOPBAR ── */
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{
  width:30px;height:30px;border-radius:7px;
  background:linear-gradient(135deg,var(--acc),var(--pur));
  display:flex;align-items:center;justify-content:center;
  font-size:14px;box-shadow:0 0 16px rgba(88,166,255,.3);
}
.logo-text{font-size:.82rem;font-weight:700;letter-spacing:.06em;color:var(--txt)}
.logo-ver{font-size:.6rem;color:var(--mut);margin-left:4px}
.topbar-r{display:flex;align-items:center;gap:10px}
.theme-row{display:flex;gap:6px}
.tp{width:22px;height:22px;border-radius:50%;border:2px solid transparent;cursor:pointer;transition:all .2s}
.tp:hover{transform:scale(1.2)}
.tp.on{border-color:var(--txt)}
.tp[data-t=dark] {background:linear-gradient(135deg,#0d1117 50%,#58a6ff 50%)}
.tp[data-t=zen]  {background:linear-gradient(135deg,#1a1612 50%,#c9956c 50%)}
.tp[data-t=cyber]{background:linear-gradient(135deg,#020812 50%,#00d4ff 50%)}
.icon-btn{
  border:1px solid var(--border);background:var(--sur);
  color:var(--mut);font-size:.72rem;font-family:var(--fb);
  padding:6px 12px;border-radius:var(--rad);cursor:pointer;
  transition:all .2s;letter-spacing:.06em;white-space:nowrap;
}
.icon-btn:hover{border-color:var(--acc);color:var(--acc)}
.icon-btn.hud-btn{
  border-color:var(--acc);color:var(--acc);
  background:rgba(88,166,255,.08);font-weight:700;
}
.icon-btn.hud-btn:hover{background:rgba(88,166,255,.18);box-shadow:0 0 12px rgba(88,166,255,.2)}
.icon-btn.danger:hover{border-color:var(--red);color:var(--red)}

/* ── STATUS STRIP ── */
.status-strip{
  display:flex;align-items:center;gap:12px;
  padding:8px 14px;background:var(--sur);
  border:1px solid var(--border2);border-radius:var(--rad);
  font-size:.65rem;letter-spacing:.06em;color:var(--mut);
}
.sdot{width:6px;height:6px;border-radius:50%;background:var(--mut);flex-shrink:0;transition:all .3s}
.sdot.run   {background:var(--grn);box-shadow:0 0 6px var(--grn);animation:blink 1.5s ease-in-out infinite}
.sdot.paused{background:var(--yel)}
.sdot.break {background:var(--acc)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
.strip-sep{color:var(--border);margin:0 2px}

/* ── GRID: clock + config side-by-side ── */
.main-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:540px){.main-grid{grid-template-columns:1fr}}

/* ── CARD ── */
.card{background:var(--sur);border:1px solid var(--border2);border-radius:var(--rad);padding:18px 20px}
.card-title{
  font-size:.6rem;font-weight:700;letter-spacing:.2em;
  text-transform:uppercase;color:var(--mut);
  margin-bottom:16px;display:flex;align-items:center;gap:8px;
}
.card-title::after{content:'';flex:1;height:1px;background:var(--border2)}

/* ── CLOCK CARD ── */
.clock-card{display:flex;flex-direction:column;align-items:center;gap:14px;padding:22px 20px}

/* Ring */
.ring-wrap{position:relative;width:200px;height:200px}
.ring-svg{width:100%;height:100%;transform:rotate(-90deg)}
.rbg{fill:none;stroke:var(--sur2);stroke-width:8}
.rp {fill:none;stroke:var(--acc);stroke-width:8;stroke-linecap:round;
     transition:stroke-dashoffset .95s linear,stroke .4s;
     filter:drop-shadow(0 0 5px var(--acc))}
body.cyber .rp{filter:drop-shadow(0 0 10px var(--acc)) drop-shadow(0 0 20px var(--acc))}

.clock-inner{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}
.c-phase{font-size:.55rem;font-weight:700;letter-spacing:.2em;text-transform:uppercase;color:var(--acc)}
.c-time{font-size:2.8rem;font-weight:700;color:var(--txt);letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums}
body.cyber .c-time{text-shadow:0 0 16px var(--acc)}
.c-sess{font-size:.58rem;color:var(--mut);letter-spacing:.08em}

/* Pomo dots */
.pomo-dots{display:flex;gap:7px;min-height:14px}
.pd{width:8px;height:8px;border-radius:50%;border:1px solid var(--mut);transition:all .3s}
.pd.done{background:var(--acc);border-color:var(--acc);box-shadow:0 0 4px var(--acc)}
.pd.cur {background:var(--red);border-color:var(--red);animation:pd-p 1.4s ease-in-out infinite}
@keyframes pd-p{0%,100%{transform:scale(1)}50%{transform:scale(1.6)}}

/* ── CONTROLS IN CLOCK CARD ── */
.ctrl-row{display:flex;gap:8px;width:100%}
.cbtn{
  flex:1;padding:9px 8px;border-radius:var(--rad);
  border:1px solid var(--border);background:var(--sur2);
  color:var(--mut);font-family:var(--fb);font-size:.62rem;
  font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  cursor:pointer;transition:all .18s;display:flex;align-items:center;justify-content:center;gap:5px;
}
.cbtn:hover{border-color:var(--acc);color:var(--acc);background:rgba(88,166,255,.07)}
.cbtn.primary{border-color:var(--acc);color:var(--acc);background:rgba(88,166,255,.1)}
.cbtn.primary:hover{background:rgba(88,166,255,.2)}
.cbtn.success{border-color:var(--grn);color:var(--grn);background:rgba(63,185,80,.1)}
.cbtn.success:hover{background:rgba(63,185,80,.2)}
.cbtn.danger{border-color:var(--border)}
.cbtn.danger:hover{border-color:var(--red);color:var(--red);background:rgba(248,81,73,.08)}
.cbtn:disabled{opacity:.3;cursor:not-allowed}

/* ── MODE SWITCH ── */
.mode-sw{display:flex;background:var(--sur2);border-radius:6px;padding:3px;gap:3px;border:1px solid var(--border2);margin-bottom:4px}
.msw{flex:1;padding:7px;border-radius:5px;border:none;background:transparent;color:var(--mut);font-family:var(--fb);font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .2s}
.msw.on{background:var(--sur);color:var(--acc);box-shadow:0 1px 4px rgba(0,0,0,.3)}

/* ── CONFIG ROWS ── */
.cfg-section{display:flex;flex-direction:column;gap:10px}
.cfg-label{font-size:.6rem;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);display:block;margin-bottom:5px}

/* Quick presets */
.presets{display:flex;gap:5px;flex-wrap:wrap}
.preset{
  padding:5px 10px;border-radius:5px;border:1px solid var(--border);
  background:transparent;color:var(--mut);font-family:var(--fb);
  font-size:.62rem;font-weight:700;cursor:pointer;transition:all .18s;
}
.preset:hover{border-color:var(--acc);color:var(--acc)}
.preset.on{border-color:var(--acc);background:rgba(88,166,255,.1);color:var(--acc)}
.preset:disabled{opacity:.3;cursor:not-allowed}

/* Slider */
.sl-wrap{display:flex;align-items:center;gap:8px}
.sl-wrap label{font-size:.6rem;color:var(--mut);white-space:nowrap}
input[type=range]{
  flex:1;-webkit-appearance:none;height:3px;
  background:linear-gradient(to right,var(--acc) var(--pct,33%),var(--border) var(--pct,33%));
  border-radius:3px;outline:none;cursor:pointer;
}
input[type=range]:disabled{opacity:.3;cursor:not-allowed}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:14px;height:14px;border-radius:50%;
  background:var(--acc);border:2px solid var(--bg);box-shadow:0 0 6px var(--acc);cursor:pointer;
}
.sl-val{font-size:.7rem;font-weight:700;color:var(--acc);min-width:32px;text-align:right}

/* Spinners */
.spin-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sf{display:flex;flex-direction:column;gap:5px}
.sf label{font-size:.58rem;letter-spacing:.14em;text-transform:uppercase;color:var(--mut)}
.spinbox{display:flex;align-items:center;background:var(--sur2);border:1px solid var(--border2);border-radius:6px;overflow:hidden}
.sp{width:28px;height:28px;border:none;background:transparent;color:var(--mut);font-size:.85rem;cursor:pointer;transition:all .15s;font-family:monospace;display:flex;align-items:center;justify-content:center}
.sp:hover{background:var(--acc);color:var(--bg)}
.sp:disabled{opacity:.3;cursor:not-allowed}
.spv{flex:1;text-align:center;font-size:.78rem;font-weight:700;color:var(--txt)}

/* ── BLOCKED APPS ── */
.blk-row{display:flex;gap:7px}
.blk-inp{
  flex:1;background:var(--sur2);border:1px solid var(--border2);
  border-radius:6px;padding:8px 12px;color:var(--txt);
  font-family:var(--fb);font-size:.7rem;outline:none;transition:border-color .2s;
}
.blk-inp:focus{border-color:var(--acc)}
.blk-inp::placeholder{color:var(--mut)}
.add-btn{
  padding:8px 14px;border-radius:6px;border:1px solid var(--acc);
  background:rgba(88,166,255,.08);color:var(--acc);font-family:var(--fb);
  font-size:.65rem;font-weight:700;cursor:pointer;transition:all .18s;white-space:nowrap;letter-spacing:.06em;
}
.add-btn:hover{background:rgba(88,166,255,.2)}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}
.tag{
  display:flex;align-items:center;gap:5px;
  background:var(--sur2);border:1px solid var(--border2);
  border-radius:5px;padding:4px 8px;font-size:.65rem;
}
.tag button{border:none;background:transparent;color:var(--mut);cursor:pointer;font-size:.7rem;padding:0;transition:color .15s;line-height:1}
.tag button:hover{color:var(--red)}
.blk-note{font-size:.6rem;color:var(--mut);line-height:1.7;margin-top:4px}
.blk-note code{color:var(--acc);background:rgba(88,166,255,.08);padding:1px 5px;border-radius:3px}

/* ── STATS ── */
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat{
  background:var(--sur2);border:1px solid var(--border2);
  border-radius:var(--rad);padding:14px 10px;text-align:center;
}
.sv{font-size:1.8rem;font-weight:700;color:var(--acc);line-height:1;font-variant-numeric:tabular-nums}
.sl{font-size:.55rem;letter-spacing:.16em;text-transform:uppercase;color:var(--mut);margin-top:4px;display:block}
.stats-footer{display:flex;justify-content:flex-end;margin-top:6px}
.mini-link{
  font-size:.6rem;color:var(--mut);background:none;border:none;
  cursor:pointer;font-family:var(--fb);letter-spacing:.06em;
  transition:color .2s;padding:0;
}
.mini-link:hover{color:var(--red)}

/* ── HISTORY ── */
.hist{display:flex;flex-direction:column;gap:5px;max-height:160px;overflow-y:auto}
.hist::-webkit-scrollbar{width:3px}
.hist::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.hi{
  display:grid;grid-template-columns:auto 1fr auto auto;
  align-items:center;gap:10px;
  padding:6px 10px;background:var(--sur2);border-radius:5px;
  border:1px solid var(--border2);font-size:.65rem;
}
.hi-type{color:var(--acc);font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.hi-time{color:var(--mut)}
.hi-dur{color:var(--txt);font-weight:700;text-align:right}
.hi-bar{height:2px;background:var(--border);border-radius:2px;overflow:hidden;width:40px}
.hi-bar-fill{height:100%;background:var(--acc);border-radius:2px}

/* ── TOAST ── */
.toast{
  position:fixed;bottom:28px;left:50%;
  transform:translateX(-50%) translateY(60px);
  background:var(--sur);border:1px solid var(--acc);
  border-radius:100px;padding:10px 22px;font-size:.7rem;
  font-weight:700;letter-spacing:.08em;color:var(--acc);
  z-index:9999;transition:transform .35s cubic-bezier(.34,1.56,.64,1);
  box-shadow:0 8px 24px rgba(0,0,0,.5);pointer-events:none;
}
.toast.show{transform:translateX(-50%) translateY(0)}

/* ── LOCK OVERLAY ── */
.lock{
  position:fixed;inset:0;z-index:9999;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;
  opacity:0;pointer-events:none;transition:opacity .4s;
  background:rgba(13,17,23,.97);
  font-family:var(--fb);
}
body.zen   .lock{background:rgba(16,14,12,.97)}
body.cyber .lock{background:rgba(2,8,18,.97)}
.lock.on{opacity:1;pointer-events:all}
/* Scanlines for cyber */
body.cyber .lock::before{
  content:'';position:absolute;inset:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,212,255,.015) 3px,rgba(0,212,255,.015) 4px);
  animation:scanline 5s linear infinite;
}
@keyframes scanline{from{background-position:0 0}to{background-position:0 100vh}}

.lk-tag{
  font-size:.6rem;font-weight:700;letter-spacing:.3em;text-transform:uppercase;
  color:var(--mut);display:flex;align-items:center;gap:8px;z-index:1;
}
.lk-tag::before,.lk-tag::after{content:'──────';opacity:.3}
.lk-time{
  font-size:6rem;font-weight:700;color:var(--txt);
  letter-spacing:-.04em;line-height:1;font-variant-numeric:tabular-nums;z-index:1;
}
body.cyber .lk-time{text-shadow:0 0 40px var(--acc),0 0 80px rgba(0,212,255,.4)}
.lk-prog-wrap{width:300px;height:2px;background:var(--sur2);border-radius:2px;overflow:hidden;z-index:1}
.lk-prog-fill{height:100%;background:var(--red);border-radius:2px;box-shadow:0 0 8px var(--red);transition:width .95s linear}
.lk-dots{display:flex;gap:10px;z-index:1;min-height:12px}
.lk-msg{font-size:.7rem;color:var(--mut);letter-spacing:.08em;z-index:1;font-style:italic}
.lk-ctrls{display:flex;gap:10px;z-index:1;margin-top:6px}
.lk-btn{
  padding:10px 22px;border-radius:6px;
  border:1px solid var(--border);background:var(--sur);
  color:var(--mut);font-family:var(--fb);font-size:.65rem;
  font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  cursor:pointer;transition:all .18s;
}
.lk-btn:hover{border-color:var(--acc);color:var(--acc);background:rgba(88,166,255,.08)}
.lk-btn.lk-resume{border-color:var(--acc);color:var(--acc);background:rgba(88,166,255,.1)}
.lk-btn.lk-resume:hover{background:rgba(88,166,255,.22)}
.lk-btn.lk-danger:hover{border-color:var(--red);color:var(--red);background:rgba(248,81,73,.08)}
.lk-paused-tag{
  font-size:.62rem;font-weight:700;letter-spacing:.2em;text-transform:uppercase;
  color:var(--yel);border:1px solid rgba(210,153,34,.4);
  padding:5px 14px;border-radius:4px;background:rgba(210,153,34,.08);z-index:1;display:none;
}
.lock.paused .lk-paused-tag{display:block}

@media(max-width:540px){.main-grid{grid-template-columns:1fr}.lk-time{font-size:4rem}}
</style>
</head>
<body>
<div class="bg"></div>

<!-- LOCK OVERLAY -->
<div class="lock" id="lock">
  <div class="lk-tag">focus mode</div>
  <div class="lk-time"  id="lkTime">25:00</div>
  <div class="lk-prog-wrap"><div class="lk-prog-fill" id="lkFill" style="width:100%"></div></div>
  <div class="lk-dots"  id="lkDots"></div>
  <div class="lk-paused-tag">⏸ paused</div>
  <div class="lk-msg"   id="lkMsg">// stay in the zone</div>
  <div class="lk-ctrls">
    <button class="lk-btn lk-resume" id="lkPlayBtn" onclick="handlePlay()">▶ Resume</button>
    <button class="lk-btn"           onclick="post('/api/restart-phase').then(r=>r.json()).then(apply)">↺ Restart</button>
    <button class="lk-btn lk-danger" onclick="post('/api/reset').then(r=>r.json()).then(apply)">■ Stop</button>
  </div>
</div>

<div class="wrap">

  <!-- TOPBAR -->
  <div class="topbar">
    <div class="logo">
      <div class="logo-icon">🔒</div>
      <div>
        <span class="logo-text">StudyLock</span>
        <span class="logo-ver">v3</span>
      </div>
    </div>
    <div class="topbar-r">
      <div class="theme-row">
        <div class="tp on" data-t="dark"  onclick="setTheme('dark')"  title="Dark"></div>
        <div class="tp"    data-t="zen"   onclick="setTheme('zen')"   title="Warm"></div>
        <div class="tp"    data-t="cyber" onclick="setTheme('cyber')" title="Cyber"></div>
      </div>
      <button class="icon-btn hud-btn" onclick="launchHUD()">⊞ Launch HUD</button>
      <button class="icon-btn danger"  onclick="quitApp()">✕ Quit</button>
    </div>
  </div>

  <!-- STATUS STRIP -->
  <div class="status-strip">
    <div class="sdot" id="sdot"></div>
    <span id="stxt">ready</span>
    <span class="strip-sep">·</span>
    <span id="sdetail">port 5050</span>
    <span class="strip-sep">·</span>
    <span id="smeta">python backend</span>
  </div>

  <!-- MAIN GRID -->
  <div class="main-grid">

    <!-- CLOCK CARD -->
    <div class="card clock-card">
      <div class="card-title">timer</div>

      <div class="ring-wrap">
        <svg class="ring-svg" viewBox="0 0 200 200">
          <circle class="rbg" cx="100" cy="100" r="88"/>
          <circle class="rp"  id="ring" cx="100" cy="100" r="88"
            stroke-dasharray="553" stroke-dashoffset="0"/>
        </svg>
        <div class="clock-inner">
          <div class="c-phase" id="cPhase">idle</div>
          <div class="c-time"  id="cTime">25:00</div>
          <div class="c-sess"  id="cSess"></div>
        </div>
      </div>

      <div class="pomo-dots" id="pomoDots"></div>

      <div class="ctrl-row">
        <button class="cbtn primary" id="playBtn"    onclick="handlePlay()">▶ Start</button>
        <button class="cbtn"         id="restartBtn" onclick="post('/api/restart-phase').then(r=>r.json()).then(apply)" title="Restart phase">↺</button>
        <button class="cbtn danger"  id="resetBtn"   onclick="handleReset()" title="Full stop">■</button>
      </div>
    </div>

    <!-- CONFIG CARD -->
    <div class="card">
      <div class="card-title">config</div>

      <!-- Mode switch -->
      <div class="mode-sw">
        <button class="msw on" id="freeBtn" onclick="setMode('free')">free</button>
        <button class="msw"    id="pomoBtn" onclick="setMode('pomodoro')">pomodoro</button>
      </div>

      <!-- FREE settings -->
      <div id="freePanel" class="cfg-section">
        <div>
          <label class="cfg-label">duration</label>
          <div class="presets" id="presets">
            <button class="preset"    onclick="qt(15,this)">15m</button>
            <button class="preset on" onclick="qt(25,this)">25m</button>
            <button class="preset"    onclick="qt(30,this)">30m</button>
            <button class="preset"    onclick="qt(45,this)">45m</button>
            <button class="preset"    onclick="qt(60,this)">60m</button>
            <button class="preset"    onclick="qt(90,this)">90m</button>
          </div>
        </div>
        <div class="sl-wrap">
          <label>custom</label>
          <input type="range" id="durSl" min="1" max="120" value="25" oninput="onSl(this)">
          <div class="sl-val" id="slVal">25m</div>
        </div>
      </div>

      <!-- POMO settings -->
      <div id="pomoPanel" class="cfg-section" style="display:none">
        <div class="spin-grid">
          <div class="sf"><label>focus</label>
            <div class="spinbox">
              <button class="sp" onclick="spin('focus',-5)">−</button>
              <div class="spv" id="spFocus">25</div>
              <button class="sp" onclick="spin('focus',5)">+</button>
            </div>
          </div>
          <div class="sf"><label>short brk</label>
            <div class="spinbox">
              <button class="sp" onclick="spin('short',-1)">−</button>
              <div class="spv" id="spShort">5</div>
              <button class="sp" onclick="spin('short',1)">+</button>
            </div>
          </div>
          <div class="sf"><label>long brk</label>
            <div class="spinbox">
              <button class="sp" onclick="spin('long',-5)">−</button>
              <div class="spv" id="spLong">15</div>
              <button class="sp" onclick="spin('long',5)">+</button>
            </div>
          </div>
          <div class="sf"><label>sessions</label>
            <div class="spinbox">
              <button class="sp" onclick="spin('sessions',-1)">−</button>
              <div class="spv" id="spSess">4</div>
              <button class="sp" onclick="spin('sessions',1)">+</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div><!-- /main-grid -->

  <!-- BLOCKED APPS -->
  <div class="card">
    <div class="card-title">app blocker</div>
    <div class="blk-row">
      <input class="blk-inp" id="appInp" placeholder="Spotify, Discord, chrome.exe …"
             onkeydown="if(event.key==='Enter')addApp()">
      <button class="add-btn" onclick="addApp()">+ add</button>
    </div>
    <div class="tags" id="tags"></div>
    <div class="blk-note">
      Killed on focus start · unblocked on break.<br>
      Use exact process name: <code>Spotify.exe</code> (Win) · <code>Spotify</code> (Mac) · <code>spotify</code> (Linux)
    </div>
  </div>

  <!-- STATS -->
  <div class="card">
    <div class="card-title">today</div>
    <div class="stats-grid">
      <div class="stat"><div class="sv" id="sSess">0</div><span class="sl">sessions</span></div>
      <div class="stat"><div class="sv" id="sFoc">0m</div><span class="sl">focus time</span></div>
      <div class="stat"><div class="sv" id="sStr">0</div><span class="sl">streak</span></div>
    </div>
    <div class="stats-footer">
      <button class="mini-link" onclick="resetStats()">clear stats</button>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="card" id="histCard" style="display:none">
    <div class="card-title">history</div>
    <div class="hist" id="hist"></div>
  </div>

</div><!-- /wrap -->

<div class="toast" id="toast"></div>

<script>
const CIRC  = 553;   // 2π×88
const POLL  = 1200;

let snap = null, running=false, phase='idle', cfg={focus:25,short:5,long:15,sessions:4};
let blocked=[], theme='dark';

// ── SSE ────────────────────────────────────────────────────────────────────
const es = new EventSource('/api/events');
es.onmessage = e => apply(JSON.parse(e.data));
es.onerror   = () => setTimeout(()=>fetch('/api/snapshot').then(r=>r.json()).then(apply),2000);

// ── Apply ──────────────────────────────────────────────────────────────────
function apply(s) {
  snap = s; running = s.running; phase = s.phase;
  cfg = s.cfg || cfg;

  const isBreak = phase==='short_break'||phase==='long_break';
  const isFocus = phase==='focus';
  const isIdle  = phase==='idle';
  const prog    = s.total>0 ? s.remaining/s.total : 1;
  const t       = fmt(s.remaining);

  // Clock
  document.getElementById('cTime').textContent  = t;
  document.getElementById('cPhase').textContent = {idle:'idle',focus:'focus',short_break:'short break',long_break:'long break'}[phase]||phase;
  const cSess = document.getElementById('cSess');
  if (s.mode==='pomodoro' && !isIdle) cSess.textContent=`session ${s.pomo_session+1} / ${s.cfg.sessions}`;
  else cSess.textContent='';

  // Ring
  const rp = document.getElementById('ring');
  rp.style.strokeDashoffset = CIRC*(1-prog);
  rp.style.stroke = isBreak ? 'var(--grn)' : 'var(--acc)';

  // Play button
  const pb = document.getElementById('playBtn');
  if (isIdle)     {pb.textContent='▶ Start';  pb.className='cbtn primary';}
  else if (running){pb.textContent='⏸ Pause'; pb.className='cbtn';}
  else             {pb.textContent='▶ Resume'; pb.className='cbtn success';}

  // Controls disabled while running
  document.querySelectorAll('.preset,.sp').forEach(b=>b.disabled=running);
  document.getElementById('durSl').disabled=running;

  // Status
  const dot=document.getElementById('sdot'), stxt=document.getElementById('stxt');
  const sdet=document.getElementById('sdetail'), smeta=document.getElementById('smeta');
  if (running&&isFocus)  {dot.className='sdot run';    stxt.textContent='focusing';}
  else if(running&&isBreak){dot.className='sdot break'; stxt.textContent='on break';}
  else if(!running&&!isIdle){dot.className='sdot paused';stxt.textContent='paused';}
  else                   {dot.className='sdot';        stxt.textContent='ready';}
  sdet.textContent = s.mode==='pomodoro' ?
    `${s.pomo_session}/${s.cfg.sessions} sessions done` :
    `${s.free_dur_min}m free`;
  smeta.textContent = `streak: ${s.stats.streak}`;

  // Lock overlay — only during focus
  const lock = document.getElementById('lock');
  lock.classList.toggle('on', isFocus);
  lock.classList.toggle('paused', isFocus && !running);
  document.getElementById('lkTime').textContent = t;
  document.getElementById('lkFill').style.width = (prog*100)+'%';
  const lkPlay = document.getElementById('lkPlayBtn');
  lkPlay.textContent = running ? '⏸ Pause' : '▶ Resume';
  lkPlay.className   = 'lk-btn'+(running?'':' lk-resume');
  // rotating messages
  const msgs=['// stay in the zone','// deep work in progress','// no distractions',
              '// you got this','/* focus = f(time) */','// commit && push later'];
  document.getElementById('lkMsg').textContent = msgs[Math.floor(Date.now()/9000)%msgs.length];

  // Pomo dots (main + lock)
  const dotsHTML = s.mode==='pomodoro' ?
    Array.from({length:s.cfg.sessions},(_,i)=>{
      let c='pd'; if(i<s.pomo_session) c+=' done'; if(i===s.pomo_session&&isFocus) c+=' cur';
      return `<div class="${c}"></div>`;
    }).join('') : '';
  document.getElementById('pomoDots').innerHTML = dotsHTML;
  document.getElementById('lkDots').innerHTML   = dotsHTML;

  // Pomo spinners
  if (s.mode==='pomodoro') {
    document.getElementById('spFocus').textContent = s.cfg.focus;
    document.getElementById('spShort').textContent = s.cfg.short;
    document.getElementById('spLong').textContent  = s.cfg.long;
    document.getElementById('spSess').textContent  = s.cfg.sessions;
  }

  // Stats
  document.getElementById('sSess').textContent = s.stats.sessions;
  document.getElementById('sFoc').textContent  = s.stats.focus_min+'m';
  document.getElementById('sStr').textContent  = s.stats.streak;

  // History
  const hist = s.stats.history||[];
  const hcard = document.getElementById('histCard');
  if (hist.length>0) {
    hcard.style.display='';
    document.getElementById('hist').innerHTML = [...hist].reverse().slice(0,10).map(h=>{
      const d = new Date(h.at||h.time||'');
      const ts = isNaN(d) ? '' : d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
      const maxMin = 90, pct = Math.min(100, (h.min/maxMin)*100);
      return `<div class="hi">
        <span class="hi-type">${h.type}</span>
        <div class="hi-bar"><div class="hi-bar-fill" style="width:${pct}%"></div></div>
        <span class="hi-dur">${h.min}m</span>
        <span class="hi-time">${ts}</span>
      </div>`;
    }).join('');
  } else hcard.style.display='none';

  // Blocked apps sync
  if (JSON.stringify(s.blocked)!==JSON.stringify(blocked)) {
    blocked = s.blocked||[]; renderTags();
  }

  // Notifs
  (s.notifs||[]).forEach(n=>toast(n.msg));
}

// ── Controls ────────────────────────────────────────────────────────────────
function handlePlay() {
  const ep = phase==='idle' ? '/api/start' : running ? '/api/pause' : '/api/resume';
  post(ep).then(r=>r.json()).then(apply);
}
function handleReset() { post('/api/reset').then(r=>r.json()).then(apply); }
function resetStats()  {
  if (!confirm('Clear today\'s stats?')) return;
  post('/api/stats/reset').then(r=>r.json()).then(apply);
}
function quitApp() {
  if (!confirm('Quit StudyLock?')) return;
  post('/api/quit').catch(()=>{}); setTimeout(()=>window.close(),500);
}

// ── Mode ─────────────────────────────────────────────────────────────────────
function setMode(m) {
  if (running) { toast('⚠ Stop timer first'); return; }
  post('/api/mode',{mode:m}).then(r=>r.json()).then(s=>{
    apply(s);
    document.getElementById('freeBtn').classList.toggle('on', m==='free');
    document.getElementById('pomoBtn').classList.toggle('on', m==='pomodoro');
    document.getElementById('freePanel').style.display  = m==='free' ? '' : 'none';
    document.getElementById('pomoPanel').style.display  = m==='pomodoro' ? '' : 'none';
  });
}

// ── Free duration ────────────────────────────────────────────────────────────
function qt(min, btn) {
  if (running) return;
  document.querySelectorAll('.preset').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  const sl=document.getElementById('durSl'); sl.value=min; updSl(sl);
  document.getElementById('slVal').textContent=min+'m';
  post('/api/free-dur',{minutes:min});
}
function onSl(el) {
  updSl(el);
  document.getElementById('slVal').textContent=el.value+'m';
  document.querySelectorAll('.preset').forEach(b=>b.classList.remove('on'));
  if (!running) post('/api/free-dur',{minutes:+el.value});
}
function updSl(el) {
  const pct=((+el.value-1)/119*100).toFixed(1);
  el.style.setProperty('--pct',pct+'%');
}

// ── Pomo spinners ─────────────────────────────────────────────────────────────
function spin(field,d) {
  if (running) return;
  const lim={focus:[1,90],short:[1,30],long:[1,90],sessions:[1,12]};
  cfg[field]=Math.min(lim[field][1],Math.max(lim[field][0],(cfg[field]||5)+d));
  document.getElementById({focus:'spFocus',short:'spShort',long:'spLong',sessions:'spSess'}[field]).textContent=cfg[field];
  post('/api/cfg',{[field]:cfg[field]});
}

// ── Blocked apps ──────────────────────────────────────────────────────────────
function addApp() {
  const inp=document.getElementById('appInp');
  inp.value.split(',').map(s=>s.trim()).filter(Boolean).forEach(n=>{if(!blocked.includes(n))blocked.push(n);});
  inp.value=''; syncBlocked(); renderTags();
}
function removeApp(n) { blocked=blocked.filter(a=>a!==n); syncBlocked(); renderTags(); }
function syncBlocked() { post('/api/blocked',{apps:blocked}); }
function renderTags() {
  document.getElementById('tags').innerHTML=blocked.map(a=>
    `<div class="tag"><span>${a}</span><button onclick="removeApp('${a.replace(/'/g,"\\'")}')" title="remove">✕</button></div>`
  ).join('');
}

// ── HUD launcher ──────────────────────────────────────────────────────────────
function launchHUD() {
  const t=theme==='dark'?'':('?theme='+theme);
  const w=window.open('/hud'+t,'studylock_hud',
    'width=280,height=320,resizable=yes,scrollbars=no,toolbar=no,menubar=no,location=no,status=no,alwaysOnTop=1');
  if (!w) toast('⚠ Allow pop-ups for this site to use the HUD');
  else toast('HUD launched — pin it above other windows with your OS');
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function setTheme(t) {
  theme = t;
  document.body.className = t==='dark' ? '' : t;
  document.querySelectorAll('.tp').forEach(el=>el.classList.toggle('on',el.dataset.t===t));
  localStorage.setItem('slTheme', t);
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastT;
function toast(msg) {
  const el=document.getElementById('toast'); el.textContent=msg; el.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(()=>el.classList.remove('show'),3600);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(s) { return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; }
function post(url,body={}) {
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}

// ── Init ──────────────────────────────────────────────────────────────────────
const savedTheme = localStorage.getItem('slTheme')||'dark';
setTheme(savedTheme);
updSl(document.getElementById('durSl'));
fetch('/api/snapshot').then(r=>r.json()).then(apply);
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  LAUNCHER
# ══════════════════════════════════════════════════════════════════════════════

def open_browser():
    time.sleep(1.0)
    webbrowser.open("http://127.0.0.1:5050")

if __name__ == "__main__":
    w = 56
    print("\n" + "═"*w)
    print("  🔒  StudyLock v3 — Developer Focus Timer")
    print("═"*w)
    print("  Control panel : http://127.0.0.1:5050")
    print("  Floating HUD  : http://127.0.0.1:5050/hud")
    print()
    print("  HOW TO USE THE HUD")
    print("  1. Click '⊞ Launch HUD' in the control panel")
    print("  2. A small 280×320 window opens")
    print("  3. Pin it always-on-top:")
    print("     Windows : right-click title bar → Always on top")
    print("     Linux   : right-click title bar → Always on top")
    print("               or use: xdotool / devilspie2")
    print("     macOS   : use Afloat or Mango5Star")
    print()
    print("  Or just open http://127.0.0.1:5050/hud")
    print("  in a small browser window alongside your editor.")
    print("═"*w + "\n")
    threading.Thread(target=open_browser, daemon=True).start()
    try:
        app.run(host="127.0.0.1", port=5050, debug=False,
                use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        print("\n  StudyLock stopped.\n")
        sys.exit(0)