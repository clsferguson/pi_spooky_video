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
sudo apt install -y mpv python3-gpiozero python3-pip python3-psutil python3-flask ffmpeg samba samba-common-bin

# Create systemd unit file
echo "🔧 Creating $SERVICE_PATH..."
sudo tee "$SERVICE_PATH" > /dev/null <<EOL
[Unit]
Description=Play video on GPIO button (mpv)
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $SCRIPT_PATH
Restart=always
RestartSec=2

# Run as root to allow USB mounts and access /root/videos
User=root
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOL

# Ensure videos directory exists for both the service and Samba share
VIDEOS_DIR="/root/videos"
if [ ! -d "$VIDEOS_DIR" ]; then
    echo "📁 Creating $VIDEOS_DIR ..."
    sudo mkdir -p "$VIDEOS_DIR"
fi
sudo chmod -R 0777 "$VIDEOS_DIR"

# Configure Samba with the exact smb.conf required
echo "🔧 Writing /etc/samba/smb.conf ..."
sudo cp -a /etc/samba/smb.conf "/etc/samba/smb.conf.backup.$(date +%Y%m%d%H%M%S)" || true
sudo bash -c 'cat > /etc/samba/smb.conf << "EOF"
[global]
server string = pi4
workgroup = WORKGROUP
security = user
map to guest = Bad User
name resolve order = bcast host
local master = no
domain master = no
preferred master = no

[videos]
path = /root/videos
force user = root
force group = root
create mask = 0777
force create mode = 0777
directory mask = 0777
force directory mode = 0777
public = yes
writeable = yes
guest ok = yes
EOF'

echo "🔎 Validating Samba configuration ..."
sudo testparm -s >/dev/null

echo "🔄 Enabling and starting smbd ..."
sudo systemctl enable --now smbd

# Open Samba ports if UFW is active
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -qi active; then
  echo "🌐 UFW detected; allowing Samba ports ..."
  sudo ufw allow 445/tcp || true
  sudo ufw allow 139/tcp || true
  sudo ufw allow 137/udp || true
  sudo ufw allow 138/udp || true
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

# Show how to reach the Samba share
IP=$(hostname -I 2>/dev/null | awk "{print \$1}")
echo "✅ Samba share ready at //${IP}/videos (guest access enabled)"
