"""Label in-scope Sony docs for the wiki import with a local LLM (llama.cpp server): document type, product kind,
the printed title, the main models and accessories covered, matching wiki pages, and duplicates already
on the wiki or in salli-cat.

Scope: Jev "yes" docs of kind crt_tv / crt_monitor / video_deck. By default only COMPLETE files
(not cut off at exactly 1 MiB); `--recovered` labels the full copies the re-download found for
cut-off files instead (run after `sonyarc.py ocr` and `classify.py` have processed them).

Model codes the LLM returns are kept only if they literally appear in the filename or text, so it
can't invent models. Writes the `labels` table (one row per blob). Resumable: labeled blobs are skipped.
`--pool` picks a different set, kept apart from the import scope by labels.pool:
  jev_yes_rest  = complete Jev "yes" docs of every OTHER kind (finds TVs/monitors/decks Jev mis-kinded)
  jev_no_sample = 1,000 random complete Jev "no" docs (seed 1), to measure how much the "no" set hides
usage: python3 label.py [--recovered] [--pool scope|jev_yes_rest|jev_no_sample] [--limit N] [--sample N] [--workers 2]
Start an OpenAI-compatible llama.cpp server (llama-server) on 127.0.0.1:8091 first.
Optional: SALLI_DB = path to a salli-cat manuals.db (https://github.com/ArclightAutomata/salli-cat),
to flag docs already there (skipped if missing)."""
import argparse, json, os, random, re, sqlite3, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "jev_eval"))
from run_eval import norm  # noqa: E402

LLM_URL = "http://127.0.0.1:8091/v1/chat/completions"
DEFAULT_MODEL = "Qwen3.8-27B-Q4_K_M"
TEXT_CHARS = 6000          # filename + roughly the first few pages
TRUNC = 1048576
KINDS = ("crt_tv", "crt_monitor", "video_deck")
SCHEMA = """CREATE TABLE IF NOT EXISTS labels (
    sha1          TEXT PRIMARY KEY REFERENCES blobs(sha1),  -- the file that will be published
    orig_sha1     TEXT,             -- the cut-off original, when sha1 is a recovered full copy
    jev_kind      TEXT,             -- product kind from the Jev classification
    product_kind  TEXT,             -- LLM: crt_tv, crt_monitor, video_deck, broadcast_camera, video_camera, broadcast_video_other, flat_panel_tv,
                                    --      flat_panel_monitor, disc_player_or_recorder, audio,
                                    --      remote_control, data_tape, video_printer, component_or_part,
                                    --      rear_projection_tv, projector, consumer_tapeless_camcorder, other
    doc_kind      TEXT,             -- LLM: user_manual, service_manual, technical_bulletin, spec_sheet, brochure,
                                    --      pricing_bulletin, parts_list, other
    doc_type      TEXT,             -- wiki Document doc_type the doc_kind maps to
    printed_title TEXT,             -- LLM: the title as printed on the document, if any
    title         TEXT,             -- proposed Document title
    language      TEXT,             -- LLM: main language (ISO 639-1)
    main_models   TEXT,             -- JSON list: main products covered (verified to appear in the text)
    accessories   TEXT,             -- JSON list: accessories/options mentioned (verified)
    dropped       TEXT,             -- JSON list: codes the LLM gave that don't appear in the text
    wiki_pages    TEXT,             -- JSON list: existing wiki pages the main models and accessories match
    dup_of        TEXT,             -- already on the wiki (Document:...) or in salli-cat (manuals.db:id)
    llm           TEXT,             -- model tag used
    seconds       REAL,
    labeled_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    pool          TEXT DEFAULT 'scope');  -- scope (the import), jev_yes_rest, jev_no_sample"""

