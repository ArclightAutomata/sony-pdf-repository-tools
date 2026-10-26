"""Upload the in-scope Sony documents to archive.org, one item per document (Abe approved 2026-10-25).

Each document gets its own item, `sony-pdf-repository-<first 12 hex of the dump file's SHA-1>`. The dump
file is the copy in Derf's sony-pdf-repository; when it was cut off and we found a full copy, we upload
the full copy but keep the dump file's SHA-1 in the identifier, so every item maps back to the dump.

Uploaded: every `labels` row in scope (product_kind in IN_SCOPE, scope_override NULL) that isn't already
on the wiki or in salli-cat (dup_of NULL). Files still cut off at 1 MiB are uploaded too, marked incomplete.

Metadata: mediatype texts; title from labels.title; creator Sony; language; subjects for the document
type, product kind, `sony-pdf-repository` and the model codes; a generated description (never manual
text); source; SHA-1/MD5 as external identifiers; the original file name.

Uploads are tracked in sony.db's `ia_uploads` table, never in salli-cat's manuals.db.

usage:
  python3 upload_ia.py plan [--limit N] [--sha1 PREFIX ...] [--out FILE]  # print the metadata, upload nothing
  python3 upload_ia.py upload [--limit N] [--sha1 PREFIX ...] [--workers 2]
  python3 upload_ia.py status
`upload` needs the `internetarchive` package and credentials from `ia configure`
(~/.config/internetarchive/ia.ini). Resumable: uploaded items are skipped."""
import argparse, csv, json, re, sqlite3, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_URL = "https://github.com/ArclightAutomata/sony-pdf-repository-tools"
DUMP_URL = "https://archive.org/details/sony-pdf-repository"
PREFIX = "sony-pdf-repository-"
TRUNC = 1048576
IN_SCOPE = ("crt_tv", "crt_monitor", "video_deck", "broadcast_video_other", "broadcast_camera",
            "data_tape", "remote_control", "dvd_recorder")
KIND_WORDS = {
    "crt_tv": "CRT television", "crt_monitor": "CRT monitor", "video_deck": "video tape deck",
    "broadcast_video_other": "broadcast video equipment", "broadcast_camera": "broadcast camera",
    "data_tape": "data tape drive", "remote_control": "remote control", "dvd_recorder": "DVD recorder",
}
# labels.language (ISO 639-1) -> the MARC codes archive.org uses
LANG = {"en": "eng", "ja": "jpn", "es": "spa", "fr": "fre", "de": "ger", "pt": "por", "it": "ita",
        "nl": "dut", "tr": "tur", "sv": "swe", "zh": "chi", "pl": "pol", "hu": "hun", "ru": "rus",
        "fi": "fin", "cs": "cze", "bg": "bul", "th": "tha", "sq": "alb", "sk": "slo", "ro": "rum",
        "ko": "kor", "el": "gre", "da": "dan", "no": "nor", "uk": "ukr", "hr": "hrv", "sl": "slv"}
MAX_SUBJECT_MODELS = 50  # metadata travels in HTTP headers; keep it bounded
MAX_DESC_MODELS = 12
UPLOAD_RETRIES, UPLOAD_RETRIES_SLEEP = 5, 30

SCHEMA = """CREATE TABLE IF NOT EXISTS ia_uploads (
    identifier   TEXT PRIMARY KEY,  -- sony-pdf-repository-<dump sha1[:12]>
    label_sha1   TEXT NOT NULL,     -- labels.sha1: the file uploaded (a recovered full copy, or the dump file)
    dump_sha1    TEXT NOT NULL,     -- the file in Derf's dump
    remote_name  TEXT NOT NULL,     -- file name inside the item
    status       TEXT NOT NULL,     -- uploaded | exists (found already on archive.org) | failed
    error        TEXT,
    metadata     TEXT,              -- JSON sent
    scanner      TEXT,
    uploaded_at  TEXT DEFAULT (datetime('now'))
)"""


def git_commit():
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def xml_safe(s):
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", s or "").strip()


def recovered_sources():
    """dump sha1 -> (full-copy source URL, result) from data/recovered.csv."""
    with open(ROOT / "data" / "recovered.csv", newline="") as f:
        return {r["truncated_sha1"]: (r["source_url"], r["result"]) for r in csv.DictReader(f)}


