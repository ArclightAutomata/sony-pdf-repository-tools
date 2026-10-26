#!/usr/bin/env python3
"""Sony manual archive: index, content-addressed copy, and OCR.

Layout (all next to this script):
  sony.db                     SQLite database
  files/ab/cd/<sha1>.<ext>    content-addressed blobs (originals and OCR variants),
                              (a git-style SHA-1 trie)
  text/ab/cd/<sha1>.txt       extracted text of each PDF (embedded or OCR'd), for classification

Commands:
  index   load source_listing.nul (from `find -printf '%s\\t%T@\\t%P\\0'`) into source_files
  ingest  hash + copy every pending source file into files/
  ocr     OCR every PDF blob that lacks a text layer, store result as an 'ocr' variant
  refetch look up and download non-truncated Wayback captures of the 1 MiB-truncated PDFs
  textstats  (re)compute language / OCR-quality stats on stored texts
  status  print progress
"""
import argparse
import concurrent.futures as cf
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "sony.db"


def load_env(path=ROOT / ".env"):
    """Read KEY=value lines from .env into os.environ (real environment variables win)."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
# The unpacked dump's UNSORTED folder (from the archive.org item sony-pdf-repository).
# Set SONY_PDF_SOURCE in .env or the environment, or pass `ingest --source`.
SOURCE_ROOT = Path(os.environ.get("SONY_PDF_SOURCE", "UNSORTED"))
FILES = ROOT / "files"
TEXT = ROOT / "text"
TMP = ROOT / ".tmp"

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    sha1        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    md5         TEXT NOT NULL,
    size        INTEGER NOT NULL,
    ext         TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- sniffed from magic bytes: pdf, jpeg, png, gif, html, zip, other
    file_path   TEXT NOT NULL,          -- relative to ROOT
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_blobs_sha256 ON blobs(sha256);
CREATE INDEX IF NOT EXISTS idx_blobs_kind ON blobs(kind);

CREATE TABLE IF NOT EXISTS source_files (
    id          INTEGER PRIMARY KEY,
    rel_path    TEXT NOT NULL UNIQUE,   -- relative to SOURCE_ROOT
    collection  TEXT NOT NULL,          -- first two path components, e.g. 'Archive Scrape/Manuals'
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL,
    sha1        TEXT REFERENCES blobs(sha1),
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending, done, failed
    error       TEXT,
    ingested_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_status ON source_files(status);
CREATE INDEX IF NOT EXISTS idx_source_sha1 ON source_files(sha1);

-- Derived versions of a blob (e.g. OCR'd copies), keyed by content.
CREATE TABLE IF NOT EXISTS file_variants (
    id            INTEGER PRIMARY KEY,
    original_sha1 TEXT NOT NULL REFERENCES blobs(sha1),
    variant_type  TEXT NOT NULL,        -- 'ocr'
    status        TEXT NOT NULL,        -- done, not_needed (already has text everywhere), failed
    variant_sha1  TEXT REFERENCES blobs(sha1),
    tool          TEXT,
    params        TEXT,
    pages         INTEGER,
    pages_ocrd    INTEGER,              -- pages actually OCR'd (textless, not blank, not a repeat)
    error         TEXT,
    duration_s    REAL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (original_sha1, variant_type)
);
CREATE INDEX IF NOT EXISTS idx_variants_status ON file_variants(variant_type, status);
CREATE INDEX IF NOT EXISTS idx_variants_vsha ON file_variants(variant_sha1);

-- Best available text for each original PDF blob (input for classification).
CREATE TABLE IF NOT EXISTS texts (
    sha1        TEXT PRIMARY KEY REFERENCES blobs(sha1),   -- the ORIGINAL blob
    source      TEXT NOT NULL,          -- 'embedded', 'ocr' or 'salvaged'
    from_sha1   TEXT NOT NULL,          -- blob the text was extracted from
    pages       INTEGER,
    chars       INTEGER NOT NULL,
    file_path   TEXT NOT NULL
);
"""

# Language / OCR-quality stats on each text (see text_stats). Added as columns so
# old databases migrate in place.
TEXT_STAT_COLUMNS = [
    ("lang_guess", "TEXT"),        # eng, fra, spa, deu, ita, por, nld, cjk, cyrillic, unknown, empty
    ("eng_share", "REAL"),         # English share of stopword hits across 7 Latin languages (0..1)
    ("eng_word_frac", "REAL"),     # share of alphabetic words (3+ letters) found in the English dictionary
    ("cjk_frac", "REAL"),          # share of letters that are CJK / kana / hangul
    ("words", "INTEGER"),          # alphabetic words counted
    ("quality", "TEXT"),           # ok, mixed, non_english, garbage, empty (see text_stats)
]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=900)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(texts)")}
    for col, typ in TEXT_STAT_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE texts ADD COLUMN {col} {typ}")
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(file_variants)")}
    if "pages_no_text" not in vcols:
        conn.execute("ALTER TABLE file_variants ADD COLUMN pages_no_text INTEGER")
    if "salvaged" not in vcols:   # 1 = original unreadable (e.g. truncated); variant rebuilt by salvage.py
        conn.execute("ALTER TABLE file_variants ADD COLUMN salvaged INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_texts_quality ON texts(quality)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_texts_lang ON texts(lang_guess)")
    return conn


