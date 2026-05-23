#!/usr/bin/env python3
"""
bambu_monitor.py

Monitor Bambu A1 printers via MQTT (read-only), keep merged state and history,
expose simple HTTP endpoints (/state.json, /history.json, /), and show notifications.

Edit the CONFIGURATION section below.
Requirements:
- Python 3.9+
- pip install paho-mqtt plyer
"""

from datetime import datetime, timedelta
import json
import ssl
import threading
import queue
import signal
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import subprocess
import sys
import os
import ctypes


import paho.mqtt.client as mqtt
from plyer import notification
import tkinter as tk
from tkinter import messagebox

# -----------------------
# CONFIGURATION
# -----------------------
# Edit these constants to customize behavior.

# Printers to monitor (example):
# PRINTERS = [
#     {"label": "A1mini", "serial": "XXXXXXX", "ip": "192.168.1.100", "code": "mqtt_password"},
# ]
PRINTERS = [{"label": "A1mini", "serial": "XXXXXXXX", "ip": "192.168.1.100", "code": "XXXXXX"}]

# MQTT
MQTT_PORT = 8883
TLS_VERIFY = False

# HTTP server default port
HTTP_PORT = 7890

# History settings
HISTORY_SIZE = 10
HISTORY_INCLUDE_PAYLOAD = True

# Notification settings
ENABLE_NOTIFICATIONS = True
ENABLE_POPUP_ON_STOP = True
PLAY_SOUND_ON_NOTIFICATION = False
# SOUND_FILE examples:
#   Windows: r"C:\Users\me\Sounds\notify.wav"
#   Linux: "/home/me/sounds/notify.wav"
#   macOS: "/Users/me/sounds/notify.aiff"
SOUND_FILE = ""

# Logging / output controls
LOG_LEVEL = "DEBUG"        # DEBUG, INFO, WARN, ERROR
LOG_RAW_MESSAGES = True   # enabled by default to print raw JSON as they arrive
LOG_STATUS_SNAPSHOTS = True
LOG_TO_FILE = r"E:\dl\tmp\bambu-print-monitor\log.txt"          # Example Windows: r"C:\logs\bambu_monitor.log"
LOG_HTTP_MESSAGES = False  # When False, HTTP server will not print per-request logs

# Console deduplication (avoid printing identical successive messages)
DEDUPLICATE_CONSOLE_MESSAGES = True

# Reset merged state when a new print starts and gcode_file changed
RESET_STATE_ON_NEW_PRINT = True

# Keys used to detect transitions; merged state stores all keys seen
PRINT_KEYS = {"gcode_file", "gcode_state", "mc_print_stage", "mc_remaining_time"}

# How many seconds to suppress stop notifications immediately after a start
START_STOP_DEBOUNCE_SECONDS = 5

# Global shutdown flag
SHUTDOWN = threading.Event()
# -----------------------
# END CONFIGURATION
# -----------------------

# Windows API constants
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")

class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("uCount", ctypes.c_uint),
        ("dwTimeout", ctypes.c_uint),
    ]

FLASHW_STOP = 0
FLASHW_CAPTION = 1
FLASHW_TRAY = 2
FLASHW_ALL = FLASHW_CAPTION | FLASHW_TRAY
FLASHW_TIMER = 4
FLASHW_TIMERNOFG = 12

GetConsoleWindow = kernel32.GetConsoleWindow
hwnd = GetConsoleWindow()


# -----------------------
# Utilities
# -----------------------

def flash_window(flags=FLASHW_ALL | FLASHW_TIMERNOFG, count=0, timeout=0):
    info = FLASHWINFO()
    info.cbSize = ctypes.sizeof(info)
    info.hwnd = hwnd
    info.dwFlags = flags
    info.uCount = count
    info.dwTimeout = timeout
    user32.FlashWindowEx(ctypes.byref(info))

def start_flashing():
    flash_window()

def stop_flashing():
    flash_window(flags=FLASHW_STOP)


