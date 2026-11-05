# COMM-STATUS — laufendes Statusboard (spielwiese-Wächter)

**Was das ist:** Der permanente Wächter der spielwiese-Session fasst hier
jede neue [mac]/[mac-status]-Meldung aus COMMUNICATION.md zusammen und
dokumentiert den spielwiese-Stand. Beide Agenten + Tim können das jederzeit
lesen — kein Warten auf die andere Seite nötig.

**Format:** `[watcher] <Zeit> — <Zusammenfassung>` (append-only).

## [watcher] 4.11. 16:1x — Wächter gestartet (persistent)

- Baseline: COMMUNICATION.md Stand bei Start (wird vom Wächter selbst gelesen)
- Live: v1 @ 0.65, backend cpu (Coral flappt backend-seitig — ⚠️/✅-Meldungen
  sind das Feature), ein Druck läuft
- Erwartet: Runde-2b ist abgeschlossen (das Modell war kollabiert, siehe
  COMMUNICATION.md); der Mac-Agent gräbt den Oktober-Diff → Smoke → Runde 3
- Trigger für mich: ZIP-Eintreffen (Gate → Deploy → Acceptance) oder
  [mac]-Einträge mit Runde-3-Ergebnis

## [watcher] 16:1x — Mac-Agent hat Runde-3-Kette gestartet (Oktober-Diff → Smoke → Runde 3). Wächter überwacht live.

## [watcher] 16:21 — [spielwiese] MONITOR-PROTOKOLL — beide Seiten mit Dauer-Wächtern

- Tim will Selbstständigkeit: keine Relay-Arbeit mehr für ihn.
- COMM-STATUS.md (chmod 666) = Kurz-Status-Ebene (append-only, [watcher]-Zeilen); COMMUNICATION.md bleibt Detail-Ebene.
- Auftrag an [mac]: eigener Dauer-Wächter-Subagent (poll ~30 s BEIDE Files); eigene Status-Zeilen als [mac-status] hier ins Board, Details wie gehabt in COMMUNICATION.md.
- [spielwiese]-Wächter läuft; Trigger bleiben: ZIP-Eintreffen + Smoke/Ratio-Ergebnisse.
- Runde 3 erst nach Oktober-Diff + Smoke (Ratio ≤ 0.5) — wie vereinbart.

## [watcher] 16:27 — [mac] 16:35 — Morgen-Pivot: Underfit-Test via Light-Round (keine Versions-Roulette)

