#!/usr/bin/env python3
import os, time, json, socket, shutil, glob, subprocess, psutil, argparse
from pathlib import Path
from gpiozero import Button
from shutil import which
from typing import Optional

# ------------------ CONFIG ------------------
TARGET_DIR = Path.home() / "videos"
USB_MOUNT_BASE = Path("/media")
USB_DEFAULT_MOUNT = USB_MOUNT_BASE / "usb"     # e.g., /media/usb
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
BUTTON_PIN = 24  # BCM numbering (physical header pin 18)

# mpv config
IPC_SOCK = "/tmp/mpv-video-sock"
MPV = "mpv"
MPV_BASE_ARGS = [
    MPV, "--fs", "--pause", "--keep-open=always", "--idle=yes",
    "--no-osd-bar", f"--input-ipc-server={IPC_SOCK}", "--really-quiet",
]

# omxplayer config
OMXPLAYER = "omxplayer"
OMX_BASE_ARGS = [OMXPLAYER, "--no-osd", "--blank"]  # add --adev/local if needed
# --------------------------------------------


# ============ Utility: filesystem & USB ============
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
                    shutil.copy2(src, dst); copied_any = True
                else:
                    sstat, dstat = src.stat(), dst.stat()
                    if (sstat.st_size != dstat.st_size) or (int(sstat.st_mtime) != int(dstat.st_mtime)):
                        shutil.copy2(src, dst); copied_any = True
            except Exception:
                pass
    return copied_any

