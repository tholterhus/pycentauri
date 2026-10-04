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
# Install from this GitHub repository — a bare `pip install pycentauri`
# would pull the upstream project's package from PyPI!
.venv/bin/pip install "pycentauri @ git+https://github.com/tholterhus/pycentauri.git"
# Optional surfaces:
.venv/bin/pip install "pycentauri[mcp,server] @ git+https://github.com/tholterhus/pycentauri.git"
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

The installer requires `PYCENTAURI_HOST` (that prefix is intentional here — it is the installer's own option name, not the config file's). Defaults are loopback binding,
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
HOST=printer.example
# Required for CC2; leave empty for CC1. Store this file securely.
ACCESS_CODE=
PORT=8787
# Prefer 127.0.0.1 ("only this computer may connect") unless a login
# proxy or a trusted network is used.
BIND=127.0.0.1
ENABLE_CONTROL=0
RTSP=0
# Set only when RTSP is enabled and MediaMTX is not on PATH.
MEDIAMTX_PATH=
# Log verbosity: info | warn | critical (default: warn)
LOG_LEVEL=warn
# Failed-print ("spaghetti") detection; see the step-by-step chapter below.
DETECT=0
DETECT_MODEL=data/models/ssd_mobilenet_v2_coco_edgetpu.tflite
DETECT_ACTION=notify
DETECT_THRESHOLD=0.65
# Seconds between analyzed frames (default 1; raise to reduce CPU load).
DETECT_INTERVAL=1
# Optional Telegram push (detection AND print-state changes, with a
# camera photo). Bot via @BotFather; empty = off.
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```

Use restrictive permissions, for example `root:pycentauri` and mode `0640`.
After editing a systemd installation, apply changes with:

```sh
sudo systemctl daemon-reload
sudo systemctl restart pycentauri.service
```

`ENABLE_CONTROL=1` enables print and heater controls. Enable it
only when the HTTP endpoint is protected and the operational risk is
understood. CC2 access codes are credentials; never commit them or paste them
into public issue reports.

## Failed-print detection (Coral) — step by step

This optional feature watches the printer's webcam while a print is running
and raises an alert when the print has failed into a stringy mess
("spaghetti"). The heavy lifting is done by a *model* — a small trained
neural-network file — executed either on a Google Coral USB Accelerator
(a stick that speeds up neural networks, ~5–15 ms per look) or, more
slowly, on the plain CPU (~200 ms per look; a Coral is optional).

You need: a working pycentauri installation (see above), and — only for the
fast Coral variant — a Coral USB Accelerator plugged into this machine.

### 1. Install the detection add-on

```sh
sudo -u pycentauri /opt/pycentauri/venv/bin/pip install '/opt/pycentauri[detect,server]'
```

The `detect` extra adds the inference runtime (LiteRT), `numpy` and `pillow`. Note the local path (`/opt/pycentauri[...]`): it upgrades your checked-out copy in place — a bare `pip install pycentauri` would fetch the upstream project from PyPI instead.

### 2. Get a model

The repository ships no model file. Run the download helper inside the
application directory:

```sh
cd /opt/pycentauri && sudo -u pycentauri ./scripts/fetch-smoke-model.sh
```

This fetches the *smoke model* (a COCO SSD MobileNet V2 in both the Edge-TPU
and the CPU variant, plus its labels). It knows everyday objects — not
spaghetti — and exists purely to prove the pipeline works end to end.

The **real spaghetti model** ships ready-made: download
`spaghetti_edgetpu.tflite`, `spaghetti.tflite` and `spaghetti.txt` from the
[v0.13.0 release](https://github.com/tholterhus/pycentauri/releases/tag/v0.13.0)
into `/opt/pycentauri/data/models/`, then set
`DETECT_MODEL=data/models/spaghetti_edgetpu.tflite` in the config. How that
model was trained — and how to retrain your own — is documented in
[`scripts/train/README.md`](scripts/train/README.md).

### 3. Coral runtime (optional — skip for CPU-only)

On Ubuntu 22.04 / Debian 12 or newer, install Google's runtime:

```sh
echo "deb [signed-by=/usr/share/keyrings/coral-edgetpu.gpg] https://packages.cloud.google.com/apt coral-edgetpu-stable main" \
  | sudo tee /etc/apt/sources.list.d/coral-edgetpu.list
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/coral-edgetpu.gpg
sudo apt-get update && sudo apt-get install libedgetpu1-std
```

Then unplug and re-plug the Coral once. Your desktop user needs permission
for the USB device (the package ships a rule granting the `plugdev` group
access; `sudo usermod -aG plugdev $USER`, then log out and in again).

### 4. Turn it on

Set `DETECT=1` in `/etc/pycentauri.conf` (see the variables in the
Configuration section above) and restart the service. The dashboard gains a
**DETECT** panel showing the backend, a live WATCHING flag while a print
runs, and evidence snapshots.

Safety: the default action is *notify only* — an alert writes evidence
frames to `data/evidence/` (and optionally posts a webhook), but never
touches the printer. `DETECT_ACTION=pause` or `stop` additionally
requires `ENABLE_CONTROL=1`.

### 5. Verify

```sh
sudo -u pycentauri /opt/pycentauri/venv/bin/centauri detect check
```

Expected output: `edge tpu : present` (or `not found ... — CPU fallback`),
your model path, `backend: edgetpu` (or `cpu`), and an inference time. Then
start any print and watch the DETECT panel switch to WATCHING after the
first ~90 seconds.

### If something does not work

| Symptom | Likely cause | Fix |
|---|---|---|
| `edge tpu: not found` although the Coral is plugged in | runtime not installed, or container without device passthrough | install `libedgetpu1-std`; in Proxmox LXCs pass the USB device through (`dev0: /dev/bus/usb/…`) |
| `backend: cpu` although a Coral is present | the model file is not Edge-TPU-compiled | a `*_edgetpu.tflite` must be loaded; the CPU twin (`*.tflite`) is fetched automatically |
| `inference failed` mentioning `edgetpu-custom-op` | an Edge-TPU model was forced onto the CPU | ship both variants; the CPU variant is loaded automatically |
| webcam snapshot returns HTTP 500/502 | the printer's camera stream is disabled | open the dashboard webcam once (or `/stream`) — the server then enables it |
| Coral worked, then stopped after a failed run | the stick re-enumerates itself after resets; the stale device node blocks it | re-plug the Coral, or restart the service (`ExecStartPre` re-syncs the node) |

Inside an unprivileged LXC container (Proxmox, Docker-like environments)
there is no udev daemon, so libusb cannot see the Coral on its own. The
installer therefore places a `pycentauri-usb-prepare` helper that re-creates
the device node and a `/run/udev` database entry at every service start —
the Proxmox device passthrough (`dev0: /dev/bus/usb/…`) plus this helper is
a proven combination.

The helper is deliberately no-op on regular hosts: when a running udev
already manages the device node and its database entry, it changes nothing.

### Docker

Pass the device (and, ideally, the udev database) into the container:

```sh
docker run ... --device /dev/bus/usb:/dev/bus/usb   -v /run/udev:/run/udev:ro ...
```

If `/run/udev` cannot be mounted, run `/usr/local/sbin/pycentauri-usb-prepare`
as root before starting the server (the systemd unit does this via
`ExecStartPre`) — it creates the device node and database entry itself.

### Raspberry Pi

Works on Raspberry Pi 4/5 with the 64-bit Raspberry Pi OS (Python 3.10+
ships with Bookworm): LiteRT publishes `aarch64` wheels, `libedgetpu1-std`
installs from the same Google repository, and CPU-only detection
needs nothing extra at all.

**The Coral USB Accelerator also works on the Pi** (ARM-64 is officially
supported for the stick): install the arm64 build of the Edge-TPU runtime,
plug the stick into a powered port, and pycentauri picks it up like on
x86 — expect slightly slower inference than a desktop x86 CPU-to-Coral
setup, but still far below CPU-only latency. Use a decent USB cable and
avoid underpowered hubs, the stick is picky about power.

Does detection without a Coral work on my CPU?

All architectures LiteRT runs on — that covers **x86-64 and ARM-64 Linux**
(including Raspberry Pi 4/5) and **macOS on Apple Silicon (M-series) as
well as Intel**. It is pure software, so no special
instructions or accelerator are needed anywhere. The Coral stick requires
Linux (x86-64 **or** ARM-64, e.g. the Raspberry Pi) plus the Edge-TPU
runtime — on macOS the detection simply always uses the CPU path. Note
the split: the *runtime* that executes a compiled model exists for both
architectures, while the *compiler* that produces such a model is
x86-64-only (see [`scripts/train/README.md`](scripts/train/README.md)).

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
`BIND=127.0.0.1` when only local clients need access. If clients on
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
  explicitly set `ENABLE_CONTROL=1` and restart after securing the
  endpoint.
- **RTSP fails:** verify `ffmpeg`, MediaMTX, executable permissions, and the
  configured path; RTSP is intentionally off by default.

For development and test commands, see the [README](../README.md). For wire
protocol details, see [PROTOCOL.md](PROTOCOL.md).