def iso_now() -> str:
    return datetime.now().isoformat(timespec='seconds')

def _log_to_stream(s: str):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()
    if LOG_TO_FILE:
        try:
            with open(LOG_TO_FILE, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except Exception:
            pass

def log(label: str, *parts, level="INFO"):
    levels = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
    if levels.get(level, 20) < levels.get(LOG_LEVEL, 20):
        return
    msg = " ".join(str(p) for p in parts)
    s = f"[{iso_now()}] [{label}] [{level}] {msg}"
    _log_to_stream(s)

def pretty_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)

def send_notification(title: str, message: str):
    if not ENABLE_NOTIFICATIONS:
        return
    try:
        notification.notify(title=title, message=message, timeout=8)
    except Exception as e:
        log("NOTIF", "Failed to send notification:", e, level="WARN")
    if PLAY_SOUND_ON_NOTIFICATION:
        play_sound(SOUND_FILE)

def _hide_from_taskbar(hwnd):
    """Remove window from taskbar and Alt-Tab."""
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style &= ~WS_EX_APPWINDOW
    style |= WS_EX_TOOLWINDOW
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

def _popup_thread(title, message):
    root = tk.Tk()
    root.withdraw()

    win = tk.Toplevel(root)
    win.title(title)
    win.attributes("-topmost", True)
    win.resizable(False, False)

    # Force window creation so we can modify its style
    win.update_idletasks()

    # Hide from taskbar
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    _hide_from_taskbar(hwnd)

    tk.Label(win, text=message, padx=20, pady=10).pack()
    tk.Button(win, text="OK", command=root.destroy).pack(pady=10)

    root.mainloop()

def popup_modal(title: str, message: str):
    if not ENABLE_POPUP_ON_STOP:
        return
    t = threading.Thread(target=_popup_thread, args=(title, message), daemon=False)
    t.start()

def play_sound(sound_file: str = ""):
    try:
        if sys.platform.startswith("win"):
            try:
                import winsound
                if sound_file and os.path.isfile(sound_file):
                    winsound.PlaySound(sound_file, winsound.SND_FILENAME | winsound.SND_ASYNC)
                else:
                    winsound.Beep(1000, 200)
                return
            except Exception:
                pass
        if sys.platform == "darwin":
            if sound_file and os.path.isfile(sound_file):
                subprocess.Popen(["afplay", sound_file])
            else:
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            return
        if sound_file and os.path.isfile(sound_file):
            for cmd in (["paplay", sound_file], ["aplay", sound_file]):
                try:
                    subprocess.Popen(cmd)
                    return
                except Exception:
                    continue
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception as e:
        log("SOUND", "Failed to play sound:", e, level="WARN")

def safe_removesuffix(s: str, suffix: str) -> str:
    if s.lower().endswith(suffix.lower()):
        return s[: -len(suffix)]
    return s

# -----------------------
# State & history storage
# -----------------------

merged_raw_state = {}
merged_locks = {}
message_history = {}
history_locks = {}

# Per-printer last seen messages for deduplication and last start file
last_raw_payload_text = {}
last_status_snapshot = {}
last_start_gcode_file = {}

# initialize structures for printers present at start
for p in PRINTERS:
    label = p['label']
    merged_raw_state[label] = {}
    merged_locks[label] = threading.Lock()
    message_history[label] = deque(maxlen=HISTORY_SIZE)
    history_locks[label] = threading.Lock()
    last_raw_payload_text[label] = None
    last_status_snapshot[label] = None
    last_start_gcode_file[label] = None

def merge_into_raw_state(label: str, new_print_obj: dict):
    """Thread-safe merge of every key in new_print_obj into merged_raw_state[label]."""
    if label not in merged_raw_state:
        merged_raw_state[label] = {}
        merged_locks[label] = threading.Lock()
    with merged_locks[label]:
        base = merged_raw_state[label]
        for k, v in new_print_obj.items():
            base[k] = v

