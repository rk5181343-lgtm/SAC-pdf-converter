# Firm PDF Workspace

A document workspace for an accounting firm: **PDF → Excel** conversion built for
statements, invoices, ledgers and trial balances, plus the everyday PDF toolbox
(merge, split, rotate, compress, OCR, images) — all in one app with a shared
library that keeps every processed file available for re-download.

There are no accounts, passwords or sign-ins: the app opens straight into the
tools and everyone who can reach the URL works in the same library. Read
**Privacy and access** below before you put it on a public address.

## Tools

| Group | Tool | What it does |
| --- | --- | --- |
| Convert | PDF → Excel | Workbooks with Original Layout, Clean Data and Validation sheets (see below) |
| Convert | Images → PDF | JPG/PNG/WebP scans into one PDF, one image per page |
| Convert | PDF → Images | Every page as PNG/JPG (ZIP for multi-page) |
| Convert | Make searchable (OCR) | Adds an invisible OCR text layer to scanned PDFs |
| Convert | PDF → Word | Text-level `.docx` export for editing copy |
| Convert | PDF → PowerPoint | One slide per page, each page as a full-slide picture |
| Convert | Extract images | Every picture embedded in the PDF, as PNG/JPG (ZIP) |
| Convert | Optimize scans | Straighten crooked pages, clean the paper, flatten to black & white |
| Organise | Merge PDFs | Join several PDFs, in the order shown in the file list |
| Organise | Split / Extract pages | Page ranges (`1-3,5,8-`) or one PDF per page |
| Organise | Reorder pages | New order (`3,1,2`), reverse (`5-1`), repeat, drop pages, insert blanks (`0`) |
| Organise | Rotate pages | 90/180/270 on all or selected pages |
| Organise | Compress PDF | Re-render at 96/120/150 dpi with JPEG quality control |
| Layout | Pages per sheet | 2/3/4/6/8/9-up with optional borders, plus booklet imposition |
| Layout | Change page size | Fit onto A4/A3/A5/Letter/Legal, flip orientation, add margins |
| Layout | Split multi-page sheets | Cut 2-up/3-up/4-up sheets back into one PDF page per part |
| Layout | Crop margins | Trim scanner edges / white borders by percentage, on any page range |
| Layout | Header / footer | Header/footer text and “Page X of Y”, with `(x)` `(y)` `(file)` placeholders |
| Protect | Watermark | Translucent text (once or tiled) such as “DRAFT” or the client name |
| Protect | Open a locked file | Uses a password you already have, or removes print/copy restrictions |
| Protect | Add protection | Adds a password and print/copy/edit permissions |

Tools that work on page ranges — Split / Extract, Reorder, Rotate, Crop, Header /
footer and Watermark — show the file as a grid of page thumbnails as soon as you
choose it. Click pages to pick them (Select all / Invert / Clear), click them in
order for Reorder, and the Pages field stays in step both ways, so what the run
does is what the grid shows. The rotation chosen for Rotate pages is drawn on the
thumbnails before you run it.

## Choosing pages per file

A multi-file run gives every PDF its own **Pages** button in the file list. Open
it, click the pages you want (or Select all / Invert / Clear), and that file —
and only that file — is trimmed before the tool runs, so a 200-page statement can
produce a three-page export without a separate split step first. The button then
shows the chosen range (`Pages 3-5`), and the run bar counts how many files have
a selection, so the run stays predictable. The trimming happens in the job
runner (`app/excel_runner.py`) rather than in each tool, which is why every tool
honours it.

## One download for everything

Results are listed file by file. Any run that produced more than one file also
offers **Download all as one ZIP** (`GET /api/batches/<id>/download.zip`), and
the same ZIP button appears on multi-file batches in the Library. Duplicate
output names are kept apart (`report.zip`, `report (1).zip`). Tool options are
remembered per tool in the browser, so a repeat run starts from the settings you
used last time — password fields are never stored.

## PDF → Excel

Every source file produces a workbook with three sheets:

| Sheet | Purpose |
| --- | --- |
| **Original Layout** | The source page recreated in Excel cells: headings, table structure, fills, fonts, row heights and embedded images in page order. Can be switched off for a faster, smaller workbook. |
| **Clean Data** | One record per row, one field per column. Real Excel dates, numeric amounts, frozen header, autofilter — ready for `SUM`, pivots, sort and copy/paste. |
| **Validation** | Counts, opening/closing balance, totals, running-balance chain, duplicates and page coverage — each `Passed` / `Warning` / `Error` / `Not Applicable`. Source values are never altered to make totals agree. |

How it works, in one page: `app/converter/pdf_source.py` opens the PDF once and
exposes positioned words, tables and image boxes (with OCR when a page has no text
layer); `extract.py` detects the document type and turns the densest table (or,
failing that, the text lines) into records; `validation.py` runs the financial
checks; `layout.py` reconstructs each page on a cell grid learned from where the
words actually sit; `workbook.py` writes the `.xlsx`; `pipeline.py` ties them
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
