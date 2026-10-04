#!/usr/bin/env bash
# Generic Linux installer for pycentauri. Run as root for a system install.
# The script installs no operating-system packages and never downloads secrets.
set -Eeuo pipefail
umask 022

APP_USER=${APP_USER:-pycentauri}
APP_DIR=${APP_DIR:-/opt/pycentauri}
DATA_DIR=${DATA_DIR:-$APP_DIR/data}
CONFIG_FILE=${CONFIG_FILE:-/etc/pycentauri.conf}
SERVICE=${SERVICE:-pycentauri.service}
SOURCE_DIR=${SOURCE_DIR:-}
SOURCE_ARCHIVE=${SOURCE_ARCHIVE:-}
SOURCE_URL=${SOURCE_URL:-}
REPO_URL=${REPO_URL:-}
REPO_REF=${REPO_REF:-main}
PYCENTAURI_HOST=${PYCENTAURI_HOST:-}
PYCENTAURI_PORT=${PYCENTAURI_PORT:-8787}
PYCENTAURI_BIND=${PYCENTAURI_BIND:-127.0.0.1}
PYCENTAURI_RTSP=${PYCENTAURI_RTSP:-0}
PYCENTAURI_ENABLE_CONTROL=${PYCENTAURI_ENABLE_CONTROL:-0}
PYCENTAURI_ACCESS_CODE=${PYCENTAURI_ACCESS_CODE:-}
MEDIAMTX_PATH=${MEDIAMTX_PATH:-}
PYCENTAURI_LOG_LEVEL=${PYCENTAURI_LOG_LEVEL:-warn}
PYCENTAURI_DETECT=${PYCENTAURI_DETECT:-0}
PYCENTAURI_DETECT_MODEL=${PYCENTAURI_DETECT_MODEL:-data/models/ssd_mobilenet_v2_coco_edgetpu.tflite}
PYCENTAURI_DETECT_ACTION=${PYCENTAURI_DETECT_ACTION:-notify}
PYCENTAURI_DETECT_THRESHOLD=${PYCENTAURI_DETECT_THRESHOLD:-0.65}
PYCENTAURI_TELEGRAM_TOKEN=${PYCENTAURI_TELEGRAM_TOKEN:-}
PYCENTAURI_TELEGRAM_CHAT_ID=${PYCENTAURI_TELEGRAM_CHAT_ID:-}

[[ $EUID -eq 0 ]] || { echo 'Run this installer as root.' >&2; exit 1; }
[[ -n "$PYCENTAURI_HOST" ]] || { echo 'Set PYCENTAURI_HOST to the printer IP or DNS name.' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'python3 is required; install it with your distribution tools.' >&2; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo 'python3-venv (or the equivalent) is required.' >&2; exit 1; }

if [[ "$PYCENTAURI_RTSP" == 1 ]]; then
  command -v ffmpeg >/dev/null || { echo 'RTSP requested but ffmpeg is missing.' >&2; exit 1; }
  if [[ -n "$MEDIAMTX_PATH" ]]; then [[ -x "$MEDIAMTX_PATH" ]] || { echo "Not executable: $MEDIAMTX_PATH" >&2; exit 1; }; fi
  command -v mediamtx >/dev/null 2>&1 || [[ -n "$MEDIAMTX_PATH" ]] || {
    echo 'RTSP requested but MediaMTX is absent. Install and verify it separately.' >&2; exit 1;
  }
fi

get_source() {
  local work=$1 archive extract root
  mkdir -p "$work"
  if [[ -n "$SOURCE_DIR" ]]; then
    cp -a "$SOURCE_DIR/." "$work/"
  elif [[ -n "$SOURCE_ARCHIVE" || -n "$SOURCE_URL" ]]; then
    archive=$work/source.archive
    if [[ -n "$SOURCE_ARCHIVE" ]]; then cp -f "$SOURCE_ARCHIVE" "$archive"; else
      command -v curl >/dev/null || { echo 'curl is required for SOURCE_URL.' >&2; exit 1; }
      curl -fL --retry 3 --proto '=https' --tlsv1.2 "$SOURCE_URL" -o "$archive"
    fi
    extract=$work/extract; mkdir "$extract"
    case "$archive" in
      *.zip) command -v unzip >/dev/null || { echo 'unzip is required for ZIP archives.' >&2; exit 1; }; unzip -q "$archive" -d "$extract" ;;
      *) tar -xf "$archive" -C "$extract" ;;
    esac
    root=$(find "$extract" -name pyproject.toml -type f -print -quit | xargs -r dirname)
    [[ -n "$root" ]] || { echo 'Archive contains no pyproject.toml.' >&2; exit 1; }
    cp -a "$root/." "$work/"
  elif [[ -n "$REPO_URL" ]]; then
    command -v git >/dev/null || { echo 'git is required for REPO_URL.' >&2; exit 1; }
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$work/repo"
    cp -a "$work/repo/." "$work/"
  else
    echo 'Set SOURCE_DIR, SOURCE_ARCHIVE, SOURCE_URL, or REPO_URL.' >&2
    exit 1
  fi
}

