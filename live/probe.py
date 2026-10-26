"""Probe: can a patchright Chrome on Xvfb fetch sony.com manual PDFs past Akamai? Needs patchright, Chrome and Xvfb."""
import base64, os, subprocess, sys, tempfile, time
from patchright.sync_api import sync_playwright

URLS = sys.argv[1:]
xvfb = subprocess.Popen(["Xvfb", ":97", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}; env["DISPLAY"] = ":97"
profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile")
try:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(profile, channel="chrome", headless=False, env=env,
                                                   no_viewport=True, args=["--window-size=1920,1080"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        r = page.goto("https://www.sony.com/electronics/support", wait_until="domcontentloaded", timeout=60000)
        print("landing:", r.status if r else None, page.title()[:60], flush=True)
        page.wait_for_timeout(4000)
        print("cookies:", sorted(c["name"] for c in ctx.cookies() if c["name"] in ("_abck", "bm_sz", "ak_bmsc", "bm_sv")))
        for u in URLS:
            res = page.evaluate("""async (u) => {
                const r = await fetch(u, {credentials: 'include'});
                const b = new Uint8Array(await r.arrayBuffer());
                let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
                return {status: r.status, type: r.headers.get('content-type'), b64: btoa(s)};
            }""", u)
            body = base64.b64decode(res["b64"])
            print(res["status"], len(body), res["type"], body[:5], u[-45:], flush=True)
            page.wait_for_timeout(2500)
        ctx.close()
finally:
    xvfb.terminate()