DOC_KINDS = {
    "user_manual": "operating instructions, owner's manual, user's guide, quick start or setup guide for users",
    "service_manual": "service or maintenance manual for technicians: adjustments, schematics, boards, service parts",
    "technical_bulletin": "technical or service bulletin, product change notice, field service information",
    "spec_sheet": "specification sheet or data sheet",
    "brochure": "brochure, catalog, product announcement, other marketing material",
    "pricing_bulletin": "price list, rebate, promotion, or dealer pricing bulletin",
    "parts_list": "parts list, accessory list, or parts catalog on its own",
    "other": "anything else",
}
PRODUCT_KINDS = {
    "crt_tv": "CRT (picture tube) television, including TV/VCR combos with a tube. NOT LCD/plasma/OLED TVs",
    "crt_monitor": "CRT monitor with a picture tube: broadcast/professional video monitor (most BVM- and PVM- "
                   "models before about 2008), computer or data monitor (CPD-, GDM-, multiscan). Clues: picture tube, "
                   "CRT, Trinitron, aperture grille, convergence, degauss, high voltage, G2",
    "video_deck": "video tape recorder/player of any format (including a VHS/DVD combo), or a broadcast tapeless "
                  "deck (e.g. XDCAM disc deck)",
    "broadcast_camera": "broadcast, studio or professional video camera or camcorder, including camera heads, "
                        "ENG/EFP camcorders and industrial cameras (e.g. BVP-, BVW- camcorders, HDC-, DXC-, DSR- and "
                        "HDW- camcorders, DNW-, PDW- XDCAM camcorders)",
    "video_camera": "consumer camcorder or home video camera that records to tape (e.g. Handycam CCD-, DCR-, Video8, Hi8, "
                    "MiniDV)",
    "consumer_tapeless_camcorder": "consumer camcorder recording to DVD, hard drive or memory card",
    "broadcast_video_other": "other professional/broadcast video gear: switchers, TBCs, edit controllers and editing "
                             "systems, routers, signal processors, camera control units and panels (CCU, RCP, CCP, MSU), "
                             "camera adaptors, video effects, character generators. A bulletin covering a mix of broadcast "
                             "products (cameras, decks, editors) goes under its main product",
    "remote_control": "remote control or a remote's code list (e.g. RM- models), when the remote itself is the subject",
    "data_tape": "data recorder, tape drive or tape library storing computer data on tape (e.g. DIR-, DTF/GY-, PC- "
                 "cartridges, DMS- Petasite, LLC- libraries)",
    "video_printer": "video or medical image printer (e.g. UP- models) and its paper or ribbons",
    "component_or_part": "datasheet for an IC, transistor or other electronic part, not a finished product",
    "rear_projection_tv": "rear-projection TV (CRT or otherwise)",
    "projector": "front video/data projector, including CRT projectors (e.g. VPH- models)",
    "flat_panel_tv": "LCD, plasma or OLED television (e.g. KDL-, KLV-, KZ-, XBR LCDs)",
    "flat_panel_monitor": "LCD, plasma or OLED monitor or display (e.g. LMD-, FWD-, SDM- models). Clues: LCD panel, backlight, OLED, "
                          "plasma, flat panel display. Use only when the text names one of these technologies or series",
    "dvd_recorder": "DVD recorder of any kind: DVD recorder, DVD/HDD recorder, VCR/DVD recorder combo, DVDirect "
                    "(e.g. RDR-, VRD- models). Pick this whenever the product can record to DVD",
    "disc_player_or_recorder": "DVD/Blu-ray/LaserDisc PLAYER, or a Blu-ray recorder (e.g. DVP-, BDP-)",
    "audio": "audio equipment: tape/MiniDisc/CD/DAT decks, multitrack recorders, receivers, microphones, Walkman",
    "other": "anything else: computer, still camera, phone, warranty or general paperwork",
}
DOC_TYPE = {"user_manual": "User manual", "service_manual": "Service manual", "technical_bulletin": "Technical bulletin",
            "spec_sheet": "Spec sheet", "brochure": "Brochure"}  # everything else -> Other
TITLE_KIND = {"user_manual": "operating instructions", "service_manual": "service manual",
              "technical_bulletin": "technical bulletin", "spec_sheet": "spec sheet", "brochure": "brochure",
              "pricing_bulletin": "pricing bulletin", "parts_list": "parts list", "other": "document"}

