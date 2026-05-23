# Bambu Monitor

A small, self-contained Python utility to monitor Bambu Lab A1 printers over MQTT, keep a merged live state and recent history, expose a simple HTTP status UI, and show desktop notifications and popups when prints start or stop.

---

### Overview

**Bambu Monitor** connects to one or more Bambu A1 printers via MQTT in read-only mode, merges incoming status messages into a single per-printer state object, stores a short history of recent messages, and serves:

- **Live JSON state** at **/state.json**  
- **Recent message history** at **/history.json**  
- A lightweight **web UI** at **/** that auto updates  
- A compact **status page** at **/state** for quick glance displays  
- A video style single line status at **/state-video** suitable for PiP or overlay

It also sends desktop notifications, optional popup dialogs, and can flash the Windows console when prints stop or fail.

---

### Features

- **MQTT watcher** for multiple printers with TLS support  
- **Merged state** that accumulates all keys seen from incoming messages  
- **Filtered status** used for start/stop transition detection  
- **History buffer** of recent messages per printer  
- **HTTP server** exposing JSON endpoints and a simple live UI  
- **Desktop notifications** and optional popups and sounds  
- **Windows console flashing** for attention on stop/failure events

---

### Requirements

- **Python 3.9 or newer**  
- Python packages: **paho-mqtt** and **plyer**  
- Optional GUI support for popups requires **tkinter** (usually included with standard Python installs)  
- On Windows the script uses native APIs for flashing and taskbar behavior

---

### Quick Install

```bash
# create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # use venv\Scripts\activate on Windows

# install dependencies
pip install paho-mqtt plyer
```

---

### Configuration

Edit the top of the script to configure printers and behavior. The main settings are grouped under **CONFIGURATION**.

**Key configuration items**

- **PRINTERS** — list of printers to monitor. Each entry requires:
  - **label**: friendly name shown in logs and UI  
  - **serial**: device serial used in MQTT topic  
  - **ip**: printer IP address for MQTT connection  
  - **code**: MQTT password for the device  
- **MQTT_PORT** — default 8883 for TLS  
- **TLS_VERIFY** — enable or disable TLS certificate verification  
- **HTTP_PORT** — port for the built in HTTP server  
- **HISTORY_SIZE** — number of recent messages to keep per printer  
- **ENABLE_NOTIFICATIONS** — toggle desktop notifications  
- **ENABLE_POPUP_ON_STOP** — toggle modal popup on stop/failure  
- **PLAY_SOUND_ON_NOTIFICATION** — toggle playing a sound on notifications  
- **SOUND_FILE** — path to a custom sound file if desired  
- **LOG_LEVEL** — DEBUG, INFO, WARN, ERROR  
- **RESET_STATE_ON_NEW_PRINT** — clear merged state when a new gcode file starts

**Example PRINTERS entry**

```python
PRINTERS = [
    {"label": "A1mini", "serial": "YOUR_PRINTER_SERIAL", "ip": "YOUR_PRINTER_IP", "code": "YOUR_MQTT_PASSWORD"}
]
```

Replace **YOUR_PRINTER_IP** / **YOUR_MQTT_PASSWORD** / **YOUR_PRINTER_SERIAL**.

---

### Running

Start the monitor from the command line:

```bash
python bambu_monitor.py
```

The script will:

- spawn a watcher thread per configured printer  
- start the event consumer that sends notifications and popups  
- start the HTTP server on the configured port

Stop the monitor with **Ctrl+C** or by sending a termination signal.

---

### HTTP Endpoints

- **/** — Live web UI that auto refreshes and shows the merged state per printer  
- **/state.json** — JSON object with the merged state for each printer  
- **/history.json** — JSON object with recent messages per printer (newest first)  
- **/state** — compact HTML status showing first available state and remaining time  
- **/state-video** — single line video stream rendered from the state suitable for PiP or overlay

Use **/state.json** for integrations or to feed other dashboards.

---

### Notifications and Popups

- **Start events** trigger a notification and stop any flashing.  
- **Stop, failed, or pause events** trigger a notification, optional modal popup, and start flashing the console on Windows.  
- A short debounce prevents stop notifications immediately after a start; adjust **START_STOP_DEBOUNCE_SECONDS** in configuration.

Sound playback behavior is platform dependent. Configure **SOUND_FILE** to use a custom sound, or rely on the built in beep/fallback.

---

### Logs and Debugging

- **LOG_LEVEL** controls verbosity. Set to **DEBUG** to see raw MQTT messages and status snapshots.  
- **LOG_RAW_MESSAGES** toggles printing of raw incoming payloads.  
- **LOG_TO_FILE** can be set to a path to persist logs.  
- Console deduplication avoids printing identical successive messages; disable by setting **DEDUPLICATE_CONSOLE_MESSAGES** to False.

If you see no state:

- confirm **PRINTERS** is populated and correct  
- ensure network connectivity to the printer IP and that the device accepts MQTT connections  
- check MQTT port and TLS settings match the device configuration

---

### Troubleshooting

- **No MQTT connection**: verify IP, port, and device code. Check firewall rules.  
- **No notifications on Linux**: ensure a desktop notification daemon is running and that **plyer** supports your environment.  
- **Popups not appearing**: ensure **tkinter** is available and the environment allows GUI windows. On headless systems disable popups.  
- **State not updating**: check logs at DEBUG level to see raw messages and whether the script is receiving MQTT payloads.

---

### Security Notes

- The script stores the device code in memory to authenticate to the printer MQTT topic. Keep the script and configuration secure.  
- TLS verification can be disabled for convenience but enabling certificate verification is recommended where possible.

---

### License

This project is provided as is for personal use. No warranty is provided. Adjust and use at your own risk.

---