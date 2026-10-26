 # sony-pdf-repository-tools

Scripts for repairing, indexing, OCR'ing and classifying the **Sony PDF Repository**, a dump of about
95,000 Sony manuals and documents scraped from Wayback Machine captures of Sony's support sites.

- Original dump: <https://archive.org/details/sony-pdf-repository/> (posted 2023-12-27)
- As referenced on the ConsoleMods wiki: <https://consolemods.org/wiki/Category:Sony_PDF_Dump>

The dump was made by Derf of the ConsoleMods CRT wiki. Thanks to Derf and to the ConsoleMods volunteers
who label the files by hand. This repo contains **no Sony documents**, only code and metadata (file paths,
hashes and URLs). Additionally, no ConsoleMods labelling metadata was used for EveryCRT - we used our own
labelling method detailed below.

## Why this exists: the EveryCRT wiki

We built this as an additional manual and technical document source for the
[EveryCRT wiki](https://wiki.everycrt.com),which documents CRT TVs, monitors, and other analog video
gear. **The classifications here reflect EveryCRT's scope**, and "relevant" means a manual is about
one of these:

- a CRT (picture tube) TV or monitor, including broadcast and professional monitors (PVM/BVM);
- a video tape recorder or player of any format, including data tape recorders that use video tape
  (DIR-, DTF, Petasite);
- a broadcast, studio or professional camera or camcorder, if it records to tape or is from the
  standard-definition era (tapeless recording lines such as XDCAM, and HD-era camera systems, are out);
- other broadcast or professional video gear: switchers, TBCs, edit controllers, camera control units,
  processors;
- a DVD recorder, because DVD recorders are used to digitize tapes;
- a remote control for any of the above.

Out of scope: flat-panel and rear-projection TVs, projectors, video printers, audio gear, disc players, and
consumer camcorders. Consumer tape camcorders may get their own pass later. The era and remote rulings,
and the hand-review corrections built on them, are in `review_fixes.py`.

Other projects will likely want different categories. The pipeline is generic; only the questions in
`jev_eval/run_eval.py` and the categories in `label.py` are EveryCRT-specific.

## 1 MiB truncation problem

**16,852 of the dump's PDFs are cut off at exactly 1,048,576 bytes (1 MiB)**, and most open as broken or
partly blank files. The cause is likely because Wayback Machine captures of these PDFs came from
[Common Crawl](https://commoncrawl.org), which stores only the first 1 MiB of each response. The scrape that
built the dump picked up those copies. You can tell them apart: archive.org's response for such a capture
carries an `x-archive-src` header starting with `CC-MAIN`.

### Fix

1. **Live from sony.com first.** Most of the files are still online, under the same path as the
   capture. Sony's site sits behind Akamai bot protection, which rejects scripted downloads. So
   `live/fetch_live.py` drives a real Google Chrome (via [patchright](https://pypi.org/project/patchright/))
   on an Xvfb virtual display. It opens the support page to get a session, then `fetch()`es each PDF from
   inside the page.
2. **archive.org only when sony.com fails.** For files that are gone from sony.com, or that only
   exist on other Sony sites, `sonyarc.py refetch` asks the Wayback Machine's CDX index for every capture
   of the URL. It skips Common Crawl captures and downloads the raw (`id_`) copy of a longer one.
3. **Verify.** A download counts only if it is a PDF larger than 1 MiB. We also check whether its first
   1 MiB is byte-for-byte identical to the cut-off file, which shows it's the same document.

| Result | Files |
|---|---|
| Fixed from sony.com | 14,243 |
| Fixed from archive.org only | 2,498 |
| **Fixed in total** | **16,741 (99.3%)** |
| Still cut off (gone from sony.com, with no full capture on archive.org) | 111 |

`data/recovered.csv` lists every cut-off file:

| Column | Contents |
|---|---|
| `rel_path`, `truncated_sha1` | the file's path in the dump and its SHA-1 |
| `result` | `sony.com`, `archive.org` or `not_recovered` |
| `full_sha1`, `full_sha256`, `full_size` | the full copy's hashes and size |
| `source_url` | the URL we downloaded it from (for archive.org, the exact Wayback capture) |
| `sony_com_status`, `archive_org_status` | what each source returned |

With the dump and this file, you can repair your own copy and check the result.
`data/truncated_pdfs.csv` is the earlier list of cut-off files, made before the re-download.

Useful notes from making the fix:

- **MacBinary headers:** some Sony PDFs start with a 128-byte MacBinary header, so look for `%PDF` within
  the first 1 KB, not only at byte 0.
- **Missing files:** Sony's storage returns **403 with an S3 `AccessDenied` XML body for a missing
  file**. That is not a bot block; treat it as "not found".
- **archive.org rate limits:** archive.org starts refusing connections above roughly 10–15 requests per
  minute from one IP, often for about an hour. `refetch` defaults to 10 requests a minute. Please be gentle
  with it.

## Pipeline

All state lives in a SQLite database (`sony.db`), next to a content-addressed file store.

| Step | Command | What it does |
|---|---|---|
| index | `python3 sonyarc.py index listing.nul` | load a `find -printf '%s\t%T@\t%P\0'` listing of the dump |
| ingest | `python3 sonyarc.py ingest --source /path/to/UNSORTED` | hash every file (SHA-1, SHA-256, MD5) and copy it to `files/ab/cd/<sha1>.<ext>` |
| OCR | `python3 sonyarc.py ocr` | `ocrmypdf` (English) on PDFs without a text layer; also rebuilds cut-off PDFs from their surviving pages (`salvage.py`) and stores the best text per PDF, with language and quality guesses |
| re-download | `python live/fetch_live.py`, then `python3 sonyarc.py refetch` | the truncation fix above |
| classify | `python3 classify.py` | ask [Jev](https://typesafe.ai) whether each PDF is EveryCRT-relevant, using the filename plus the first 2,000 characters |
| label | `python3 label.py [--pool ...] [--recovered]` | label the relevant ones with a local LLM: document type, product kind, title, models covered (see below) |
| review | `python3 review_fixes.py` | apply the scope rulings and hand-review corrections to the labels |

Every step can resume: re-running it skips work that's already done. Each script's docstring lists
its options.

### Classification

`jev_eval/` holds the test we used to pick how much text to send. It covers 400 hand-labeled documents
(rules in `jev_eval/LABELING.md`), and `jev_eval/README.md` has the results. The filename plus the first
2,000 characters found all 156 relevant documents at about 92% precision. Classifying the full corpus cost
about $6 as of this writing.

Use Jev's yes/no answer as a first filter only. Its product **kind** is often wrong; for example, it
confuses LCD and CRT monitors. The labeling step below decides the kind again, and its `labels.product_kind`
is what EveryCRT uses.

### Labeling with a local LLM

`label.py` reads each relevant document again and fills the `labels` table:

| Column | Contents |
|---|---|
| `doc_kind` / `doc_type` | user manual, service manual, technical bulletin, spec sheet, brochure, pricing bulletin, parts list, other (`doc_type` is the EveryCRT wiki's name for it) |
| `product_kind` | CRT TV, CRT monitor, video deck, broadcast camera, other broadcast video, data tape, remote control, DVD recorder, plus out-of-scope kinds (flat panel, projector, audio and so on) |
| `printed_title`, `title`, `language` | the title as printed, a proposed title, and the main language |
| `main_models`, `accessories` | the products the document covers, and options it only mentions |
| `dropped` | model codes the LLM gave that don't appear in the document |
| `wiki_pages`, `dup_of` | matching EveryCRT wiki pages, and whether the file is already on the wiki or in another archive (by SHA-1) |
| `pool` | which run labeled it (below) |
| `scope_override`, `review_note` | set by `review_fixes.py`: `out` for documents its rulings exclude, and why |

How it works:

- **Model.** Qwen3.8-27B (Q4_K_M GGUF, with its MTP draft model for speculative decoding) on a
  [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` with 2 parallel slots, on one 24 GB
  GPU. Any OpenAI-compatible server works; set `LLM_URL` and `--model` in `label.py`.
- **Input.** The file path plus the first 6,000 characters of text. Thinking is off, temperature is 0, and
  the answer is forced into a JSON schema.
- **No invented models.** A model code is kept only if it appears in the filename or text, ignoring case
  and punctuation. Codes that don't appear go to `dropped`.
- **Product kinds come from evidence.** The prompt asks for technology words (picture tube, LCD panel,
  tape) rather than series names, because series hints sent CRT monitors to the flat-panel bucket and the
  other way round.
- **Cut-off files** are labeled from their recovered full copy (`--recovered`). `orig_sha1` points back
  to the original.

Runs (`--pool`), each kept apart in `labels.pool`:

| Pool | Documents | What it is |
|---|---|---|
| `scope` | 7,655 | Jev "yes" documents of the kinds we wanted (CRT TV, CRT monitor, video deck) |
| `jev_yes_rest` | 18,326 | every other Jev "yes" document, which turned up broadcast gear, cameras and mis-kinded TVs and monitors |
| `jev_no_sample` | 1,000 | a random sample of Jev "no" documents, to measure what the filter misses |
| `dvd_recorders` | 285 | Jev "disc player" documents that look like DVD recorders by model code or wording |

About 27,000 documents took roughly 13 hours on a 4090 (about 3 seconds per request, two at a time).
The `jev_no_sample` run found almost no misses under the original scope, so the other 64,000 "no"
documents weren't labeled.

### Review fixes

`review_fixes.py` applies corrections from a hand review of label samples. It changes `labels` in place,
records the reason in `review_note`, and keeps a backup in `labels_v3` from its first run. It can be run
again safely. It handles:

- **Scope rulings:** remotes for out-of-scope gear, tapeless recording lines and HD-era camera systems
  are marked `scope_override = 'out'`.
- **Known mis-kinds:** for example, APR- multitrack audio recorders, RVP-/CHP- projection cubes and VPH-
  projectors.
- **Documents with no main model:** the listed accessories are promoted, or dropped codes are kept when
  they match the text after OCR look-alike folding (O/0, I/L/1).

Use rows whose `product_kind` is in scope and whose `scope_override` is not `out`. That is 14,752
documents: about 5,200 other broadcast video, 4,500 video decks, 2,500 cameras, 980 CRT TVs, 950 CRT
monitors, 300 DVD recorders, 220 data tape and 80 remotes.

## Setup

- Python 3.11+. `sonyarc.py` and `classify.py` use only the standard library.
- System tools: `ocrmypdf`, `tesseract` with English data, poppler (`pdftotext`, `pdfinfo`), `qpdf`, and
  `pikepdf` for `SALVAGE_PYTHON`, which defaults to `/usr/bin/python3`.
- Live re-download: `pip install -r requirements.txt` (patchright), Google Chrome, and `Xvfb`.
- Copy `.env.example` to `.env` and fill in what you need:

| Variable | Used by |
|---|---|
| `SONY_PDF_SOURCE` | `ingest`: the dump's `UNSORTED` folder |
| `JEV_API_KEY` | `classify.py` and `jev_eval/`: a TypeSafe AI key |
| `SALLI_DB` | `label.py` (optional): a [salli-cat](https://github.com/ArclightAutomata/salli-cat) `manuals.db`, from our separate CRT-manual archive tool. Documents whose SHA-1 is already in its `manuals` table (`file_sha1` or `original_file_sha1`) get `dup_of = manuals.db:<id>`. Skipped if the file is missing. |

`label.py` and `review_fixes.py` also read the EveryCRT wiki's API to match model codes to wiki pages.

## License

MIT; see `LICENSE`. The license covers the code and metadata only. Sony's manuals belong to Sony.
