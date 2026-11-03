# False Positives / Hard Negatives (kein Spaghetti, hohe Scores)

**Dateinamen sind UTC** (+1 h = Ortszeit).

## Fehlalarme vom 2.11.2026 (Threshold 0.5, action=pause löste aus)

Evidence-Paare (JPG + JSON mit Score und Boxen) aus `data/evidence/`:

| Frame (UTC) | Ortszeit | Score | Befund |
|---|---|---|---|
| 20261102-204543 | 21:45 | 0.6133 | junger Druck, kein Spaghetti; Box am oberen Bildrand (Druckkopf/Kabel) |
| 20261102-204929 | 21:49 | 0.50  | derselbe Druck nach Resume, erneut Fehlalarm + Pause |

Score-Verteilung dieses Fehlalarm-Drucks (82 gesammelte Frames, Replay):
28× ≥0.50, 9× ≥0.55, 2× ≥0.60, **0× ≥0.62**, max 0.6172.

## Hard Negatives: 1.11., „riesiges blaues Teil" noch intakt

21 Frames (`20261101-200044` … `20261101-202154`, Druckphase ohne Shift,
Teil voll intakt) — das Modell scorete hier bis **0.625** (20:10:26 UTC,
einzelner Aussreißer; typisch 0.29–0.58). Gleiche Fehlermode wie die
Fehlalarme: Modell reagiert auf große blaue Wände / Druckkopf-Region,
nicht auf Spaghetti.

## Konsequenz fürs Training

Score-Verteilungen von „normal (inkl. großes intaktes Teil)" und „echtes
Spaghetti" überlappen (normal bis 0.625, Spaghetti 0.29–0.625) — kein
Threshold trennt sie. Retraining mit diesen Frames ist der Fix; die
intakten Frames als Negatives beilegen, die Spaghetti-Frames aus
`../spaghetti-positive/` als Positives.
