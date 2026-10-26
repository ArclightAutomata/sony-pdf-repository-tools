# Labeling guide for the Jev test set

Each excerpt file in `jev_eval/excerpts/dNNN.txt` is the extracted text (embedded or OCR) of one Sony PDF:
manuals, service manuals, spec sheets, brochures, technical bulletins, and so on. The PATH line often hints at the source.
Long texts show the first 4,000 and last 2,000 characters. Text may be OCR noise or in another language.

Decide which product(s) the document is about, then write one JSON object per line:

{"id": "d007", "product": "KV-27FS100 27in Trinitron color TV", "category": "crt_tv", "relevant": "yes", "edge": false, "note": ""}

category: exactly one of
- crt_tv: a CRT television, including TV/VCR or TV/DVD combos with a tube
- crt_monitor: a CRT monitor of any kind (PVM/BVM broadcast or pro video monitors, CPD/GDM computer monitors, data/presentation monitors)
- video_deck: a video tape recorder or player, any format (VHS, Beta, Video8/Hi8, U-matic, Betacam, DV/DVCAM/HDV decks, VTRs)
- video_camera: a camcorder or video camera (consumer camcorders, broadcast/pro cameras, CCTV video cameras)
- broadcast_video_other: other professional video equipment (switchers, TBCs, frame syncs, video editors and edit controllers, routers, distribution amps, character generators, video printers, telecine)
- flat_panel_or_projection: LCD/LED/OLED/plasma TVs or monitors, rear/front projection TVs, projectors
- computer: PCs, laptops (VAIO), tablets, phones, PC peripherals
- audio: audio gear (Walkman, MP3, receivers, speakers, headphones, CD/MD/tape audio decks, pro audio consoles/recorders, microphones)
- still_camera: digital still cameras, lenses, flashes
- disc_or_media_player: DVD/Blu-ray players and recorders with no tube, streaming boxes, game consoles
- other_product: any other product (robots, e-readers, batteries, accessories, car audio, etc.)
- not_product_doc: not about a specific product (warranty only, marketing program, dealer policy, form)
- unclear: cannot tell from the text (garbage OCR, too little text, unknown language with no model clues)

relevant: "yes" if category is crt_tv, crt_monitor, video_deck, video_camera, or broadcast_video_other, or if a
multi-product document's main products include one of those. "no" for the other product categories. "unclear" only when
category is unclear.

edge: true when a reasonable person could argue the other way. Examples: a disc- or HDD-based camcorder with no tape; a
digital HD broadcast switcher; an accessory such as a battery or remote that is only for a camcorder or TV; a DVD recorder
with a VCR built in. Explain briefly in note.

Use your knowledge of Sony model prefixes when the text gives them. Examples: KV-/KP-/KF-/KDL-/XBR- (KV is a CRT TV,
KP is rear projection, KDL is LCD), SLV-/EV-/SL- (VCRs), CCD-/DCR-/HDR- (camcorders), PVM-/BVM- (monitors, which can be
CRT or LCD, so read the text), BVW-/UVW-/DSR-/HDW- (VTRs), DXC-/BVP-/HDC- (cameras), DFS-/DVS-/BVS-/MVS- (switchers),
CPD-/GDM- (CRT computer monitors), SDM- (LCD monitors).
