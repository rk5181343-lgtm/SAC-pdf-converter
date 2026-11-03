# Security and roadmap

## What this app is

A single-tenant workspace for one firm. It has no accounts because the intended
deployment is either on the firm's own network or behind a host-level access gate.
Everything below follows from that choice.

## Access model

| Concern | Current behaviour |
| --- | --- |
| Sign-in | None. Opening the URL lands in the tools. |
| Library | Shared: every batch and every file is visible to everyone who can reach the app. |
| Per-user data | None stored. There is nobody to attribute a file to. |
| File URLs | Served only by `/api/documents/<id>/download`; there is no static file directory and no public object storage URL. |
| Admin actions | `POST /api/library/clear` is open unless `ADMIN_TOKEN` is set, in which case it requires the `X-Admin-Token` header. |

**The URL is the access control.** If the app is on the public internet, put a
gate in front of it (Vercel Deployment Protection, an SSO proxy, a VPN, or basic
auth at the edge). Do not rely on the library being private.

## Input handling

- Uploads are limited by `MAX_UPLOAD_MB` (Flask `MAX_CONTENT_LENGTH`), and by
  extension/mimetype for each tool. PDFs are additionally sniffed by their `%PDF`
  header when the extension is unclear.
- Page counts are limited by `MAX_PAGES`; files per run by `MAX_FILES_PER_RUN`.
- Every file is parsed as bytes: nothing is written to disk under a user-supplied
  name. `library.safe_filename` strips directories and control characters, and
  stored files use generated ids.
- Text beginning with `=` is written with a quote prefix in the workbook so a
  document cannot smuggle a formula into a spreadsheet (CSV/Excel injection).
- HTML is never rendered from user input: the UI sets `textContent`, not
  `innerHTML`, for filenames and job messages.
- Passwords typed into Protect PDF are used for that run only; they are not
  logged and not stored with the batch.

## Data at rest

- `STORAGE_MODE=fs` keeps files under `STORAGE_DIR` on the host; back that
  directory up and restrict its permissions.
- `STORAGE_MODE=db` keeps bytes in the database; encrypt the database volume and
  restrict access to it.
- Deleting a batch removes its documents and its job records. There is no
  soft-delete and no retention schedule yet.

## Optional network calls

`EXA_API_KEY` enables the “look something up” panel, which sends **only the text
you type into that box** to `api.exa.ai`. Conversion never sends document content
anywhere. Leave the key unset to disable the panel entirely.

## Known limits

- OCR requires `rapidocr-onnxruntime`. Without it, scanned pages are reproduced
  visually but produce no records.
- Very large or heavily graphical PDFs can hit the serverless bundle and memory
  limits. Split them first.
- The poll-driven job model advances one step per request; a single huge file is
  one step, so very large conversions are still bounded by the function timeout.
- Concurrency is single-worker per job (an in-process lock); two requests cannot
  advance the same job at once, but two users can run different jobs in parallel.
- No virus scanning. If untrusted parties can reach the URL, add ClamAV or an
  upload scanner in front of `POST /api/uploads`.

## Roadmap

1. **Retention and quota controls** — per-workspace size cap, age-based cleanup,
   and a visible “storage used” figure with an admin action to prune it.
2. **Audit trail** — who uploaded and downloaded what, once an identity exists.
3. **Auth option** — optional sign-in (OIDC/SAML) with the library becoming
   per-user while keeping the no-sign-in mode for private networks.
4. **Conversion depth** — multi-page statements with continuation tables,
   credit/debit sign normalisation that is reported rather than applied, and
   per-document header profiles.
5. **Background workers** — move poll-driven jobs to a real queue (Celery/RQ or a
   hosted queue) for large batches on serverless.
6. **Office → PDF** — LibreOffice in a container image, deliberately out of scope
   for the Python-only runtime.
7. **Test coverage** — golden-file fixtures for each document type plus a
   regression suite that compares extracted records, not just that a workbook
   was produced.