def rows(db, prefixes=None, limit=None):
    q = (f"SELECT l.sha1, l.orig_sha1, l.doc_type, l.product_kind, l.printed_title, l.title, l.language, "
         f"l.main_models, b.file_path, b.size, b.md5 FROM labels l JOIN blobs b ON b.sha1 = l.sha1 "
         f"WHERE l.scope_override IS NULL AND l.dup_of IS NULL "
         f"AND l.product_kind IN ({','.join('?' * len(IN_SCOPE))}) ORDER BY coalesce(l.orig_sha1, l.sha1)")
    out = []
    for r in db.execute(q, IN_SCOPE):
        d = dict(zip(("sha1", "orig_sha1", "doc_type", "kind", "printed_title", "title", "language",
                      "models", "file_path", "size", "md5"), r))
        d["dump_sha1"] = d["orig_sha1"] or d["sha1"]
        if prefixes and not any(d["dump_sha1"].startswith(p) or d["sha1"].startswith(p) for p in prefixes):
            continue
        out.append(d)
        if limit and len(out) >= limit:
            break
    return out


def dump_paths(db, sha1):
    return [p for p, in db.execute("SELECT rel_path FROM source_files WHERE sha1 = ? ORDER BY rel_path", (sha1,))]


def remote_name(paths, sha1):
    """The dump's file name. Web-scraped PDFs saved as <folder>/index.html take the folder's name."""
    path = Path(paths[0]) if paths else Path("")
    name = path.parent.name if path.name.lower() == "index.html" else path.name
    if name.lower().strip(" .") in ("", "pdf"):
        return f"{sha1[:12]}.pdf"
    if Path(name).suffix.lower() != ".pdf":
        name = Path(name).stem + ".pdf" if Path(name).suffix.lower() in (".html", ".htm") else name + ".pdf"
    if not Path(name).stem.strip(" ."):
        return f"{sha1[:12]}.pdf"
    return re.sub(r'[\\/:*?"<>|\x00-\x1F]', "_", name)


def models_phrase(models):
    if len(models) <= MAX_DESC_MODELS:
        return ", ".join(models[:-1]) + (" and " if len(models) > 1 else "") + models[-1]
    return ", ".join(models[:MAX_DESC_MODELS]) + f" and {len(models) - MAX_DESC_MODELS} other models"


def build(db, d, rec, scanner):
    models = json.loads(d["models"] or "[]")
    paths = dump_paths(db, d["dump_sha1"])
    recovered = d["orig_sha1"] is not None
    incomplete = d["size"] == TRUNC and not recovered
    doc = (d["doc_type"] or "document").lower()
    if d["doc_type"] == "Other":
        doc = "document"
    # no models: the printed title alone is often generic ("Technical Bulletin"), so add the file name
    fname = remote_name(paths, d["dump_sha1"])
    title = d["title"] or (f"Sony {d['printed_title']} ({fname})" if d["printed_title"] else f"Sony {doc} ({fname})")

    desc = []
    if incomplete:
        desc.append("Incomplete: this copy is cut off at 1 MiB in the source dump; no complete copy was "
                    "found on sony.com or the Wayback Machine.")
    lead = f"Sony {doc}"
    if d["kind"] in KIND_WORDS:
        lead += f" ({KIND_WORDS[d['kind']]})"
    desc.append(lead + (f" covering the {models_phrase(models)}." if models else "."))
    if d["printed_title"]:
        desc.append(f'Printed title: "{d["printed_title"]}".')
    src = f'From Derf\'s <a href="{DUMP_URL}">sony-pdf-repository</a> dump'
    if paths:
        shown = "; ".join(paths[:3]) + (f"; and {len(paths) - 3} more" if len(paths) > 3 else "")
        src += f", where it is filed as {shown}"
    desc.append(src + ".")
    source = DUMP_URL
    if recovered:
        url, how = rec.get(d["dump_sha1"], (None, None))
        if url:
            source = url
            where = "the Wayback Machine" if "web.archive.org" in url else "sony.com"
            desc.append(f"The dump's copy is cut off at 1 MiB; this is the complete file, downloaded from "
                        f'<a href="{url}">{where}</a>.')
    desc.append(f'Catalogued for the <a href="https://wiki.everycrt.com">EveryCRT wiki</a> with '
                f'<a href="{REPO_URL}">sony-pdf-repository-tools</a>.')

    subjects = ["Sony", doc, "sony-pdf-repository"]
    if d["kind"] in KIND_WORDS:
        subjects.insert(2, KIND_WORDS[d["kind"]])
    if incomplete:
        subjects.append("incomplete")
    subjects += models[:MAX_SUBJECT_MODELS]

    ext = [f"urn:sha1:{d['sha1']}"] + ([f"urn:md5:{d['md5']}"] if d["md5"] else [])
    if recovered:
        ext.append(f"urn:original-sha1:{d['dump_sha1']}")

    md = {"mediatype": "texts", "title": xml_safe(title), "creator": "Sony",
          "subject": [xml_safe(s) for s in dict.fromkeys(subjects)],
          "description": xml_safe(" ".join(desc)), "source": source,
          "external-identifier": ext, "scanner": scanner}
    if d["language"] in LANG:
        md["language"] = LANG[d["language"]]
    return {"identifier": PREFIX + d["dump_sha1"][:12], "remote_name": remote_name(paths, d["dump_sha1"]),
            "local_file": str(ROOT / d["file_path"]), "label_sha1": d["sha1"], "dump_sha1": d["dump_sha1"],
            "metadata": md}


