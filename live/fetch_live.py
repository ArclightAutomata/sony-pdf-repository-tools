"""Fetch complete copies of truncated www.sony.com PDFs from the live site, through a real Chrome
(patchright with the installed Google Chrome, on an Xvfb virtual display) so Akamai's bot check passes.

Needs patchright (see requirements.txt), Google Chrome and Xvfb.
    python live/fetch_live.py [--limit N] [--delay 2.5]
    [--shard I/N] [--proxy http://127.0.0.1:PORT] [--display :97]
Several copies can run at once, one per outgoing IP: give each its own --shard, --proxy and --display.

sony.com comes first: `sonyarc.py refetch` (archive.org) only takes a www.sony.com file after this script
has failed on it (not_found / small / error). Order: files archive.org already tried and couldn't fix, then
the rest (Jev "yes" first). A 403 whose body is S3's AccessDenied XML means the file is gone (not_found);
any other 403 is Akamai blocking us (blocked, retried on the next run).
Results go in the `live_fetches` table. A download is accepted only if it is a PDF larger than 1 MiB;
prefix_match = 1 when its first 1 MiB is byte-identical to our truncated copy (the same file).
"""
import argparse, base64, os, re, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sonyarc  # noqa: E402  (store_file, insert_blob)
from patchright.sync_api import sync_playwright  # noqa: E402

MIB = 1048576
PATH_RE = re.compile(r"Consumer CRT Manuals/(www\.sony\.com)/\d{14}/(.*)$")
SCHEMA = """CREATE TABLE IF NOT EXISTS live_fetches (
    source_id INTEGER PRIMARY KEY, orig_sha1 TEXT, url TEXT,
    status TEXT,          -- recovered / not_found / small / blocked / error
    http_status INTEGER, size INTEGER, new_sha1 TEXT, prefix_match INTEGER, note TEXT,
    fetched_at TEXT DEFAULT (datetime('now')));"""
LANDING = "https://www.sony.com/electronics/support"
FETCH_JS = """async (u) => {
    const r = await fetch(u, {credentials: 'include'});
    const b = new Uint8Array(await r.arrayBuffer());
    let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return {status: r.status, type: r.headers.get('content-type'), b64: btoa(s)};
}"""


def is_pdf(body):
    # Same test as sonyarc's sniffing: some Sony PDFs carry a 128-byte MacBinary header before %PDF.
    return b"%PDF" in body[:1024]


