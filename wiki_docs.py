"""Link the uploaded Sony documents into the EveryCRT wiki as Document pages (Abe approved 2026-10-26).

  python3 wiki_docs.py match                  # re-match every in-scope doc's models to current wiki pages
  python3 wiki_docs.py pages [--limit N] [--dry-run] [--sha1 PREFIX ...]

`match` fills the `wiki_links` table: for each in-scope label row, the existing wiki pages of its main
models, split by the page's category (CRT models / AV devices / Accessories). Only docs with at least one
linked page are uploaded and get a Document page for now; the rest wait for the manuals-to-wiki rounds,
which create model pages first.

`pages` creates `Document:` pages for docs already on archive.org (ia_uploads status uploaded or exists),
with url= pointing at the item, sha1= of the uploaded file, original_sha1= of the dump's copy when we
uploaded a recovered full copy, and the linked model pages. It skips a doc when a wiki Document already
carries one of its SHA-1s. Results go to the `wiki_docs` table. Saves go through salli-cat's
wiki-sweep/wiki_save.py (create-only: it refuses if the page appeared meanwhile).
Needs the mwclient-cli environment (MWCLI_HOST/PATH/USERNAME/PASSWORD)."""
import argparse, json, os, re, sqlite3, subprocess, sys, tempfile
from pathlib import Path

from label import ROOT, nkey, page_for, wiki_ask, wiki_index
from upload_ia import IN_SCOPE, PREFIX

PY = os.path.expanduser("~/.local/share/pipx/venvs/mwclient-cli/bin/python")
WIKI_SAVE = os.path.expanduser("~/salli-cat/wiki-sweep/wiki_save.py")
SUMMARY = "Sony document from the sony-pdf-repository dump (archive.org copy)"
CATS = (("crt_models", "CRT models"), ("av_devices", "AV devices"), ("accessories", "Accessories"))
BAD_TITLE = re.compile(r"[#<>\[\]|{}/]")