def add_history_entry(label: str, topic: str, payload_obj):
    """Append a new history entry (newest first)."""
    if label not in message_history:
        message_history[label] = deque(maxlen=HISTORY_SIZE)
        history_locks[label] = threading.Lock()
    entry = {"ts": iso_now(), "topic": topic}
    if HISTORY_INCLUDE_PAYLOAD:
        entry["payload"] = payload_obj
    with history_locks[label]:
        message_history[label].appendleft(entry)

# -----------------------
# Filtered status merging
# -----------------------

def update_status(original: dict, new: dict):
    """Merge only keys in PRINT_KEYS into original (recursive)."""
    for key, value in new.items():
        if key not in PRINT_KEYS:
            continue
        if isinstance(value, dict) and isinstance(original.get(key), dict):
            update_status(original[key], value)
        else:
            original[key] = value

# -----------------------
# MQTT watcher
# -----------------------

class PrinterWatcher:
    """
    Watches device/{serial}/report for one printer.
    Uses:
      - status["is_printing"] to track active prints
      - status["notified_stop"] to avoid duplicate stop notifications
      - status["_last_start_ts"] to debounce stop notifications shortly after a start
    """

    def __init__(self, printer: dict, event_queue: queue.Queue):
        self.printer = printer
        self.event_queue = event_queue
        # status flags: should_update, is_printing, notified_stop, _last_start_ts
        self.status = {"should_update": True, "is_printing": False, "notified_stop": False, "_last_start_ts": None}
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            None,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            transport='tcp'
        )
        self.client.username_pw_set('bblp', printer['code'])
        if TLS_VERIFY:
            self.client.tls_set()
        else:
            self.client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

        self._connected_event = threading.Event()

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        log(self.printer['label'], "Connected:", reason_code)
        topic = f"device/{self.printer['serial']}/report"
        client.subscribe(topic)
        log(self.printer['label'], "Subscribed to topic:", topic)
        self._connected_event.set()

    def on_disconnect(self, client, userdata, rc=None, properties=None, *args, **kwargs):
        # rc may be passed positionally or as a keyword; normalize for logging
        try:
            rc_val = rc if rc is not None else kwargs.get("v1_rc", None)
        except Exception:
            rc_val = None
        log(self.printer['label'], "Disconnected, rc=", rc_val)
        self._connected_event.clear()


    def _just_started_recently(self) -> bool:
        ts = self.status.get("_last_start_ts")
        if not ts:
            return False
        return (datetime.utcnow() - ts) < timedelta(seconds=START_STOP_DEBOUNCE_SECONDS)

    def _maybe_log_raw(self, payload_text: str, topic: str):
        """Log raw payload text unless deduplication prevents it."""
        label = self.printer['label']
        if DEDUPLICATE_CONSOLE_MESSAGES:
            last = last_raw_payload_text.get(label)
            if last == payload_text:
                return
            last_raw_payload_text[label] = payload_text
        log(label, "MQTT message: topic=", topic, "payload=", payload_text, level="DEBUG")

    def _maybe_log_status(self, status_snapshot: dict):
        """Log status snapshot unless deduplication prevents it."""
        label = self.printer['label']
        snap_text = pretty_json(status_snapshot)
        if DEDUPLICATE_CONSOLE_MESSAGES:
            last = last_status_snapshot.get(label)
            if last == snap_text:
                return
            last_status_snapshot[label] = snap_text
        log(label, "Status snapshot:", snap_text, level="DEBUG")

    def _reset_merged_state_if_needed(self, new_gcode_file: str):
        """Reset merged state if configured and gcode_file changed since last start."""
        if not RESET_STATE_ON_NEW_PRINT:
            return
        label = self.printer['label']
        prev = last_start_gcode_file.get(label)
        prev = prev or ""
        new = new_gcode_file or ""
        if new != prev:
            with merged_locks.setdefault(label, threading.Lock()):
                merged_raw_state[label] = {}
            last_start_gcode_file[label] = new
            log(label, "Merged state reset due to new gcode_file:", new, level="DEBUG")

    def on_message(self, client, userdata, msg):
        try:
            payload_text = msg.payload.decode('utf-8', errors='replace')
        except Exception as e:
            payload_text = "<binary or decode error>"
            log(self.printer['label'], "Decode error:", e, level="WARN")

        topic = msg.topic

        # Optionally print raw payload (deduplicated)
        if LOG_RAW_MESSAGES:
            self._maybe_log_raw(payload_text, topic)

        parsed = None
        try:
            parsed = json.loads(payload_text)
        except Exception:
            parsed = payload_text

        # If print object present, merge filtered status and full merged state; always store history
        if isinstance(parsed, dict) and 'print' in parsed and isinstance(parsed['print'], dict):
            print_obj = parsed['print']
            update_status(self.status, print_obj)          # filtered keys (for transitions)
            # reset merged state if new gcode file will be used (handled at start detection)
            merge_into_raw_state(self.printer['label'], print_obj)  # accumulate all keys
            add_history_entry(self.printer['label'], topic, print_obj)
        else:
            add_history_entry(self.printer['label'], topic, parsed)

        # Optionally log status snapshot (deduplicated)
        if LOG_STATUS_SNAPSHOTS:
            self._maybe_log_status(self.status)

        # Transition detection
        stage = str(self.status.get("mc_print_stage", "")).strip()
        gstate = str(self.status.get("gcode_state", "")).strip().upper()

        raw_file = str(self.status.get("gcode_file", "") or "").strip()
        file_disp = raw_file
        for s in (".3mf", ".gcode"):
            if file_disp.lower().endswith(s):
                file_disp = file_disp[: -len(s)]

        now = datetime.utcnow()

        # START detection
        if (stage == '2' or gstate in {"PREPARE", "RUNNING"}) and self.status.get("should_update", True):
            # Optionally reset merged state if gcode_file changed since last start
            self._reset_merged_state_if_needed(self.status.get("gcode_file", ""))
            self.status["should_update"] = False
            self.status["is_printing"] = True
            self.status["notified_stop"] = False
            self.status["_last_start_ts"] = now
            remaining = self.status.get("mc_remaining_time")
            log(self.printer['label'], "New print started:", file_disp, "| remaining min =", remaining)
            self.event_queue.put({
                "printer": self.printer['label'],
                "event": "start",
                "file": file_disp,
                "remaining_min": remaining
            })

        # STOP / FAILURE / PAUSE detection
        elif (stage == '1' or gstate in {"IDLE", "FAILED", "FINISH", "PAUSE"}):
            self.status["should_update"] = True
            # Only notify if we were previously printing and haven't already notified
            if self.status.get("is_printing", False) and not self.status.get("notified_stop", False):
                if self._just_started_recently():
                    log(self.printer['label'], "Detected stop shortly after start; debouncing (skip)", level="DEBUG")
                else:
                    self.status["is_printing"] = False
                    self.status["notified_stop"] = True
                    event_type = "failed" if gstate == "FAILED" else "stopped"
                    # Treat PAUSE as a stop-like event (user requested)
                    if gstate == "PAUSE":
                        event_type = "stopped"
                    log(self.printer['label'], "Print", event_type.upper(), "file:", file_disp, "| last remaining min:", self.status.get("mc_remaining_time"))
                    self.event_queue.put({
                        "printer": self.printer['label'],
                        "event": event_type,
                        "file": file_disp,
                        "remaining_min": self.status.get("mc_remaining_time"),
                        "gcode_state": gstate,
                        "mc_print_stage": stage
                    })
            else:
                log(self.printer['label'], "Stop detected but either not printing or already notified; skipping", level="DEBUG")

    def run_loop(self):
        while not SHUTDOWN.is_set():
            try:
                log(self.printer['label'], "Attempting MQTT connection to", self.printer['ip'])
                self.client.connect(self.printer['ip'], MQTT_PORT, keepalive=60)
                self.client.loop_start()
                while not SHUTDOWN.is_set():
                    if not self._connected_event.is_set():
                        time.sleep(1)
                        continue
                    time.sleep(1)
                break
            except Exception as exc:
                log(self.printer['label'], "Connection exception:", exc, level="WARN")
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except Exception:
                    pass
                if SHUTDOWN.wait(10):
                    break
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        log(self.printer['label'], "Watcher stopped.")