OUT_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_kind": {"type": "string", "enum": list(DOC_KINDS)},
        "product_kind": {"type": "string", "enum": list(PRODUCT_KINDS)},
        "printed_title": {"type": "string"},
        "language": {"type": "string"},
        "main_models": {"type": "array", "items": {"type": "string"}},
        "accessories": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_kind", "product_kind", "printed_title", "language", "main_models", "accessories"],
}
PROMPT = """You catalog Sony product documents. Read the file path and the opening text of one document and answer in JSON.

doc_kind, one of:
{doc_kinds}

product_kind (the main product the document is about), one of:
{product_kinds}

printed_title: the document's own title as printed (e.g. "Operating Instructions", "Service Manual",
"Technical Bulletin: ..."), without the model list; "" if there is none.
language: main language as an ISO 639-1 code.
main_models: Sony model codes of the main products the document covers, exactly as written (e.g. KV-27FV300,
PVM-14M2U, BVW-75). Include every model in a multi-model manual. Not accessories, boards or part numbers.
accessories: model codes of optional accessories, boards, kits or remote controls mentioned (e.g. BKM-10R, RM-Y180).
Use only codes that appear in the path or text. Do not guess.

File path: {path}

Text:
{text}"""

nkey = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())


def wiki_ask(query):
    out, off = [], 0
    while True:
        u = "https://wiki.everycrt.com/api.php?" + urllib.parse.urlencode(
            {"action": "ask", "format": "json", "query": f"{query}|limit=500|offset={off}"})
        d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "EveryCRT sony-pdfs label"}), timeout=120))
        out += list((d.get("query", {}).get("results", {}) or {}).items())
        nxt = d.get("query-continue-offset")
        if not nxt or nxt <= off:
            return out
        off = nxt


def wiki_index():
    """normalized model code -> wiki page for Sony pages. A page's own code wins over another page's variants."""
    own, var = {}, {}
    for t, v in wiki_ask("[[Brand::Sony]]|?Model code|?Variants"):
        po = v["printouts"]
        for c in [t.split(" ", 1)[1] if " " in t else t] + [str(x) for x in po.get("Model code", [])]:
            own.setdefault(nkey(c), t)
        for c in po.get("Variants", []):
            for part in re.split(r"[,/]", str(c)):
                if part.strip():
                    var.setdefault(nkey(part), t)
    return {**var, **own}


def wiki_sha1s():
    s = {}
    for t, v in wiki_ask("[[Document SHA1::+]]|?Document SHA1|?Original file SHA1"):
        for k in ("Document SHA1", "Original file SHA1"):
            for x in v["printouts"].get(k, []):
                s[str(x).lower()] = t
    return s


def salli_sha1s():
    path = Path(os.environ.get("SALLI_DB", Path.home() / "salli-cat" / "manuals.db"))
    if not path.exists():
        print(f"no salli-cat db at {path}; skipping that duplicate check", flush=True)
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    s = {}
    for mid, a, b in db.execute("SELECT id, file_sha1, original_file_sha1 FROM manuals "
                                "WHERE file_sha1 IS NOT NULL OR original_file_sha1 IS NOT NULL"):
        for x in (a, b):
            if x:
                s.setdefault(x.lower(), f"manuals.db:{mid}")
    return s


def ask_llm(model, path, text):
    prompt = PROMPT.format(doc_kinds="\n".join(f"- {k}: {v}" for k, v in DOC_KINDS.items()),
                           product_kinds="\n".join(f"- {k}: {v}" for k, v in PRODUCT_KINDS.items()),
                           path=path, text=text)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                       "max_tokens": 4096, "chat_template_kwargs": {"enable_thinking": False},
                       "response_format": {"type": "json_schema", "json_schema": {"name": "label", "schema": OUT_SCHEMA}},
                       }).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(LLM_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])
        except Exception:
            if attempt == 2:
                raise
            time.sleep(10)


def clean(c):
    """Upper-case and drop trailing slashes/notes: 'HVR-1500//C' -> 'HVR-1500/C', 'sls600' -> 'SLS600'."""
    c = re.sub(r"/+", "/", c.strip().upper()).strip("/ ")
    return re.sub(r"\s+", "", c)


def verify(codes, haystack):
    """Keep codes that appear in the filename/text (ignoring case and punctuation); dedupe.
    A code also counts if its part after the prefix (3+ chars) appears, for model lists written
    like 'HVR-M25AU/M25AN' or 'DVR-2000/2100'."""
    keep, drop, seen = [], [], set()
    for c in map(clean, codes):
        k = nkey(c)
        if not k or k in seen:
            continue
        seen.add(k)
        rest = nkey(c.split("-", 1)[1]) if "-" in c else ""
        ok = k in haystack or (len(rest) >= 3 and re.search(r"\d", rest) and rest in haystack)
        (keep if ok else drop).append(c)
    return keep, drop


