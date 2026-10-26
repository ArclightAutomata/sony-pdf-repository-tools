"""Rebuild a readable PDF from a truncated one: collect surviving /Page objects into a new
page tree, and give pages that inherited their resources from the (lost) page tree a merged
resource dict built from every surviving resource dict. Prints the recovered page count."""
import sys, pikepdf
from pikepdf import Name, Dictionary
src, dst = sys.argv[1], sys.argv[2]
pdf = pikepdf.open(src, attempt_recovery=True, suppress_warnings=True)
pages, merged = [], {}
for obj in pdf.objects:
    try:
        if not isinstance(obj, Dictionary):
            continue
        t = obj.get("/Type")
        if t == "/Page":
            pages.append(obj)
        elif t is None and ("/Font" in obj or "/XObject" in obj):
            for cat in ("/Font", "/XObject", "/ExtGState", "/ColorSpace", "/Pattern", "/Shading"):
                if cat in obj and isinstance(obj[cat], Dictionary):
                    d = merged.setdefault(cat, {})
                    for k, v in obj[cat].items():
                        d.setdefault(k, v)
    except Exception:
        pass
res = Dictionary({k: Dictionary(v) for k, v in merged.items()})
res[Name.ProcSet] = pikepdf.Array([Name.PDF, Name.Text, Name.ImageB, Name.ImageC, Name.ImageI])
for p in pages:
    if "/Resources" not in p:
        p[Name.Resources] = res
    if "/MediaBox" not in p:
        p[Name.MediaBox] = pikepdf.Array([0, 0, 612, 792])
out = pikepdf.Pdf.new()
for p in pages:
    try:
        out.pages.append(pikepdf.Page(p))
    except Exception:
        pass
out.save(dst)
print(len(out.pages))
