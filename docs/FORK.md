# Fork divergence from upstream (bjan/pycentauri)

This repository started as a deployment of upstream's v0.9.0 and has
evolved independently since; version numbers are assigned in this fork
and no longer track upstream's numbering (the old `0.11.2` label was an
internal marker, not an upstream release). This document maps the
divergence — read it before merging upstream changes and use it as the
outline for any future upstream contribution.

## Relationship to upstream

* `origin` = tholterhus/pycentauri (this repository, private)
* `upstream` = bjan/pycentauri (public, main at v0.9.0 when this was
  written)
* Histories share the v0.9.0 base. The fork tree adds ~4,171 lines of
  fork work; upstream holds only ~165 lines that this fork lacks, and
  all of those are superseded v0.9-era code or deliberately generalized
  content (see below).

## Features added in this fork

* **0.11.x era (retroactively documented in CHANGELOG 0.11.0)**: system
  installer + INSTALL.md, camera stream lifecycle (SDCP Cmd 386 with
  `allow_empty_mainboard`), PWA assets, `LOG_LEVEL`,
  `pycentauri.conf-example`, generalized example addresses
* **Failed-print ("spaghetti") detection** (v0.12.0):
  * `src/pycentauri/detect/` — LiteRT/Edge-TPU backend with CPU fallback
    and automatic loading of the uncompiled model sibling, plus the
    collection + arming pipeline
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
  (`DETECT*`), runtime cache-busting strings

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
