#!/usr/bin/env python3
import os, time, json, socket, shutil, glob, subprocess, psutil, threading
from pathlib import Path
from gpiozero import Button
from flask import Flask, request, jsonify

# ------------------ CONFIG ------------------
TARGET_DIR = Path.home() / "videos"   # With User=root this is /root/videos
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
BUTTON_PIN = 24  # BCM numbering
IPC_SOCK = "/tmp/mpv-video-sock"
MPV = "mpv"
MPV_BASE_ARGS = [
    MPV,
    "--fs",
    "--keep-open=always",
    "--idle=yes",
    "--no-osd-bar",
    f"--input-ipc-server={IPC_SOCK}",
    "--really-quiet",
]
API_HOST = "0.0.0.0"
API_PORT = 8080
# --------------------------------------------

app = Flask(__name__)

CURRENT_MODE = "idle"      # idle | loop | triggered | custom
CURRENT_FILE = None
WATCHDOG_STOP = threading.Event()

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def log(msg):
    print(f"[VIDEO_RUNNER] {msg}", flush=True)

def kill_existing_mpv():
    if os.path.exists(IPC_SOCK):
        try: os.remove(IPC_SOCK)
        except Exception: pass
    for p in psutil.process_iter(attrs=["name","cmdline"]):
        try:
            if p.info["name"] == "mpv" or (p.info["cmdline"] and "mpv" in p.info["cmdline"][0]):
                p.terminate()
        except Exception:
            pass
    psutil.wait_procs(psutil.process_iter(), timeout=0.1)

def start_mpv_idle():
    kill_existing_mpv()
    proc = subprocess.Popen(MPV_BASE_ARGS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait for IPC socket
    for _ in range(200):
        if os.path.exists(IPC_SOCK): break
        time.sleep(0.025)
    return proc

def mpv_cmd(obj, timeout=2.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(IPC_SOCK)
                s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
                try:
                    s.settimeout(0.2)
                    _ = s.recv(4096)
                except Exception:
                    pass
                return True
        except Exception as e:
            last_err = e
            time.sleep(0.05)
    if last_err:
        log(f"mpv_cmd error: {last_err}")
    return False

def mpv_set_pause(val: bool):
    mpv_cmd({"command":["set_property","pause", bool(val)]})

def mpv_get_eof_reached() -> bool:
    req = {"command":["get_property","eof-reached"]}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(IPC_SOCK)
            s.sendall((json.dumps(req)+"\n").encode("utf-8"))
            s.settimeout(0.2)
            data = s.recv(4096)
            if not data:
                return False
            resp = json.loads(data.decode("utf-8", errors="ignore").splitlines()[-1])
            return bool(isinstance(resp, dict) and resp.get("data") is True)
    except Exception:
        return False

def loadfile(path: Path, loop_inf: bool):
    # Replace current file, set loop-file property, unpause
    mpv_cmd({"command":["loadfile", str(path), "replace"]})
    mpv_cmd({"command":["set_property", "loop-file", "inf" if loop_inf else "no"]})
    mpv_set_pause(False)

def play_loop():
    global CURRENT_MODE, CURRENT_FILE
    loop_path = TARGET_DIR / "loop.mp4"
    if loop_path.exists():
        loadfile(loop_path, loop_inf=True)
        CURRENT_MODE = "loop"
        CURRENT_FILE = str(loop_path)
        log(f"Looping {loop_path}")
        return True
    log("loop.mp4 not found")
    return False

def play_triggered():
    global CURRENT_MODE, CURRENT_FILE
    trig_path = TARGET_DIR / "triggered.mp4"
    if trig_path.exists():
        loadfile(trig_path, loop_inf=False)
        CURRENT_MODE = "triggered"
        CURRENT_FILE = str(trig_path)
        log(f"Triggered {trig_path}")
        return True
    log("triggered.mp4 not found")
    return False

def play_named(name: str):
    global CURRENT_MODE, CURRENT_FILE
    # Restrict to TARGET_DIR to avoid directory traversal
    p = (TARGET_DIR / name).resolve()
    try:
        if not str(p).startswith(str(TARGET_DIR.resolve())):
            return False, "outside videos dir"
    except Exception:
        return False, "path error"
    if not p.exists() or p.suffix.lower() not in VIDEO_EXTS:
        return False, "missing or unsupported"
    loadfile(p, loop_inf=False)
    CURRENT_MODE = "custom"
    CURRENT_FILE = str(p)
    log(f"Custom {p}")
    return True, "ok"

def eof_watchdog():
    # When a one-shot video ends, return to loop.mp4
    while not WATCHDOG_STOP.is_set():
        if CURRENT_MODE in ("triggered","custom") and mpv_get_eof_reached():
            play_loop()
        time.sleep(0.1)

# ------------------ GPIO ------------------
def setup_button():
    btn = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
    btn.when_pressed = lambda: play_triggered()
    return btn
# ------------------------------------------

# ------------------ API -------------------
@app.get("/status")
def api_status():
    return jsonify({"mode": CURRENT_MODE, "file": CURRENT_FILE})

@app.post("/trigger")
@app.get("/trigger")
def api_trigger():
    ok = play_triggered()
    return jsonify({"ok": ok, "mode": CURRENT_MODE, "file": CURRENT_FILE}), (200 if ok else 404)

@app.post("/loop")
@app.get("/loop")
def api_loop():
    ok = play_loop()
    return jsonify({"ok": ok, "mode": CURRENT_MODE, "file": CURRENT_FILE}), (200 if ok else 404)

@app.post("/play")
@app.get("/play")
def api_play():
    # name via querystring or JSON {"name": "..."}
    name = request.args.get("name")
    if not name and request.is_json:
        name = (request.get_json(silent=True) or {}).get("name")
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    ok, msg = play_named(name)
    return jsonify({"ok": ok, "message": msg, "mode": CURRENT_MODE, "file": CURRENT_FILE}), (200 if ok else 404)
# ------------------------------------------

def main():
    ensure_dir(TARGET_DIR)
    # Start mpv idle with IPC
    mpv_proc = start_mpv_idle()
    # Start looping ASAP if available
    play_loop()
    # Start watchdog
    t = threading.Thread(target=eof_watchdog, daemon=True)
    t.start()
    # Setup GPIO
    _btn = setup_button()
    # Start API
    threading.Thread(target=lambda: app.run(host=API_HOST, port=API_PORT, threaded=True), daemon=True).start()
    # Keep the service alive
    try:
        while True:
            time.sleep(1)
            # If mpv crashed, respawn and resume loop
            if mpv_proc.poll() is not None:
                log("mpv exited; restarting")
                mpv_proc = start_mpv_idle()
                play_loop()
    except KeyboardInterrupt:
        pass
    finally:
        WATCHDOG_STOP.set()

if __name__ == "__main__":
    main()
