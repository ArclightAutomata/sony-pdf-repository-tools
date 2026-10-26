"""Hand-review fixes to `labels` after the Qwen runs (Abe approved 2026-10-25). Safe to re-run.

1. Remote controls: a remote is in scope only if it serves in-scope gear (TVs, monitors, decks,
   broadcast/pro cameras and video gear). Remotes for action cams, projectors, car audio, receivers,
   DSLRs, consumer camcorders, CLIÉ, videoconferencing get scope_override='out'. Unclear ones stay in.
2. Known mis-kinds, when EVERY main model is one of these lines:
   APR-* (multitrack audio recorders) -> audio
   RVP-/CHP- (rear video projection cubes), KP- (rear-projection TVs) -> rear_projection_tv
   VPH-/VPL-/HDIH-/HDIR- (front projectors) -> projector
3. In-scope docs with no main model: promote the accessories the LLM listed (the doc is about them),
   else recover dropped codes that match the text after OCR look-alike folding (O/0, I/L/1).
   Docs still without a model are generic (series bulletins, catalogs, promotions) and stay unlinked.

Every change sets review_note. The first run copies labels to labels_v3 as a backup.
The import must skip rows with scope_override = 'out'.
Later rulings (Abe 2026-10-25): tape-based or older recording gear only (tapeless lines out); DVD recorders IN
(product_kind dvd_recorder; they digitize tapes), labeled via `label.py --pool dvd_recorders`."""
import json, re, sqlite3
from pathlib import Path

from label import ROOT, make_title, nkey, norm, page_for, wiki_index

IN_SCOPE = ("crt_tv", "crt_monitor", "video_deck", "broadcast_video_other", "broadcast_camera",
            "data_tape", "remote_control", "dvd_recorder")

REMOTES_OUT = {  # code prefix -> why
    "RM-LVR": "Action Cam live-view remote",
    "RMT-VP1K": "consumer camcorder grip remote",
    "RMT-DSLR": "DSLR remote",
    "RM-PJD50": "projector remote",
    "RM-X60M": "car audio remote",
    "RM-X2S": "car audio remote",
    "RMX114": "car audio card remote",
    "RM-D7100": "professional audio remote",
    "RM-PP401": "audio receiver remote",
    "PEGA-MD700": "CLIE accessory",
    "PCS-R510": "videoconferencing remote",
}
REMOTES_OUT_SHA = {"98e6d9a39e": "audio receiver remote (AV1/AV2 command modes)"}

# Abe 2026-10-25: HXR-MC1 is out because it isn't analog-era gear (flash-memory camcorder line).
CAMERAS_OUT = {"HXR-MC": "HXR-MC flash-memory camcorder, not analog-era (Abe 2026-10-25)"}

# Abe 2026-10-25: recording gear is in scope only if it is tape-based or older. A doc goes out when every
# main model is from a tapeless (disc/memory/HDD) recording line, ignoring option and accessory codes.
TAPELESS = re.compile(r"^(PDW-|PMW-|PXW-|XDS-|PZW-|XDCA-|PDBK-|PDJ-|SBAC-|AXS-|SR-R|SRMASTER|NEX-?FS|NEX-?EA|ILME-|PWA-|MPC-|"
                      r"FDR-|HXR-NX|HXR-MC|HVR-MRC|HVR-DR|DSR-DR|PWS-|F65|F55|F5\b)")
# Abe 2026-10-25: modern (HD and later) non-recording camera lines are out too. SD lines stay
# (BVP, DXC, DXF, SSC, XC, EVI-D, FCB-EX, BRC-300); analog HDVS HDC-100/300/500 count as older.
MODERN = re.compile(r"^(HDC-?(F|X|P|[6-9]\d\d(?!\d)|[1-9]\d{3})|HSC-?\d|HSCU|HXC-|HXCU-|HDCU-?(F|X|[6-9]\d\d(?!\d)|[1-9]\d{3})|"
                    r"HDLA-|HDCA-|HDCE-|HDTX-|BRC-(H|Z|X|AW)|EVI-H|SRG-|FCB-(EV|EH|ES|ER|H|SE|CS)|XC[DGL]-)")
NEUTRAL = re.compile(r"^(HDVF-|DVF-|RCP-|MSU-|HKCU|CBK-|BKP|SBP-|SBS-|BP-|ECM-|LC-|VCT-|AC-|BC-|NP-|MPA-|URX-|UWP-|HVL-)")

KIND_RULES = [
    (re.compile(r"^(RDR|VRD)-"), "dvd_recorder", "DVD recorder (in scope, Abe 2026-10-25)"),
    (re.compile(r"^APR-"), "audio", "APR multitrack audio recorder"),
    (re.compile(r"^(RVP|CHP|KP)-"), "rear_projection_tv", "rear projection (RVP/CHP cubes, KP TVs)"),
    (re.compile(r"^(VPH|VPL|HDIH|HDIR)-"), "projector", "front projector"),
]

OCR = str.maketrans("OILS", "0115")