install -d -m 0755 "$DATA_DIR"
if ! getent passwd "$APP_USER" >/dev/null; then
  useradd --system --home-dir "$APP_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"
fi
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
get_source "$work/source"
install -d -m 0755 "$APP_DIR"
cp -a "$work/source/." "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/venv/bin/pip" install --no-cache-dir "$APP_DIR[mcp,server]"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod -R a+rX "$APP_DIR" "$DATA_DIR"
# Uniform permissions: app files readable (0644/0755); venv/ and data/ keep their own modes.
find "$APP_DIR" \( -path "$APP_DIR/venv" -o -path "$APP_DIR/data" -o -path "$DATA_DIR" \) -prune -o -type d -exec chmod 0755 {} +
find "$APP_DIR" \( -path "$APP_DIR/venv" -o -path "$APP_DIR/data" -o -path "$DATA_DIR" \) -prune -o -type f -exec chmod 0644 {} +
chmod 0755 "$APP_DIR/install-linux.sh"

if [[ ! -e "$CONFIG_FILE" ]]; then
  install -d -m 0750 "$(dirname "$CONFIG_FILE")"
  cat > "$CONFIG_FILE" <<EOF
# pycentauri service settings; keep this file readable only by root and the service account.
PYCENTAURI_HOST=$PYCENTAURI_HOST
PYCENTAURI_ACCESS_CODE=$PYCENTAURI_ACCESS_CODE
PYCENTAURI_PORT=$PYCENTAURI_PORT
PYCENTAURI_BIND=$PYCENTAURI_BIND
PYCENTAURI_RTSP=$PYCENTAURI_RTSP
PYCENTAURI_ENABLE_CONTROL=$PYCENTAURI_ENABLE_CONTROL
PYCENTAURI_MEDIAMTX_PATH=$MEDIAMTX_PATH
PYCENTAURI_LOG_LEVEL=${PYCENTAURI_LOG_LEVEL:-warn}
PYCENTAURI_DETECT=$PYCENTAURI_DETECT
PYCENTAURI_DETECT_MODEL=$PYCENTAURI_DETECT_MODEL
PYCENTAURI_DETECT_ACTION=$PYCENTAURI_DETECT_ACTION
PYCENTAURI_DETECT_THRESHOLD=$PYCENTAURI_DETECT_THRESHOLD
PYCENTAURI_TELEGRAM_TOKEN=$PYCENTAURI_TELEGRAM_TOKEN
PYCENTAURI_TELEGRAM_CHAT_ID=$PYCENTAURI_TELEGRAM_CHAT_ID
EOF
else
  echo "Keeping existing $CONFIG_FILE; edit it explicitly to change service settings."
fi
chown root:"$APP_USER" "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

if [[ ! -e "$APP_DIR/pycentauri.conf-example" ]]; then
  cat > "$APP_DIR/pycentauri.conf-example" <<'EOF'
# Copy to /etc/pycentauri.conf and adapt for the local printer.
HOST=printer.example
ACCESS_CODE=
PORT=8787
BIND=127.0.0.1
RTSP=0
ENABLE_CONTROL=0
MEDIAMTX_PATH=
# Failed-print (spaghetti) detection; see docs/CORAL_SPAGHETTI_DETECTION.md.
# notify-only by default — pause/stop additionally require ENABLE_CONTROL=1.
DETECT=0
DETECT_MODEL=data/models/ssd_mobilenet_v2_coco_edgetpu.tflite
DETECT_ACTION=notify
DETECT_THRESHOLD=0.65
# Optional Telegram push on detection (bot token from BotFather + chat id).
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  chown root:"$APP_USER" "$APP_DIR/pycentauri.conf-example"
  chmod 0644 "$APP_DIR/pycentauri.conf-example"
fi

if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  command -v curl >/dev/null || { echo 'curl is required for the systemd health check.' >&2; exit 1; }
  rtsp_args=(); [[ "$PYCENTAURI_RTSP" == 1 ]] && rtsp_args+=(--rtsp)
  control_args=(); [[ "$PYCENTAURI_ENABLE_CONTROL" == 1 ]] && control_args+=(--enable-control)
  detect_args=(); [[ "$PYCENTAURI_DETECT" == 1 ]] && detect_args+=(--detect --detect-model "$PYCENTAURI_DETECT_MODEL" --detect-action "$PYCENTAURI_DETECT_ACTION" --detect-threshold "$PYCENTAURI_DETECT_THRESHOLD" --detect-telegram-token "\${PYCENTAURI_TELEGRAM_TOKEN}" --detect-telegram-chat-id "\${PYCENTAURI_TELEGRAM_CHAT_ID}")
  # Inside an unprivileged LXC there is no udevd; libusb needs a hand-made
  # device node plus /run/udev database entry to see the Coral. No-op with
  # a warning when the stick is absent or on a normal host. Only installed
  # when detection is enabled — Coral-less setups get no helper code.
  if [[ "$PYCENTAURI_DETECT" == 1 ]]; then
    install -d -m 0755 /usr/local/sbin
    cat > /usr/local/sbin/pycentauri-usb-prepare <<'USBPREP'
