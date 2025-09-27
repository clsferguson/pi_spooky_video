# Pi Spooky Video

Video player script for raspberry pi triggered by button attached to GPIO

## Features

### 🔌 USB handling

* Detects connected USB storage partitions (/dev/sdX1 style).
* Mounts a partition read-only if not already mounted (to /media/usb).
* Searches common paths on the USB (/, /videos, /Videos, /media) for video files.
* Compares file size/mtime to decide if copy is needed (avoids redundant copies).
* Copies only new or changed video files into ~/videos.
* After copying, unmounts the USB drive (but only if the script mounted it).

### 🎬 Video playback (MPV integration)

* Uses MPV in fullscreen with an IPC socket (/tmp/mpv-video-sock) for control.
* Starts videos paused at the first frame (default idle state).
* Shows the first frame by seeking + frame-step when paused.
* If new videos are copied from USB:
* Stops the running MPV instance.
* Starts MPV with the newest file.
* Immediately begins playback (no pause-first-frame).
* If no new files were copied:
* Always repositions to the first frame paused.
* Waits for a button press to start.

### 🖲 Button trigger (GPIO)
* Uses gpiozero.Button on BCM pin 18 (or BCM 24 if physical pin 18).
* Internal pull-up enabled, so wiring is simple (button between pin and GND).
* Button press → unpauses playback from the first frame.

### 🔁 Looping logic
* After playback finishes (EOF), automatically:
* Returns to idle state (first frame paused).
* Re-checks USB for new files.
* If new files were added, reloads them and plays immediately.
* Keeps looping indefinitely.

### 🧹 Process & socket management
* Cleans up old MPV IPC sockets before starting new MPV instances.
* Gracefully quits MPV via IPC before killing the process.
* Removes leftover sockets to avoid connection errors.

### ⚙️ Other technical details
* Runs safely as a systemd service (with GPIOZERO_PIN_FACTORY=rpigpio recommended).
* Can run either as root or as user pi with the right groups (gpio, video, render).
* Picks the newest video in ~/videos automatically if multiple files exist.
* Works even if no USB is connected (reuses existing videos in ~/videos).
* Supports multiple video formats: .mp4, .mov, .mkv, .avi, .m4v.


## Install
clone this repo and run install script.

```
git@github.com:roymacdonald/pi_spooky_video.git
cd pi_spooky_video
sudo ./install.sh
```