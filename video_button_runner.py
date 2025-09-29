#!/usr/bin/env python3
import os, time, json, socket, shutil, glob, subprocess, psutil
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
    "--fs",
    "--pause",                 # we’ll unpause when needed
    "--keep-open=always",
    "--idle=yes",
    "--no-osd-bar",
    f"--input-ipc-server={IPC_SOCK}",
    "--really-quiet",
]
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

def kill_existing_mpv():
    if os.path.exists(IPC_SOCK):
        try: os.remove(IPC_SOCK)
        except Exception: pass
    for p in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            if p.info["name"] == "mpv" or (p.info["cmdline"] and "mpv" in p.info["cmdline"][0]):
                p.terminate()
        except Exception:
            pass
    psutil.wait_procs(psutil.process_iter(), timeout=0.1)

def stop_mpv(proc):
    """Gracefully stop the running MPV instance."""
    try:
        # Try IPC quit
        mpv_cmd({"command": ["quit"]})
        time.sleep(0.2)
    except Exception:
        pass
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # Clean socket
    if os.path.exists(IPC_SOCK):
        try: os.remove(IPC_SOCK)
        except Exception: pass

def start_mpv(file_path: Path):
    kill_existing_mpv()
    cmd = MPV_BASE_ARGS + [str(file_path)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def mpv_cmd(obj):
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

def mpv_set_pause(val: bool):
    mpv_cmd({"command":["set_property","pause", bool(val)]})

def mpv_seek_zero_and_pause_show_first_frame():
    mpv_cmd({"command":["set_property","pause", True]})
    mpv_cmd({"command":["seek", 0, "absolute", "exact"]})
    mpv_cmd({"command":["frame-step"]})

def mpv_get_eof_reached() -> bool:
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
                    return bool(isinstance(resp, dict) and resp.get("data") is True)
                except Exception:
                    return False
        except Exception:
            time.sleep(0.05)
    return False

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

def main_loop():
    ensure_dir(TARGET_DIR)
    button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
    last_loaded = None
    mpv_proc = None

    while True:
        # Phase A: see if any USB has updates; only then we’ll close mpv and copy
        needs_list = check_usb_for_updates()
        if needs_list:
            # 1) Close current video instance BEFORE copying
            if mpv_proc and mpv_proc.poll() is None:
                stop_mpv(mpv_proc)
                mpv_proc = None

            # 2) Copy, then UNMOUNT USB (only those we mounted)
            copied = perform_copy_and_unmount(needs_list)

            # 3) If anything copied, start playing immediately
            if copied:
                chosen = pick_video_from_target()
                if chosen:
                    mpv_proc = start_mpv(chosen)
                    # wait for IPC socket to appear then unpause to play
                    for _ in range(50):
                        if os.path.exists(IPC_SOCK): break
                        time.sleep(0.05)
                    mpv_set_pause(False)

                    # Wait for EOF then continue loop to re-check USB
                    while True:
                        if mpv_get_eof_reached():
                            break
                        if mpv_proc.poll() is not None:
                            break
                        time.sleep(0.1)
                    # After EOF, continue (don’t reload unless new copy next time)
                    continue

        # Phase B: normal idle behavior (no new files): show first frame paused, wait for button
        chosen = pick_video_from_target()
        if not chosen:
            time.sleep(2)
            continue

        # (Re)start or ensure paused at first frame
        if (last_loaded is None) or (chosen != last_loaded) or (mpv_proc is None or mpv_proc.poll() is not None):
            mpv_proc = start_mpv(chosen)
            for _ in range(50):
                if os.path.exists(IPC_SOCK): break
                time.sleep(0.05)
            mpv_seek_zero_and_pause_show_first_frame()
            last_loaded = chosen
        else:
            mpv_seek_zero_and_pause_show_first_frame()

        # Wait for button to start playback
        print("wait for button")
        button.wait_for_press()
        print("unpause video")
        mpv_set_pause(False)

        # Play until EOF, then loop (show first frame paused again next cycle)
        while True:
            if mpv_get_eof_reached():
                print("end of file")
                break
            if mpv_proc.poll() is not None:
                break
            time.sleep(0.1)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
