# Installing pycentauri on Linux

This guide describes a normal Linux installation on a host that can reach the
printer over the local network. It does not require a particular distribution,
virtualisation platform, or hosting provider. Commands below use common
POSIX/Linux tools; adapt package-manager commands to your distribution.

## Prerequisites

- A supported Linux system with network access to the printer.
- Python 3.10 or newer, including the `venv` module.
- `pip` (the installer creates a virtual environment and installs the package).
- `git` if installing from a Git repository, or `tar`/`unzip` for an archive.
- `curl` for the health check and optional URL-based source installation.
- A user with `sudo` access, or root for a system installation.
- Optional: `systemd` if the service unit and automatic restart are wanted.
- Optional RTSP dependencies: `ffmpeg` and a separately installed, verified
  [MediaMTX](https://github.com/bluenviron/mediamtx) binary.

The printer and the Linux host must be able to communicate directly. The
application does not provide authentication for its HTTP API.

## Installation choices

### Package installation

For a library/CLI-only installation, use a virtual environment in the project
or another user-owned directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install pycentauri
# Optional surfaces:
.venv/bin/pip install 'pycentauri[mcp,server]'
```

### System installation with the installer

`install-linux.sh` installs the checked-out source (or another explicitly
selected source) into `/opt/pycentauri`, creates a service account, creates a
virtual environment, and optionally installs a systemd unit. It does not use a
Linux-distribution package manager and does not fetch credentials.

From a checked-out copy of this repository:

```sh
sudo env \
  PYCENTAURI_HOST=printer.example \
  SOURCE_DIR="$PWD" \
  ./install-linux.sh
```

For a repository or archive, set exactly one source selector. Review remote
source and pin a commit or release before using it in production:

```sh
sudo env PYCENTAURI_HOST=printer.example \
  REPO_URL='https://example.invalid/your/pycentauri.git' REPO_REF='main' \
  ./install-linux.sh
```

The installer requires `PYCENTAURI_HOST`. Defaults are loopback binding,
port `8787`, read-only mode, and RTSP disabled. Override paths with
`APP_DIR`, `DATA_DIR`, `APP_USER`, or `SERVICE` when your host layout requires
it. The script is intended to be rerunnable; it refreshes the application and
virtual environment while retaining the data directory.

If systemd is unavailable, the application is installed but not started. Run
the printed `centauri server ...` command as the service account, or create an
 equivalent supervisor configuration for your operating system.

## Files and directories

A default system installation uses:

```text
/opt/pycentauri/                 application source, configuration example and runtime files
/opt/pycentauri/venv/             Python runtime and console scripts
/opt/pycentauri/data/             persistent application data
/opt/pycentauri/pycentauri.conf-example  documented starter configuration
/etc/pycentauri.conf              active service configuration
/etc/systemd/system/pycentauri.service
```

The service runs as the unprivileged `pycentauri` account. Do not put source
credentials, access codes, or tokens in the repository, command-line URLs, or
shell history.

## Configuration

The service reads `/etc/pycentauri.conf`. A generic starting point is:

```sh
# Printer address or DNS name; replace the placeholder locally.
PYCENTAURI_HOST=printer.example
# Required for CC2; leave empty for CC1. Store this file securely.
PYCENTAURI_ACCESS_CODE=
PYCENTAURI_PORT=8787
# Prefer loopback unless an authenticated proxy or trusted LAN is used.
PYCENTAURI_BIND=127.0.0.1
PYCENTAURI_ENABLE_CONTROL=0
PYCENTAURI_RTSP=0
# Set only when RTSP is enabled and MediaMTX is not on PATH.
PYCENTAURI_MEDIAMTX_PATH=
```

Use restrictive permissions, for example `root:pycentauri` and mode `0640`.
After editing a systemd installation, apply changes with:

```sh
sudo systemctl daemon-reload
sudo systemctl restart pycentauri.service
```

`PYCENTAURI_ENABLE_CONTROL=1` enables print and heater controls. Enable it
only when the HTTP endpoint is protected and the operational risk is
understood. CC2 access codes are credentials; never commit them or paste them
into public issue reports.

## Network and firewall

The Linux host needs outbound access to the printer. The exact ports depend on
the model:

- CC1: TCP `3030` for control and normally TCP `3031` for the camera; UDP
  discovery uses LAN broadcast.
- CC2: TCP `1883` for MQTT plus the printer's HTTP/camera endpoints (normally
  camera TCP `8080`). CC2 discovery is direct-address only.
- pycentauri's HTTP server: TCP `8787` by default, or the configured port.
- Optional RTSP output: TCP `8554` by default, when enabled.

Permit only the required paths in the host and network firewalls. Keep
`PYCENTAURI_BIND=127.0.0.1` when only local clients need access. If clients on
a trusted LAN need access, bind to the host's LAN address rather than all
interfaces where practical and restrict the source subnet. The HTTP server has
no built-in authentication.

### Optional reverse proxy and TLS

A reverse proxy such as nginx, Caddy, or Apache can terminate TLS, enforce
authentication, and forward to `http://127.0.0.1:8787`. Configure websocket
and Server-Sent Events forwarding, preserve the `Host` header, and set suitable
read/idle timeouts. Keep proxy credentials and certificates outside this
repository. Test both `/api/info` and `/events/status` through the proxy before
exposing it.

### Optional multiple devices

Run one service instance per printer, each with its own configuration file,
port, and service name. For example, use `pycentauri-a.service` with
`/etc/pycentauri-a.conf` on port `8787`, and `pycentauri-b.service` with a
different printer and port such as `8788`. Do not reuse an access code or put
multiple credentials in a shared public configuration file. A reverse proxy
can route separate authenticated paths or hostnames to those loopback ports.

## Health checks and logs

The lightweight health endpoint is:

```sh
curl --fail --silent --show-error http://127.0.0.1:8787/api/info
```

It reports the server/version and connection state; a successful HTTP response
does not guarantee that the printer is powered on. For systemd:

```sh
systemctl status pycentauri.service
journalctl -u pycentauri.service -f
systemctl is-enabled pycentauri.service
```

For a deeper check, use the CLI from the installed environment:

```sh
sudo -u pycentauri /opt/pycentauri/venv/bin/centauri status \
  --host printer.example
```

Check firewall reachability from the Linux host with the distribution's
standard tools. Avoid probing undocumented printer commands while a print is
running.

## Updates

1. Review the release notes and back up `/etc/pycentauri.conf`.
2. Obtain and verify the new source (prefer a reviewed tag or commit).
3. Stop the service before replacing files if your deployment process requires
   a maintenance window.
4. Rerun the installer with the same `APP_DIR`, `DATA_DIR`, and configuration.
5. Confirm the health endpoint, service status, and a read-only printer status.

Example for a local checkout:

```sh
sudo cp -p /etc/pycentauri.conf /etc/pycentauri.conf.backup
sudo env PYCENTAURI_HOST=printer.example SOURCE_DIR="$PWD" ./install-linux.sh
curl --fail http://127.0.0.1:8787/api/info
```

The installer does not automatically migrate or delete the data directory.
Review dependency changes and systemd hardening settings when upgrading.

## Rollback

Keep the previous source checkout or release archive until the new version has
passed its health checks. To roll back, stop the service, install the known-good
source with `SOURCE_DIR` or `SOURCE_ARCHIVE`, restore the saved environment
file if needed, then start the service and repeat the checks:

```sh
sudo systemctl stop pycentauri.service
sudo env PYCENTAURI_HOST=printer.example SOURCE_DIR=/srv/pycentauri-known-good \
  ./install-linux.sh
sudo cp -p /etc/pycentauri.conf.backup /etc/pycentauri.conf
sudo systemctl daemon-reload
sudo systemctl start pycentauri.service
curl --fail http://127.0.0.1:8787/api/info
```

Do not roll back while a print is in a safety-critical state without first
checking the printer and deciding whether pausing it is appropriate.

## Troubleshooting

- **Service exits immediately:** inspect `systemctl status` and the journal;
  validate the venv and the values in `/etc/pycentauri.conf`.
- **Cannot connect to a printer:** verify its address, model-specific ports,
  firewall rules, and (for CC2) LAN-only mode and access code handling.
- **UI works but controls are absent:** the service is likely read-only;
  explicitly set `PYCENTAURI_ENABLE_CONTROL=1` and restart after securing the
  endpoint.
- **RTSP fails:** verify `ffmpeg`, MediaMTX, executable permissions, and the
  configured path; RTSP is intentionally off by default.

For development and test commands, see the [README](../README.md). For wire
protocol details, see [PROTOCOL.md](PROTOCOL.md).
