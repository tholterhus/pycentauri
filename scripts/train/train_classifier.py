#!/usr/bin/env python3
"""Train the two-stage classifier from the labeled inbox (NO TF, seconds).

The base tflite detector stays frozen — this script trains a lightweight
sklearn classifier on the detection FEATURES (scores, box geometry,
positions) extracted from the labeled inbox frames:

    data/train/inbox/spaghetti/*.jpg   → label 1 (failure)
    data/train/inbox/negative/*.jpg    → label 0 (quiet)

The artifact lands at data/models/classifier.joblib — the detection
pipeline picks it up automatically on the next reload (no deploy needed:
the classifier reloads per backend load; a service restart makes it live
immediately).

Usage:
    venv/bin/python scripts/train/train_classifier.py [--min-score 0.30]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/opt/pycentauri/src")

import numpy as np

from pycentauri.detect.backend import Detector, detection_features

ROOT = Path("/opt/pycentauri")
INBOX = ROOT / "data/train/inbox"
OUT = ROOT / "data/models/classifier.joblib"


def main() -> int:
    from sklearn.linear_model import LogisticRegression
    
    det = Detector.from_model(ROOT / "data/models/spaghetti.tflite", force_cpu=True)

    feats: list[list[float]] = []
    labels: list[int] = []
    for cls, label in (("spaghetti", 1), ("negative", 0)):
        folder = INBOX / cls
        if not folder.is_dir():
            continue
        for jpg in sorted(folder.glob("*.jpg")):
            dets = det.detect(jpg.read_bytes(), threshold=0.10)
            feats.append(detection_features(dets))
            labels.append(label)

    if len(feats) < 8 or not (0 < sum(labels) < len(labels)):
        print(f"zu wenig gemischte Daten: {len(feats)} Frames, "
              f"{sum(labels)} positive — Inbox füllen und erneut.")
        return 1

    X = np.array(feats, dtype=np.float32)
    y = np.array(labels)
    clf = LogisticRegression(max_iter=2000, class_weight="balanced")
    clf.fit(X, y)

    import joblib

    OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, OUT)

    # In-Sample-Bericht (grob — die echte Prüfung ist der Live-Lauf)
    probs = clf.predict_proba(X)[:, 1]
    pos = probs[np.array(y) == 1]
    neg = probs[np.array(y) == 0]
    sep = (pos.min() > neg.max()) if len(pos) and len(neg) else False
    print(f"trainiert: {len(X)} Frames ({sum(y)} pos / {len(y) - sum(y)} neg)")
    print(f"prob: positives {pos.min():.3f}-{pos.max():.3f} | "
          f"negatives {neg.min():.3f}-{neg.max():.3f} | "
          f"separation: {'PASS' if sep else 'FAIL'}")
    print(f"artefakt: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