def db_open():
    db = sqlite3.connect(ROOT / "sony.db", timeout=900)
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_links (
        sha1 TEXT PRIMARY KEY,      -- labels.sha1
        crt_models TEXT, av_devices TEXT, accessories TEXT,   -- JSON lists of existing wiki pages
        matched_at TEXT DEFAULT (datetime('now')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_docs (
        sha1 TEXT PRIMARY KEY,      -- labels.sha1
        identifier TEXT,            -- archive.org item
        title TEXT,                 -- Document page created, or the existing Document that already has the file
        status TEXT,                -- created | exists | failed
        error TEXT,
        saved_at TEXT DEFAULT (datetime('now')))""")
    return db


def in_scope_rows(db):
    q = (f"SELECT sha1, coalesce(orig_sha1, sha1), main_models FROM labels WHERE scope_override IS NULL "
         f"AND dup_of IS NULL AND product_kind IN ({','.join('?' * len(IN_SCOPE))})")
    return db.execute(q, IN_SCOPE).fetchall()


def cmd_match(db):
    idx = wiki_index()
    cat_of = {}
    for key, cat in CATS:
        for t, _ in wiki_ask(f"[[Category:{cat}]][[Brand::Sony]]"):
            cat_of.setdefault(t, key)
    n = linked = 0
    for sha1, _, models in in_scope_rows(db):
        got = {k: [] for k, _ in CATS}
        for m in json.loads(models or "[]"):
            p = page_for(idx, m)
            if p and p in cat_of and p not in got[cat_of[p]]:
                got[cat_of[p]].append(p)
        db.execute("INSERT OR REPLACE INTO wiki_links (sha1, crt_models, av_devices, accessories, matched_at) "
                   "VALUES (?,?,?,?,datetime('now'))", (sha1, *(json.dumps(got[k]) for k, _ in CATS)))
        n += 1
        linked += any(got.values())
    db.commit()
    print(f"{n} docs matched; {linked} link to at least one existing wiki page")


def existing_doc_sha1s():
    out = {}
    for t, v in wiki_ask("[[Category:Documents]]|?Document SHA1|?Original file SHA1"):
        for p in ("Document SHA1", "Original file SHA1"):
            for s in v["printouts"].get(p, []):
                out.setdefault(str(s).lower(), t)
    return out


def page_exists(title):
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run([PY, WIKI_SAVE, "read", title, f"{d}/p"], capture_output=True, text=True)
        return r.returncode == 0 and Path(f"{d}/p.rev").read_text().strip() not in ("", "0")


def doc_title(models, doc_type, fname, taken):
    codes = [BAD_TITLE.sub(" ", m).strip() for m in models][:3]
    lead = " ".join(codes) + (" and others" if len(models) > 3 else "")
    base = re.sub(r"\s+", " ", f"Sony {lead} {doc_type}".strip())
    for t in (base, f"{base} ({BAD_TITLE.sub(' ', Path(fname).stem).strip()})"):
        if t not in taken and not page_exists("Document:" + t):
            return t
    return None


def cmd_pages(db, a):
    have = existing_doc_sha1s()
    q = ("SELECT l.sha1, u.dump_sha1, u.identifier, u.remote_name, l.doc_type, l.title, l.printed_title, "
         "l.main_models, w.crt_models, w.av_devices, w.accessories FROM ia_uploads u "
         "JOIN labels l ON l.sha1 = u.label_sha1 JOIN wiki_links w ON w.sha1 = l.sha1 "
         "LEFT JOIN wiki_docs d ON d.sha1 = l.sha1 "
         "WHERE u.status IN ('uploaded', 'exists') AND (d.sha1 IS NULL OR d.status = 'failed') "
         "ORDER BY u.identifier")
    rows = db.execute(q).fetchall()
    if a.sha1:
        rows = [r for r in rows if any(r[0].startswith(p) or r[1].startswith(p) for p in a.sha1)]
    taken, n = set(), {"created": 0, "exists": 0, "failed": 0, "unlinked": 0}
    for sha1, dump, ident, fname, dtype, title, printed, models, crt, av, acc in rows[: a.limit or None]:
        links = {"crt_models": json.loads(crt), "av_devices": json.loads(av), "accessories": json.loads(acc)}
        if not any(links.values()):
            n["unlinked"] += 1
            continue
        dup = have.get(sha1) or have.get(dump)
        if dup:
            db.execute("INSERT OR REPLACE INTO wiki_docs (sha1, identifier, title, status) VALUES (?,?,?,?)",
                       (sha1, ident, dup, "exists"))
            n["exists"] += 1
            continue
        models = json.loads(models or "[]")
        t = doc_title(models, dtype or "Other", fname, taken)
        if not t:
            db.execute("INSERT OR REPLACE INTO wiki_docs (sha1, identifier, status, error) VALUES (?,?,?,?)",
                       (sha1, ident, "failed", "no free title"))
            n["failed"] += 1
            continue
        taken.add(t)
        shown = title or (f"Sony {printed}" if printed else None)
        if dtype == "Other" and printed and title and printed.lower() not in title.lower():
            shown = f"{title}: {printed}"
        lines = ["{{Document", f"|doc_type={dtype or 'Other'}"]
        if shown:
            lines.append(f"|title={shown}")
        lines.append(f"|url=https://archive.org/details/{ident}")
        lines.append(f"|sha1={sha1}")
        if dump != sha1:
            lines.append(f"|original_sha1={dump}")
        for k, v in links.items():
            if v:
                lines.append(f"|{k}={','.join(v)}")
        lines.append("}}")
        text = "\n".join(lines) + "\n"
        if a.dry_run:
            print(f"=== Document:{t}\n{text}")
            continue
        with tempfile.TemporaryDirectory() as d:
            f = f"{d}/p"
            subprocess.run([PY, WIKI_SAVE, "read", "Document:" + t, f], check=True, capture_output=True)
            Path(f).write_text(text)
            r = subprocess.run([PY, WIKI_SAVE, "save", "Document:" + t, f, "--base", f + ".rev",
                                "--summary", SUMMARY], capture_output=True, text=True)
        ok = r.returncode == 0
        db.execute("INSERT OR REPLACE INTO wiki_docs (sha1, identifier, title, status, error) VALUES (?,?,?,?,?)",
                   (sha1, ident, "Document:" + t, "created" if ok else "failed",
                    None if ok else (r.stdout + r.stderr)[-300:]))
        db.commit()
        n["created" if ok else "failed"] += 1
        if not ok:
            print("FAIL", t, (r.stdout + r.stderr)[-200:], flush=True)
    db.commit()
    print(json.dumps(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["match", "pages"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sha1", nargs="*")
    a = ap.parse_args()
    db = db_open()
    cmd_match(db) if a.cmd == "match" else cmd_pages(db, a)


if __name__ == "__main__":
    main()
