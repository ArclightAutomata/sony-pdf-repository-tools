"""Ask Jev about every test doc under each text variant. Resumable: results.jsonl keeps (id, variant) rows already done.
usage: python3 jev_eval/run_eval.py [variant ...]"""
import json, os, re, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "jev_eval"
sys.path.insert(0, str(ROOT))
import sonyarc  # noqa: E402  (loads .env into the environment)
KEY = os.environ.get("JEV_API_KEY") or sys.exit("set JEV_API_KEY in .env or the environment (a TypeSafe AI key)")
MODEL = "jev-1.14.0"

RELEVANT = {"type": "noul", "instructions":
    "Is the main product this `document` is about a CRT (picture tube) TV or monitor, a video tape recorder or player, "
    "a camcorder or video camera, or other professional or broadcast video equipment?"}
KIND = {"type": "choice", "instructions": "What kind of product is this `document` mainly about?", "criteria": {
    "crt_tv": "A CRT (picture tube) television, including TV/VCR combos with a tube.",
    "crt_monitor": "A CRT monitor: broadcast or professional video monitor, computer monitor, or data monitor with a picture tube.",
    "video_deck": "A video tape recorder or player of any format (VHS, Beta, Video8, Hi8, U-matic, Betacam, DV, DVCAM).",
    "video_camera": "A camcorder or video camera, consumer or professional.",
    "broadcast_video_other": "Other professional video equipment: switchers, time base correctors, editors, routers, video printers.",
    "flat_panel_or_projection": "An LCD, LED, OLED or plasma TV or monitor, a projection TV, or a projector.",
    "computer": "A computer, laptop, tablet, phone, or computer peripheral.",
    "audio": "Audio equipment: Walkman, music players, receivers, speakers, headphones, audio recorders, microphones.",
    "still_camera": "A digital still camera, lens, or flash.",
    "disc_or_media_player": "A DVD or Blu-ray player or recorder, streaming box, or game console.",
    "other_product": "Any other product or accessory.",
    "not_product_doc": "Not about a specific product: warranty, policy, marketing program, or form.",
}}

def norm(t):
    t = re.sub(r"[ \t ]+", " ", t.replace("\f", "\n"))
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()

def ends(t, n):
    return t if len(t) <= 2 * n else t[:n] + "\n[...]\n" + t[-n:]

VARIANTS = {
    "fname":   lambda t: None,
    "head1k":  lambda t: t[:1000],
    "head2k":  lambda t: t[:2000],
    "head4k":  lambda t: t[:4000],
    "head8k":  lambda t: t[:8000],
    "head16k": lambda t: t[:16000],
    "ends2k":  lambda t: ends(t, 1000),
    "ends4k":  lambda t: ends(t, 2000),
}

def ask(state):
    body = json.dumps({"model": MODEL, "state": state, "questions": {"relevant": RELEVANT, "kind": KIND}}).encode()
    for attempt in range(6):
        req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(5 * (attempt + 1)); continue
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < 5: time.sleep(5 * (attempt + 1)); continue
            raise

def main():
    variants = sys.argv[1:] or list(VARIANTS)
    docs = [json.loads(l) for l in open(EV / "sample.jsonl")]
    out = EV / "results.jsonl"
    done = set()
    if out.exists():
        for l in open(out):
            r = json.loads(l); done.add((r["id"], r["variant"]))
    jobs = [(d, v) for v in variants for d in docs if (d["id"], v) not in done]
    print(len(jobs), "calls to make", flush=True)
    lock = threading.Lock(); f = open(out, "a"); tokens = [0]; n = [0]

    def run(job):
        d, v = job
        text = VARIANTS[v](norm((ROOT / d["text_path"]).read_text(errors="replace")))
        doc = {"filename": d["filename"]}
        if text: doc["text"] = text
        t0 = time.time()
        try:
            resp = ask({"document": doc})
            a = resp["answers"]
            row = dict(id=d["id"], variant=v, relevant=a["relevant"]["noul"], kind=a["kind"]["choice"],
                       kind_conf=a["kind"].get("confidence"), kind_probs=a["kind"].get("probabilities"),
                       input_tokens=resp["usage"]["input_tokens"], sent_chars=len(text or ""), secs=round(time.time() - t0, 2))
        except Exception as e:
            print(d["id"], v, "ERROR", e, flush=True); return
        with lock:
            f.write(json.dumps(row) + "\n"); f.flush()
            tokens[0] += row["input_tokens"]; n[0] += 1
            if n[0] % 200 == 0:
                print(f"{n[0]}/{len(jobs)} tokens={tokens[0]:,} cost=${tokens[0] * 0.042 / 1e6:.3f}", flush=True)

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(run, jobs))
    print(f"done {n[0]} calls, {tokens[0]:,} input tokens, ${tokens[0] * 0.042 / 1e6:.3f}")

if __name__ == "__main__":
    main()