def check_usb_for_updates():
    """
    Mount USB partitions if needed. If copy required from any, return list of (mnt, mounted_by_us).
    Unmount immediately if nothing needed and we mounted it.
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
                    raise StopIteration
        except StopIteration:
            pass
        if (mnt, mounted_by_us) not in needs and mounted_by_us:
            unmount_path(mnt)
    return needs

def perform_copy_and_unmount(needs_list) -> bool:
    copied_any = False
    for mnt, mounted_by_us in needs_list:
        for cand in scan_usb_candidates(mnt):
            if copy_new_videos(cand, TARGET_DIR):
                copied_any = True
        if mounted_by_us:
            unmount_path(mnt)
    return copied_any

def pick_video_from_target() -> Optional[Path]:
    if not TARGET_DIR.exists():
        return None
    vids = [p for p in TARGET_DIR.iterdir() if p.suffix.lower() in VIDEO_EXTS and p.is_file()]
    if not vids:
        return None
    vids.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return vids[0]


# ============ Player Abstraction ============
class BasePlayer:
    def start_paused(self, file_path: Path): ...
    def start_playing(self, file_path: Path): ...
    def play(self): ...
    def pause(self): ...
    def show_first_frame_paused(self): ...
    def is_eof(self) -> bool: ...
    def stop(self): ...

class MPVPlayer(BasePlayer):
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

    def _wait_ipc(self, timeout=2.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(IPC_SOCK): return True
            time.sleep(0.05)
        return False

    def _ipc(self, obj, timeout=2.0):
        end = time.time() + timeout
        while time.time() < end:
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

    def start_paused(self, file_path: Path):
        self._kill_existing()
        self.proc = subprocess.Popen(MPV_BASE_ARGS + [str(file_path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_ipc()
        self.show_first_frame_paused()

    def start_playing(self, file_path: Path):
        self._kill_existing()
        # start paused then immediately unpause (consistency)
        self.proc = subprocess.Popen(MPV_BASE_ARGS + [str(file_path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_ipc()
        self._ipc({"command":["set_property","pause", False]})

    def play(self):
        self._ipc({"command":["set_property","pause", False]})

    def pause(self):
        self._ipc({"command":["set_property","pause", True]})

    def show_first_frame_paused(self):
        self._ipc({"command":["set_property","pause", True]})
        self._ipc({"command":["seek", 0, "absolute", "exact"]})
        self._ipc({"command":["frame-step"]})

    def is_eof(self) -> bool:
        req = {"command":["get_property","eof-reached"]}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(IPC_SOCK)
                s.sendall((json.dumps(req)+"\n").encode("utf-8"))
                s.settimeout(0.2)
                data = s.recv(4096)
                if not data: return False
                resp = json.loads(data.decode("utf-8", errors="ignore").splitlines()[-1])
                return bool(isinstance(resp, dict) and resp.get("data") is True)
        except Exception:
            return False

    def stop(self):
        try: self._ipc({"command":["quit"]})
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

class OMXPlayer(BasePlayer):
    """
    Controls omxplayer via stdin.
    - Start paused with --pause to show first frame.
    - Play/pause by sending 'p' to stdin.
    - Stop by sending 'q' or terminating the process.
    - 'show_first_frame_paused' is implemented by restarting paused at t=0
      to avoid dealing with escape sequences for seeking over stdin.
    """
    def __init__(self):
        self.proc = None

    def _start(self, file_path: Path, paused: bool):
        # Build args. --pause makes it start paused at first frame.
        args = OMX_BASE_ARGS[:]
        if paused:
            args += ["--pause"]
        args += [str(file_path)]
        # Use a pipe to send key commands
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_paused(self, file_path: Path):
        self.stop()
        self._start(file_path, paused=True)

    def start_playing(self, file_path: Path):
        self.stop()
        # Start paused then unpause immediately (ensures consistent first-frame display)
        self._start(file_path, paused=True)
        time.sleep(0.1)
        self.play()

    def _send_key(self, key: str):
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            try:
                self.proc.stdin.write(key.encode("utf-8"))
                self.proc.stdin.flush()
            except Exception:
                pass

    def play(self):
        # 'p' toggles pause; ensure we send 'p' to unpause from paused state
        self._send_key('p')

    def pause(self):
        # toggles as well; for our flow we don't need two-way state tracking
        self._send_key('p')

    def show_first_frame_paused(self):
        # For simplicity, restart paused at t=0 to render first frame
        if self.proc and self.proc.poll() is None:
            self.stop()
        # We don't know the file here; caller will call start_paused(file)
        # so this method is not used standalone in the omx path.
        pass

    def is_eof(self) -> bool:
        # omxplayer exits when playback finishes
        return self.proc is not None and (self.proc.poll() is not None)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self._send_key('q')
                self.proc.wait(timeout=1.0)
            except Exception:
                try: self.proc.terminate()
                except Exception: pass
        self.proc = None


# ============ Driver / main loop ============
def pick_player(mode: str):
    mode = (mode or "auto").lower()
    env_mode = os.environ.get("VIDEO_PLAYER", "").lower().strip()
    if env_mode in ("mpv","omxplayer"):
        mode = env_mode

    if mode == "mpv":
        if which(MPV): return "mpv", MPVPlayer()
        raise RuntimeError("mpv requested but not found in PATH")
    if mode == "omxplayer":
        if which(OMXPLAYER): return "omxplayer", OMXPlayer()
        raise RuntimeError("omxplayer requested but not found in PATH")

    # auto
    if which(MPV):
        return "mpv", MPVPlayer()
    if which(OMXPLAYER):
        return "omxplayer", OMXPlayer()
    raise RuntimeError("Neither mpv nor omxplayer found. Install one: sudo apt install mpv OR sudo apt install omxplayer")

def main_loop(player_mode: str):
    ensure_dir(TARGET_DIR)
    button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
    player_name, player = pick_player(player_mode)

    last_loaded = None
    current_file = None

    while True:
        # Phase A: check USB; if updates needed, stop player, copy, unmount, then PLAY immediately
        needs_list = check_usb_for_updates()
        if needs_list:
            if hasattr(player, "stop"):
                player.stop()
            copied = perform_copy_and_unmount(needs_list)
            if copied:
                chosen = pick_video_from_target()
                if chosen:
                    current_file = chosen
                    player.start_playing(chosen)  # start playing immediately after copy
                    # Wait until playback ends
                    while True:
                        if player.is_eof():
                            break
                        time.sleep(0.1)
                    # go back to loop (will idle-show first frame)
                    continue

        # Phase B: normal idle → show first frame paused, wait button, then play
        chosen = pick_video_from_target()
        if not chosen:
            time.sleep(2)
            continue

        # (Re)show first frame paused
        if (last_loaded is None) or (chosen != last_loaded):
            current_file = chosen
            # MPV can seek & frame-step; OMX we (re)start paused to show frame 1
            if isinstance(player, MPVPlayer):
                player.start_paused(chosen)
            else:
                player.start_paused(chosen)
            last_loaded = chosen
        else:
            if isinstance(player, MPVPlayer):
                player.show_first_frame_paused()
            else:
                # Restart paused to guarantee first frame
                player.start_paused(current_file)

        # Wait for button to start
        button.wait_for_press()
        player.play()

        # Wait until EOF
        while True:
            if player.is_eof():
                break
            time.sleep(0.1)

        # Loop back; next cycle will present first frame paused again

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Play video on GPIO button with mpv or omxplayer.")
    parser.add_argument("--player", choices=["auto","mpv","omxplayer"], default="auto",
                        help="Select player backend (default: auto)")
    args = parser.parse_args()
    try:
        main_loop(args.player)
    except KeyboardInterrupt:
        pass
