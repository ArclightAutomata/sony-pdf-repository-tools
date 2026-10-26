# Jev text-length test (2026-10-24)

400 unique PDFs: 260 random + 140 whose text hints at CRT/video gear. Hand-labeled by reading the text
(labels/, rules in LABELING.md): 156 relevant, 244 not. Every doc was sent to jev-1.14.0 under 8 text variants
with two questions (relevant = noul, kind = 12-way choice). 3,200 calls, 5.83M input tokens, $0.245.

Scripts: make_sample.py (sample + excerpts), run_eval.py (Jev calls → results.jsonl, resumable), score.py.

Best decision rule: relevant noul >= 0.2 AND summed probability of the five relevant kinds >= 0.2.

| variant | recall (all 156) | precision (all) | random 260: recall / false yes | corpus cost (90k docs) |
|---|---|---|---|---|
| filename only | 0.89 | 0.43 | | $2.74 |
| first 1k | 0.98 | 0.90 | 70/73, 10 | $4.07 |
| first 2k | 1.00 | 0.92 | 73/73, 9 | $5.13 |
| first 1k + last 1k | 0.99 | 0.95 | 72/73, 4 | $5.32 |
| first 4k | 0.99 | 0.95 | 72/73, 5 | $6.89 |
| first 2k + last 2k | 1.00 | 0.95 | 73/73, 4 | $7.17 |
| first 8k | 1.00 | 0.96 | 73/73, 4 | $9.85 |
| first 16k | 0.99 | 0.97 | 72/73, 3 | $14.59 |

About 700 tokens per call are the questions themselves; shorter category descriptions would cut cost further.
Most remaining false yeses are edge cases: projectors, LCD/OLED PVM/BVMs, rebate and pricing bulletins.
