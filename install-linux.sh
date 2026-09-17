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
EOF
else
  echo "Keeping existing $CONFIG_FILE; edit it explicitly to change service settings."
fi
chown root:"$APP_USER" "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

if [[ ! -e "$APP_DIR/pycentauri.conf-example" ]]; then
  cat > "$APP_DIR/pycentauri.conf-example" <<'EOF'
# Copy to /etc/pycentauri.conf and adapt for the local printer.
PYCENTAURI_HOST=printer.example
PYCENTAURI_ACCESS_CODE=
PYCENTAURI_PORT=8787
PYCENTAURI_BIND=127.0.0.1
PYCENTAURI_RTSP=0
PYCENTAURI_ENABLE_CONTROL=0
PYCENTAURI_MEDIAMTX_PATH=
EOF
  chown root:"$APP_USER" "$APP_DIR/pycentauri.conf-example"
  chmod 0644 "$APP_DIR/pycentauri.conf-example"
fi

if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  command -v curl >/dev/null || { echo 'curl is required for the systemd health check.' >&2; exit 1; }
  rtsp_args=(); [[ "$PYCENTAURI_RTSP" == 1 ]] && rtsp_args+=(--rtsp)
  control_args=(); [[ "$PYCENTAURI_ENABLE_CONTROL" == 1 ]] && control_args+=(--enable-control)
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
ExecStart=$APP_DIR/venv/bin/centauri server --host \${PYCENTAURI_HOST} --bind \${PYCENTAURI_BIND} --port \${PYCENTAURI_PORT} ${control_args[*]} ${rtsp_args[*]}
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