def todo(db, limit, shard=0, nshards=1):
    rows = db.execute("""
        SELECT s.id, s.rel_path, s.sha1, b.file_path,
               CASE WHEN r.status IN ('no_capture', 'only_truncated') THEN 0 WHEN r.status IS NULL THEN 1 ELSE 9 END AS grp,
               (SELECT MAX(c.decision = 'yes') FROM classifications c WHERE c.sha1 = s.sha1) AS yes
        FROM source_files s JOIN blobs b ON b.sha1 = s.sha1
        LEFT JOIN refetches r ON r.source_id = s.id
        WHERE s.size = 1048576 AND b.kind = 'pdf' AND s.rel_path LIKE 'Consumer CRT Manuals/www.sony.com/%'
          AND s.id NOT IN (SELECT source_id FROM live_fetches WHERE status NOT IN ('blocked', 'error'))
          AND COALESCE(r.status, '') != 'recovered'
          AND s.id % ? = ?
        ORDER BY grp, yes DESC, s.id DESC""", (nshards, shard)).fetchall()
    return rows[:limit] if limit else rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between PDF requests")
    ap.add_argument("--shard", default="0/1", help="I/N: take only files with id %% N == I")
    ap.add_argument("--proxy", help="send this browser through an HTTP proxy, e.g. http://127.0.0.1:18898")
    ap.add_argument("--display", default=":97", help="Xvfb display number for this browser")
    args = ap.parse_args()
    db = sqlite3.connect(ROOT / "sony.db", timeout=900)
    db.executescript(SCHEMA)
    shard, nshards = map(int, args.shard.split("/"))
    rows = todo(db, args.limit, shard, nshards)
    print(f"{len(rows)} files to try live (shard {shard}/{nshards}, via {args.proxy or 'direct'})", flush=True)

    xvfb = subprocess.Popen(["Xvfb", args.display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    env["DISPLAY"] = args.display
    counts, t0, blocked_streak = {}, time.time(), 0
    try:
        with sync_playwright() as p:
            profile = ROOT / "live" / ("profile" if shard == 0 else f"profile-{shard}")
            ctx = p.chromium.launch_persistent_context(str(profile), channel="chrome",
                                                       headless=False, env=env, no_viewport=True,
                                                       args=["--window-size=1920,1080"],
                                                       proxy={"server": args.proxy} if args.proxy else None)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def warm():
                page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)

            warm()
            for i, (sid, rel, sha1, fp, grp, yes) in enumerate(rows, 1):
                url = "https://%s/%s" % PATH_RE.match(rel).groups()
                rec = dict(source_id=sid, orig_sha1=sha1, url=url, status="error", http_status=None,
                           size=None, new_sha1=None, prefix_match=None, note=None)
                try:
                    res = page.evaluate(FETCH_JS, url)
                    body = base64.b64decode(res["b64"])
                    rec.update(http_status=res["status"], size=len(body))
                    if res["status"] == 403 and b"<Code>AccessDenied</Code>" in body[:300]:
                        # S3's answer for a missing file (listing is off), not Akamai: the file is gone.
                        rec.update(status="not_found", note="S3 AccessDenied")
                    elif res["status"] == 403:
                        rec["status"] = "blocked"
                    elif res["status"] == 404:
                        rec["status"] = "not_found"
                    elif res["status"] == 200 and is_pdf(body) and len(body) > MIB:
                        ours = (ROOT / fp).read_bytes()
                        rec["prefix_match"] = int(body[:MIB] == ours[:MIB])
                        sonyarc.TMP.mkdir(exist_ok=True)
                        fd, tmp = tempfile.mkstemp(dir=sonyarc.TMP, suffix=".pdf")
                        with os.fdopen(fd, "wb") as f:
                            f.write(body)
                        blob = sonyarc.store_file(Path(tmp), "x.pdf", kind_hint="pdf")
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                        sonyarc.insert_blob(db, blob)
                        rec.update(status="recovered", new_sha1=blob["sha1"])
                    elif res["status"] == 200 and is_pdf(body):
                        rec["status"] = "small"
                    else:
                        rec.update(status="error", note=f"{res['status']} {res['type']}")
                except Exception as e:
                    rec["note"] = f"{type(e).__name__}: {e}"[:300]
                db.execute("""INSERT OR REPLACE INTO live_fetches (source_id, orig_sha1, url, status, http_status,
                              size, new_sha1, prefix_match, note) VALUES (:source_id, :orig_sha1, :url, :status,
                              :http_status, :size, :new_sha1, :prefix_match, :note)""", rec)
                db.commit()
                counts[rec["status"]] = counts.get(rec["status"], 0) + 1
                # Akamai turned on us: get a fresh session; stop if it keeps happening.
                blocked_streak = blocked_streak + 1 if rec["status"] == "blocked" else 0
                if blocked_streak in (3, 6):
                    print(f"{blocked_streak} blocks in a row; re-warming session", flush=True)
                    page.wait_for_timeout(60000 * blocked_streak)
                    warm()
                elif blocked_streak >= 9:
                    print("still blocked after re-warming; stopping", flush=True)
                    break
                if i % 25 == 0 or i == len(rows):
                    el = time.time() - t0
                    print(f"{i}/{len(rows)} {counts} {i / el * 3600:.0f}/h", flush=True)
                page.wait_for_timeout(args.delay * 1000)
            ctx.close()
    finally:
        xvfb.terminate()
    print("done", counts, flush=True)


if __name__ == "__main__":
    main()
