# Firm PDF Workspace

A small, self-hosted workspace for everyday firm paperwork: drop a PDF in, pick a
tool, download the result. One Python service (Flask), one page of vanilla
JavaScript, no build step and no frameworks to keep up with.

Live: **https://sac-pdf-converter.vercel.app**

## What it does

20 tools in four groups, from the same drop area:

| Group | Tools |
| --- | --- |
| Organise | Merge, split, extract pages, rotate, reorder, compress, crop, N-up |
| Convert | PDF → Excel, PDF → images, images → PDF, PDF → text, PDF → Word |
| Secure | Password protect, unlock, redact, watermark, stamp page numbers |
| Inspect | Page count, metadata, document info, compare two PDFs |

Every tool is written against `pypdf`, `pdfminer.six` and Pillow; nothing shells out
to a binary, so the same code runs on a laptop and on a serverless host.

## The pages grid

Upload a file and its pages appear as thumbnails in the Pages panel. Each page has
its own `↺` / `↻` buttons, so a sideways scan is turned where it sits; the counts
(`Select all`, `Invert`, `Clear`, `Rotate all ↺`, `Rotate all ↻`) sit above the
grid. Page ranges can be typed per file (`1-3,7`), and both the turns and the
ranges are applied to the source before the tool runs, so every one of the 20
tools honours them.

## PDF → Excel

Every source file produces a workbook with three sheets:

| Sheet | Purpose |
| --- | --- |
| **Original Layout** | Every page of the document recreated in Excel cells on one sheet: headings, table structure, fills, fonts, row heights and embedded images, one page after another with a `Page 2`, `Page 3` … marker between them. Can be switched off for a faster, smaller workbook. |
| **Clean Data** | One record per row, one field per column, for every page of every file. Real Excel dates, numeric amounts, frozen header, autofilter — ready for `SUM`, pivots, sort and copy/paste. The opening and closing balance rows are highlighted in place (amber and teal, with a note on the balance cell), and the last column, `Source`, names the page the row was read from (`Page 3`, or `jan · Page 3` in a combined workbook). |
| **Validation** | Counts, totals, running-balance chain, duplicates and page coverage — each `Passed` / `Warning` / `Error` / `Not Applicable`. Source values are never altered to make totals agree. |

How it works, in one page: `app/converter/pdf_source.py` opens the PDF once and
exposes positioned words, tables and image boxes (with OCR when a page has no text
layer); `extract.py` detects the document type and turns each page's densest table
(or, failing that, the text lines) into records, remembering the page each row came
from; `validation.py` runs the financial checks; `layout.py` reconstructs each page
on a cell grid learned from where the words actually sit; `workbook.py` stacks the
pages into one layout sheet and writes the `.xlsx`; `pipeline.py` ties them
together. Every step is byte-in / byte-out, so the same code runs for an upload, a
library download and a test.

Not included: converting Word/Excel/PowerPoint **to** PDF. That needs LibreOffice,
which cannot run inside the serverless Python runtime this app is deployed on.

## Privacy and access

- **No sign-in.** The workspace opens straight into the tools; there is no
  administrator and no per-person account.
- **One shared library.** Every batch is filed under the workspace itself, so
  anyone who reaches the app sees the same history and can download any file in
  it. Protect the URL (Vercel Deployment Protection, or your own network).
- **Files stay server-side.** Uploads and outputs live in the workspace and are
  served only through the app's own routes; there are no public file URLs.
- **Set `DATABASE_URL` for a durable library.** Without it the app uses SQLite;
  on Vercel that file lives in `/tmp`, so the history is lost whenever the
  instance is recycled.
