# pycentauri

> **Fork notice** — this is a fork of
> [`bjan/pycentauri`](https://github.com/bjan/pycentauri) (v0.9.0 base),
> evolved independently since. The original project is not mine. Main
> additions here:
>
> Everything this fork adds on top of upstream:
>
> 1. **Failed-print ("spaghetti") detection** - runs on the plain CPU of
>    the box (a Coral Edge TPU is supported but not required and is
>    currently unused - the compiled TPU model degraded detection
>    quality), trained model as release asset, evidence snapshots,
>    optional Telegram push, pause/stop response, shift detection,
>    failure-ring evidence, two-stage classifier
> 2. **Chamber heating automation** - a Shelly-controlled heater for the
>    print room: slicer-follow target, learned overshoot, safety
>    watchdog, manual on/off buttons, a rolling last-hour chamber graph
> 3. **Chamber light automation** - on any printer activity (preheat,
>    print, filament change) or a hot nozzle; off when idle and cool, or
>    manual
> 4. **System installer + hardening** - systemd unit, Coral USB setup
>    for udev-less containers, PWA dashboard
> 5. **Training pipeline** for the detection model (dataset assembly,
>    Mac/Colab scripts, Edge-TPU compilation)
>
> Full divergence map: [`docs/FORK.md`](docs/FORK.md). Detailed feature
> guides: [docs/SPAGHETTI.md](docs/SPAGHETTI.md) ·
> [docs/HEATING.md](docs/HEATING.md) · [docs/LIGHT.md](docs/LIGHT.md).

Local-network toolkit for [Elegoo Centauri Carbon](https://www.elegoo.com/)
3D printers — the **original Centauri Carbon (CC1)** and the **Centauri
Carbon 2 (CC2)**. One async client, six surfaces: Python library, CLI,
MCP server for AI agents, REST/SSE HTTP server, built-in web dashboard,
and an RTSP bridge for your NVR.

No cloud account, no Elegoo servers — everything talks directly to the
printer on your LAN. `pycentauri` auto-detects which model it's talking
to and speaks the right protocol:

| | CC1 | CC2 |
|---|---|---|
| Transport | SDCP v3 over WebSocket (`:3030`) | JSON-RPC over MQTT (`:1883`) |
| Auth | none | access code (printer screen) |
| Discovery | UDP broadcast (`M99999` :3000) | UDP broadcast (JSON-RPC :52700) |
| Webcam | MJPEG `:3031` | MJPEG `:8080` |
| Live head speed | — | ✓ (`gcode_move.speed` ÷ 60 = the screen's mm/s readout) |
| Fan channels | 3 | 5 |
| Canvas multi-filament | ✓ (status, read-only) | ✓ (status + auto-refill) |
| File management | list, upload, delete, history | list, upload, delete, reprint, disk info, history |
| Filament-switch detection | — | ✓ (position-based) |

> **Status:** alpha, but used daily against real printers. Protocols were
> reverse-engineered from Elegoo's official
> [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) C++ SDK, the
> [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link) project,
> and live wire captures. CC1 tested on firmware V1.1.46 and OpenCentauri
> V0.3.0-o; CC2 tested on firmware 01.03.02.51. Full wire-protocol notes
> live in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Quick start

From a fresh Linux box (x86, Raspberry Pi, VM or container - macOS runs
it manually without the service) to a working dashboard in three steps:

1. **Get the code and run the installer** (as root, on the box that can
   reach the printer):

   ```sh
   git clone https://github.com/tholterhus/pycentauri.git
   cd pycentauri
   sudo PYCENTAURI_HOST=<printer-ip> ./install-linux.sh
   ```

   This sets up a systemd service and the web dashboard. Nothing beyond
   Python 3.10+ is required, and nothing is downloaded from the internet
   except the code itself.

2. **Open the dashboard** at `http://<box-ip>:8787/ui` and enter the
   printer's host or IP (the installer asks for it too). Status, camera,
   control and print history work right away - read-only by default.

3. **Optional extras**, configured in `/etc/pycentauri.conf` or the web
   UI:

   - **Telegram push** for print events - a bot token from @BotFather,
     set as `PYCENTAURI_TELEGRAM_TOKEN` / `_CHAT_ID`
   - **Failed-print ("spaghetti") detection** - set `PYCENTAURI_DETECT=1`;
     the trained model ships as a release asset - see
     [docs/SPAGHETTI.md](docs/SPAGHETTI.md)
   - **Chamber heating** - a PTC heater on a Shelly smart plug, URLs set
     in the dashboard's Chamber heating section - see
     [docs/HEATING.md](docs/HEATING.md)
   - **Chamber light** - see [docs/LIGHT.md](docs/LIGHT.md)

That's it. Everything below is reference material.

## Install

For a Python/library installation:

Install from this repository directly (the `pycentauri` name on PyPI
belongs to the upstream project — a bare `pip install pycentauri`
would install *their* package, not this one):

```sh
pip install "pycentauri @ git+https://github.com/tholterhus/pycentauri.git"                             # library + CLI
pip install "pycentauri[mcp] @ git+https://github.com/tholterhus/pycentauri.git"                        # + MCP server
pip install "pycentauri[server] @ git+https://github.com/tholterhus/pycentauri.git"                     # + HTTP REST/SSE server + web UI
pip install "pycentauri[mcp,server] @ git+https://github.com/tholterhus/pycentauri.git"                 # all Python surfaces
pip install "pycentauri[detect,server] @ git+https://github.com/tholterhus/pycentauri.git"              # + failed-print detection (CPU)
```

For a Linux service installation with an optional systemd unit, see the
complete [Linux installation guide](INSTALL.md). It covers prerequisites,
configuration, firewalling, reverse proxies, multiple devices, health checks,
updates, and rollback. The installer defaults to loopback binding and
read-only operation; no credentials belong in source control or shell history.

The RTSP bridge additionally requires
[MediaMTX](https://github.com/bluenviron/mediamtx/releases) and `ffmpeg`
on `$PATH`.

Python 3.10+. Core dependencies: `websockets`, `paho-mqtt`, `httpx`,
`typer`, `pydantic`.

## Connecting to your printer

**CC1** needs only its IP (or nothing at all — it answers UDP discovery).

**CC2** needs its IP *and* its access code, found on the printer's
touchscreen under network/connectivity settings. Pass it as
`--access-code` / `access_code=` / `ACCESS_CODE`. The examples
below use `ACCESS_CODE` as a stand-in — substitute your own.

> **Enable "LAN Only" mode on the CC2** (network settings on the
> touchscreen). The CC2 gates its local API behind it — with LAN Only
> off the printer works through Elegoo's cloud and leaves the local HTTP
> endpoint closed, so pycentauri can't reach it and you'll get a
> connection error. This is required on firmware 2.0 and recommended on
> all CC2 firmware.

Every CLI command accepts `--host` (env: `HOST`). With no host
given, commands try UDP discovery, which finds CC1s and CC2s.

## CLI

```sh
# Discovery (CC1 via M99999 :3000, CC2 via JSON-RPC :52700)
centauri discover

# Status, attributes, live watch, snapshot
centauri status     --host printer.example                        # CC1
centauri status     --host printer-cc2.example --access-code ACCESS_CODE   # CC2
centauri status     --host printer.example --json
centauri attributes --host printer.example
centauri watch      --host printer.example
centauri snapshot   --host printer.example shot.jpg

# Upload a file, then print it — all writes require --enable-control
centauri upload model.gcode         --host printer.example --enable-control
centauri upload model.gcode --start --host printer.example --enable-control

# Print control
centauri print start model.gcode --host printer.example --enable-control
centauri print pause             --host printer.example --enable-control
centauri print resume            --host printer.example --enable-control
centauri print stop              --host printer.example --enable-control

# Live adjust while printing
centauri speed sport                            --host printer.example --enable-control
centauri fan  --model 100 --aux 60 --chamber 30 --host printer.example --enable-control
centauri temp --nozzle 215 --bed 60             --host printer.example --enable-control

# File management (both models; `disk` is CC2-only)
centauri files        --host printer.example                             # CC1
centauri files --storage u-disk --host printer-cc2.example --access-code ACCESS_CODE
centauri disk         --host printer-cc2.example --access-code ACCESS_CODE        # CC2 only
centauri history      --host printer.example                             # CC1 or CC2
centauri delete old.gcode --host printer.example --enable-control

# Chamber light (both models)
centauri light on   --host printer-cc2.example --access-code ACCESS_CODE --enable-control
centauri light off  --host printer-cc2.example --access-code ACCESS_CODE --enable-control

# Canvas multi-filament (status: CC1 + CC2; auto-refill: CC2 only)
centauri canvas       --host printer.example
centauri canvas       --host printer-cc2.example --access-code ACCESS_CODE
centauri refill --on  --host printer-cc2.example --access-code ACCESS_CODE --enable-control
```

`centauri canvas` prints each tray's filament, color, temperature range,
and loaded state:

```
auto_refill  : OFF
active_tray  : none
connected    : yes

canvas #0:
  ● tray 0: PLA Wood (PLA) #F72221 [190-230°C]
  ● tray 1: PLA Wood (PLA) #AF7832 [190-230°C]
  ● tray 2: PETG (PETG) #A03BF7 [230-260°C]
  ● tray 3: PLA Wood (PLA) #D2C5A3 [190-230°C]
```

### Speed modes

Both printers accept exactly four speed settings — arbitrary percentages
are silently ignored by the firmware:

| Mode | CC1 wire value (`PrintSpeedPct`) | CC2 wire value (`speed_mode`) |
|---|---|---|
| `silent` | 50 | 0 |
| `balanced` | 100 | 1 |
| `sport` | 130 | 2 |
| `ludicrous` | 160 | 3 |

Speed changes only take effect while a print is actively running.

#### CC2 speed pinning

Historically the CC2 firmware reset the speed mode back to balanced on
every Canvas filament switch, losing your choice. pycentauri works
around it by **pinning** the mode you set and re-applying it when a
filament switch completes (CC2 only, requires `--enable-control`):

- The mode you set via pycentauri is *pinned*.
- When a Canvas filament switch finishes, the pinned mode is re-applied
  once (harmless if the firmware didn't reset it).
- The pin clears when the print ends.

A note on firmware: on **02.01.00.00** the printer no longer exposes a
stable "set speed mode" over the wire — `gcode_move.speed_mode` in the
real-time stream is the *current move's* speed factor, which varies per
feature. pycentauri therefore treats the pin as an explicit setting and
never infers or enforces it from that noisy value; it only re-applies
your pinned mode on switch completion. To change speed, set it through
pycentauri (the dashboard, `centauri speed`, etc.).

The CC1 has none of this — its speed mode stays where you put it, so
`set_print_speed` is a plain one-shot there.

## Python library

```python
import asyncio
from pycentauri import Printer, CC2Printer, connect_auto


async def main():
    # Explicit CC1
    async with await Printer.connect("printer.example") as printer:
        st = await printer.status()
        print(st.print_status, st.progress, st.temp_nozzle)

    # Explicit CC2
    async with await CC2Printer.connect(
        "printer-cc2.example", access_code="ACCESS_CODE"
    ) as printer:
        st = await printer.status()
        print(st.temp_nozzle, st.raw["_cc2"]["gcode_move_speed"])  # mm/min; ÷60 = screen's mm/s

        canvas = await printer.canvas_status()
        for unit in canvas.canvas_list:
            for tray in unit.tray_list:
                print(tray.tray_id, tray.filament_name, tray.filament_color)

    # Auto-detect — port-probes :3030 vs :1883 and returns the right class
    async with await connect_auto("printer-cc2.example", access_code="ACCESS_CODE") as printer:
        attrs = await printer.attributes()
        print(attrs.machine_name, attrs.firmware_version)


asyncio.run(main())
```

Both classes expose the same API: `status()`, `attributes()`, `watch()`
(async iterator of live status), `snapshot()`, `upload_file()`,
`start_print()`, `pause()`, `resume()`, `stop()`, `set_print_speed()`,
`set_fan_speed()`, `set_temperatures()`, `set_light()`, `list_files()`,
`delete_files()`, `disk_info()`, `print_history()`, `canvas_status()`,
`set_auto_refill()`. Write methods require `enable_control=True` at
connect time and raise `ControlDisabledError` otherwise. On a CC1,
`canvas_status()` is read-only (Cmd 324, verified safe — see
`docs/PROTOCOL.md`) and raises `PrinterError` when no Canvas is attached;
`set_auto_refill()` stays CC2-only, and CC1 file management covers
list/delete/history (`disk_info` is CC2).

`upload_file(local_path, *, remote_name=None, progress=None)` pushes a
file to the printer's internal storage over chunked HTTP (independent of
the control channel, so it can't disrupt a print) and returns the name
`start_print()` expects — so the typical flow is `await
p.upload_file("model.gcode")` then `await p.start_print(...)`. The
optional `progress` callback receives `(bytes_sent, total_bytes)`.

CC2-only telemetry rides along in `Status.raw["_cc2"]`: the live head
speed (`gcode_move_speed` — the commanded speed of the current move in
**mm/min**; divide by 60 for the mm/s figure the printer's screen
shows), `speed_mode`, filament runout sensor state,
firmware-computed `remaining_time_sec`, `machine_status`/`sub_status`
raw codes, and `external_device` (camera / U-disk presence).

## HTTP server + web UI

```sh
# Read-only, loopback only
centauri server --host printer.example

# Read + write + RTSP, on the LAN (put an authenticating proxy in front)
centauri server --host printer.example --bind 0.0.0.0 --port 8787 \
                --enable-control --rtsp

# CC2
centauri server --host printer-cc2.example --access-code ACCESS_CODE \
                --bind 0.0.0.0 --port 8787 --enable-control

# Opt in to a dashboard "update available" badge (the only outbound call;
# off by default — a cached PyPI check every 12 h, fail-silent)
centauri server --host printer.example --check-updates
```

The server holds a single long-lived connection to the printer
(WebSocket for CC1, MQTT for CC2) with automatic reconnect and
exponential backoff — it will never exhaust CC1's 5-connection limit.

The **web UI** at `/ui/` is a clean dark dashboard, mobile-friendly and
dependency-free (no CDN assets — works on an air-gapped LAN): live
webcam, job progress with layer/ETA, printer state, thermals, kinematics
(with live head speed on CC2), pause/resume/stop, speed-mode selector,
fan and heater sliders that hydrate from live values, a Canvas panel
with per-tray color swatches and an auto-refill toggle (CC2), and RTSP
bridge controls. Control panels only render when the server was started
with `--enable-control`.

### Where you'll want the dashboard

- **Phone** — installable PWA: open `/ui/`, use "Add to Home Screen",
  and it runs fullscreen like a native app.
- **Browser tab** — just `http://<server>:8787/ui/` on any machine.
- **OrcaSlicer** — under *Print Host upload* (Host type *Elegoo Link*),
  put `http://<server>:8787/ui/` into the **Device UI** field; the
  printer tab then shows this dashboard instead of the vendor cloud
  page.

All three share the same server, so each viewer counts once for the
camera. One extra "viewer" is the failed-print detection: while a print
runs it subscribes to the same stream (no second camera slot), so the
camera stays on for the whole print even when no dashboard is open —
that is the monitoring working.

**When exactly is the camera on?** The stream runs while at least one
of these holds, and sleeps the moment none does:

| Camera is on when… | Because |
|---|---|
| any UI client is open (PWA, browser tab, OrcaSlicer Device UI) | someone is watching |
| a print runs **and** detection is enabled (`DETECT=1`) | the detector is watching |
| a print runs **and** frame collection is on | training frames are being saved |

With detection (and collection) disabled, the camera — and its network
traffic — is therefore only live while you have a dashboard open; idle
printer overnight = silent network. See the network-load note in the
detection section for the bandwidth numbers.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | Redirects to `/ui/` |
| `GET` | `/ui/` | Web dashboard |
| `GET` | `/api/info` | Health + version + connection state |
| `GET` | `/status` | Latest status (typed summary + full `raw` payload) |
| `GET` | `/attributes` | Model, firmware, mainboard ID |
| `GET` | `/snapshot` | Single JPEG frame |
| `GET` | `/stream` | MJPEG proxy (drop into an `<img>` tag) |
| `GET` | `/events/status` | Server-Sent Events stream of status pushes |
| `GET` | `/discover` | UDP LAN scan (finds CC1s and CC2s) |
| `GET` | `/canvas` | Canvas state (CC1 via Cmd 324, CC2 via MQTT; `501` when no Canvas answers) |
| `GET` | `/docs`, `/redoc` | OpenAPI documentation |
| `POST` | `/print/start` | `{"filename": "cube.gcode", "storage": "local"}` † |
| `POST` | `/print/pause` · `/print/resume` · `/print/stop` | † |
| `POST` | `/print/speed` | `{"mode": "sport"}` or `{"mode": 130}` † |
| `POST` | `/print/fan` | `{"model": 50, "auxiliary": 30, "chamber": 0}` — any subset, 0–100 † |
| `POST` | `/print/temperature` | `{"nozzle": 215, "bed": 60}` — any subset, °C, 0 = off † |
| `POST` | `/files/upload` | multipart `file=@model.gcode` (+ optional `start=true`) † |
| `POST` | `/files/delete` | `{"filenames": ["a.gcode"], "storage": "local"}` — refuses the printing file † |
| `GET` | `/files` | `?storage=local&limit=100` — file list |
| `GET` | `/disk` | Disk usage: `total_bytes`, `used_bytes` (CC2; `501` on CC1) |
| `GET` | `/history` | Print job history |
| `POST` | `/light` | `{"on": true}` — chamber light † |
| `POST` | `/canvas/refill` | `{"enabled": true}` (CC2) † |
| `GET` | `/api/rtsp` | RTSP bridge state (when `--rtsp`) |
| `POST` | `/api/rtsp/start` · `/api/rtsp/stop` | Toggle the bridge (when `--rtsp`) |
| `GET` | `/api/detect` | Failed-print detection state (when `--detect`) |
| `GET` | `/api/detect/evidence/{name}` | Evidence frame `<timestamp>.jpg` (when `--detect`) |

† requires the server to be launched with `--enable-control`; otherwise
the route isn't registered at all.

Temperature writes are bounds-checked server-side: nozzle 0–300 °C,
bed 0–110 °C, chamber 0–60 °C.

## MCP server (AI agents)

Give Claude Code, Claude Desktop, Cursor, or any MCP client eyes and
hands on your printer:

```sh
# Read-only (status, snapshot, attributes, discovery, canvas)
claude mcp add pycentauri --env HOST=printer.example \
    -- python -m pycentauri.mcp

# With control tools
claude mcp add pycentauri-cc2 \
    --env HOST=printer-cc2.example \
    --env ACCESS_CODE=ACCESS_CODE \
    -- python -m pycentauri.mcp --enable-control
```

The target host is pinned in the server's environment at spawn time — a
prompt-injected agent cannot redirect commands to an arbitrary IP,
because no tool takes a host parameter.

| Tool | Availability | Description |
|---|---|---|
| `get_status` | always | State, temps, progress, layer, position, fans |
| `get_attributes` | always | Model, firmware, mainboard ID |
| `get_snapshot` | always | Webcam frame as MCP image — the model *sees* the print |
| `discover_printers` | always | UDP LAN scan |
| `get_canvas_status` | always | Canvas trays, colors, auto-refill (CC1 read-only + CC2) |
| `start_print` | `--enable-control` | Start a file already on the printer |
| `pause_print` / `resume_print` / `stop_print` | `--enable-control` | Job control |
| `set_print_speed` | `--enable-control` | `silent`/`balanced`/`sport`/`ludicrous` |
| `set_fan_speed` | `--enable-control` | Any subset of model/aux/chamber, 0–100% |
| `set_temperatures` | `--enable-control` | Any subset of nozzle/bed/chamber, °C |
| `set_auto_refill` | `--enable-control` | Canvas auto-refill toggle (CC2) |

Control tools aren't merely gated — without the flag they are never
registered, so they don't appear in the model's tool list at all.

## RTSP bridge

Re-streams the printer's MJPEG webcam as H.264/RTSP for clients that
don't speak MJPEG — Home Assistant, Frigate, Jellyfin, Synology
Surveillance, VLC:

```sh
# Standalone (foreground, Ctrl-C to stop)
centauri rtsp --host printer.example
# → rtsp://<this-host>:8554/printer

# Integrated with the HTTP server — adds a STREAM panel to the web UI
centauri server --host printer.example --rtsp --bind 0.0.0.0
```

MediaMTX only runs the ffmpeg transcode while a client is connected, so
idle cost is zero. Tunables: `--fps`, `--bitrate`, `--preset`, `--path`,
`--port` (standalone) or the `--rtsp-*` variants on `centauri server`.
The bridge picks the correct camera port for CC1 vs CC2 automatically.

## Failed-print detection ("spaghetti")

pycentauri watches every print through the printer's own camera and
catches failed prints early - with the trained model shipped as a
release asset, evidence snapshots, optional Telegram push, and
pause/stop response. Runs on the plain CPU of the box - measured at
~50-65 ms per analysis even on this box's weak Celeron (J4105, 2
threads), far faster than the camera's 1 fps analysis needs. This
deployment deliberately serves the CPU model because the compiled Coral
variant measured both slower (~126 ms) and quality-degraded (see the
design record in docs/CORAL_SPAGHETTI_DETECTION.md); a Coral Edge TPU
is supported by the code but not required, and the dashboard shows
live state, the evidence gallery and the temperature trend.

Hardware, setup, tuning, the two-stage classifier and training:
**[docs/SPAGHETTI.md](docs/SPAGHETTI.md)** - and the design record in
[CORAL_SPAGHETTI_DETECTION.md](CORAL_SPAGHETTI_DETECTION.md).

## Chamber light

The dashboard can switch the printer's chamber light - on motion, while
printing, or manually from the UI. Setup and behavior:
**[docs/LIGHT.md](docs/LIGHT.md)**.

## Chamber heating (heated print room)

ABS and ASA print best in a warm chamber - this printer cannot warm its
own air, so pycentauri drives an external heater (e.g. a PTC fan heater
on a Shelly smart plug) and holds the chamber at the temperature your
slicer profile asks for: armed at print start, regulated by the
printer's own chamber sensor, unattended, with a safety watchdog and
manual on/off buttons. Hardware, setup, the learned overshoot and the
safety
watchdog: **[docs/HEATING.md](docs/HEATING.md)**.

## Reference

- **Print status codes**: [docs/STATUS_CODES.md](docs/STATUS_CODES.md)
- **Known firmware quirks** (CC1 & CC2): [docs/FIRMWARE_QUIRKS.md](docs/FIRMWARE_QUIRKS.md)
- **Wire protocols**: [docs/PROTOCOL.md](docs/PROTOCOL.md)
- **Architecture**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Fork divergence from upstream**: [docs/FORK.md](docs/FORK.md)


## Project layout & docs

```
src/pycentauri/
├── client.py      # CC1: async SDCP-over-WebSocket client
├── cc2.py         # CC2: async JSON-RPC-over-MQTT client (same API)
├── connect.py     # connect_auto() — port-probe model detection
├── sdcp.py        # SDCP v3 envelope build/parse
├── discovery.py   # UDP broadcast discovery
├── camera.py      # MJPEG frame grabber
├── models.py      # Status / Attributes / CanvasStatus / PrintInfo
├── cli.py         # Typer CLI
├── server.py      # FastAPI app + connection supervisor
├── rtsp.py        # MediaMTX/ffmpeg bridge
├── detect/        # Failed-print detection: Edge TPU/CPU backend + pipeline
├── mcp/           # FastMCP stdio server
└── web/           # Static dashboard (no build step, no CDN)
```

- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — both wire protocols in
  detail: envelopes, command/method tables with tested-on dates, status
  payloads, error codes, failure modes, and a CC1-vs-CC2 comparison.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module map and
  request flow.
- [`docs/FORK.md`](docs/FORK.md) — how this fork diverges from upstream
  and how upstream updates are absorbed.

## Development

```sh
git clone https://github.com/tholterhus/pycentauri && cd pycentauri
python -m venv .venv && .venv/bin/pip install -e ".[mcp,server,dev]"

.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy src          # strict mode
.venv/bin/pytest -q         # no printer required — tests use in-process fakes
```

Tests run against an in-process fake SDCP WebSocket server plus pure
translation-layer tests for CC2; nothing in CI touches real hardware.
Live verification against a physical printer is manual — `centauri
status`, `centauri canvas`, and a fan write are the standard smoke
test after protocol-layer changes.

## Credits & license

- **Original project: [`bjan/pycentauri`](https://github.com/bjan/pycentauri)**
  — this repository started as a fork of its v0.9.0 (see the fork notice
  above and [`docs/FORK.md`](docs/FORK.md)).
- **AI-generated code**: everything added in this fork since the v0.9.0
  base — including the failed-print detection, the trained model and the
  dashboard — was written 100 % by AI (GLM-Flash 5.3, driven through
  ZCode). No humans were actively involved in the coding process (or
  were harmed); a human decided *what* to build, the machine wrote *how*
  — and upstream's v0.9.0 base may well have been built the same way.
  Credit to bjan for the starting point either way.
- Protocol references: Elegoo's
  [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) SDK
  (Apache-2.0) and
  [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link).
- Licensed under Apache-2.0 — see [LICENSE](LICENSE).
- Not affiliated with or endorsed by Elegoo. Reverse-engineered
  protocols can break with any firmware update; nothing here is
  warranted to keep your prints alive.