# ---------------------------------------------------------------- language / OCR quality

STOPWORDS = {
    "eng": "the and of to in is for with on this that be are not your you it or as by from when use",
    "fra": "le la les des et est pour dans une du sur pas que qui vous avec sont ce au par ne",
    "spa": "el la los las de y en para con que una por del se es no su al como lo",
    "deu": "der die das und ist nicht mit sie für den von zu ein eine auf dem des wird im bei",
    "ita": "il di che la per non una sono con della del le gli si da nel alla questo",
    "por": "de que os as para com uma não do da em no na se por ao dos das",
    "nld": "de het een en van is niet op te met voor dat zijn bij aan worden als wordt",
}
STOPWORDS = {k: set(v.split()) for k, v in STOPWORDS.items()}
_EN_DICT = None


def en_dict():
    global _EN_DICT
    if _EN_DICT is None:
        words = set()
        for f in ("/usr/share/dict/american-english", "/usr/share/dict/british-english"):
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    words.update(w.strip().lower() for w in fh if "'" not in w)
            except OSError:
                pass
        _EN_DICT = words
    return _EN_DICT


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def is_cjk(ch):
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF
            or 0xF900 <= o <= 0xFAFF or 0xFF66 <= o <= 0xFF9F)


def text_stats(text: str) -> dict:
    """Cheap offline signals for "is this mainly English, and did OCR work?".

    quality:
      empty        almost no letters at all
      garbage      Latin-script text but few real words in any language (failed OCR, bad font encoding)
      non_english  mainly another language/script (CJK, Cyrillic, or a Latin language's stopwords dominate)
      mixed        English present but under half the stopwords (typical multilingual Sony manual)
      ok           mainly English
    """
    sample = text[:400_000]
    letters = [c for c in sample if c.isalpha()]
    nl = len(letters)
    cjk = sum(1 for c in letters if is_cjk(c))
    cyr = sum(1 for c in letters if 0x0400 <= ord(c) <= 0x04FF)
    cjk_frac = cjk / nl if nl else 0.0
    words = [w.lower() for w in _WORD_RE.findall(sample) if not any(is_cjk(c) for c in w)]
    long_words = [w for w in words if len(w) >= 3]
    d = en_dict()
    eng_word_frac = (sum(1 for w in long_words if w in d) / len(long_words)) if long_words else 0.0
    hits = {lang: sum(1 for w in words if w in sw) for lang, sw in STOPWORDS.items()}
    total_hits = sum(hits.values())
    eng_share = hits["eng"] / total_hits if total_hits else 0.0
    st = dict(cjk_frac=round(cjk_frac, 4), eng_word_frac=round(eng_word_frac, 4),
              eng_share=round(eng_share, 4), words=len(words))

    if nl < 100:
        st.update(lang_guess="empty", quality="empty")
    elif cjk_frac > 0.3:
        st.update(lang_guess="cjk", quality="non_english")
    elif cyr / nl > 0.3:
        st.update(lang_guess="cyrillic", quality="non_english")
    else:
        top = max(hits, key=hits.get) if total_hits else "unknown"
        st["lang_guess"] = top
        # Stopwords should be a decent share of words in any real prose; real-word OCR
        # output of manuals tends to be > 5% stopwords. Far below that = noise.
        stop_density = total_hits / len(words) if words else 0.0
        if stop_density < 0.03 and eng_word_frac < 0.5:
            st.update(lang_guess="unknown", quality="garbage")
        elif eng_share >= 0.5:
            st["quality"] = "ok" if eng_word_frac >= 0.5 else "garbage"
        elif eng_share >= 0.15:
            st["quality"] = "mixed"
        else:
            st["quality"] = "non_english"
    return st


def trie_path(base: Path, sha1: str, ext: str) -> Path:
    return base / sha1[:2] / sha1[2:4] / f"{sha1}{ext}"


def sniff(head: bytes) -> str:
    h = head.lstrip()[:16]
    if b"%PDF" in head[:1024]:
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if h.lower().startswith((b"<!doctype", b"<html", b"<?xml", b"<head")):
        return "html"
    return "other"


def clean_ext(name: str, kind: str) -> str:
    if kind == "pdf":
        return ".pdf"
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext):
        return {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "html": ".html", "zip": ".zip"}.get(kind, ".bin")
    return ext