def page_for(idx, code):
    return idx.get(nkey(code)) or idx.get(nkey(code.split("/", 1)[0]))


def make_title(doc_kind, models, pages):
    shown = [p.split(" ", 1)[1] for p in pages] or models
    if not shown:
        return None
    lead = ", ".join(shown[:3]) + (" and others" if len(shown) > 3 else "")
    return f"Sony {lead} {TITLE_KIND.get(doc_kind, 'document')}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recovered", action="store_true", help="label recovered full copies of cut-off files")
    ap.add_argument("--pool", default="scope", choices=["scope", "jev_yes_rest", "jev_no_sample", "dvd_recorders"],
                    help="with --recovered: scope or jev_yes_rest")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sample", type=int, help="label N random docs (seed 1) instead of all")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=2, help="parallel requests (match llama-server --parallel)")
    a = ap.parse_args()

    db = sqlite3.connect(ROOT / "sony.db", timeout=900)
    db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA)
    if "pool" not in [r[1] for r in db.execute("PRAGMA table_info(labels)")]:
        db.execute("ALTER TABLE labels ADD COLUMN pool TEXT DEFAULT 'scope'"); db.commit()
    kinds = ",".join(f"'{k}'" for k in KINDS)
    first_path = "(SELECT rel_path FROM source_files s WHERE s.sha1 = {} ORDER BY s.id LIMIT 1)"
    if a.pool == "dvd_recorders":
        # Jev "disc player" docs that look like DVD recorders (logs/dvd_recorder_candidates.txt, by
        # model code or wording; Abe 2026-10-25: DVD recorders are in scope, they digitize tapes).
        # A cut-off file is labeled from its recovered full copy when there is one.
        cand = [x for x in (ROOT / "logs/dvd_recorder_candidates.txt").read_text().split() if x]
        rows = []
        for c in cand:
            r = db.execute("""SELECT new_sha1 FROM refetches WHERE orig_sha1 = ? AND status = 'recovered'
                              UNION SELECT new_sha1 FROM live_fetches WHERE orig_sha1 = ? AND status = 'recovered'""",
                           (c, c)).fetchone()
            sha1, orig = (r[0], c) if r else (c, None)
            # a recovered copy can be a candidate too; label it once, through its original
            is_copy = db.execute("""SELECT 1 FROM refetches WHERE new_sha1 = ? AND status = 'recovered'
                                    UNION SELECT 1 FROM live_fetches WHERE new_sha1 = ? AND status = 'recovered'""",
                                 (c, c)).fetchone()
            if (is_copy and not r) or any(x[0] == sha1 for x in rows) or \
                    db.execute("SELECT 1 FROM labels WHERE sha1 = ?", (sha1,)).fetchone():
                continue
            row = db.execute(f"SELECT b.sha1, ?, cl.kind, t.file_path, {first_path.format('?')} FROM blobs b "
                             "JOIN texts t ON t.sha1 = b.sha1 LEFT JOIN classifications cl ON cl.sha1 = ? "
                             "WHERE b.sha1 = ?", (orig, orig or sha1, orig or sha1, sha1)).fetchone()
            if row:
                rows.append(row)
    elif a.recovered:
        # A recovered full copy is in a pool if Jev put either it or its cut-off original there
        # (the original was classified from its surviving first pages only).
        def inpool(c):
            return {"scope": f"({c}.decision = 'yes' AND {c}.kind IN ({kinds}))",
                    "jev_yes_rest": f"{c}.decision = 'yes'"}[a.pool]
        excl = "" if a.pool == "scope" else f"""AND NOT (COALESCE(co.decision = 'yes' AND co.kind IN ({kinds}), 0)
                                                   OR COALESCE(cn.decision = 'yes' AND cn.kind IN ({kinds}), 0))"""
        rows = db.execute(f"""
            SELECT DISTINCT r.new_sha1, r.orig_sha1, COALESCE(cn.kind, co.kind), t.file_path, {first_path.format('r.orig_sha1')}
            FROM (SELECT orig_sha1, new_sha1 FROM refetches WHERE status = 'recovered'
                  UNION SELECT orig_sha1, new_sha1 FROM live_fetches WHERE status = 'recovered') r
            JOIN texts t ON t.sha1 = r.new_sha1
            LEFT JOIN classifications co ON co.sha1 = r.orig_sha1
            LEFT JOIN classifications cn ON cn.sha1 = r.new_sha1
            WHERE (COALESCE({inpool('co')}, 0) OR COALESCE({inpool('cn')}, 0)) {excl}
              AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.sha1 = r.new_sha1)
            GROUP BY r.new_sha1""").fetchall()
    else:
        cond = {"scope": f"c.decision = 'yes' AND c.kind IN ({kinds})",
                "jev_yes_rest": f"c.decision = 'yes' AND c.kind NOT IN ({kinds})",
                "jev_no_sample": "c.decision = 'no'"}[a.pool]
        rows = db.execute(f"""
            SELECT c.sha1, NULL, c.kind, t.file_path, {first_path.format('c.sha1')}
            FROM classifications c JOIN blobs b ON b.sha1 = c.sha1 JOIN texts t ON t.sha1 = c.sha1
            WHERE {cond} AND b.size <> {TRUNC}
              AND c.sha1 NOT IN (SELECT new_sha1 FROM refetches WHERE new_sha1 IS NOT NULL
                                 UNION SELECT new_sha1 FROM live_fetches WHERE new_sha1 IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.sha1 = c.sha1)
            ORDER BY c.sha1""").fetchall()
    if a.pool == "jev_no_sample":
        a.sample = a.sample or 1000
    if a.sample:
        rows = random.Random(1).sample(rows, min(a.sample, len(rows)))
    total = len(rows)
    rows = rows[: a.limit]
    print(f"{len(rows)} docs to label with {a.model}" + (f" (of {total})" if a.limit is not None else ""), flush=True)
    if not rows:
        return
    idx, wsha, ssha = wiki_index(), wiki_sha1s(), salli_sha1s()
    print(f"wiki: {len(set(idx.values()))} Sony pages, {len(wsha)} Document SHA-1s; salli-cat: {len(ssha)} SHA-1s", flush=True)

    def one(row):
        sha1, orig, jev_kind, tpath, path = row
        text = norm((ROOT / tpath).read_text(errors="replace"))[:TEXT_CHARS]
        t0 = time.time()
        try:
            r = ask_llm(a.model, path or "", text)
        except Exception as e:
            print("llm error", sha1, type(e).__name__, str(e)[:120], flush=True)
            return None
        hay = nkey((path or "") + " " + text)
        main_m, drop1 = verify(r.get("main_models", []), hay)
        acc, drop2 = verify([c for c in r.get("accessories", []) if nkey(c) not in {nkey(m) for m in main_m}], hay)
        pages = []
        for i, c in enumerate(main_m):
            p = page_for(idx, c)
            if p:
                main_m[i] = p.split(" ", 1)[1]  # the wiki's spelling of the code
                if p not in pages:
                    pages.append(p)
        main_pages = list(pages)
        for c in acc:
            p = page_for(idx, c)
            if p and p not in pages:
                pages.append(p)
        dk = r.get("doc_kind")
        dup = wsha.get(sha1) or (orig and wsha.get(orig)) or ssha.get(sha1)
        return (sha1, orig, jev_kind, r.get("product_kind"), dk, DOC_TYPE.get(dk, "Other"),
                (r.get("printed_title") or "").strip() or None, make_title(dk, main_m, []),
                (r.get("language") or "").strip().lower()[:5] or None,
                json.dumps(main_m), json.dumps(acc), json.dumps(drop1 + drop2), json.dumps(pages), dup,
                a.model, round(time.time() - t0, 1))

    n, t0 = 0, time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        for r in ex.map(one, rows):
            if r is None:
                continue  # retried on the next run
            db.execute("INSERT OR REPLACE INTO labels (sha1, orig_sha1, jev_kind, product_kind, doc_kind, doc_type, "
                       "printed_title, title, language, main_models, accessories, dropped, wiki_pages, dup_of, llm, "
                       "seconds, pool) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r + (a.pool,))
            db.commit()
            n += 1
            if n % 50 == 0 or n == len(rows):
                el = time.time() - t0
                print(f"{n}/{len(rows)} labeled, {el / n:.1f}s/doc, ETA {(len(rows) - n) * el / n / 3600:.1f}h", flush=True)
    print(f"done: {n} labeled", flush=True)


if __name__ == "__main__":
    main()
