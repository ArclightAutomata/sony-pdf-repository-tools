"""Draw the Jev test set: random unique PDFs with text, plus extra docs whose text hints at a CRT or video product.
Writes jev_eval/sample.jsonl and one excerpt file per doc (for hand labeling) in jev_eval/excerpts/."""
import json, random, re, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "jev_eval"
N_RANDOM, N_HINT = int(sys.argv[1]), int(sys.argv[2])
HINT = re.compile(r"trinitron|video ?cassette|\bvcr\b|camcorder|handycam|betacam|u-matic|betamax|video ?8|\bhi8\b|"
                  r"colou?r (?:tele|tv)|television receiver|\b(?:KV|PVM|BVM|SLV|CCD|DCR|EV|SL|BVW|UVW|DSR|DXC|BVP|KX|CPD|GDM)-[A-Z0-9]", re.I)
MODEL = re.compile(r"\b[A-Z]{2,5}-[A-Z0-9]{2,}[A-Z0-9/-]*")

random.seed(20261024)
db = sqlite3.connect(ROOT / "sony.db")
rows = db.execute("""SELECT t.sha1, t.file_path, t.quality, t.lang_guess, t.chars,
        (SELECT rel_path FROM source_files s WHERE s.sha1 = t.sha1 ORDER BY s.id LIMIT 1)
    FROM texts t JOIN blobs b ON b.sha1 = t.sha1
    WHERE b.kind = 'pdf' AND t.quality NOT IN ('empty')""").fetchall()
rows = [r for r in rows if r[5]]
random.shuffle(rows)
picked, hints = rows[:N_RANDOM], []
for r in rows[N_RANDOM:]:
    if len(hints) >= N_HINT: break
    if HINT.search((ROOT / r[1]).read_text(errors="replace")[:20000]): hints.append(r)

(OUT / "excerpts").mkdir(exist_ok=True)
with open(OUT / "sample.jsonl", "w") as f:
    for i, (r, stratum) in enumerate([(r, "random") for r in picked] + [(r, "hint") for r in hints]):
        sha1, fp, quality, lang, chars, rel = r
        text = (ROOT / fp).read_text(errors="replace")
        doc = dict(id=f"d{i:03d}", sha1=sha1, rel_path=rel, filename=rel.rsplit("/", 1)[-1], stratum=stratum,
                   quality=quality, lang=lang, chars=len(text), text_path=fp)
        f.write(json.dumps(doc) + "\n")
        models = sorted(set(MODEL.findall(text)))[:40]
        body = text if len(text) <= 6000 else text[:4000] + "\n\n[... middle omitted ...]\n\n" + text[-2000:]
        (OUT / "excerpts" / f"{doc['id']}.txt").write_text(
            f"ID: {doc['id']}\nPATH: {rel}\nCHARS: {len(text)}  QUALITY: {quality}  LANG: {lang}\n"
            f"MODEL-LIKE TOKENS: {' '.join(models)}\n=====\n{body}\n")
print(len(picked), "random +", len(hints), "hint docs")
