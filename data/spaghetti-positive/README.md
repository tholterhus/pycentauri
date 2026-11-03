# Positive Beispiele: echtes Spaghetti / verrutschtes Teil

Druck vom **1.11.2026, 18:25–21:27 Ortszeit** (Dateinamen UTC: 17:25–20:27),
„riesiges blaues Teil" (~3 h). In den letzten Layern ist das Teil auf dem Bett
verrutscht → Spaghetti.

**Dateinamen sind UTC** (+1 h = Ortszeit).

| Frame (UTC) | Ortszeit | Befund |
|---|---|---|
| 20261101-202247 | 21:22 | Teil klar verrutscht (oberer Rand schräg) |
| 20261101-202409 | 21:24 | verrutscht |
| 20261101-202500 | 21:25 | verrutscht |
| 20261101-202553 | 21:25 | verrutscht + Spaghetti |
| 20261101-202646 | 21:26 | Spaghettinest deutlich sichtbar |
| 20261101-202739 | 21:27 | letzter Frame vor Abbruch |

Modell-Scores auf genau diesen Frames (Replay, edgetpu): **0,38–0,555** —
unter jedem brauchbaren Threshold. Die Phase 20:00–20:21 UTC (21 Frames,
Teil noch intakt) liegt als Hard Negatives in `../false-positives/`.

Quelle: `data/collect/` (FIFO 2000 Frames — hier gesichert, bevor sie
verdrängt werden).
