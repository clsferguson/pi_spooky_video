#!/bin/bash
set -e

SERVICE_NAME="video-button.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
SCRIPT_PATH="/home/pi/pi_spooky_video/video_button_runner.py"

# Check script exists
if [ ! -f "$SCRIPT_PATH" ]; then
  echo "❌ Script not found at $SCRIPT_PATH"
  echo "Please place video_button_runner.py there first."
  exit 1
fi


echo "🔧 Installing dependencies"
sudo apt update
sudo apt install -y mpv python3-gpiozero python3-pip python3-psutil


# Create systemd unit file
echo "🔧 Creating $SERVICE_PATH..."
sudo tee "$SERVICE_PATH" > /dev/null <<EOL
[Unit]
Description=Play video on GPIO button (mpv)
After=network-online.target

[Service]
Type=simple
# User=pi
ExecStart=/usr/bin/env python3 $SCRIPT_PATH
Restart=always
RestartSec=2

# If you want USB mounting from within the script, run as root instead of pi
# and uncomment below:
User=root
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOL

VIDEOS_DIR="/home/pi/videos"
# Check if it exists
if [ ! -d "$VIDEOS_DIR" ]; then
    echo "Directory $VIDEOS_DIR does not exist. Creating it..."
    mkdir -p "$VIDEOS_DIR"
# else
    # echo "Directory $VIDEOS_DIR already exists."
fi

# Reload systemd to pick up the new service
echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload

# Enable service to start at boot
echo "✅ Enabling $SERVICE_NAME..."
sudo systemctl enable "$SERVICE_NAME"

# Start service now
echo "▶️ Starting $SERVICE_NAME..."
sudo systemctl start "$SERVICE_NAME"

# Check status
echo "ℹ️ Checking service status..."
sudo systemctl --no-pager --full status "$SERVICE_NAME"
