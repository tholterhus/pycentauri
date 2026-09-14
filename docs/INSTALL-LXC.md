# Debian 12 LXC installation

Run as `root` inside a fresh Debian 12 LXC. The installer is idempotent: it keeps `/var/lib/pycentauri`, refreshes `/opt/pycentauri` and its venv, and restarts/checks `pycentauri.service`.

Published script example (replace the URL with the reviewed release URL):

```sh
curl -fsSL https://example.invalid/pycentauri/install-debian12.sh | env PYCENTAURI_HOST=192.0.2.50 bash
```

Private source, with SSH/deploy-key or git credentials already configured in the container (never put a token in this command):

```sh
curl -fsSL https://example.invalid/pycentauri/install-debian12.sh | env PYCENTAURI_HOST=printer.example REPO_URL='ssh://git@github.com/ORG/pycentauri-private.git' REPO_REF='main' bash
```

Alternatively set `SOURCE_DIR`, `SOURCE_ARCHIVE`, or `SOURCE_URL`; these are preferable for an audited local/private source. `wget -qO- URL | bash` works too. The installer creates user `pycentauri`, installs `.[mcp,server]`, and writes non-secret service settings to `/etc/default/pycentauri`.

## Configuration and network

`PYCENTAURI_HOST` is required and is the printer IP/hostname. `PYCENTAURI_PORT` defaults to 8787 and `PYCENTAURI_BIND` defaults to `0.0.0.0`. For CC2, put `PYCENTAURI_ACCESS_CODE` in `/etc/default/pycentauri` after installation; do not include it in shell history or a URL. The LXC needs TCP reachability to the printer: CC1 normally uses TCP 3030/3031, CC2 TCP 1883 and its HTTP/camera ports; CC1 discovery additionally needs LAN UDP broadcast. Restrict the HTTP bind/firewall or use an authenticated reverse proxy; pycentauri itself is not an authentication layer.

## Optional RTSP

The default service does **not** enable RTSP. To install ffmpeg and enable it during installation, use `INSTALL_FFMPEG=1 PYCENTAURI_RTSP=1`. RTSP also requires MediaMTX. This installer deliberately does not download an unknown MediaMTX binary. Install a distro/package/release binary after independently verifying its provenance and checksum, then set `MEDIAMTX_PATH=/usr/local/bin/mediamtx` (or ensure `mediamtx` is on `PATH`) and rerun the installer with `PYCENTAURI_RTSP=1`. Without a verified MediaMTX binary, leave RTSP disabled.

After installation: `systemctl status pycentauri`, `curl http://127.0.0.1:8787/api/info`, and `journalctl -u pycentauri -f`.
