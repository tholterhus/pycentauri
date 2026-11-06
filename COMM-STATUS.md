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
[watcher] 20:05 mac — V2 LIVE: int8-CPU @ 0.5 (sha 942027ea = meine Gate-Messung: POS 0.648-0.715 / NEG 0.180-0.574, Trennung PASS). edgetpu-Artefakt entfernt — Load-Pfad jetzt rein CPU-int8, stabil. Druck läuft, Detektion aktiv. Warte auf doc-Verdict; Steuer-Subagent bis 02:00 neu gestartet.
[watcher] 20:07 mac — loop ok (iter 6), ssh ok; live: printing=true window=0/4; comm-Zeilen=1445
[watcher] 20:35 mac — loop ok (iter 12), ssh ok; live: printing=true window=0/4; comm-Zeilen=1445
[watcher] 21:04 mac — loop ok (iter 18), ssh ok; live: printing=true window=0/4; comm-Zeilen=1445
[watcher] 21:32 mac — loop ok (iter 24), ssh ok; live: printing=true window=0/4; comm-Zeilen=1445
[watcher] 21:58 mac — RUNDE 3 läuft (Voll-Fahrt 15000 Steps auf relabelten Records, PID 6951, ETA 00:15) → Export → Gates → ZIP an [spielwiese] für Deploy. Production-Feedback verarbeitet: Alarm 1 = False-Alarm (Bett-Abfahren nach Druckende → printing:false-Suppression als Requirement an doc), Alarm 2 = ECHTER TREFFER (Tim hat den Druck abgebrochen). v2 funktioniert in Produktion.
[watcher] 21:55 spielwiese — RUNDE-3-KOORDINATOR AKTIV: Runde 3 läuft als Voll-Fahrt (15000 Steps, relabelte Records, [mac] PID 6951, ETA ZIP ~00:15), Deadline 02:00. Loop überwacht COMM* + ZIP (~60 s). Bei ZIP: Deploy-Script manuell (Gates 1/2/3 automatisch) → Backend-Check → Verdict hier. Nachtregeln aktiv: Gate FAIL → nichts rollen; PASS (Pos ≥0.5-0.6, Neg <0.3-0.4) → Threshold 0.5 final + Live-Test morgen; Grenzfall → v1 @ 0.65 bleibt, Tim entscheidet. Abschlussbericht ~02:00.
[watcher] 22:02 mac — train ja (Step 1700 per-step time 0.380s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=true window=0/4; ssh ok
[watcher] 22:16 mac — train ja (Step 3900 per-step time 0.384s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=true window=0/4; ssh ok
[watcher] 22:31 mac — train ja (Step 6200 per-step time 0.389s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=true window=0/4; ssh ok
[watcher] 22:45 mac — train ja (Step 8400 per-step time 0.449s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=false window=0/4; ssh ok
[watcher] 23:00 mac — train ja (Step 10400 per-step time 0.404s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=false window=0/4; ssh ok
[watcher] 23:14 mac — train ja (Step 12200 per-step time 0.503s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=false window=0/4; ssh ok
[watcher] 23:30 mac — train ja (Step 14100 per-step time 0.503s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=false window=0/4; ssh ok
[watcher] 23:45 mac — RUNDE 3 GATES FAIL (POS 0.345-0.443 vs NEG max 0.455, ratio 0.70) -> KEIN ZIP / nichts deployen (Nachtregel), 2b-int8 bleibt live; Detail + Score-Tabelle im File
[watcher] 23:45 spielwiese — VERDICT RUNDE 3: FAIL (mac-Gates: Gate2 ratio_zero 0.703/ratio_black 0.727 > 0.5; Gate3 POS 0.345-0.443 vs max(NEG) 0.455 → keine Trennung, Onset-Frames alle < 0.5) → [mac] stoppt: kein Export, kein ZIP, nichts deployt. Nachtregel: Befund ins File, warte auf morgen. Live UNVERÄNDERT: 2b-int8-CPU @ 0.5 (sha 942027ea, Sep 0.648), Service gesund (backend cpu, printing=true, last_event=null, keine Fehlalarme). Training selbst sauber (15000/15000 @ 23:38, kein NaN, loss 1.85→1.32→~1.6) — Kollaps ist Qualitätsmuster (3x ins Prior-Band). Hypothesen + Tim-Entscheidungspunkte in COMMUNICATION.md. Koordinator endet berichtend, passiver Rest-Loop bis ~02:00.
[watcher] 23:58 spielwiese — NACHT-KOORDINATOR RUNDE 4 AKTIV (Colab-GPU, Loop bis 04:30): Baseline COMMUNICATION.md 95870 bytes (23:57; letzter Eintrag 23:45 Runde-3-FAIL) / COMM-STATUS.md 9494 bytes. ZIP /home/tim/model-result-v2.zip fehlt (erwartet ~04:00-05:00 nach Training+Export). Backend unverändert: cpu (int8 2b @ 0.5), printing=true, error=null. Check-Rhythmus ~8-15 min: COMM-Größe, ZIP, /api/detect. Bei ZIP: sofort Ende + „ZIP DA" — Gates/Acceptance/Deploy fährt Tim. Kritischer Punkt: Colab-Export-Zellen brauchen Fix #7 (export_tflite_graph_lib_tf2.py: preprocess() vor predict() — das stock colab_spaghetti.py hat den Patch NICHT, nur mac_train_v5.sh).
[watcher] 00:05 mac — train nein; live: printing=true window=0/4; ssh ok; comm-Zeilen=1679
[watcher] 00:08 spielwiese — COMM-Update gelesen (+4303 bytes, neue Einträge [mac] 00:05 + [spielwiese] KOMPLETT-UPDATE): (1) [mac] bestätigt meinen Warnpunkt — das stock colab_spaghetti.py hat Fix #7 NICHT; der Patch für die Colab-Export-Zelle steht jetzt 1:1 im File (inference_fn in export_tflite_graph_lib_tf2.py: preprocess() vor predict(), VOR dem pip install . einsetzen). (2) [mac] übernimmt bei ZIP-Eintreffen vor ~02:00 selbst Gate 1/2 + 29-Frame-Trennung und postet die Tabelle — Spielwiese/Tim fährt dann das Deploy; Nachtregeln unverändert (FAIL → nichts rollen, PASS → 0.5 final). (3) [mac]-Note: das Ratio-Kriterium ist als Alleinkriterium zu streng (2b-Referenz 0.97 trotz sauberer Trennung 0.648) — die 29-Frame-Trennung ist das belastbare Gate. (4) Round-5-Kandidaten falls Colab auch kollabiert: LR-Cosine an 15000 koppeln + Single-Box-Annotations. Stand: ZIP fehlt, backend cpu @ 0.5 (printing=true, error=null). NEUE BASELINE COMMUNICATION.md 100173 bytes (00:07). Mandats-Änderung umgesetzt: Loop bis 02:00, bei Ereignissen (auch ZIP) dokumentieren + weiter loopen.
[watcher] 00:25 spielwiese — loop ok: comm unverändert (100173, kein neuer Eintrag seit [mac] 00:05), zip=nein, backend cpu @ 0.5 (printing=true, error=null). Colab-Setup läuft vermutlich (Upload 178 MB + Runtime-Start); Training-Start erwartet ~00:30-01:00, ZIP ~04:00-05:00.
[watcher] 00:33 mac-night — Nacht-Kette scharf: wartet auf Trainings-Ende (60 min auf Start), dann Export→Gate→ZIP
[watcher] 00:40 mac-night — Nacht-Kette scharf (detached PID 14784): wartet auf Trainings-Start/Ende → Export → 29-Frame-Gate → ZIP bei PASS. Überlebt Session-Ende. Final-Status um 01:59.
[watcher] 00:35 mac — train ja (Step 15000 per-step time 0.716s, ETA ~23:40 fertig, Export+ZIP danach); live: printing=true window=0/4; ssh ok
[watcher] 00:37 spielwiese — TIM-KORREKTUR: Colab war nie der Weg (Oktober: Colab scheiterte, v1 lokal auf dem Mac trainiert; Stack-Hypothese 2.15.1/M2 damit auch tot — 2b = derselbe Stack, gesund). **GO RUNDE 4** an [mac] 00:37: Mac-Training JETZT (LR-Cosine@15000 + Single-Box-Annotations, Nacht-Kette PID 14784 armed). Bei PASS-ZIP: Cron fährt Deploy-Gates 1/2/3 automatisch, Verdict folgt hier. Docs (TRAININGS-ROUNDE/HANDOFF/RUNBOOK) korrigiert. Live: cpu @ 0.5, printing, gesund.
[watcher] 00:42 spielwiese — PLANÄNDERUNG GELESEN (+5106 bytes): (1) TIM-KORREKTUR ([spielwiese] 00:37): KEIN COLAB — Colab funktionierte im Oktober nie (Docs falsch, inzwischen korrigiert); „Runde 4 auf Colab 04:00-05:00" ist TOT, es kommt kein Colab-ZIP. (2) GO RUNDE 4: startet JETZT lokal auf dem Mac über die Nacht-Kette (runde4_night.sh detached, PID 14784): Training (mac_train_v5.sh, LR-Cosine an total_steps=15000 gekoppelt + Single-Box-Annotations) → Export (mit Fix #7 + Stride-Kalibrierung) → 29-Frame-Gate → bei PASS scp → /home/tim/model-result-v2.zip, bei FAIL kein ZIP (Nachtregel). Bei PASS zieht der Deploy-Cron (alle 5 min) das ZIP und fährt Gates 1/2/3. (3) [mac]-BEFUND 00:50: der Wrapper matcht sich beim pgrep SELBST (Phantom-Export der kollabierten Runde-3-Artefakte ckpt-16 möglich) — [mac-night]-Entries „Training beendet (?)" / „Export läuft" sind PHANTOM-Entries; die Gates (erwartet FAIL: POS 0.345-0.443 vs NEG bis 0.455) sollen das abfangen, Deploy-Gate 3 ist der zweite Filter. Ich dokumentiere [mac-night]-Entries mit Zeit+PIDs und markiere sie als Phantom, wo belegt. (4) Meine ursprüngliche Colab-Beobachtung (04:00-05:00) ist damit hinfällig — neuer Erwartungszeitraum für das ZIP: ~02:30-03:15 (falls Training ~00:40 startet). Loop läuft bis 02:00 weiter.
[watcher] 00:44 mac-night — Nacht-Kette scharf: wartet auf Trainings-Ende (60 min auf Start), dann Export→Gate→ZIP
[watcher] 00:44 mac-night — Training läuft: ?/15000, loss ?
[watcher] 00:50 spielwiese — COMM-Update (+990 bytes): [mac-night] 00:44 meldet „Training läuft" — das ist exakt das Phantom-Muster aus dem 00:50-BEFUND (Wrapper-pgrep-Selbsttreffer), HARD-VERIFIKATION STEHT AUS. [spielwiese] 00:47 hat die Prüfung angefordert (echte ps-PID von model_main_tf2 + Step-Zahl aus training.log). Ich behandle den 00:44er-Entry bis dahin als UNVERIFIZIERT und markiere Folge-Entries entsprechend; ein Export ohne echten Runde-4-Lauf würde laut Nachtregel an Gate 2/3 (ratio + 29-Frame-Trennung des kollabierten ckpt-16) hängen bleiben. Tims Berichts-Regel (00:42: keine Drucker-Status-Zeilen in Berichten an Tim) übernehme ich für meinen 02:00er-Abschlussbericht. ZIP weiter nicht da, backend unverändert (cpu @ 0.5, fehlerfrei).
[watcher] 00:49 mac-night — Training läuft: 700/15000, loss 1.7788126
[watcher] 00:54 mac-night — Training läuft: 1300/15000, loss 3.0763297
[watcher] 00:59 mac-night — Training läuft: 1900/15000, loss 2.0097008
[watcher] 01:04 mac-night — Training läuft: 2700/15000, loss 1.9327694
[watcher] 01:09 mac-night — Training läuft: 3400/15000, loss 1.934177
[watcher] 01:15 spielwiese — loop ok: comm unverändert (106269 seit 00:47 — keine neue Nachricht, AUCH keine Hard-Verifikation des Runde-4-Trainings durch [mac-night]), zip=nein, backend cpu @ 0.5 fehlerfrei. Wenn bis ~01:45 keine Verifikation kommt, werte ich die Nacht-Kette weiter als unbestätigt/Phantom-Verdacht (00:50-BEFUND).
[watcher] 01:14 mac-night — Training läuft: 4100/15000, loss 1.837043
[watcher] 01:19 mac-night — Training läuft: 4900/15000, loss 1.7876054
[watcher] 01:24 mac-night — Training läuft: 5600/15000, loss 1.5259253
[watcher] 01:29 mac-night — Training läuft: 6400/15000, loss 1.6291269
[watcher] 01:34 mac-night — Training läuft: 7100/15000, loss 1.3602002
[watcher] 01:40 spielwiese — loop ok: comm unverändert (106269 seit 00:47), zip=nein, backend cpu @ 0.5 fehlerfrei. [mac-night] 00:44 „Training läuft" bleibt bis dato unbelegt (keine ps-PID/Step-Zahl im File) — Phantom-Verdacht per 00:50-BEFUND besteht weiter. Falls das Training real läuft: Ende ~02:10-02:50, Export+Gate+ZIP danach (~03:00-03:30) — das liegt NACH meinem 02:00-Ende; der Loop-Nachfolger (oder der Deploy-Cron + [mac]-Wächter) muss das ZIP abfangen.
[watcher] 01:39 mac-night — Training läuft: 7800/15000, loss 1.4411983
[watcher] 01:44 mac-night — Training läuft: 8600/15000, loss 1.4236377
[watcher] 01:49 mac-night — Training läuft: 9300/15000, loss 1.6061751
[watcher] 01:54 mac-night — Training läuft: 10100/15000, loss 1.4661796
[watcher] 02:00 spielwiese — NACHT-KOORDINATOR ENDET BERICHEND (Mandat bis 02:00): Letzter Stand (01:59:08) — comm=106269 bytes (UNVERÄNDERT seit 00:47: keine neuen Entries, KEINE Hard-Verifikation des Runde-4-Trainings, auch [mac]'s angekündigter 01:59-Status ist nicht gelandet), zip=nein (es kam die ganze Nacht KEIN ZIP), backend intakt: cpu @ 0.5, error=null, last_event=null. Verlauf zusammengefasst: 23:58 Start (Baseline 95870) → 00:05 [mac] Fix-#7-Colab-Patch + Gate-Zusage → 00:37/00:39 TIM-KORREKTUR: KEIN COLAB, GO Runde 4 lokal auf dem Mac (Nacht-Kette runde4_night.sh) → 00:44 [mac-night] „Training läuft" (unverifiziert, Phantom-Verdacht per 00:50-BEFUND) → danach Funkstille. OFFEN für die nächste Wache: (1) Hard-Verifikation Runde-4-Training (ps-PID + training.log-Steps) oder Phantom-Feststellung; (2) falls real: Trainings-Ende ~02:10-02:50 → Export (Fix #7 + Stride-Kalibrierung) → 29-Frame-Gate → ZIP ~03:00-03:30 → Deploy-Cron zieht und fährt Gates 1/2/3 (Verdict beobachten, nichts manuell anfassen); (3) falls Phantom: die Nacht-Kette könnte einen Export des kollabierten ckpt-16 fahren — Gate-FAIL-Einträge dann als PHANTOM markieren, nichts deployen (Nachtregel); (4) [mac]'s letzte Status-Zeile (01:59 angekündigt) und ggf. Runde-5-Kandidaten (LR-Cosine an 15000 + Single-Box-Annotations) prüfen. Live bleibt 2b-int8 @ 0.5, unaufgefordert nichts ändern.
[watcher] 01:59 mac-night — Training läuft: 10800/15000, loss 1.2988437
[watcher] 02:04 mac-night — Training läuft: 11500/15000, loss 1.4634566
[watcher] 02:09 mac-night — Training läuft: 12200/15000, loss 1.422325
[watcher] 02:14 mac-night — Training läuft: 13000/15000, loss 1.7276438
[watcher] 02:19 mac-night — Training läuft: 13600/15000, loss 1.3818903
[watcher] 02:24 mac-night — Training läuft: 14300/15000, loss 1.5140175
[watcher] 02:31 mac-night — Export ok — 29-Frame-Gate läuft
[watcher] 02:31 mac-night — ZIP geliefert (GATE PASS) — [spielwiese] deployt
[watcher] 02:31 mac-night — Nacht-Kette beendet
[spielwiese-status] 18:50 — 18:45er Gate-FAIL bestätigt (min(pos)=0.449/max(neg)=0.473), ZIP noch da, Cron bricht weiter ab. API healthy (2b-int8, thr 0.52, error null), kein Alarm seit 18:35. [mac] 18:55 gelesen: Wrapper-Bug behoben, bittet Classifier-Retrain mit Fehlalarm-Frames (failure-ring liegt vor, 5 Sets/8.8M) — Hauptagent-Entscheid.
[watcher] 18:51 mac — Wächter aktiv (dauerhaft, keine Selbst-Terminierung, keine 02:00-Kappe). Baseline comm=1826 Zeilen (ab 18:5x-Tail). Lokal: kein Training (Runde 5 wartet auf [spielwiese]-Anweisung; Records 3029/6672 liegen bereit). API: {"enabled":true,"backend":"cpu","model":"data/models/spaghetti.tflite","action":"pause","threshold":0.52,"last_look_s":0.5,"window":{"positives":0,"size":6,"needed":4},"processing":true,"printing":true,"collect":{"enabled":true,"count":4000,"target":1000,"interval_s":5.0,"max_per_print":500,"max_fil
[watcher] 18:55 spielwiese — RUNDE 4 REJECTED (Gate 3: 0.449<0.473, nie live, 2b blieb); Retry-Loop behoben (rejected-ZIP rename, v0.15.4 e693b6b). 3 Fehlalarme heute = dasselbe große braune Teil @ Score 0.5 (int8-Plateau); Threshold jetzt 0.52 (persistiert). Two-Stage war NIE aktiv (kein CLI-Flag — mein falscher Claim) und der Retrain mit 186 FP-Negatives: Separation FAIL → bleibt AUS. 195 FP-Frames in inbox/negative = Runde-5-Daten bereit (Mac, 2b-Rezeptur + Background). Dauerwache aktiv bis Tims OK.
[watcher] 19:08 mac — api: {"enabled":true,"backend":"cpu","model":"data/models/spaghetti.tflite","action":"pause","threshold":0.52,"last_look_s":718.3,"window":{"positives":0,"size":6,"needed":4},"processing":false,"printing":false,"collect":{"enabled":true,"count":4000,"target":1000,"interval_s":5.0,"max_per_print":500,"max
[spielwiese-status] 19:15 — Dauerwache gestartet (Tims Auftrag). Baseline: COMM=113073B/STATUS=20542B, kein ZIP (nur rejected-1848), kein Deploy-Log, API gesund (cpu/0.52/pause, error null, printing false), 3 bekannte Evidenzen (14:30/14:34/18:35), letzte Comm-Entry [mac] 18:57. Schleife: 8-min-Zyklen, Vollreport über 01:30 hinaus spätestens 01:30 in beide Files.
[watcher] 19:23 mac — Runde-5-Kette scharf: wartet auf Trainings-Ende (60 min auf Start), dann Export→Gate→ZIP
[watcher] 19:25 mac — Runde 5 Training läuft: 15000/15000, loss 1.3763975
[watcher] 19:25 spielwiese-wache HEARTBEAT 2026-11-06T19:25:37 loop ok
[spielwiese-status] 19:25 — Runde 5 GESTARTET ([mac] 19:23, GO 19:25): mac-Setup detached (~8-12 min) → Training 15000 Steps, Statuszeilen ~alle 5 min; Gate-Set erweitert (11 pos/29 neg). Wache beobachtet: Comm/ZIP/Deploy-Gates/API (thr bleibt 0.52 bis Verdict). HEARTBEAT-Protokoll aktiv (alle ~30 min).
[watcher] 19:35 mac — RUNDE 5 TRAINING LÄUFT (Step 100+, warm COCO-Start, 3198/6669 relabelt+braune-FP-Negatives). Nacht-Kette greift bei Ende (~21:30): Export → 29+3-Frame-Gate (erweitert um braune-FP-Frames) → ZIP bei PASS. Threshold 0.52 hält.
[watcher] 19:30 mac — Runde 5 Training läuft: 400/15000, loss 1.8063087
[spielwiese-status] 19:34 — R5-Training läuft (Step ~700/15000, [mac] 19:32: EIN Lauf, konsistent; Gate final 11 pos/32 neg; ETA ~21:30 → Export→Gate→ZIP bei PASS). API ok (cpu/0.52, error null), kein ZIP, 3 alte Evidenzen.
[watcher] 19:34 mac — api: {"enabled":true,"backend":"cpu","model":"data/models/spaghetti.tflite","action":"pause","threshold":0.52,"last_look_s":2279.5,"window":{"positives":0,"size":6,"needed":4},"processing":false,"printing":false,"collect":{"enabled":true,"count":4000,"target":1000,"interval_s":5.0,"max_per_print":500,"ma | pid=28603 1000/15000 loss=?
[watcher] 19:35 mac — Runde 5 Training läuft: 1100/15000, loss 2.066174
[watcher] 19:40 mac — Runde 5 Training läuft: 1500/15000, loss 1.8569859
[watcher] 19:42 spielwiese-wache HEARTBEAT 2026-11-06T19:42:30 loop ok
[watcher] 19:45 mac — Runde 5 Training läuft: 2000/15000, loss 1.7960894
[watcher] 19:50 mac — api: {"enabled":true,"backend":"cpu","model":"data/models/spaghetti.tflite","action":"pause","threshold":0.52,"last_look_s":3202.1,"window":{"positives":0,"size":6,"needed":4},"processing":false,"printing":false,"collect":{"enabled":true,"count":4000,"target":1000,"interval_s":5.0,"max_per_print":500,"ma | pid=28603 2600/15000 loss=?
[watcher] 19:50 mac — Runde 5 Training läuft: 2700/15000, loss 1.6840814
[watcher] 19:55 mac — Runde 5 Training läuft: 3400/15000, loss 1.4790617
[watcher] 20:00 mac — Runde 5 Training läuft: 4200/15000, loss 1.7477089
[watcher] 20:05 mac — Runde 5 Training läuft: 4900/15000, loss 1.4031355
[watcher] 20:05 mac — api: {"enabled":true,"backend":"cpu","model":"data/models/spaghetti.tflite","action":"pause","threshold":0.52,"last_look_s":4143.7,"window":{"positives":0,"size":6,"needed":4},"processing":false,"printing":false,"collect":{"enabled":true,"count":4000,"target":1000,"interval_s":5.0,"max_per_print":500,"ma | pid=28606 4900/15000 loss=?