def store_file(src: Path, name_hint: str, kind_hint: str | None = None):
    """Copy src into the trie while hashing it in one read. Returns blob metadata dict."""
    TMP.mkdir(exist_ok=True)
    s1, s256, m5 = hashlib.sha1(), hashlib.sha256(), hashlib.md5()
    size = 0
    head = b""
    fd, tmp = tempfile.mkstemp(dir=TMP)
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            while chunk := fin.read(1 << 20):
                if not head:
                    head = chunk[:2048]
                s1.update(chunk); s256.update(chunk); m5.update(chunk)
                fout.write(chunk)
                size += len(chunk)
        kind = kind_hint or sniff(head)
        ext = clean_ext(name_hint, kind)
        sha1 = s1.hexdigest()
        dst = trie_path(FILES, sha1, ext)
        if dst.exists():
            os.unlink(tmp)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dst)
        return dict(sha1=sha1, sha256=s256.hexdigest(), md5=m5.hexdigest(), size=size,
                    ext=ext, kind=kind, file_path=str(dst.relative_to(ROOT)))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def insert_blob(conn, b):
    conn.execute("""INSERT OR IGNORE INTO blobs (sha1, sha256, md5, size, ext, kind, file_path)
                    VALUES (:sha1, :sha256, :md5, :size, :ext, :kind, :file_path)""", b)


# ---------------------------------------------------------------- index

def cmd_index(args):
    conn = connect()
    data = Path(args.listing).read_bytes().split(b"\0")
    rows = []
    for rec in data:
        if not rec:
            continue
        size, mtime, path = rec.split(b"\t", 2)
        p = path.decode("utf-8", "surrogateescape")
        parts = p.split("/")
        coll = "/".join(parts[:2]) if len(parts) > 2 else parts[0] if len(parts) > 1 else "."
        rows.append((p, coll, parts[-1], int(size), float(mtime)))
    before = conn.total_changes
    conn.executemany("""INSERT OR IGNORE INTO source_files (rel_path, collection, filename, size, mtime)
                        VALUES (?, ?, ?, ?, ?)""", rows)
    conn.commit()
    print(f"listing: {len(rows)} files, newly indexed: {conn.total_changes - before}")


# ---------------------------------------------------------------- ingest

def _ingest_one(row):
    rel = row["rel_path"]
    try:
        return row["id"], store_file(SOURCE_ROOT / rel, row["filename"]), None
    except Exception as e:
        return row["id"], None, f"{type(e).__name__}: {e}"


def cmd_ingest(args):
    if not SOURCE_ROOT.is_dir():
        sys.exit(f"source folder not found: {SOURCE_ROOT} (set SONY_PDF_SOURCE or pass --source)")
    conn = connect()
    q = "SELECT id, rel_path, filename FROM source_files WHERE status = 'pending' ORDER BY id"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    todo = conn.execute(q).fetchall()
    print(f"ingesting {len(todo)} files with {args.workers} threads", flush=True)
    t0, done, nbytes, fails = time.time(), 0, 0, 0
    with cf.ThreadPoolExecutor(args.workers) as ex:
        it = ex.map(_ingest_one, todo)  # map keeps memory bounded enough for ~700k small dicts
        for sid, blob, err in it:
            if blob:
                insert_blob(conn, blob)
                conn.execute("""UPDATE source_files SET sha1=?, status='done', error=NULL,
                                ingested_at=CURRENT_TIMESTAMP WHERE id=?""", (blob["sha1"], sid))
                nbytes += blob["size"]
            else:
                fails += 1
                conn.execute("UPDATE source_files SET status='failed', error=? WHERE id=?", (err, sid))
            done += 1
            if done % 1000 == 0:
                conn.commit()
                el = time.time() - t0
                print(f"{done}/{len(todo)}  {nbytes/el/1e6:.1f} MB/s  {done/el:.1f} files/s  "
                      f"failed={fails}  eta={(len(todo)-done)/(done/el)/3600:.1f}h", flush=True)
    conn.commit()
    print(f"done: {done} files, {nbytes/1e9:.1f} GB, failed={fails}, {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- ocr

MIN_PAGE_CHARS = 20  # non-whitespace chars for a page to count as "has text"


def page_texts(pdf: Path, timeout=300):
    """Return list of per-page text via pdftotext (form-feed separated)."""
    out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, timeout=timeout)
    if out.returncode != 0 and not out.stdout:
        raise RuntimeError(f"pdftotext rc={out.returncode}: {out.stderr.decode(errors='replace')[:300]}")
    pages = out.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def write_text(orig_sha1: str, text: str) -> str:
    dst = trie_path(TEXT, orig_sha1, ".txt")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    return str(dst.relative_to(ROOT))


def page_ranges(nums):
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev > start else str(start)); start = prev = n
    out.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(out)


def select_pages(pdf: Path, candidates, td: Path, dpi=20):
    """Of the textless pages, return those worth OCR: not blank, and only the first of
    any set of pixel-identical pages (e.g. spec sheets padded to 1001 copies of a
    header-only template page). Renders thumbnails, which is fast."""
    rd = td / "thumbs"; rd.mkdir()
    first, last = candidates[0], candidates[-1]
    subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-f", str(first), "-l", str(last), str(pdf), str(rd / "p")],
                   capture_output=True, timeout=1800)
    thumbs = {}
    for f in rd.iterdir():
        m = re.search(r"-(\d+)\.pgm$", f.name)
        if m:
            thumbs[int(m.group(1))] = f
    if not thumbs:          # rendering failed: let ocrmypdf try every candidate
        return list(candidates)
    seen, keep = set(), []
    for n in candidates:
        f = thumbs.get(n)
        if f is None:
            keep.append(n); continue
        px = f.read_bytes().split(b"\n", 3)[-1]   # PGM: P5 / w h / maxval / data
        if sum(1 for b in px if b < 200) <= 3:     # blank page
            continue
        h = hashlib.sha1(px).digest()
        if h in seen:
            continue
        seen.add(h); keep.append(n)
    return keep