def upload_one(item, lock, db):
    import internetarchive as ia
    ident = item["identifier"]
    status, err = "failed", None
    try:
        ia_item = ia.get_item(ident)
        if ia_item.item_metadata.get("error"):
            raise RuntimeError(ia_item.item_metadata["error"])
        if ia_item.exists and any(f.get("source") == "original" and f.get("format") != "Metadata"
                                  for f in ia_item.files):
            status = "exists"
        else:
            res = ia.upload(ident, files={item["remote_name"]: item["local_file"]}, metadata=item["metadata"],
                            retries=UPLOAD_RETRIES, retries_sleep=UPLOAD_RETRIES_SLEEP, verbose=False)
            if not res or any(r.status_code != 200 for r in res):
                raise RuntimeError(f"upload returned {[getattr(r, 'status_code', r) for r in res or []]}")
            if ia_item.exists:  # metadata sent with a PUT into an existing item is ignored
                ia.modify_metadata(ident, item["metadata"])
            status = "uploaded"
    except Exception as e:
        err = str(e)[:500]
    with lock:
        db.execute("INSERT OR REPLACE INTO ia_uploads (identifier, label_sha1, dump_sha1, remote_name, status, "
                   "error, metadata, scanner) VALUES (?,?,?,?,?,?,?,?)",
                   (ident, item["label_sha1"], item["dump_sha1"], item["remote_name"], status, err,
                    json.dumps(item["metadata"]), item["metadata"]["scanner"]))
        db.commit()
    return ident, status, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "upload", "status"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sha1", nargs="*", help="only these dump or label SHA-1 prefixes")
    ap.add_argument("--out", help="plan: write the JSON here instead of stdout")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--linked", action="store_true",
                    help="only docs that link to an existing wiki page (wiki_links, from wiki_docs.py match)")
    a = ap.parse_args()

    db = sqlite3.connect(ROOT / "sony.db", timeout=900, check_same_thread=False)
    db.execute(SCHEMA)
    db.commit()
    if a.cmd == "status":
        for s, n in db.execute("SELECT status, count(*) FROM ia_uploads GROUP BY 1"):
            print(f"{s:10s} {n}")
        print(f"{'todo':10s} {len(rows(db)) - db.execute('SELECT count(*) FROM ia_uploads WHERE status != ?', ('failed',)).fetchone()[0]}")
        return

    commit = git_commit()
    if a.cmd == "upload":
        import internetarchive  # noqa: F401  (fail now, not inside a worker)
        git = lambda *c: subprocess.run(["git", *c], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if not commit or git("status", "--porcelain") or not git("branch", "-r", "--contains", "HEAD"):
            sys.exit("commit and push the code first, so the scanner field names a published commit")
    scanner = f"sony-pdf-repository-tools ({REPO_URL})" + (f" @ {commit}" if commit else "")
    rec = recovered_sources()
    done = {i for i, in db.execute("SELECT identifier FROM ia_uploads WHERE status != 'failed'")}
    todo = rows(db, a.sha1)
    if a.linked:
        linked = {s for s, c, v, x in db.execute("SELECT sha1, crt_models, av_devices, accessories FROM wiki_links")
                  if any(json.loads(j or "[]") for j in (c, v, x))}
        todo = [d for d in todo if d["sha1"] in linked]
    items = [build(db, d, rec, scanner) for d in todo]
    if a.cmd == "upload":
        items = [i for i in items if i["identifier"] not in done]
    items = items[: a.limit or None]

    if a.cmd == "plan":
        text = json.dumps(items, indent=1, ensure_ascii=False)
        if a.out:
            Path(a.out).write_text(text)
            print(f"{len(items)} items -> {a.out}")
        else:
            print(text)
        return

    lock, n, t0 = threading.Lock(), {"uploaded": 0, "exists": 0, "failed": 0}, time.time()
    with ThreadPoolExecutor(a.workers) as pool:
        for k, (ident, status, err) in enumerate(pool.map(lambda i: upload_one(i, lock, db), items), 1):
            n[status] += 1
            if err:
                print(f"FAIL {ident}: {err}", flush=True)
            if k % 25 == 0 or k == len(items):
                print(f"{k}/{len(items)} {n} {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
