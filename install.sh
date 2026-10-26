#!/usr/bin/env bash
set -euo pipefail

# Installer: creates venv, installs requirements, writes desktop file and systemd user service
ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$ROOT_DIR/.venv"

echo "Creating virtualenv..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "Installing pip packages..."
pip install -r "$ROOT_DIR/requirements.txt"

echo "Creating systemd user service..."
SERVICE_FILE="$HOME/.config/systemd/user/iptv-monitor.service"
mkdir -p "$(dirname "$SERVICE_FILE")"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=StreamWatcher IPTV monitor (user-level)
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment=IPTV_PORT=9001
ExecStart=$VENV_DIR/bin/python $ROOT_DIR/run_local_test.py --no-browser
Restart=on-failure

[Install]
WantedBy=default.target
EOF

echo "Creating desktop entry..."
DESKTOP_FILE="$HOME/.local/share/applications/streamwatcher.desktop"
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=StreamWatcher
Comment=IPTV stream validation dashboard
Exec=$VENV_DIR/bin/python $ROOT_DIR/run_local_test.py
Path=$ROOT_DIR
Icon=video-x-generic
Type=Application
Terminal=false
Categories=AudioVideo;Utility;
EOF

echo "Install complete."
echo "Launch StreamWatcher from your application menu, or run:"
echo "  $VENV_DIR/bin/python $ROOT_DIR/run_local_test.py"
echo "Enable the background service with:"
echo "  systemctl --user enable --now iptv-monitor.service"