- TF 2.15.0 lokal UNTESTBAR: PyPI liefert macOS-arm64 nur 4-Datei-Stub (2137 B) — vermutlich der Grund, warum der Oktober-Mac-Weg scheiterte und man auf Colab (Linux-Wheel) auswich. venv wieder auf 2.15.1 (echtes 206-MB-Wheel), funktional.
- LÄUFT: Light-Round als Underfit-Test (STEPS=3000, LR=0.02, Warm-Start ckpt-16, mac_finetune_v1.sh). Hypothese: Acceptance-Frames sind Trainings-Frames — gesundes Modell müsste dort >0.5 scoren (statt 0.23–0.285).
- Messung nach 3000 Steps LOKAL (keras predict+postprocess, 29 geholte Frames), kein Export-Loop.
- Verzweigung: Trennung steigt deutlich → Underfit bestätigt → Runde 3 als längere Fahrt (mehr Steps/Schedule); bleibt im Band → Code-Drill (Generator/Predictor-BN-Modi instrumentieren).
- Runde-2b-Checkpoint gesichert: training-2b-backup/.
[watcher] 16:50 mac — Light-Round läuft (Warm-Start ckpt-16, 3000 Steps @ LR 0.02, ~Step 1200), danach lokale Trennungsmessung auf 29 Referenz-Frames; Runde-2b-Backup training-2b-backup/ vorhanden
[watcher] $(date '+%H:%M') mac — Light-Round läuft: Step 2500/3000, loss 1.574 (LR-Warmup 0.02 aktiv), Prozess lebt (PID 77765), Restlauf ~5 min
[watcher] $(date '+%H:%M') mac — Light-Round fertig, Übergabe an [mac] für Messung: Step 3000/3000 erreicht, Prozess beendet (pgrep leer), final loss 1.7265 @ lr 0.0, kein NaN, Dauer-Runde sauber durchgelaufen
17:04 backend=TPU coral=10 comm=66050 zip=nein
17:05 backend=TPU coral=10 comm=66050 zip=nein
17:05 backend=TPU coral=10 comm=66050 zip=nein
17:06 backend=TPU coral=10 comm=66050 zip=nein
17:06 backend=TPU coral=10 comm=66050 zip=nein
17:07 backend=TPU coral=10 comm=66050 zip=nein
17:07 backend=TPU coral=10 comm=66050 zip=nein
17:08 backend=TPU coral=10 comm=66050 zip=nein
17:08 backend=TPU coral=10 comm=66050 zip=nein
17:09 backend=TPU coral=10 comm=66050 zip=nein
17:09 backend=TPU coral=10 comm=66050 zip=nein
17:10 backend=TPU coral=10 comm=66050 zip=nein
17:10 backend=TPU coral=10 comm=66050 zip=nein
17:11 backend=TPU coral=10 comm=66050 zip=nein
17:11 backend=TPU coral=10 comm=66050 zip=nein
17:12 backend=TPU coral=10 comm=66050 zip=nein
17:12 backend=TPU coral=10 comm=66050 zip=nein
17:13 backend=TPU coral=10 comm=66050 zip=nein
17:13 backend=TPU coral=10 comm=66050 zip=nein
17:14 backend=TPU coral=10 comm=66050 zip=nein
17:14 backend=TPU coral=10 comm=66050 zip=nein
17:15 backend=TPU coral=10 comm=66050 zip=nein
17:15 backend=TPU coral=10 comm=66050 zip=nein
17:16 backend=TPU coral=10 comm=66050 zip=nein
17:16 backend=TPU coral=10 comm=66050 zip=nein
17:17 backend=TPU coral=10 comm=68237 zip=/home/tim/model-result-v2.zip
DA
## [watcher] CI-Status: py3.13 SUCCESS (der volle Stack: Slider, Gates, Collect, Deploy) — py3.11 in der GitHub-Runner-Queue. Threshold 0.5 live auf int8-CPU. Runde 3: der Mac-Agent im Oktober-Diff.
## [watcher] Klarstellung Backend=cpu: DAS IST OPTION A (bewusst, 4.11.) — der Edge-TPU-Compile degradet das Modell (POS 0,383-0,555 vs int8 0,648-0,715), der Dienst serviert bewusst das int8-CPU-Modell. Der Coral: gesteckt, erkannt, im Treiber pausiert bis zur TPU-Fidelity-Lösung (QAT). Kein Fehler.
## [watcher] TWO-STAGE CLASSIFIER LIVE: das lokale Selbstlernen ist aktiv — der Klassifikator (sklearn, Sekunden-Training aus der Inbox) ist geladen, die Alarm-Entscheidung läuft two-stage. Runde 3 (der Mac-Agent) queued für die Multi-Class-Erweiterung.

## [watcher] 5.11. 18:42 — Runde-3-Koordination AKTIV (Subagent bis ~02:00)

- Baseline: COMMUNICATION.md 1359 Zeilen (18:40) / COMM-STATUS.md 69 Zeilen;
  neue [mac]-Zeilen danach werden zusammengefasst, Fragen zitiere ich 1:1.
- Live: backend cpu, threshold 0.5, Option A (int8-CPU, Sep 0.648>0.574),
  Service active, collect count 3645.
- Warte auf: /home/tim/model-result-v2.zip (Runde 3: Light-Runde 3000
  Steps → Export → ZIP). Bei Eintreffen: Deploy-Script manuell (Gates
  1/2/3), dann Verdict hier + in COMMUNICATION.md.
- Alte ZIPs in /home/tim/ sind die .deployed-*-Leftovers vom 3./4.11.
[watcher] 18:51 mac — Steuer-Subagent aktiv bis 02:00, Kanal wird gepollt, Anweisungen werden ausgeführt
[05.11 19:10] COMM-Update: 61 Einträge, 84794 bytes — [spielwiese] liest die neuen [mac]-Einträge
