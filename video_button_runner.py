#!/usr/bin/env python3
import os, time, json, socket, shutil, glob, subprocess, psutil, argparse
from pathlib import Path
from gpiozero import Button

# ------------------ CONFIG ------------------
TARGET_DIR = Path.home() / "videos"
USB_MOUNT_BASE = Path("/media")
USB_DEFAULT_MOUNT = USB_MOUNT_BASE / "usb"     # e.g., /media/usb
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
BUTTON_PIN = 18  # BCM numbering (use 24 if your wiring is physical header pin 18)

# MPV specifics
IPC_SOCK = "/tmp/mpv-video-sock"
MPV = "mpv"
MPV_BASE_ARGS = [
    MPV,
    "--fs",
    "--pause",                 # start paused
    "--keep-open=always",
    "--idle=yes",
    "--no-osd-bar",
    f"--input-ipc-server={IPC_SOCK}",
    "--really-quiet",
]

# OMXPlayer specifics (stdin control; no dbus needed)
OMXPLAYER = "omxplayer"
# --pause shows first frame on start; --no-osd to keep it clean
OMX_BASE_ARGS = [OMXPLAYER, "--no-osd", "--pause"]
# --------------------------------------------

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def usb_partitions():
    return sorted(glob.glob("/dev/sd*[0-9]"))

def is_mounted(dev):
    try:
        with open("/proc/mounts","r") as f:
            for line in f:
                if line.split()[0] == dev:
                    return line.split()[1]
    except Exception:
        pass
    return None

def mount_partition(dev: str):
    """
    Returns (mountpoint: Path|None, mounted_by_us: bool)
    """
    pre = is_mounted(dev)
    if pre:
        return Path(pre), False
    ensure_dir(USB_DEFAULT_MOUNT)
    subprocess.run(["mount", "-o", "ro", dev, str(USB_DEFAULT_MOUNT)], check=False)
    post = is_mounted(dev)
    return (Path(post), True) if post else (None, False)

def unmount_path(mnt: Path):
    # Best-effort unmount
    subprocess.run(["umount", str(mnt)], check=False)

def would_copy_new_videos(src_dir: Path, dst_dir: Path) -> bool:
    """Dry-run check: True if there exists any video that would be copied/updated."""
    if not src_dir.exists():
        return False
    for root, _, files in os.walk(src_dir):
        for name in files:
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            src = Path(root) / name
            dst = dst_dir / name
            try:
                if not dst.exists():
                    return True
                sstat, dstat = src.stat(), dst.stat()
                if (sstat.st_size != dstat.st_size) or (int(sstat.st_mtime) != int(dstat.st_mtime)):
                    return True
            except Exception:
                # If we can’t stat/compare, assume copy needed
                return True
    return False

def copy_new_videos(src_dir: Path, dst_dir: Path) -> bool:
    """Real copy. Returns True if anything was copied."""
    ensure_dir(dst_dir)
    copied_any = False
    if not src_dir.exists():
        return False
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
                    if (sstat.st_size != dstat.st_size) or (int(sstat.st_mtime) != int(dstat.st_mtime)):
                        shutil.copy2(src, dst)
                        copied_any = True
            except Exception:
                pass
    return copied_any

def scan_usb_candidates(mnt: Path):
    # Places to search within a mounted USB
    return [mnt, mnt / "videos", mnt / "Videos", mnt / "media"]

def pick_video_from_target() -> Path | None:
    if not TARGET_DIR.exists():
        return None
    vids = [p for p in TARGET_DIR.iterdir() if p.suffix.lower() in VIDEO_EXTS and p.is_file()]
    if not vids:
        return None
    vids.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return vids[0]

# ---------- Player abstraction ----------
class Player:
    def start(self, file_path: Path): ...
    def show_first_frame_paused(self): ...
    def play(self): ...
    def wait_eof(self): ...
    def stop(self): ...
    def is_running(self) -> bool: ...

