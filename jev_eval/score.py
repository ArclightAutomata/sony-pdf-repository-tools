"""Score Jev results against the hand labels. Prints one row per text variant, plus the projected cost for the whole corpus."""
import json, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "jev_eval"
YES = {"crt_tv", "crt_monitor", "video_deck", "video_camera", "broadcast_video_other"}
ORDER = ["fname", "head1k", "head2k", "head4k", "head8k", "head16k", "ends2k", "ends4k"]

docs = {d["id"]: d for d in map(json.loads, open(EV / "sample.jsonl"))}
labels = {}
for p in sorted((EV / "labels").glob("part-*.jsonl")):
    for l in open(p):
        if l.strip(): r = json.loads(l); labels[r["id"]] = r
res = defaultdict(dict)
for l in open(EV / "results.jsonl"):
    r = json.loads(l); res[r["variant"]][r["id"]] = r

n_corpus = sqlite3.connect(ROOT / "sony.db").execute(
    "SELECT count(*) FROM texts t JOIN blobs b ON b.sha1=t.sha1 WHERE b.kind='pdf' AND t.quality!='empty'").fetchone()[0]

def auc(pos, neg):
    if not pos or not neg: return float("nan")
    return sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))

lab = {i: l for i, l in labels.items() if l["relevant"] in ("yes", "no")}
print(f"labeled {len(labels)} docs, {len(lab)} usable (yes {sum(l['relevant']=='yes' for l in lab.values())}, "
      f"no {sum(l['relevant']=='no' for l in lab.values())}); corpus {n_corpus:,} docs\n")
th = [float(x) for x in (sys.argv[1:] or ["0.5", "0.2", "0.05"])]
hdr = f"{'variant':8} {'n':>4} {'AUC':>5} " + " ".join(f"{'rec@'+str(t):>8} {'prec@'+str(t):>9}" for t in th) + \
      f" {'kindAcc':>7} {'kindRec':>7} {'tok/doc':>7} {'corpus$':>7}"
print(hdr)
for v in [v for v in ORDER if v in res] + [v for v in res if v not in ORDER]:
    rows = [(lab[i], r) for i, r in res[v].items() if i in lab]
    pos = [r["relevant"] for l, r in rows if l["relevant"] == "yes"]
    neg = [r["relevant"] for l, r in rows if l["relevant"] == "no"]
    cells = []
    for t in th:
        tp = sum(p >= t for p in pos); fp = sum(q >= t for q in neg)
        cells.append(f"{tp / len(pos):8.3f} {tp / max(tp + fp, 1):9.3f}")
    kind_acc = sum(r["kind"] == l["category"] for l, r in rows) / len(rows)
    kind_rec = sum(r["kind"] in YES for l, r in rows if l["relevant"] == "yes") / len(pos)
    rand = [r["input_tokens"] for i, r in res[v].items() if docs[i]["stratum"] == "random"]
    tok = sum(rand) / len(rand)
    print(f"{v:8} {len(rows):4} {auc(pos, neg):5.3f} " + " ".join(cells) +
          f" {kind_acc:7.3f} {kind_rec:7.3f} {tok:7.0f} {tok * n_corpus * 0.042 / 1e6:7.2f}")

if True:  # list misses
    print("\nmissed yes-docs at 0.2 (variant: ids)")
    for v in ORDER:
        if v not in res: continue
        miss = [i for i, r in res[v].items() if i in lab and lab[i]["relevant"] == "yes" and r["relevant"] < 0.2]
        print(f"  {v}: {len(miss)} {' '.join(sorted(miss))}")
