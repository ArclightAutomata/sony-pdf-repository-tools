#!/usr/bin/env python3
"""Repair a PDF that archive.org rejects, and record the fixed file as a variant.

archive.org refuses some PDFs from the dump ("Syntax error detected in pdf data",
"error checking pdf file"). This rewrites the file with qpdf, which rebuilds the
cross-reference table and drops broken linearization hints without touching page
content, checks the result, and stores it as an ordinary blob with a
file_variants row (variant_type 'repaired') pointing back at the original.
upload_ia.py then uploads the repaired file, with the original's checksum as
urn:original-sha1 and a description line saying what was changed.

When the dump's copy is cut off rather than damaged, pass --from FILE (a complete
copy found elsewhere) and --source-url URL (where it came from). The complete
copy is stored as a blob too, then repaired the same way.

    repair_pdf.py SHA1 [--from FILE --source-url URL --note TEXT]
    repair_pdf.py list
"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path

import sonyarc


def qpdf_version():
    return subprocess.run(["qpdf", "--version"], capture_output=True, text=True).stdout.split("\n")[0].strip()


def pages(path):
    r = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return 0


def check(path):
    """qpdf --check: exit 0 is clean, 3 is warnings only, 2 is errors."""
    r = subprocess.run(["qpdf", "--check", str(path)], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip().splitlines()[-3:]


def cmd_repair(a):
    conn = sonyarc.connect()
    row = conn.execute("SELECT file_path FROM blobs WHERE sha1 = ?", (a.sha1,)).fetchone()
    if not row:
        sys.exit(f"no blob {a.sha1}")
    src = sonyarc.ROOT / row[0]
    params = {"steps": []}
    if a.source:
        if not a.source_url:
            sys.exit("--from needs --source-url")
        full = sonyarc.store_file(Path(a.source), "complete.pdf", "pdf")
        sonyarc.insert_blob(conn, full)
        params.update(source_url=a.source_url, source_sha1=full["sha1"])
        params["steps"].append(f"replaced the dump's copy with the complete file from {a.source_url}")
        src = sonyarc.ROOT / full["file_path"]

    with tempfile.TemporaryDirectory(dir=sonyarc.ROOT) as tmp:
        out = Path(tmp) / "repaired.pdf"
        r = subprocess.run(["qpdf", str(src), str(out)], capture_output=True, text=True)
        if r.returncode not in (0, 3) or not out.exists():
            sys.exit(f"qpdf failed: {(r.stdout + r.stderr).strip()[-500:]}")
        code, tail = check(out)
        n_in, n_out = pages(src), pages(out)
        if code == 2 or n_out == 0 or n_out < n_in:
            sys.exit(f"repair rejected: check={code} pages {n_in}->{n_out}: {tail}")
        params["steps"].append("rewrote the file with qpdf (rebuilt the cross-reference table and "
                               "linearization hints; page content unchanged)")
        if a.note:
            params["note"] = a.note
        fixed = sonyarc.store_file(out, "repaired.pdf", "pdf")
    sonyarc.insert_blob(conn, fixed)
    conn.execute("INSERT OR REPLACE INTO file_variants (original_sha1, variant_type, status, variant_sha1, "
                 "tool, params, pages) VALUES (?, 'repaired', 'done', ?, ?, ?, ?)",
                 (a.sha1, fixed["sha1"], qpdf_version(), json.dumps(params), n_out))
    conn.commit()
    print(f"{a.sha1[:12]} -> {fixed['sha1']} ({n_out} pages, qpdf --check exit {code})")


def cmd_list(a):
    conn = sonyarc.connect()
    for o, v, t, p, n, c in conn.execute("SELECT original_sha1, variant_sha1, tool, params, pages, created_at "
                                         "FROM file_variants WHERE variant_type = 'repaired' ORDER BY id"):
        print(o[:12], v, n, t, c, json.loads(p or "{}").get("source_url", ""), sep="\t")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sha1", help="the blob to repair (the labeled file), or 'list'")
    ap.add_argument("--from", dest="source", help="a complete copy to use instead of the dump's cut-off file")
    ap.add_argument("--source-url", help="where the --from copy came from")
    ap.add_argument("--note", help="one sentence for the archive.org description")
    a = ap.parse_args()
    (cmd_list if a.sha1 == "list" else cmd_repair)(a)


if __name__ == "__main__":
    main()