# ----- MPV backend -----
class MPVPlayer(Player):
    def __init__(self):
        self.proc = None

    def _kill_existing(self):
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

    def start(self, file_path: Path):
        self._kill_existing()
        cmd = MPV_BASE_ARGS + [str(file_path)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # wait for IPC socket
        for _ in range(50):
            if os.path.exists(IPC_SOCK): break
            time.sleep(0.05)

    def _cmd(self, obj):
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(IPC_SOCK)
                    s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
                    try:
                        s.settimeout(0.2)
                        s.recv(4096)
                    except Exception:
                        pass
                    return True
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
        return False

    def show_first_frame_paused(self):
        self._cmd({"command":["set_property","pause", True]})
        self._cmd({"command":["seek", 0, "absolute", "exact"]})
        self._cmd({"command":["frame-step"]})

    def play(self):
        self._cmd({"command":["set_property","pause", False]})

    def wait_eof(self):
        # poll eof-reached
        req = {"command":["get_property","eof-reached"]}
        while True:
            if self.proc and self.proc.poll() is not None:
                break
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(IPC_SOCK)
                    s.sendall((json.dumps(req)+"\n").encode("utf-8"))
                    s.settimeout(0.2)
                    data = s.recv(4096)
                    if data:
                        try:
                            resp = json.loads(data.decode("utf-8", errors="ignore").splitlines()[-1])
                            if isinstance(resp, dict) and resp.get("data") is True:
                                break
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(0.1)

    def stop(self):
        # graceful quit
        try: self._cmd({"command":["quit"]})
        except Exception: pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        if os.path.exists(IPC_SOCK):
            try: os.remove(IPC_SOCK)
            except Exception: pass
        self.proc = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

# ----- OMXPlayer backend -----
class OMXPlayerBackend(Player):
    """
    Control via stdin:
      'p' toggle pause, 'q' quit.
    We start with --pause so the first frame is shown paused.
    To re-show first frame, we restart omxplayer paused.
    """
    def __init__(self):
        self.proc = None

    def start(self, file_path: Path):
        # Ensure clean start
        self.stop()
        cmd = OMX_BASE_ARGS + [str(file_path)]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0
        )
        # give it a moment to present first frame
        time.sleep(0.15)

    def show_first_frame_paused(self):
        # Restart to guarantee at 0 paused
        if self.proc and self.proc.poll() is None:
            self.stop()
        # Caller must call start(file) before this; noop otherwise.

    def play(self):
        # toggle pause with 'p' if running
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            try:
                self.proc.stdin.write(b"p")
                self.proc.stdin.flush()
            except Exception:
                pass

    def wait_eof(self):
        if not self.proc:
            return
        while True:
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        self.proc = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

# ---------- USB update helpers ----------
def check_usb_for_updates():
    """
    Mount any USB partitions if needed, decide if copy is needed, and (only if needed)
    return a list of (mountpoint, mounted_by_us) to copy from. If none, returns [].
    """
    needs = []
    for dev in usb_partitions():
        mnt, mounted_by_us = mount_partition(dev)
        if not mnt:
            continue
        try:
            for cand in scan_usb_candidates(mnt):
                if would_copy_new_videos(cand, TARGET_DIR):
                    needs.append((mnt, mounted_by_us))
                    # one hit per device is enough; break to avoid duplicates
                    raise StopIteration
        except StopIteration:
            pass
        # If nothing needed and we mounted it, unmount immediately
        if (mnt, mounted_by_us) not in needs and mounted_by_us:
            unmount_path(mnt)
    return needs

def perform_copy_and_unmount(needs_list) -> bool:
    """
    For each (mountpoint, mounted_by_us) in needs_list, copy and then unmount if mounted_by_us.
    Returns True if anything was copied.
    """
    copied_any = False
    for mnt, mounted_by_us in needs_list:
        for cand in scan_usb_candidates(mnt):
            if copy_new_videos(cand, TARGET_DIR):
                copied_any = True
        if mounted_by_us:
            unmount_path(mnt)
    return copied_any

# ---------- Main loop ----------
def run(player_kind: str):
    ensure_dir(TARGET_DIR)
    button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
    last_loaded = None

    # choose backend
    if player_kind == "omxplayer":
        player: Player = OMXPlayerBackend()
    else:
        player = MPVPlayer()

    while True:
        # A) Check USB; if updates, stop → copy → unmount → start playing immediately
        needs_list = check_usb_for_updates()
        if needs_list:

            # 2) Copy, then UNMOUNT USB (only those we mounted)
            if player.is_running():
                player.stop()
            copied = perform_copy_and_unmount(needs_list)

            # 3) If anything copied, start playing immediately
            if copied:
                chosen = pick_video_from_target()
                if chosen:
                    player.start(chosen)
                    # MPV: unpause to play; OMXPlayer: send 'p' to start
                    if isinstance(player, MPVPlayer):
                        player.play()
                    else:
                        player.play()
                    player.wait_eof()
                    # after EOF, loop back (we’ll re-check USB again)
                    continue

        # B) Normal idle: load newest, show first frame paused, wait for button, then play
        chosen = pick_video_from_target()
        if not chosen:
            time.sleep(2)
            continue

        # (Re)start or ensure paused at first frame
        if (last_loaded is None) or (not player.is_running()) or (chosen != last_loaded):
            player.start(chosen)
            if isinstance(player, MPVPlayer):
                player.show_first_frame_paused()
            # OMXPlayer already starts paused on first frame
            last_loaded = chosen
        else:
            if isinstance(player, MPVPlayer):
                player.show_first_frame_paused()
            # OMX: leave as-is (already paused at first frame)

        # Wait for button press to