SALVAGE = ROOT / "salvage.py"   # runs under a python that has pikepdf (default: the system one, via ocrmypdf; set SALVAGE_PYTHON)


def salvage_pdf(src: Path, dst: Path) -> int:
    """Rebuild a truncated/damaged PDF from its surviving page objects. Returns pages recovered."""
    p = subprocess.run([os.environ.get("SALVAGE_PYTHON", "/usr/bin/python3"), "-I", str(SALVAGE), str(src), str(dst)],
                       capture_output=True, text=True, timeout=600)
    try:
        return int(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and dst.exists() else 0
    except (ValueError, IndexError):
        return 0


def _ocr_one(sha1, file_path, lang, jobs, timeout):
    """Runs in a worker process. Returns a result dict; never raises."""
    t0 = time.time()
    src = ROOT / file_path
    res = dict(sha1=sha1, status=None, variant=None, pages=None, pages_ocrd=None, pages_no_text=None,
               error=None, text=None, salvaged=0)
    try:
        TMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP) as td:
            td = Path(td)
            try:
                pages = page_texts(src)
                if not pages:
                    raise RuntimeError("pdftotext found no pages")
            except RuntimeError as e:
                # Unreadable (most often: the scrape stopped downloading at exactly 1 MiB).
                salv = td / "salvaged.pdf"
                if salvage_pdf(src, salv) == 0:
                    raise RuntimeError(f"unreadable and salvage recovered 0 pages: {e}")
                src, res["salvaged"] = salv, 1
                pages = page_texts(src)
            res["pages"] = len(pages)
            missing = [i + 1 for i, p in enumerate(pages) if len(re.sub(r"\s", "", p)) < MIN_PAGE_CHARS]
            res["pages_no_text"] = len(missing)
            todo = select_pages(src, missing, td) if missing else []
            res["pages_ocrd"] = len(todo)
            if not todo:
                # Everything has text, or the textless pages are blank / repeats of a template page.
                if res["salvaged"]:
                    blob = store_file(src, "x.pdf", kind_hint="pdf")   # keep the readable rebuild
                    res.update(status="done", variant=blob,
                               text=("salvaged", blob["sha1"], len(pages), "\f".join(pages)))
                else:
                    res.update(status="not_needed", text=("embedded", sha1, len(pages), "\f".join(pages)))
                return res
            out = td / "out.pdf"
            cmd = ["ocrmypdf", "--skip-text", "--output-type", "pdf", "--optimize", "0",
                   "-l", lang, "-j", str(jobs), "--tesseract-timeout", "300", "-q",
                   "--max-image-mpixels", "1000", "--invalidate-digital-signatures"]
            if len(todo) < len(pages):
                cmd += ["--pages", page_ranges(todo)]
            cmd += [str(src), str(out)]
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if p.returncode == 8:   # encrypted (usually owner-password only): decrypt a temp copy and retry
                dec = td / "decrypted.pdf"
                q = subprocess.run(["qpdf", "--decrypt", str(src), str(dec)], capture_output=True, timeout=600)
                if dec.exists() and q.returncode in (0, 3):   # 3 = warnings
                    cmd[-2] = str(dec)
                    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if (p.returncode != 0 or not out.exists()) and not res["salvaged"]:
                # Structural problems ocrmypdf can't handle: retry on a rebuilt copy.
                salv = td / "salvaged.pdf"
                if salvage_pdf(src, salv) > 0:
                    res["salvaged"] = 1
                    cmd[-2] = str(salv)
                    if "--pages" in cmd:   # page numbering may differ after a rebuild
                        i = cmd.index("--pages"); del cmd[i:i + 2]
                    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if p.returncode != 0 or not out.exists():
                err = p.stderr.decode(errors="replace").strip().splitlines()
                res.update(status="failed", error=f"ocrmypdf rc={p.returncode}: " + " | ".join(err[-3:])[:500])
                # still keep whatever embedded text exists
                if any(x.strip() for x in pages):
                    res["text"] = ("embedded", sha1, len(pages), "\f".join(pages))
                return res
            blob = store_file(out, "x.pdf", kind_hint="pdf")
            res["variant"] = blob
            opages = page_texts(ROOT / blob["file_path"])
            res.update(status="done", text=("ocr", blob["sha1"], len(opages), "\f".join(opages)))
            return res
    except subprocess.TimeoutExpired:
        res.update(status="failed", error=f"timeout after {timeout}s")
        return res
    except Exception as e:
        res.update(status="failed", error=f"{type(e).__name__}: {e}"[:500])
        return res
    finally:
        res["duration"] = time.time() - t0
        if res.get("text"):
            res["stats"] = text_stats(res["text"][3])