#!/bin/sh
# pycentauri: make the Coral USB Accelerator visible to libusb inside the
# unprivileged LXC. The container runs no udevd, so Debian's libusb (udev
# backend) needs a device node plus a /run/udev database entry for the
# CURRENT bus/dev numbers — the Coral re-enumerates itself whenever an
# open attempt resets it, and Proxmox's dev0 bind holds the node it saw
# at container start. Runs as root via ExecStartPre=+.
set -u

for d in /sys/bus/usb/devices/*; do
    vendor=$(cat "$d/idVendor" 2>/dev/null) || continue
    product=$(cat "$d/idProduct" 2>/dev/null) || continue
    case "$vendor:$product" in
        1a6e:089a|18d1:9302) ;;
        *) continue ;;
    esac
    bus=$(cat "$d/busnum")
    dev=$(cat "$d/devnum")
    minor=$(( (bus - 1) * 128 + (dev - 1) ))
    busdir=$(printf '/dev/bus/usb/%03d' "$bus")
    node=$(printf '%s/%03d' "$busdir" "$dev")
    mkdir -p "$busdir" /run/udev/data
    if [ -e "$node" ]; then
        : # the node exists — udev (or a previous run) manages it
    else
        mknod "$node" c 189 "$minor"
        chmod 0666 "$node"
    fi
    dbfile="/run/udev/data/c189:$minor"
    # On hosts with a running udevd the database entry already exists and is
    # maintained by udev itself — leave it untouched. On udev-less containers
    # (unprivileged LXC, some Docker setups) we provide it, and refresh it
    # when the Coral re-enumerated with a different devnum.
    if ! grep -qs "DEVNAME=$node" "$dbfile"; then
        {
            printf 'I: 1\n'
            printf 'E:DEVPATH=%s\n' "$d"
            printf 'E:SUBSYSTEM=usb\nE:DEVTYPE=usb_device\n'
            printf 'E:DEVNAME=%s\n' "$node"
            printf 'E:ID_BUS=usb\nE:ID_VENDOR_ID=%s\nE:ID_MODEL_ID=%s\n' "$vendor" "$product"
            printf 'E:MAJOR=189\nE:MINOR=%d\n' "$minor"
        } > "$dbfile"
    fi
    echo "pycentauri-usb-prepare: coral ready at $node (c189:$minor)"
    exit 0
done
echo "pycentauri-usb-prepare: coral not on the USB bus — continuing on CPU" >&2
exit 0
USBPREP
    chmod 0755 /usr/local/sbin/pycentauri-usb-prepare
  fi
  prep_line=""
  if [[ "$PYCENTAURI_DETECT" == 1 ]]; then prep_line="ExecStartPre=+/usr/local/sbin/pycentauri-usb-prepare"; fi
  if [[ "$PYCENTAURI_DETECT" == 1 ]]; then
    model_target="$APP_DIR/$PYCENTAURI_DETECT_MODEL"
    if [[ -f "$model_target" ]]; then
      echo "detection model already present: $PYCENTAURI_DETECT_MODEL"
    elif [[ "$PYCENTAURI_DETECT_MODEL" == data/models/ssd_mobilenet_v2_coco_edgetpu.tflite && -x "$APP_DIR/scripts/fetch-smoke-model.sh" ]]; then
      install -d -o "$APP_USER" -g "$APP_USER" "$(dirname "$model_target")"
      sudo -u "$APP_USER" "$APP_DIR/scripts/fetch-smoke-model.sh" "$(dirname "$model_target")" || \
        echo 'Model download failed — run scripts/fetch-smoke-model.sh later.' >&2
    else
      echo 'Detection model not found — place it at the configured PYCENTAURI_DETECT_MODEL path.' >&2
    fi
  fi
  cat > "/etc/systemd/system/$SERVICE" <<EOF
[Unit]
Description=pycentauri printer server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$CONFIG_FILE
${prep_line}
ExecStart=$APP_DIR/venv/bin/centauri server --host \${PYCENTAURI_HOST} --bind \${PYCENTAURI_BIND} --port \${PYCENTAURI_PORT} ${control_args[*]} ${rtsp_args[*]} ${detect_args[*]}
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR $DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
  health_ok=0
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE" && curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:$PYCENTAURI_PORT/api/info" >/dev/null; then health_ok=1; break; fi
    sleep 1
  done
  if [[ "$health_ok" != 1 ]]; then
    systemctl --no-pager --full status "$SERVICE" || true
    journalctl -u "$SERVICE" -n 80 --no-pager || true
    exit 1
  fi
  printf 'Installed %s; health check passed on http://127.0.0.1:%s/api/info\n' "$SERVICE" "$PYCENTAURI_PORT"
else
  printf 'Installed pycentauri in %s. systemd was not detected; start it manually as %s:\n' "$APP_DIR" "$APP_USER"
  printf '  %s %s\n' "$APP_DIR/venv/bin/centauri" "server --host \"$PYCENTAURI_HOST\" --bind \"$PYCENTAURI_BIND\" --port \"$PYCENTAURI_PORT\""
fi