# -----------------------
# Event consumer
# -----------------------

def event_consumer(evq: queue.Queue):
    while not SHUTDOWN.is_set():
        try:
            ev = evq.get(timeout=1)
        except Exception:
            continue
        try:
            printer = ev.get("printer")
            event = ev.get("event")
            file = ev.get("file", "<unknown>")
            if event == "start":
                title = f"{printer} started printing"
                msg = f"{file} — remaining {ev.get('remaining_min')}"
                log(printer, "[EVENT]", title, "-", msg)
                send_notification(title, msg)
                stop_flashing()
            else:
                title = f"{printer} print {event}"
                state = ev.get("gcode_state")
                if "print_error" in ev:
                    state = f"ERROR-{state}"
                msg = f"{file} — state={state} stage={ev.get('mc_print_stage')}"
                log(printer, "[EVENT]", title, "-", msg)
                send_notification(title, msg)
                popup_modal(title, msg)
                start_flashing()
        except Exception as e:
            log("EVENT", "Error handling event:", e, level="ERROR")
    
# -----------------------
# HTTP server (state & history)
# -----------------------

HTML_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Bambu Monitor — Live State</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial; margin: 20px; }
    pre { background:#f6f8fa; padding:10px; border-radius:6px; overflow:auto; }
    header { margin-bottom: 10px; }
    .printer { margin-bottom: 18px; }
  </style>
</head>
<body>
  <header>
    <h1>Bambu Monitor — Live State</h1>
    <p>Automatically updates every 2 seconds. Use <code>/history.json</code> for recent messages.</p>
  </header>
  <div id="content">Loading…</div>

  <script>
    async function fetchState() {
      try {
        const r = await fetch('/state.json?_=' + Date.now());
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const j = await r.json();
        render(j);
      } catch (e) {
        document.getElementById('content').innerHTML = '<pre>Failed to fetch state: ' + String(e) + '</pre>';
      }
    }
    function render(state) {
      const cont = document.getElementById('content');
      if (!state || Object.keys(state).length === 0) {
        cont.innerHTML = '<pre>No state yet. Ensure the monitor is running and printers are configured.</pre>';
        return;
      }
      let html = '';
      for (const label of Object.keys(state)) {
        try {
            html += '<div class="printer"><h2>' + label + (state[label].gcode_state ? ' ' + state[label].gcode_state : '') + (state[label].mc_remaining_time ? ' - ' + state[label].mc_remaining_time : '') + '</h2><pre>' + JSON.stringify(state[label], null, 2) + '</pre></div>';
        } catch(err) {
            console.log(err.msg);
            console.log(JSON.stringify(state[label], null, 2));
        }
      }
      cont.innerHTML = html;
    }
    fetchState();
    setInterval(fetchState, 2000);
  </script>
</body>
</html>
"""

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == '/' or path == '/index.html':
            body = HTML_PAGE.encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if LOG_HTTP_MESSAGES:
                log("HTTP", "Served HTML /")
            return

        if path == '/state.json':
            snapshot = {}
            for label in list(merged_raw_state.keys()):
                lock = merged_locks.get(label)
                if lock:
                    with lock:
                        snapshot[label] = merged_raw_state.get(label, {}).copy()
                else:
                    snapshot[label] = merged_raw_state.get(label, {}).copy()
            body = json.dumps(snapshot, ensure_ascii=False).encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if LOG_HTTP_MESSAGES:
                log("HTTP", "Served /state.json")
            return

        if path == '/history.json':
            out = {}
            for label in list(message_history.keys()):
                lock = history_locks.get(label)
                if lock:
                    with lock:
                        out[label] = list(message_history[label])
                else:
                    out[label] = list(message_history[label])
            body = json.dumps(out, ensure_ascii=False).encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if LOG_HTTP_MESSAGES:
                log("HTTP", "Served /history.json")
            return

        if path == '/state':
            # Find the first available gcode_state from merged_raw_state
            state_text = "UNKNOWN"
            remain_perc = "%"
            remain_min = 0
            hours = 0
            minutes = 0
            for label in list(merged_raw_state.keys()):
                lock = merged_locks.get(label)
                if lock:
                    with lock:
                        gs = merged_raw_state.get(label, {}).get("gcode_state", "")
                        rp = merged_raw_state.get(label, {}).get("mc_percent", "")
                        rm = merged_raw_state.get(label, {}).get("mc_remaining_time", "")
                else:
                    gs = merged_raw_state.get(label, {}).get("gcode_state", "")
                    rp = merged_raw_state.get(label, {}).get("mc_percent", "")
                    rm = merged_raw_state.get(label, {}).get("mc_remaining_time", "")
                if gs:
                    state_text = str(gs)
                if rp:
                    remain_perc = str(rp)
                if rm:
                    remain_min = int(rm)


            #_log_to_stream(f"state_text={state_text} remain_perc={remain_perc} remain_min={remain_min}")
            if remain_min > 0:
                hours = remain_min // 60 
                minutes = remain_min % 60

            # Simple HTML page with auto-refresh every 30 seconds and only the status text
            body_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="30"></head><body>{state_text} {remain_perc}% {hours:02d}:{minutes:02d}</body></html>"""
            body = body_text.encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if LOG_HTTP_MESSAGES:
                log("HTTP", "Served /gcode_state")
            return

            # Insert this inside your request handler where you handle paths (e.g., in do_GET)
        if path == '/state-video':
            body_text = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>State Video</title>
<style>
  html,body{height:100%;margin:0;background:#000;color:#fff;font-family:system-ui,Arial,sans-serif}
  .wrap{display:flex;gap:12px;align-items:center;padding:8px}
  /* video has no padding or border; it will be sized to the text exactly */
  video{display:block;background:transparent;border:0;padding:0;margin:0;line-height:1}
  /* controls kept separate */
  .controls{display:flex;flex-direction:column;gap:6px;font-size:13px;color:#ddd}
  button{padding:6px 10px;border-radius:6px;border:0;background:#0ab4ff;color:#001;cursor:pointer}
  button.secondary{background:#333;color:#fff}
  .small{font-size:12px;color:#9fb6c9}
  /* hide the canvas element visually; video shows the stream */
  canvas{display:none}
</style>
</head>
<body>
  <div class="wrap">
    <video id="video" autoplay muted playsinline></video>

    <div class="controls">
      <div style="display:flex;gap:8px;align-items:center;">
        <button id="dec">-</button>
        <div id="intervalLabel" class="small">1000 ms</div>
        <button id="inc">+</button>
      </div>
      <div style="display:flex;gap:8px;">
        <button id="pip">PiP</button>
        <button id="pause" class="secondary">Pause</button>
      </div>
      <div id="status" class="small">Status: idle</div>
    </div>
  </div>

  <!-- canvas used to render the single-line text; size is set dynamically to fit text exactly -->
  <canvas id="canvas"></canvas>

<script>
(function(){
  const STATE_URL = '/state.json';
  let POLL_MS = 1000;
  const fontFamily = 'system-ui, Arial, sans-serif';
  const fontSize = 28; // base font size in CSS px; adjust if you want larger/smaller text
  const deviceRatio = window.devicePixelRatio || 1;

  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');
  const video = document.getElementById('video');
  const statusEl = document.getElementById('status');
  const pipBtn = document.getElementById('pip');
  const pauseBtn = document.getElementById('pause');
  const decBtn = document.getElementById('dec');
  const incBtn = document.getElementById('inc');
  const intervalLabel = document.getElementById('intervalLabel');

  let polling = true;
  let lastSnapshot = null;
  let pollTimer = null;

  // Start canvas stream -> video
  const stream = canvas.captureStream(30);
  video.srcObject = stream;

  // Format minutes -> HH:MM
  function fmtMinutes(mins) {
    const m = parseInt(mins, 10) || 0;
    const hh = Math.floor(m / 60);
    const mm = m % 60;
    return String(hh).padStart(2,'0') + ':' + String(mm).padStart(2,'0');
  }

  // Build display string from snapshot
  function buildLine(snapshot) {
    let state = 'UNKNOWN';
    let perc = '';
    let remainMin = 0;
    const labels = Object.keys(snapshot || {});
    for (const label of labels) {
      const item = snapshot[label] || {};
      if (item.gcode_state) state = String(item.gcode_state);
      if (item.mc_percent !== undefined && item.mc_percent !== '') perc = String(item.mc_percent);
      if (item.mc_remaining_time) {
        const v = parseInt(item.mc_remaining_time, 10);
        if (!isNaN(v) && v > 0) remainMin = v;
      }
    }
    const timeStr = fmtMinutes(remainMin);
    const percText = perc !== '' ? perc + '%' : '';
    // ensure single spaces between parts, trim ends
    return `${state} ${percText} ${timeStr}`.replace(/\s+/g,' ').trim();
  }

  // Resize canvas to exactly fit the text with no padding and draw it
  function drawExactText(text) {
    // set font for measurement
    const cssFont = `${fontSize}px ${fontFamily}`;
    // use a temporary 2D context for measurement (same ctx is fine)
    ctx.font = cssFont;
    // measure text width
    const metrics = ctx.measureText(text);
    // width from metrics (use actual width)
    const textWidth = Math.ceil(metrics.width);
    // height: approximate using fontSize; include small extra to avoid clipping
    const textHeight = Math.ceil(fontSize * 1.15);

    // set canvas pixel size using devicePixelRatio for crispness
    canvas.width = Math.max(1, Math.ceil(textWidth * deviceRatio));
    canvas.height = Math.max(1, Math.ceil(textHeight * deviceRatio));
    // set CSS size to exact text pixel dimensions (no padding)
    canvas.style.width = textWidth + 'px';
    canvas.style.height = textHeight + 'px';

    // scale drawing for DPR
    ctx.setTransform(deviceRatio, 0, 0, deviceRatio, 0, 0);
    // clear and draw
    ctx.clearRect(0, 0, textWidth, textHeight);
    ctx.fillStyle = '#000'; // background black (transparent could be used but some players prefer opaque)
    ctx.fillRect(0, 0, textWidth, textHeight);
    ctx.fillStyle = '#fff';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.font = cssFont;
    // draw at x=0, y=middle
    ctx.fillText(text, 0, textHeight / 2);
    // update video element size to match canvas CSS size
    video.width = textWidth;
    video.height = textHeight;
    video.style.width = textWidth + 'px';
    video.style.height = textHeight + 'px';
  }

  // fetch loop
  async function fetchAndRender() {
    if (!polling) return scheduleNext();
    try {
      const res = await fetch(STATE_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      lastSnapshot = json;
      const line = buildLine(json);
      drawExactText(line);
      statusEl.textContent = 'Status: updated ' + new Date().toLocaleTimeString();
    } catch (err) {
      statusEl.textContent = 'Status: fetch error';
      console.warn('state-video fetch error', err);
      // if error and we have lastSnapshot, keep it; otherwise show ERROR
      if (!lastSnapshot) drawExactText('ERROR  % 00:00');
    } finally {
      scheduleNext();
    }
  }

  function scheduleNext() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(fetchAndRender, POLL_MS);
    intervalLabel.textContent = POLL_MS + ' ms';
  }

  // keep re-rendering last snapshot so the video stream stays active
  setInterval(() => {
    if (lastSnapshot) drawExactText(buildLine(lastSnapshot));
  }, 1000);

  // Controls
  pipBtn.addEventListener('click', async () => {
    try {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else {
        await video.requestPictureInPicture();
      }
    } catch (e) {
      statusEl.textContent = 'Status: PiP failed';
      console.warn('PiP error', e);
    }
  });

  pauseBtn.addEventListener('click', () => {
    polling = !polling;
    pauseBtn.textContent = polling ? 'Pause' : 'Resume';
    statusEl.textContent = polling ? 'Status: polling' : 'Status: paused';
    if (polling) fetchAndRender();
  });

  decBtn.addEventListener('click', () => {
    POLL_MS = Math.max(200, POLL_MS - 200);
    scheduleNext();
  });
  incBtn.addEventListener('click', () => {
    POLL_MS = Math.min(10000, POLL_MS + 200);
    scheduleNext();
  });

  // initial placeholder and start
  drawExactText('UNKNOWN % 00:00');
  fetchAndRender();

  // expose for debugging
  window.__stateVideo = { setIntervalMs: ms => { POLL_MS = Math.max(200, Number(ms) || 1000); scheduleNext(); }, redraw: () => { if (lastSnapshot) drawExactText(buildLine(lastSnapshot)); } };
})();
</script>
</body>
</html>"""
            body = body_text.encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # optional: prevent caching
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
            return


        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()
        if LOG_HTTP_MESSAGES:
            log("HTTP", "404 for", path)

    def log_message(self, format, *args):
        # Suppress BaseHTTPRequestHandler's default logging; use LOG_HTTP_MESSAGES flag
        if LOG_HTTP_MESSAGES:
            print(f"[{iso_now()}] [HTTP] {format % args}")

def run_http_server(port: int):
    server = HTTPServer(('', port), SimpleHTTPRequestHandler)
    log("HTTP", f"Starting HTTP server on port {port}")
    try:
        server.serve_forever()
    except Exception:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        log("HTTP", "HTTP server stopped.")

# -----------------------
# Main
# -----------------------

def main():
    if not PRINTERS:
        print("No printers configured in PRINTERS list. Edit the script and re-run.")
        return

    # Ensure data structures for configured printers
    for p in PRINTERS:
        label = p['label']
        if label not in merged_raw_state:
            merged_raw_state[label] = {}
            merged_locks[label] = threading.Lock()
        if label not in message_history:
            message_history[label] = deque(maxlen=HISTORY_SIZE)
            history_locks[label] = threading.Lock()
        else:
            history_locks[label] = history_locks.get(label, threading.Lock())
            if message_history[label].maxlen != HISTORY_SIZE:
                message_history[label] = deque(message_history[label], maxlen=HISTORY_SIZE)
        last_raw_payload_text[label] = last_raw_payload_text.get(label)
        last_status_snapshot[label] = last_status_snapshot.get(label)
        last_start_gcode_file[label] = last_start_gcode_file.get(label)

    evq = queue.Queue()

    t_consumer = threading.Thread(target=event_consumer, args=(evq,), daemon=True)
    t_consumer.start()

    watcher_threads = []
    for p in PRINTERS:
        w = PrinterWatcher(p, evq)
        t = threading.Thread(target=w.run_loop, daemon=True)
        t.start()
        watcher_threads.append(t)
        log("MAIN", f"Started watcher for {p['label']} at {p['ip']}")

    http_thread = threading.Thread(target=run_http_server, args=(HTTP_PORT,), daemon=True)
    http_thread.start()

    def handle_sigint(signum, frame):
        log("MAIN", "Shutdown requested, stopping...")
        SHUTDOWN.set()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    try:
        while not SHUTDOWN.is_set():
            time.sleep(1)
    finally:
        SHUTDOWN.set()
        log("MAIN", "Waiting for threads to stop...")
        for t in watcher_threads:
            t.join(timeout=2)
        t_consumer.join(timeout=2)
        log("MAIN", "Exited cleanly.")

if __name__ == "__main__":
    main()