def cmd_ocr(args):
    conn = connect()
    retry = "OR v.status = 'failed'" if args.retry_failed else ""
    q = f"""SELECT b.sha1, b.file_path, b.size FROM blobs b
            LEFT JOIN file_variants v ON v.original_sha1 = b.sha1 AND v.variant_type = 'ocr'
            WHERE b.kind = 'pdf'
              AND b.sha1 NOT IN (SELECT variant_sha1 FROM file_variants WHERE variant_sha1 IS NOT NULL)
              AND (v.id IS NULL {retry})
            ORDER BY b.size"""
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    todo = conn.execute(q).fetchall()
    params = f"--skip-text --output-type pdf --optimize 0 -l {args.lang} (+ --pages: skip blank/repeated textless pages)"
    tool = "ocrmypdf " + subprocess.run(["ocrmypdf", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"OCR queue: {len(todo)} PDFs, {args.workers} workers x {args.jobs} jobs", flush=True)
    t0, n, counts = time.time(), 0, {}
    ex = cf.ProcessPoolExecutor(args.workers)
    last_commit = time.time()
    try:
        futs = {ex.submit(_ocr_one, r["sha1"], r["file_path"], args.lang, args.jobs, args.timeout): r
                for r in todo}
        for fut in cf.as_completed(futs):
            r = fut.result()
            vsha = None
            if r["variant"]:
                insert_blob(conn, r["variant"])
                vsha = r["variant"]["sha1"]
            if r["text"]:
                source, from_sha1, npages, text = r["text"]
                path = write_text(r["sha1"], text)
                st = r["stats"]
                conn.execute("""INSERT OR REPLACE INTO texts (sha1, source, from_sha1, pages, chars, file_path,
                                    lang_guess, eng_share, eng_word_frac, cjk_frac, words, quality)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (r["sha1"], source, from_sha1, npages, len(text), path, st["lang_guess"],
                              st["eng_share"], st["eng_word_frac"], st["cjk_frac"], st["words"], st["quality"]))
            conn.execute("""INSERT OR REPLACE INTO file_variants
                            (original_sha1, variant_type, status, variant_sha1, tool, params,
                             pages, pages_no_text, pages_ocrd, error, duration_s, salvaged)
                            VALUES (?, 'ocr', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (r["sha1"], r["status"], vsha, tool, params, r["pages"], r["pages_no_text"],
                          r["pages_ocrd"], r["error"], r["duration"], r.get("salvaged", 0)))
            n += 1
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if n % 200 == 0 or time.time() - last_commit > 60:
                conn.commit()
                last_commit = time.time()
                el = time.time() - t0
                print(f"{n}/{len(todo)}  {n/el:.2f}/s  {counts}  eta={(len(todo)-n)/(n/el)/3600:.1f}h", flush=True)
    except BaseException:
        # Don't sit in shutdown() while workers grind through a queue whose results are discarded.
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        conn.commit()
    ex.shutdown()
    print(f"done: {n} in {time.time()-t0:.0f}s {counts}")


def _stats_one(sha1, file_path):
    return sha1, text_stats((ROOT / file_path).read_text(encoding="utf-8", errors="replace"))


def cmd_textstats(args):
    """(Re)compute language/quality stats for stored texts."""
    conn = connect()
    where = "" if args.all else "WHERE quality IS NULL"
    todo = conn.execute(f"SELECT sha1, file_path FROM texts {where}").fetchall()
    print(f"computing stats for {len(todo)} texts", flush=True)
    with cf.ProcessPoolExecutor(args.workers) as ex:
        for i, (sha1, st) in enumerate(ex.map(_stats_one, *zip(*todo), chunksize=64) if todo else []):
            conn.execute("""UPDATE texts SET lang_guess=?, eng_share=?, eng_word_frac=?, cjk_frac=?,
                            words=?, quality=? WHERE sha1=?""",
                         (st["lang_guess"], st["eng_share"], st["eng_word_frac"], st["cjk_frac"],
                          st["words"], st["quality"], sha1))
            if i % 5000 == 0:
                conn.commit()
    conn.commit()
    print("done")


def approx_tokens(text: str) -> float:
    """Rough token count: ~3.5 chars/token for Latin text (OCR noise tokenizes worse than
    clean prose, so this leans high), ~1 token per CJK character."""
    cjk = sum(1 for c in text if is_cjk(c))
    return (len(text) - cjk) / 3.5 + cjk


def cmd_estimate(args):
    """Estimate Jev input tokens/cost for classifying every original PDF (no API calls)."""
    conn = connect()
    rows = conn.execute("""
        SELECT b.sha1, t.file_path, t.quality,
               (SELECT GROUP_CONCAT(filename, ' | ') FROM (SELECT DISTINCT filename FROM source_files s
                 WHERE s.sha1 = b.sha1 LIMIT 5)) AS names
        FROM blobs b LEFT JOIN texts t ON t.sha1 = b.sha1
        WHERE b.kind = 'pdf'
          AND b.sha1 NOT IN (SELECT variant_sha1 FROM file_variants WHERE variant_sha1 IS NOT NULL)""").fetchall()
    budgets = [int(x) for x in args.budgets.split(",")]
    totals = {b: 0.0 for b in budgets}
    full = 0.0
    by_quality = {}
    for r in rows:
        text = (ROOT / r["file_path"]).read_text(encoding="utf-8", errors="replace") if r["file_path"] else ""
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text))   # what we'd actually send
        base = args.overhead + approx_tokens(r["names"] or "")
        for b in budgets:
            totals[b] += base + approx_tokens(text[:b])
        full += base + approx_tokens(text)
        q = r["quality"] or "no_text"
        by_quality[q] = by_quality.get(q, 0) + 1
    n = len(rows)
    price = args.price_per_mtok
    print(f"{n} unique original PDFs; by text quality: {by_quality}")
    print(f"per-call overhead assumed: {args.overhead} tokens (question + JSON) + filenames")
    for b in budgets:
        print(f"  first {b:>6} chars of text: {totals[b]/1e6:8.1f} Mtok  avg {totals[b]/n:6.0f}/doc  ${totals[b]/1e6*price:8.2f}")
    print(f"  entire text (reference):  {full/1e6:8.1f} Mtok  avg {full/n:6.0f}/doc  ${full/1e6*price:8.2f}")