def text_of(db, sha1, orig):
    for s in (sha1, orig):
        r = s and db.execute("SELECT file_path FROM texts WHERE sha1 = ?", (s,)).fetchone()
        if r:
            return norm((ROOT / r[0]).read_text(errors="replace"))
    return ""


def main():
    db = sqlite3.connect(ROOT / "sony.db", timeout=900)
    cols = [r[1] for r in db.execute("PRAGMA table_info(labels)")]
    for c in ("scope_override", "review_note"):
        if c not in cols:
            db.execute(f"ALTER TABLE labels ADD COLUMN {c} TEXT")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'labels_v3'").fetchone():
        db.execute("CREATE TABLE labels_v3 AS SELECT * FROM labels")
    db.commit()
    n = {"remote_out": 0, "rekind": 0, "promoted_acc": 0, "recovered_dropped": 0}

    # 1. remotes
    for sha1, models in db.execute("SELECT sha1, main_models FROM labels WHERE product_kind = 'remote_control' "
                                   "AND scope_override IS NULL").fetchall():
        why = next((w for s, w in REMOTES_OUT_SHA.items() if sha1.startswith(s)), None)
        for m in json.loads(models or "[]"):
            why = why or next((w for p, w in REMOTES_OUT.items() if m.startswith(p)), None)
        if why:
            db.execute("UPDATE labels SET scope_override = 'out', review_note = ? WHERE sha1 = ?",
                       ("remote for out-of-scope gear: " + why, sha1))
            n["remote_out"] += 1

    # 1b. cameras Abe ruled out
    for sha1, models in db.execute("SELECT sha1, main_models FROM labels WHERE product_kind = 'broadcast_camera' "
                                   "AND scope_override IS NULL").fetchall():
        ms = json.loads(models or "[]")
        hits = [w for m in ms for p, w in CAMERAS_OUT.items() if m.startswith(p)]
        if ms and len(hits) == len(ms):
            db.execute("UPDATE labels SET scope_override = 'out', review_note = ? WHERE sha1 = ?", (hits[0], sha1))
            n["camera_out"] = n.get("camera_out", 0) + 1

    # 1c. tapeless recording gear (Abe: tape-based or older)
    for sha1, models in db.execute("SELECT sha1, main_models FROM labels WHERE product_kind IN "
                                   "('broadcast_camera', 'video_deck', 'broadcast_video_other') "
                                   "AND scope_override IS NULL").fetchall():
        ms = [m for m in json.loads(models or "[]") if not NEUTRAL.match(m)]
        if ms and all(TAPELESS.match(m) or MODERN.match(m) for m in ms):
            why = ("tapeless recording gear (Abe 2026-10-25: tape-based or older only)"
                   if all(TAPELESS.match(m) for m in ms) else
                   "modern HD-era camera system (Abe 2026-10-25: modern lines out)")
            db.execute("UPDATE labels SET scope_override = 'out', review_note = ? WHERE sha1 = ?", (why, sha1))
            n["tapeless_out"] = n.get("tapeless_out", 0) + 1

    # 2. mis-kinds
    for sha1, kind, models in db.execute("SELECT sha1, product_kind, main_models FROM labels").fetchall():
        ms = json.loads(models or "[]")
        for rx, new, why in KIND_RULES:
            if ms and all(rx.match(m) for m in ms) and kind != new and not (new == "dvd_recorder" and kind == "video_deck"):
                db.execute("UPDATE labels SET product_kind = ?, review_note = ? WHERE sha1 = ?",
                           (new, f"was {kind}: {why}", sha1))
                n["rekind"] += 1

    # 3. no-model docs
    idx = wiki_index()
    q = ("SELECT sha1, orig_sha1, doc_kind, accessories, dropped FROM labels WHERE product_kind IN (%s) "
         "AND (main_models IS NULL OR main_models = '[]')" % ",".join("?" * len(IN_SCOPE)))
    for sha1, orig, dk, acc, dropped in db.execute(q, IN_SCOPE).fetchall():
        acc, dropped = json.loads(acc or "[]"), json.loads(dropped or "[]")
        if acc:
            models, rest, note = acc, [], "no main model: accessories promoted"
            key = "promoted_acc"
        else:
            hay = nkey(text_of(db, sha1, orig)).translate(OCR)
            models = [c for c in dropped if len(nkey(c)) >= 4 and nkey(c).translate(OCR) in hay]
            if not models:
                continue
            rest, note, key = [], "no main model: dropped codes found after OCR folding", "recovered_dropped"
        pages = []
        for i, c in enumerate(models):
            p = page_for(idx, c)
            if p:
                models[i] = p.split(" ", 1)[1]
                if p not in pages:
                    pages.append(p)
        db.execute("UPDATE labels SET main_models = ?, accessories = ?, wiki_pages = ?, title = ?, review_note = ? "
                   "WHERE sha1 = ?", (json.dumps(models), json.dumps(rest), json.dumps(pages),
                                      make_title(dk, models, []), note, sha1))
        n[key] += 1
    db.commit()
    print(json.dumps(n))


if __name__ == "__main__":
    main()
