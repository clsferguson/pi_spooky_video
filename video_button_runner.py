#!/usr/bin/env python3
import os, time, json, socket, shutil, glob, subprocess, psutil, stat
from pathlib import Path
from gpiozero import Button

# ------------------ CONFIG ------------------
TARGET_DIR = Path.home() / "videos"
USB_MOUNT_BASE = Path("/media")
USB_DEFAULT_MOUNT = USB_MOUNT_BASE / "usb"     # e.g., /media/usb
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
BUTTON_PIN = 24  # BCM numbering. pin 18
IPC_SOCK = "/tmp/mpv-video-sock"
MPV = "mpv"
MPV_BASE_ARGS = [
    MPV,
    "--fs",                    # fullscreen
    "--pause",                 # start paused (show first frame)
    "--keep-open=always",      # don't exit at EOF; we’ll seek back to 0
    "--idle=yes",              # keep running for IPC
    "--no-osd-bar",
    f"--input-ipc-server={IPC_SOCK}",
    "--really-quiet",
]
# --------------------------------------------

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def usb_partitions():
    # Return list of /dev/sdX1 style partitions
    parts = sorted(glob.glob("/dev/sd*[0-9]"))
    return parts

def is_mounted(dev):
    try:
        with open("/proc/mounts","r") as f:
            for line in f:
                if line.split()[0] == dev:
                    return line.split()[1]
    except Exception:
        pass
    return None

def mount_partition(dev: str) -> Path:
    # Mount read-only to a stable path
    ensure_dir(USB_DEFAULT_MOUNT)
    if not is_mounted(dev):
        subprocess.run(["mount", "-o", "ro", dev, str(USB_DEFAULT_MOUNT)], check=False)
        # If mount fails (no fs), ignore
    mnt = is_mounted(dev)
    return Path(mnt) if mnt else None

def copy_new_videos(src_dir: Path, dst_dir: Path) -> bool:
    """Copy videos that are new or changed. Returns True if anything was copied."""
    ensure_dir(dst_dir)
    copied_any = False
    for root, _, files in os.walk(src_dir):
        for name in files:
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            src = Path(root) / name
            dst = dst_dir / name
            try:
                if not dst.exists():
                    shutil.copy2(src, dst)
                    copied_any = True
                else:
                    sstat, dstat = src.stat(), dst.stat()
                    # Copy if size or mtime differs
                    if (sstat.st_size != dstat.st_size) or (int(sstat.st_mtime) != int(dstat.st_mtime)):
                        shutil.copy2(src, dst)
                        copied_any = True
            except Exception:
                # ignore per-file issues; keep going
                pass
    return copied_any

def scan_and_copy_from_usb() -> bool:
    """Mount any USB partition and copy videos. True if something new was copied."""
    copied = False
    for dev in usb_partitions():
        mnt = is_mounted(dev)
        if not mnt:
            mnt = mount_partition(dev)
        if mnt:
            # Search common top-level paths
            for candidate in [Path(mnt), Path(mnt)/"videos", Path(mnt)/"Videos", Path(mnt)/"media"]:
                if candidate.exists():
                    if copy_new_videos(candidate, TARGET_DIR):
                        copied = True
    return copied

def pick_video_from_target() -> Path | None:
    if not TARGET_DIR.exists():
        return None
    vids = [p for p in TARGET_DIR.iterdir() if p.suffix.lower() in VIDEO_EXTS and p.is_file()]
    if not vids:
        return None
    # pick newest by mtime
    vids.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return vids[0]

def kill_existing_mpv():
    # If an mpv is already holding the IPC socket, kill it
    if os.path.exists(IPC_SOCK):
        try:
            os.remove(IPC_SOCK)
        except Exception:
            pass
    for p in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            if p.info["name"] == "mpv" or (p.info["cmdline"] and "mpv" in p.info["cmdline"][0]):
                p.terminate()
        except Exception:
            pass
    psutil.wait_procs(psutil.process_iter(), timeout=0.1)

def start_mpv(file_path: Path):
    kill_existing_mpv()
    cmd = MPV_BASE_ARGS + [str(file_path)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def mpv_cmd(obj):
    # Send a JSON command over the IPC socket
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(IPC_SOCK)
                s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
                # Read one line response (optional)
                try:
                    s.settimeout(0.2)
                    s.recv(4096)
                except Exception:
                    pass
                return True
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
    return False

def mpv_set_pause(val: bool):
    mpv_cmd({"command":["set_property","pause", bool(val)]})

def mpv_seek_zero_and_pause_show_first_frame():
    # Seek to start and pause; step 1 frame to ensure first frame is shown
    mpv_cmd({"command":["set_property","pause", True]})
    mpv_cmd({"command":["seek", 0, "absolute", "exact"]})
    # Single frame-step to guarantee the first frame is presented when paused
    mpv_cmd({"command":["frame-step"]})

def mpv_get_eof_reached() -> bool:
    # Poll eof-reached property
    # We’ll ask and parse the reply by opening the socket and reading
    req = {"command":["get_property","eof-reached"]}
    deadline = time.time() + 0.5
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(IPC_SOCK)
                s.sendall((json.dumps(req)+"\n").encode("utf-8"))
                s.settimeout(0.2)
                data = s.recv(4096)
                if not data:
                    return False
                try:
                    resp = json.loads(data.decode("utf-8", errors="ignore").splitlines()[-1])
                    if isinstance(resp, dict) and resp.get("data") is True:
                        return True
                except Exception:
                    pass
                return False
        except Exception:
            time.sleep(0.05)
    return False

def main_loop():
    ensure_dir(TARGET_DIR)

    button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)

    last_loaded = None
    mpv_proc = None

    while True:
        # 1) Check USB and copy if present
        copied = scan_and_copy_from_usb()

        # 2) Choose video (newest in TARGET_DIR)
        chosen = pick_video_from_target()
        if not chosen:
            # Nothing to play yet—sleep and retry
            time.sleep(2)
            continue

        # 3) Start or reuse MPV
        if copied or (last_loaded is None) or (chosen != last_loaded) or (mpv_proc is None or mpv_proc.poll() is not None):
            # Start/restart mpv with the chosen file
            mpv_proc = start_mpv(chosen)
            # Wait for socket and put it at first frame paused
            for _ in range(50):
                if os.path.exists(IPC_SOCK): break
                time.sleep(0.05)
            mpv_seek_zero_and_pause_show_first_frame()
            last_loaded = chosen
        else:
            # No new files; ensure we’re at frame 1, paused
            mpv_seek_zero_and_pause_show_first_frame()

        # 4) Wait for button press to play
        # (Block here until pressed)
        button.wait_for_press()
        # Unpause to start playback
        mpv_set_pause(False)

        # 5) Wait until playback ends (EOF)
        # Poll eof-reached; when True, we’ll present first frame again
        while True:
            if mpv_get_eof_reached():
                break
            # if mpv unexpectedly died, break to restart
            if mpv_proc.poll() is not None:
                break
            time.sleep(0.1)

        # If we reached EOF and no new files were copied this loop,
        # DO NOT reload; just seek back to start and pause for the next trigger.
        # If new files were copied, next iteration will restart mpv with the new file.
        # (This loop will iterate again immediately.)
        continue

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
