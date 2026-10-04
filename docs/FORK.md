# Fork divergence from upstream (bjan/pycentauri)

This repository started as a deployment of upstream's v0.9.0 and has
evolved independently since; version numbers are assigned in this fork
and no longer track upstream's numbering (the old `0.11.2` label was an
internal marker, not an upstream release). This document maps the
divergence — read it before merging upstream changes and use it as the
outline for any future upstream contribution.

## Relationship to upstream

* `origin` = tholterhus/pycentauri (this repository, public)
* `upstream` = bjan/pycentauri (public, main at v0.9.0 when this was
  written)
* Histories share the v0.9.0 base. The fork tree adds ~4,171 lines of
  fork work; upstream holds only ~165 lines that this fork lacks, and
  all of those are superseded v0.9-era code or deliberately generalized
  content (see below).

## Features added in this fork

* **Installation & operations (0.11.x)**:
  * `install-linux.sh` — one-command setup as a systemd service (venv,
    unit file, `/etc/pycentauri.conf`), with a plain-language
    [`INSTALL.md`](INSTALL.md) walkthrough
  * **Camera lifecycle**: the printer only runs its webcam while someone
    is "watching". The dashboard asks it to start the stream (SDCP
    command 386) and keeps it alive with a synthetic viewer
    (`allow_empty_mainboard`) while any client is connected, then lets
    it sleep again — no permanent second connection to the printer.
    Viewers are reference-counted across all usage surfaces (PWA on the
    phone, plain browser tab, OrcaSlicer's embedded Device UI). One
    extra "viewer" is the failed-print detection itself: while a print
    runs it subscribes to the same stream, so the camera also stays on
    for the whole print even when no dashboard is open — that is the
    point of the monitoring. Between prints, with no UI client, it
    sleeps.
  * **PWA assets**: the dashboard can be installed as an app on phone
    and desktop (icons, manifest)
  * Configurable log verbosity (`LOG_LEVEL`), an example service config
    (`pycentauri.conf-example`) and `printer.example` placeholders
    instead of the hardcoded LAN addresses in upstream's examples
* **Failed-print ("spaghetti") detection** (v0.12.0):
  * `src/pycentauri/detect/` — **works with or without a Google Coral**:
    with the Coral stick, its AI chip does the camera analysis (~5–15 ms
    per look); without one, the same analysis runs on the normal CPU
    (~200 ms — still plenty at one look per second). The right variant
    is chosen automatically at startup, and the two model files ship as
    a pair so a missing Coral never breaks anything. Includes the alert
    pipeline (evidence snapshots, Telegram, optional pause/stop) and
    training-frame collection
  * Dashboard DETECT panel: backend badge, WATCHING flag, K-of-M window,
    evidence thumbnails, runtime response switch (training-frame
    collection runs headless via `POST /api/detect/collect` and persists
    across restarts)
  * CLI: `centauri detect check | test | watch`
  * HTTP: `GET /api/detect`, `POST /api/detect/collect`,
    `POST /api/detect/action`, evidence serving
* **Deployment hardening**: `pycentauri-usb-prepare` (device node +
  udev-database entry for udev-less containers, no-op elsewhere),
  `scripts/fetch-smoke-model.sh`, installer knobs
  (`DETECT*`, `TELEGRAM*`), runtime cache-busting strings

## Deliberate behavior changes

* Server log default `warn` (upstream v0.9.0 shipped `info`)
* Examples use `printer.example` — upstream v0.9.0 contains hardcoded
  `192.168.1.x` addresses and an example access code
* URLs point at tholterhus/pycentauri (upstream attribution kept in
  Credits)
* No PyPI publish workflow (the `pycentauri` PyPI name belongs to
  upstream; distribution is clone-based), CI matrix slimmed to
  ubuntu × {3.11, 3.13}

## Superseded upstream code

Upstream v0.9.0's raw-chunk MJPEG fanout, the webcam keepalive reload
and the INFO log default are replaced by the FrameAssembler design and
the fetch-based webcam parser (see CHANGELOG 0.10.0).

## Upstream sync routine

1. Watch bjan/pycentauri for releases (Custom → Releases only).
2. On a release: read its CHANGELOG and check against this map.
3. `git fetch upstream && git merge upstream/main` — the shared v0.9.0
   base makes this a normal 3-way merge; conflicts are expected only
   where bjan re-implements something this fork already has (e.g. the
   webcam fixes).
4. Re-run the local gates: `ruff format --check .`, `ruff check .`,
   `mypy src`, `pytest`.

## Contributing back (if ever)

Patch-based, not merge-based: branch from an upstream tag, extract the
themed diff (this document is the slicing outline), open one PR per
theme and file an issue first for large features — the Coral detection
is the flagship candidate.
