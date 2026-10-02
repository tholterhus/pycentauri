#!/usr/bin/env python3
"""Roboflow Universe harvest helper — standalone, stdlib only.

Human-operated tool for the pycentauri spaghetti-model training plan
(docs/TRAINING_PLAN.md). No AI, no venv needed: runs with any
Python 3.9+ that has internet access.

Modes:
  check                    read metadata for every slug in the candidates
                           file (needs an API key)
  download <slug> [slug…]  download the latest dataset version in YOLO
                           format into data/train/roboflow/<project>/
                           and report classes + per-split image counts
  report                   summarize what has already been downloaded
  add <workspace/project>  append a slug to the candidates file

The API key is a free Roboflow account key (roboflow.com → Settings →
API Key). It is read from, in order: --key, $ROBOFLOW_API_KEY, or the
file ~/.roboflow-key (one line, chmod 600).

Exit codes: 0 ok, 1 usage, 2 key missing, 3 network/API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANDIDATES = Path(__file__).resolve().parent / "roboflow-candidates.txt"
OUT_DIR = REPO / "data" / "train" / "roboflow"
KEY_FILE = Path.home() / ".roboflow-key"
API = "https://api.roboflow.com"


def get_key(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    if os.environ.get("ROBOFLOW_API_KEY"):
        return os.environ["ROBOFLOW_API_KEY"].strip()
    if KEY_FILE.is_file():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    print(
        "Kein API-Key gefunden.\n"
        "  1. Kostenlosen Account auf roboflow.com anlegen\n"
        "  2. Settings → Roboflow API Key kopieren\n"
        f"  3. install -m 600 /dev/null {KEY_FILE}  &&  nano {KEY_FILE}\n"
        "Danach diesen Befehl erneut ausführen.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def api(path: str, key: str) -> dict:
    url = f"{API}/{path.lstrip('/')}" + ("&" if "?" in path else "?") + f"api_key={key}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def cmd_check(args: argparse.Namespace) -> int:
    key = get_key(args.key)
    slugs = read_candidates()
    if args.slug:
        slugs = [s.strip() for s in args.slug if s.strip()]
    rc = 0
    for slug in slugs:
        print(f"== {slug}")
        try:
            d = api(slug, key)
        except Exception as err:
            print(f"   FEHLER: {err}")
            rc = 3
            continue
        name = d.get("name") or slug
        license_ = d.get("license") or "nicht angegeben (Universe-Seite im Browser prüfen!)"
        print(f"   name   : {name}")
        print(f"   type   : {d.get('type', '?')}")
        print(f"   license: {license_}")
        versions = d.get("versions") or []
        for v in versions[:3]:
            print(f"   version {v.get('version')}: {v.get('images')} Bilder, id={v.get('id')}")
        if not versions:
            print("   (keine veröffentlichten Versionen)")
    print(
        "\nHinweis: Lizenzangaben immer zusätzlich auf der Universe-Seite im\n"
        "Browser prüfen (nur CC0/CC BY 4.0/CC BY-SA-artige Lizenz für unser\n"
        "öffentliches Repo verwenden)."
    )
    return rc


def cmd_download(args: argparse.Namespace) -> int:
    key = get_key(args.key)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    for slug in args.slug:
        if "/" not in slug:
            print(f"überspringe {slug!r} — Format ist <workspace>/<project>")
            rc = 1
            continue
        base = slug.rstrip("/")
        try:
            d = api(base, key)
            versions = sorted((v.get("version") or ""), key=str) or []
            want = args.version or (versions[-1] if versions else "1")
            meta = api(f"{base}/{want}?format={args.format}", key)
        except Exception as err:
            print(f"== {slug}: FEHLER {err}")
            rc = 3
            continue
        export = meta.get("export") or {}
        link = export.get("link")
        if not link:
            state = export.get("generateRequested") or export.get("generating")
            print(
                f"== {slug}: Export nicht bereit ({export.get('status') or state}) — in 1–2 Min erneut."
            )
            rc = 3
            continue
        project = base.split("/")[-1]
        dest = OUT_DIR / f"{project}-v{want}"
        zpath = dest.with_suffix(".zip")
        print(f"== {slug}: lade Version {want} → {zpath}")
        urllib.request.urlretrieve(link, zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
        zpath.unlink()
        _report_dir(dest)
    return rc


def _report_dir(dest: Path) -> None:
    classes: list[str] = []
    yaml = dest / "data.yaml"
    if yaml.is_file():
        for line in yaml.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("-") or line.strip().startswith("["):
                classes = (
                    [c.strip(" -'\"[]") for c in line.split("[")[-1].split(",")]
                    if "[" in line
                    else []
                )
                if not classes:
                    continue
    splits = {}
    for split in ("train", "valid", "test"):
        n = len(list((dest / split).glob("*.jpg"))) + len(list((dest / split).glob("*.png")))
        if n:
            splits[split] = n
    print(f"   nach {dest}: {splits}")
    if classes:
        print(f"   klassen: {classes}")


def cmd_report(_args: argparse.Namespace) -> int:
    if not OUT_DIR.is_dir():
        print(f"{OUT_DIR} existiert nicht — noch nichts heruntergeladen.")
        return 0
    found = sorted(p for p in OUT_DIR.iterdir() if p.is_dir())
    if not found:
        print("keine Datensätze vorhanden")
        return 0
    for d in found:
        print(f"== {d.name}")
        _report_dir(d)
    return 0


def read_candidates() -> list[str]:
    if not CANDIDATES.is_file():
        return []
    out = []
    for line in CANDIDATES.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and "/" in line:
            out.append(line.split()[0])
    return out


def cmd_add(args: argparse.Namespace) -> int:
    slug = args.slug.strip().strip("/")
    CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    with CANDIDATES.open("a", encoding="utf-8") as f:
        f.write(f"{slug}\n")
    print(f"kandidat hinzugefügt: {slug}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c_check = sub.add_parser("check", help="Metadaten aller Kandidaten abrufen (API-Key nötig)")
    c_check.add_argument("slug", nargs="*", help="optional: konkrete Slugs statt Kandidatendatei")
    c_check.add_argument("--key", help="Roboflow API-Key")
    c_dl = sub.add_parser("download", help="Datensätze im YOLO-Format laden")
    c_dl.add_argument("slug", nargs="+", help="<workspace>/<project>")
    c_dl.add_argument("--version", help="Version (default: neueste)")
    c_dl.add_argument("--format", default="yolov8", help="yolov8|voc|coco (default: yolov8)")
    c_dl.add_argument("--key", help="Roboflow API-Key (sonst ~/.roboflow-key)")
    sub.add_parser("report", help="zusammenfassung der heruntergeladenen daten")
    c_add = sub.add_parser("add", help="slug in die kandidatendatei aufnehmen")
    c_add.add_argument("slug", help="<workspace>/<project>")
    args = ap.parse_args()
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "download":
        return cmd_download(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "add":
        return cmd_add(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
