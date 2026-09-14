#!/usr/bin/env bash
# Idempotent Debian 12 LXC installer. Run as root. No credentials are fetched.
set -Eeuo pipefail
umask 022
APP_USER=${APP_USER:-pycentauri}; APP_DIR=${APP_DIR:-/opt/pycentauri}; DATA_DIR=${DATA_DIR:-/var/lib/pycentauri}
SERVICE=${SERVICE:-pycentauri.service}; SOURCE_DIR=${SOURCE_DIR:-}; SOURCE_ARCHIVE=${SOURCE_ARCHIVE:-}; SOURCE_URL=${SOURCE_URL:-}; REPO_URL=${REPO_URL:-}; REPO_REF=${REPO_REF:-main}
PYCENTAURI_HOST=${PYCENTAURI_HOST:-}; PYCENTAURI_PORT=${PYCENTAURI_PORT:-8787}; PYCENTAURI_BIND=${PYCENTAURI_BIND:-0.0.0.0}
PYCENTAURI_RTSP=${PYCENTAURI_RTSP:-0}; PYCENTAURI_ENABLE_CONTROL=${PYCENTAURI_ENABLE_CONTROL:-1}; INSTALL_FFMPEG=${INSTALL_FFMPEG:-0}; MEDIAMTX_PATH=${MEDIAMTX_PATH:-}
[[ $EUID -eq 0 ]] || { echo 'Run this installer as root.' >&2; exit 1; }
[[ -n "$PYCENTAURI_HOST" ]] || { echo 'Set PYCENTAURI_HOST to the printer hostname/IP.' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv python3-dev build-essential
if [[ "$INSTALL_FFMPEG" == 1 ]]; then apt-get install -y --no-install-recommends ffmpeg; fi
if [[ "$PYCENTAURI_RTSP" == 1 ]]; then
  command -v ffmpeg >/dev/null || { echo 'RTSP requested but ffmpeg is missing; set INSTALL_FFMPEG=1.' >&2; exit 1; }
  if [[ -n "$MEDIAMTX_PATH" ]]; then [[ -x "$MEDIAMTX_PATH" ]] || { echo "Not executable: $MEDIAMTX_PATH" >&2; exit 1; }; fi
  command -v mediamtx >/dev/null 2>&1 || [[ -n "$MEDIAMTX_PATH" ]] || { echo 'RTSP requested but MediaMTX is absent. Install a verified distro/package binary yourself and set MEDIAMTX_PATH; this installer will not download an unverified binary.' >&2; exit 1; }
fi
get_source() {
  local work=$1 archive extract root; mkdir -p "$work"
  if [[ -n "$SOURCE_DIR" ]]; then cp -a "$SOURCE_DIR/." "$work/"
  elif [[ -n "$SOURCE_ARCHIVE" || -n "$SOURCE_URL" ]]; then
    archive=$work/source.archive; [[ -n "$SOURCE_ARCHIVE" ]] && cp -f "$SOURCE_ARCHIVE" "$archive" || curl -fL --retry 3 --proto '=https' --tlsv1.2 "$SOURCE_URL" -o "$archive"
    extract=$work/extract; mkdir "$extract"; case "$archive" in *.zip) apt-get install -y --no-install-recommends unzip; unzip -q "$archive" -d "$extract";; *) tar -xf "$archive" -C "$extract";; esac
    root=$(find "$extract" -name pyproject.toml -type f -print -quit | xargs -r dirname); [[ -n "$root" ]] || { echo 'Archive contains no pyproject.toml.' >&2; exit 1; }; cp -a "$root/." "$work/"
  elif [[ -n "$REPO_URL" ]]; then git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$work/repo"; cp -a "$work/repo/." "$work/"
  else echo 'Set SOURCE_DIR, SOURCE_ARCHIVE, SOURCE_URL, or REPO_URL.' >&2; exit 1; fi
}
install -d -m 0755 "$DATA_DIR"
if ! getent passwd "$APP_USER" >/dev/null; then useradd --system --home-dir "$APP_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"; fi
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT; get_source "$work/source"
install -d -m 0755 "$APP_DIR"; cp -a "$work/source/." "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"; "$APP_DIR/venv/bin/python" -m pip install --upgrade pip; "$APP_DIR/venv/bin/pip" install --no-cache-dir "$APP_DIR[mcp,server]"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
cat > /etc/default/pycentauri <<EOF
PYCENTAURI_HOST=$PYCENTAURI_HOST
PYCENTAURI_ACCESS_CODE=${PYCENTAURI_ACCESS_CODE:-}
PYCENTAURI_PORT=$PYCENTAURI_PORT
PYCENTAURI_BIND=$PYCENTAURI_BIND
PYCENTAURI_RTSP=$PYCENTAURI_RTSP
PYCENTAURI_ENABLE_CONTROL=$PYCENTAURI_ENABLE_CONTROL
PYCENTAURI_MEDIAMTX_PATH=$MEDIAMTX_PATH
EOF
rtsp_args=''; [[ "$PYCENTAURI_RTSP" == 1 ]] && rtsp_args=' --rtsp'
mediamtx_env=''; [[ -n "$MEDIAMTX_PATH" ]] && mediamtx_env="Environment=MEDIAMTX_PATH=$MEDIAMTX_PATH"
control_arg=''; [[ "$PYCENTAURI_ENABLE_CONTROL" == 1 ]] && control_arg=' --enable-control'
exec_start="$APP_DIR/venv/bin/centauri server --host \${PYCENTAURI_HOST} --bind \${PYCENTAURI_BIND} --port \${PYCENTAURI_PORT}$control_arg$rtsp_args"
cat > /etc/systemd/system/$SERVICE <<EOF
[Unit]
Description=pycentauri printer server
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-/etc/default/pycentauri
$mediamtx_env
ExecStart=$exec_start
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
systemctl enable --now "$SERVICE"
health_ok=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "$SERVICE" && curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:$PYCENTAURI_PORT/api/info" >/dev/null; then
    health_ok=1
    break
  fi
  sleep 1
done
if [[ "$health_ok" != 1 ]]; then
  systemctl --no-pager --full status "$SERVICE" || true
  journalctl -u "$SERVICE" -n 80 --no-pager || true
  exit 1
fi
printf 'Installed %s; health check passed on 127.0.0.1:%s/api/info\n' "$SERVICE" "$PYCENTAURI_PORT"
