"""Classify every PDF's text with Jev (first 2,000 chars + filename), using the questions tested in jev_eval/.
Writes to the `classifications` table in sony.db. Resumable: blobs already classified with the same variant are skipped.
usage: python3 classify.py [--workers 8] [--limit N] [--max-tokens 160000000]"""
import argparse, json, sqlite3, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "jev_eval"))
from run_eval import RELEVANT, KIND, MODEL, norm, ask  # same questions as the test

VARIANT = "head2k"
YES_KINDS = {"crt_tv", "crt_monitor", "video_deck", "video_camera", "broadcast_video_other"}
SCHEMA = """CREATE TABLE IF NOT EXISTS classifications (
    sha1 TEXT NOT NULL, variant TEXT NOT NULL, model TEXT, filename TEXT,
    relevant REAL, kind TEXT, kind_conf REAL, kind_probs TEXT, yes_kind_prob REAL,
    decision TEXT,            -- yes / no, rule: relevant >= 0.2 AND yes_kind_prob >= 0.2
    input_tokens INTEGER, sent_chars INTEGER, classified_at TEXT,
    PRIMARY KEY (sha1, variant));"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-tokens", type=int, default=160_000_000)
    args = ap.parse_args()
    db = sqlite3.connect(ROOT / "sony.db", timeout=900)
    db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA)
    todo = db.execute("""SELECT t.sha1, t.file_path,
            COALESCE((SELECT rel_path FROM source_files s WHERE s.sha1 = t.sha1 ORDER BY s.id LIMIT 1),
                     -- a re-downloaded full copy: use the path of the truncated file it replaces
                     (SELECT s.rel_path FROM live_fetches l JOIN source_files s ON s.id = l.source_id
                      WHERE l.new_sha1 = t.sha1 ORDER BY s.id LIMIT 1),
                     (SELECT s.rel_path FROM refetches r JOIN source_files s ON s.id = r.source_id
                      WHERE r.new_sha1 = t.sha1 ORDER BY s.id LIMIT 1))
        FROM texts t JOIN blobs b ON b.sha1 = t.sha1
        WHERE b.kind = 'pdf' AND t.quality != 'empty'
          AND NOT EXISTS (SELECT 1 FROM classifications c WHERE c.sha1 = t.sha1 AND c.variant = ?)
        ORDER BY t.sha1""", (VARIANT,)).fetchall()
    todo = [r for r in todo if r[2]][: args.limit]
    print(f"{len(todo)} docs to classify with {args.workers} workers", flush=True)
    tokens, stop, lock = [0], threading.Event(), threading.Lock()

    def one(row):
        if stop.is_set(): return None
        sha1, fp, rel = row
        text = norm((ROOT / fp).read_text(errors="replace"))[:2000]
        doc = {"filename": rel.rsplit("/", 1)[-1]}
        if text: doc["text"] = text
        try:
            resp = ask({"document": doc})
        except Exception as e:
            return ("error", sha1, f"{type(e).__name__}: {e}"[:200])
        a = resp["answers"]; probs = a["kind"].get("probabilities") or {}
        yk = sum(probs.get(k, 0) for k in YES_KINDS)
        rel_p = a["relevant"]["noul"]
        with lock:
            tokens[0] += resp["usage"]["input_tokens"]
            if tokens[0] >= args.max_tokens: stop.set()
        return ("ok", sha1, doc["filename"], rel_p, a["kind"]["choice"], a["kind"].get("confidence"), json.dumps(probs),
                yk, "yes" if rel_p >= 0.2 and yk >= 0.2 else "no", resp["usage"]["input_tokens"], len(text))

    t0, n, errs, last = time.time(), 0, 0, time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        for r in ex.map(one, todo):
            if r is None: continue
            if r[0] == "error":
                errs += 1; print("ERROR", r[1], r[2], flush=True); continue
            db.execute("INSERT OR REPLACE INTO classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                       (r[1], VARIANT, MODEL) + r[2:])
            n += 1
            if time.time() - last > 30: db.commit(); last = time.time()
            if n % 2000 == 0:
                el = time.time() - t0
                print(f"{n}/{len(todo)} errors={errs} tokens={tokens[0]:,} ${tokens[0]*0.042/1e6:.2f} "
                      f"{n/el*3600:.0f}/h eta={(len(todo)-n)/(n/el)/3600:.1f}h", flush=True)
    db.commit()
    print(f"done {n} classified, {errs} errors, {tokens[0]:,} tokens, ${tokens[0]*0.042/1e6:.2f}"
          + (" (STOPPED at token cap)" if stop.is_set() else ""), flush=True)

if __name__ == "__main__":
    main()