# ---------------------------------------------------------------- refetch truncated PDFs from the Wayback Machine

REFETCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS refetches (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL UNIQUE REFERENCES source_files(id),  -- the truncated source file
    orig_sha1     TEXT NOT NULL,          -- its (truncated) blob
    url           TEXT,                   -- original URL the capture is of
    status        TEXT NOT NULL,          -- recovered, no_capture, only_truncated, error
    capture_ts    TEXT,                   -- Wayback timestamp of the capture used
    archive_src   TEXT,                   -- x-archive-src of that capture (which crawl it came from)
    new_sha1      TEXT REFERENCES blobs(sha1),   -- the complete file, stored as an ordinary original blob
    candidates    INTEGER,                -- other 200 captures listed by CDX
    note          TEXT,
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_refetch_status ON refetches(status);
"""
WAYBACK_PATH_RE = re.compile(r"(?:Consumer CRT Manuals|Archive Scrape/websites)/([^/]+)/(\d{14})/(.*)$")
UA = "sony-pdfs-archive-refetch/1.0 (personal preservation project; polite single-threaded)"


import threading

THROTTLED = [0]
# archive.org blocks IPs that go much past ~15 requests/minute (connection refused for about an hour).
# Each route (direct, or through a proxy on another host) is one outgoing IP with its own pacer and
# its own block timer. Workers are spread over the routes round-robin.
MIN_INTERVAL = 6.0          # seconds between requests on one route; set from --rate


class Route:
    def __init__(self, proxy=None, cap_mb=None):
        import urllib.request, urllib.parse
        # Never show a proxy's login: name the route by host:port only.
        self.name = urllib.parse.urlsplit(proxy).netloc.rpartition("@")[2] if proxy else "direct"
        self.cap_bytes = cap_mb * 1e6 if cap_mb else None
        self.bytes = 0
        self.dead = None        # reason, once the proxy refuses us for good (quota used up, bad login)
        handlers = [urllib.request.ProxyHandler({"https": proxy} if proxy else {})]
        self.opener = urllib.request.build_opener(*handlers)
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.blocked_until = 0.0
        self.blocks = 0
        self.requests = 0

    def pace(self):
        while True:
            with self.lock:
                now = time.time()
                start = max(self.next_slot, self.blocked_until)
                if now >= start:
                    self.next_slot = now + MIN_INTERVAL
                    self.requests += 1
                    return
                wait = start - now
            time.sleep(min(wait, 5))

    @property
    def exhausted(self):
        return bool(self.dead) or (self.cap_bytes is not None and self.bytes >= self.cap_bytes)

    def block(self, seconds):
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + seconds)


ROUTES = [Route()]
_local = threading.local()
_route_counter = [0]
_route_lock = threading.Lock()


def _route():
    # Each worker sticks to one route; when a capped route runs out, its workers move to the others.
    if not hasattr(_local, "route") or _local.route.exhausted:
        with _route_lock:
            live = [r for r in ROUTES if not r.exhausted] or ROUTES[:1]
            _local.route = live[_route_counter[0] % len(live)]
            _route_counter[0] += 1
    return _local.route


def _proxy_arg(value):
    """--proxy value: a URL, or @file holding "URL [cap_mb]" (keeps logins out of the process list)."""
    if value.startswith("@"):
        parts = Path(value[1:]).expanduser().read_text().split()
        return parts[0], float(parts[1]) if len(parts) > 1 else None
    return value, None


def _http(url, method="GET", timeout=120, retries=6):
    import urllib.request, urllib.error
    route = _route()
    for attempt in range(retries):
        route.pace()
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            return route.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                THROTTLED[0] += e.code == 429
                route.block(60 * (attempt + 1))   # back off every worker on this route
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # The proxy itself said no (402/403/407: quota used up or login rejected): retire the route
            # and carry on through another one.
            if route.name != "direct" and re.search(r"Tunnel connection failed: (402|403|407)", str(e)):
                with route.lock:
                    if not route.dead:
                        route.dead = str(e)[:200]
                        print(f"route {route.name} retired after {route.bytes/1e6:.0f}MB: {route.dead}", flush=True)
                route = _route()
                continue
            refused = "refused" in str(e).lower()
            if attempt < retries - 1:
                if refused:            # this IP is blocked: its workers wait 15 minutes
                    THROTTLED[0] += 1; route.blocks += 1
                    route.block(900)
                else:
                    time.sleep(15 * (attempt + 1))
                continue
            raise


def _refetch_one(host, ts, path):
    """Find a non-truncated Wayback capture of https://host/path. Returns (status, info dict)."""
    import json, urllib.parse
    url = f"https://{host}/{path}"
    cdx = ("https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {"url": f"{host}/{path}", "output": "json", "fl": "timestamp,statuscode,length,mimetype", "limit": "200"}))
    with _http(cdx, timeout=90) as r:
        body = r.read()
    _route().bytes += len(body)
    rows = json.loads(body or b"[]")[1:]
    caps = [row for row in rows if row[1] == "200" and row[0] != ts]
    info = dict(url=url, candidates=len(caps))
    if not caps:
        return "no_capture", info
    # CDX length is the compressed WARC record size; bigger is a decent proxy for "not cut off".
    caps.sort(key=lambda row: int(row[2]) if row[2].isdigit() else 0, reverse=True)
    tried = []
    for cts, _, clen, _ in caps[:4]:
        wb = f"https://web.archive.org/web/{cts}id_/{url}"
        try:
            with _http(wb, timeout=300) as r:
                src = r.headers.get("x-archive-src", "")
                if src.startswith("CC-MAIN"):
                    tried.append(f"{cts}:commoncrawl"); continue
                TMP.mkdir(exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=TMP, suffix=".pdf")
                with os.fdopen(fd, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
        except Exception as e:
            tried.append(f"{cts}:{type(e).__name__}"); continue
        size = os.path.getsize(tmp)
        _route().bytes += size
        ok = size > 1048576 and subprocess.run(["pdfinfo", tmp], capture_output=True, timeout=120).returncode == 0
        if not ok:
            tried.append(f"{cts}:size={size}"); os.unlink(tmp); continue
        blob = store_file(Path(tmp), "x.pdf", kind_hint="pdf")
        os.unlink(tmp) if os.path.exists(tmp) else None
        info.update(capture_ts=cts, archive_src=src, blob=blob, note=" ".join(tried) or None)
        return "recovered", info
    info["note"] = " ".join(tried)
    return "only_truncated", info


def _refetch_row(row, delay):
    m = WAYBACK_PATH_RE.match(row["rel_path"])
    if not m:
        return row, "error", dict(url=None, note="path is not a Wayback capture path")
    try:
        status, info = _refetch_one(*m.groups())
    except Exception as e:
        status, info = "error", dict(url=f"https://{m[1]}/{m[3]}", note=f"{type(e).__name__}: {e}"[:300])
    time.sleep(delay)
    return row, status, info


def cmd_refetch(args):
    global MIN_INTERVAL
    MIN_INTERVAL = 60.0 / args.rate
    ROUTES[:] = [Route()] + [Route(*_proxy_arg(p)) for p in (args.proxy or [])]
    conn = connect()
    conn.executescript(REFETCH_SCHEMA)
    order = "RANDOM()" if args.sample else "s.id"
    has_cls = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'classifications'").fetchone()
    if has_cls and not args.sample:
        # Jev "yes" manuals first, then unclassified ones, then "no".
        order = ("(SELECT CASE MAX(c.decision = 'yes') WHEN 1 THEN 0 WHEN 0 THEN 2 ELSE 1 END "
                 "FROM classifications c WHERE c.sha1 = s.sha1), s.id")
    # sony.com comes first: a www.sony.com file goes to archive.org only after the live fetcher
    # (live/fetch_live.py) has definitely failed on it (404, a smaller copy, or an error).
    # Untried and Akamai-blocked files are left for the live fetcher. Other paths go straight here.
    conn.execute("CREATE TABLE IF NOT EXISTS live_fetches (source_id INTEGER PRIMARY KEY, status TEXT)")
    skip_live = ("AND (s.rel_path NOT LIKE 'Consumer CRT Manuals/www.sony.com/%' OR s.id IN "
                 "(SELECT source_id FROM live_fetches WHERE status IN ('not_found', 'small', 'error')))")
    todo = conn.execute(f"""
        SELECT s.id, s.rel_path, s.sha1 FROM source_files s JOIN blobs b ON b.sha1 = s.sha1
        WHERE s.size = 1048576 AND b.kind = 'pdf'
          AND s.id NOT IN (SELECT source_id FROM refetches WHERE status != 'error')
          {skip_live}
        ORDER BY {order} {f"LIMIT {int(args.limit)}" if args.limit else ""}""").fetchall()
    print(f"refetching {len(todo)} truncated PDFs with {args.workers} workers", flush=True)
    counts, t0 = {}, time.time()
    ex = cf.ThreadPoolExecutor(args.workers)
    try:
        futs = [ex.submit(_refetch_row, row, args.delay) for row in todo]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            row, status, info = fut.result()
            if info.get("blob"):
                insert_blob(conn, info["blob"])
            conn.execute("""INSERT OR REPLACE INTO refetches (source_id, orig_sha1, url, status, capture_ts,
                            archive_src, new_sha1, candidates, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (row["id"], row["sha1"], info.get("url"), status, info.get("capture_ts"),
                          info.get("archive_src"), info.get("blob", {}).get("sha1"), info.get("candidates"),
                          info.get("note")))
            conn.commit()
            counts[status] = counts.get(status, 0) + 1
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"{i}/{len(todo)} {counts} {i/el*3600:.0f}/h throttled={THROTTLED[0]} "
                      f"eta={(len(todo)-i)/(i/el)/3600:.1f}h "
                      + " ".join(f"[{r.name}: req={r.requests} blocks={r.blocks} {r.bytes/1e6:.0f}MB"
                               + (f"/{r.cap_bytes/1e6:.0f}" if r.cap_bytes else "") + "]" for r in ROUTES), flush=True)
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()
    print("done", counts)


def cmd_gc(args):
    """List (or delete with --delete) files under files/ that no blobs row references."""
    conn = connect()
    known = {r[0] for r in conn.execute("SELECT file_path FROM blobs")}
    orphans = [p for p in FILES.rglob("*") if p.is_file() and str(p.relative_to(ROOT)) not in known]
    print(f"{len(orphans)} orphaned files, {sum(p.stat().st_size for p in orphans)/1e9:.2f} GB")
    if args.delete:
        for p in orphans:
            p.unlink()
        print("deleted")


# ---------------------------------------------------------------- status

def cmd_status(args):
    conn = connect()
    for sql in [
        "SELECT status, COUNT(*), ROUND(SUM(size)/1e9,1) AS gb FROM source_files GROUP BY status",
        "SELECT kind, COUNT(*), ROUND(SUM(size)/1e9,1) AS gb FROM blobs GROUP BY kind ORDER BY 2 DESC",
        "SELECT status, COUNT(*), SUM(pages), SUM(pages_ocrd), ROUND(SUM(duration_s)/3600,1) AS cpu_h "
        "FROM file_variants WHERE variant_type='ocr' GROUP BY status",
        "SELECT source, COUNT(*), SUM(chars) FROM texts GROUP BY source",
        "SELECT source, quality, COUNT(*) FROM texts GROUP BY source, quality ORDER BY 1, 3 DESC",
        "SELECT lang_guess, COUNT(*) FROM texts GROUP BY lang_guess ORDER BY 2 DESC",
    ]:
        rows = conn.execute(sql).fetchall()
        print(sql.split("FROM")[1].split()[0], [tuple(r) for r in rows])


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("index"); a.add_argument("listing", nargs="?", default=str(ROOT / "source_listing.nul"))
    a = sp.add_parser("ingest"); a.add_argument("--workers", type=int, default=16); a.add_argument("--limit", type=int)
    a.add_argument("--source", help="the dump's UNSORTED folder (default: $SONY_PDF_SOURCE)")
    a = sp.add_parser("ocr")
    a.add_argument("--workers", type=int, default=12)
    a.add_argument("--jobs", type=int, default=2, help="ocrmypdf -j per file")
    a.add_argument("--lang", default="eng")
    a.add_argument("--timeout", type=int, default=7200)
    a.add_argument("--limit", type=int)
    a.add_argument("--retry-failed", action="store_true")
    a = sp.add_parser("textstats"); a.add_argument("--workers", type=int, default=16)
    a.add_argument("--all", action="store_true", help="recompute even if already set")
    a = sp.add_parser("estimate"); a.add_argument("--budgets", default="1000,2000,4000,8000,16000")
    a.add_argument("--overhead", type=int, default=250); a.add_argument("--price-per-mtok", type=float, default=0.042)
    a = sp.add_parser("refetch"); a.add_argument("--limit", type=int); a.add_argument("--sample", action="store_true")
    a.add_argument("--delay", type=float, default=0, help="seconds each worker pauses between files")
    a.add_argument("--workers", type=int, default=4, help="parallel downloads (requests are still paced globally)")
    a.add_argument("--rate", type=float, default=10, help="max archive.org requests per minute per route (outgoing IP)")
    a.add_argument("--proxy", action="append", help="extra route: HTTPS proxy URL, or @file containing 'URL [cap_mb]' (repeatable)")
    a = sp.add_parser("gc"); a.add_argument("--delete", action="store_true")
    sp.add_parser("status")
    args = ap.parse_args()
    if getattr(args, "source", None):
        global SOURCE_ROOT
        SOURCE_ROOT = Path(args.source)
    {"index": cmd_index, "ingest": cmd_ingest, "ocr": cmd_ocr, "textstats": cmd_textstats, "gc": cmd_gc, "refetch": cmd_refetch, "estimate": cmd_estimate, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
