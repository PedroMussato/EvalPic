# EvalPic

A small local web app for **manually evaluating and ranking photographs**.

You look at each photo and answer a set of Yes/No questions ("Does it have a clear subject?", "Is there depth?", ...). Every answer is worth a number of points you define. evalpic adds them up and ranks your library by score, so you can quickly find your best shots and drop the rest.

There is **no AI** involved. You make every judgement; evalpic only records your answers and does the arithmetic.

---

## Features

- **Fast evaluation workflow**: one photo at a time, large preview, Yes/No buttons, **Save & Next** jumps to the next unevaluated photo.
- **Configurable rubric**: create, edit, reorder, disable and re-weight criteria. Points can be positive, negative or zero for both Yes and No.
- **Library** with thumbnails, pagination, sorting (date / score), and filtering (all / evaluated / not evaluated).
- **Date range filter** using the capture date read from each photo's EXIF metadata.
- **Bulk actions**: select many photos and **download** them (single ZIP of untouched originals) or **delete** them (originals, previews, thumbnails and evaluations are all removed).
- **JPEG, PNG, WebP and camera RAW** (CR2, CR3, NEF, ARW, RAF, ORF, RW2, PEF, DNG and more, via LibRaw).
- **Originals are never modified.** Browser-friendly previews and thumbnails are generated separately.
- **Single file, no build step.** Flask + SQLite + Jinja templates + a little vanilla JavaScript.

---

## Quick start

Requires Python 3.9+.

```bash
git clone https://github.com/<your-user>/evalpic.git
cd evalpic

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>.

`rawpy` is optional. Without it, JPEG/PNG/WebP work normally and RAW uploads are rejected with a clear message.

### Configuration

Set with environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PHOTO_EVAL_HOST` | `127.0.0.1` | Interface to listen on. Use `0.0.0.0` to reach it from other devices on your network. |
| `PHOTO_EVAL_PORT` | `5000` | Port. |
| `PAGE_SIZE` | `24` | Photos per library page. |
| `SECRET_KEY` | built-in local value | Flask session key (used for status messages). |

Example: `PAGE_SIZE=100 PHOTO_EVAL_PORT=8080 python app.py`

Image sizes and limits are constants at the top of `app.py`: `THUMB_MAX` (400 px), `PREVIEW_MAX` (2000 px), JPEG qualities, `MAX_UPLOAD_BYTES` (4 GB per upload request), and the list of accepted RAW extensions.

> **Security:** there is no authentication and no CSRF protection. evalpic is meant for your own machine or a trusted private network. Do not expose it to the internet.

---

## How to use it

1. **Upload** photos on the *Upload* page. You can select many files at once. evalpic saves the original, generates a preview and thumbnail, and reads the metadata.
2. **Set up your rubric** on the *Criteria* page (or keep the defaults).
3. Open **Evaluate**. Answer every question and click **Save & Next**. The score is calculated automatically and you are taken to the next unevaluated photo. When none are left you'll see "All photographs have been evaluated."
4. Go to the **Library**, sort by **Score**, and pick out your best photos.
5. Filter by **Taken** date if you only want a shooting session or a date range.
6. Tick photos and **Download selected** or **Delete selected**.

To change an earlier evaluation, open the photo from the library and click **Change evaluation**. Previous answers are pre-selected.

---

## Criteria and scoring

A criterion has a name, an optional description (shown under the name while evaluating), a **Yes score**, a **No score**, an active flag and a sort order.

Default criteria:

| Criterion | Yes | No |
|---|---|---|
| Subject | +5 | 0 |
| Centered | -2 | 0 |
| Triangles | +5 | 0 |
| Framing | +5 | 0 |
| Foreground | +2 | 0 |
| Depth | +5 | 0 |
| Layout | +5 | 0 |

Example: Subject Yes (+5), Centered Yes (-2), Triangles Yes (+5), Framing Yes (+5), Foreground No (0), Depth Yes (+5), Layout Yes (+5) gives a total of **23**.

Rules worth knowing:

- Nothing is hardcoded: any criterion can be edited, reordered (Up/Down), disabled or added.
- Scores can be anything: `Yes +5 / No 0`, `Yes -2 / No 0`, `Yes +5 / No -5`, or `0 / 0`.
- Criteria are **disabled, not deleted**, so existing answers are never lost. Disabled criteria are not asked during evaluation and do not count toward scores.
- Editing a criterion's points, or disabling/enabling it, **recalculates the stored scores of all already-evaluated photos**, so your ranking always reflects the current rubric.
- If you add a criterion after evaluating some photos, those photos keep their existing score until you re-open them and answer the new question.
- Each photo has at most one answer per criterion; re-evaluating updates it rather than duplicating.

---

## Library

- **Sort:** Date (newest first by default) or Score (highest first by default). Click the active sort again to flip direction. When sorting by score, unevaluated photos always appear last.
- **Show:** All, Evaluated, Not evaluated.
- **Taken (date range):** filters on the capture date stored in the photo's metadata. Leave *To* empty for "from this date until today", or leave *From* empty for "everything up to this date". Both ends are inclusive. Photos with no capture date in their metadata fall back to their upload date.
- Sorting, status filter, date range and page number combine, and are kept in the URL (`/?sort=score&status=evaluated&date_from=2026-09-01&date_to=2026-09-05&page=2`).
- Pagination is done in SQL (`LIMIT`/`OFFSET`).
- **Select all** selects the photos on the current page. Selection is not remembered across pages.

### Download

*Download selected* returns the **original files**. One photo is served directly; several are packed into a single ZIP (stored without recompression, since photos are already compressed). Duplicate filenames get a suffix such as `IMG_0001 (2).jpg` inside the ZIP.

### Delete

*Delete selected* asks for confirmation, then removes each photo's original file, preview, thumbnail, evaluations and database record.

---

## Supported formats and RAW handling

| Type | Extensions |
|---|---|
| Standard | `jpg`, `jpeg`, `png`, `webp` |
| RAW | `cr2`, `cr3`, `crw`, `nef`, `nrw`, `arw`, `srf`, `sr2`, `raf`, `orf`, `rw2`, `pef`, `dng`, `srw`, `3fr`, `erf`, `kdc`, `mef`, `mos`, `mrw`, `iiq` |

Files are validated by their actual content, not just their extension. Whether a specific RAW file works depends on the LibRaw version bundled with `rawpy`.

RAW pipeline:

1. The original RAW is stored untouched.
2. The **embedded JPEG preview** is extracted (fast).
3. If there is no usable embedded preview, or it is smaller than 1200 px on its long side, the RAW is decoded with `rawpy`/LibRaw (half-size for speed).
4. Orientation is applied, then a preview (max 2000 px) and thumbnail (max 400 px) are written as JPEG. Images are never upscaled.

The generated JPEGs are only for viewing.

### Metadata

Where available: capture date, camera, lens, focal length, aperture, shutter speed, ISO, width and height. Missing or unreadable metadata never blocks an import; the fields are simply left empty.

### Import errors

An invalid file does not abort the rest of the upload. You get a summary such as:

```
5 images uploaded successfully.
1 image failed.
IMG_1234.xyz - Unsupported format.
```

Files that were saved but could not be processed (corrupt image, RAW that cannot be decoded) are **not deleted**. They are moved to `uploads/_failed/`.

---

## Project layout

Everything lives in one file so it is trivial to copy and run.

```text
evalpic/
├── app.py              # the whole application (routes, DB, image/RAW processing, templates, CSS)
├── requirements.txt
├── .gitignore
├── database/app.db     # created on first run: SQLite
├── uploads/            # created on first run: original files (+ _failed/)
├── previews/           # created on first run: JPEG previews (max 2000 px)
└── thumbnails/         # created on first run: JPEG thumbnails (max 400 px)
```

Images are stored on the filesystem, never as BLOBs. Files get a unique internal name (`<uuid>.<ext>`); the original filename is kept in the database and shown in the UI.

### Database

SQLite, three tables:

- `images`: filenames and paths, mime type, size, dimensions, RAW flag, upload/capture time, camera/lens/exposure metadata, cached `score`, `evaluated` flag.
- `criteria`: name, description, `yes_score`, `no_score`, `active`, `sort_order`.
- `evaluations`: `image_id`, `criterion_id`, `answer` (1 = Yes, 0 = No), stored `score`; unique on `(image_id, criterion_id)`.

The image `score` is a cached sum of its evaluation scores for active criteria, used for fast sorting.

### Routes

| Route | Purpose |
|---|---|
| `GET /` | Library (`sort`, `dir`, `status`, `date_from`, `date_to`, `page`) |
| `GET, POST /upload` | Upload photos |
| `GET /evaluate` | Redirect to the first unevaluated photo |
| `GET, POST /evaluate/<id>` | Evaluate one photo (Save & Next) |
| `GET /images/<id>` | Photo details, metadata and evaluation breakdown |
| `POST /images/download` | Download selected originals (ZIP for several) |
| `POST /images/delete` | Delete selected photos and their files |
| `GET /criteria` | List criteria |
| `GET, POST /criteria/new` | Add a criterion |
| `GET, POST /criteria/<id>/edit` | Edit a criterion |
| `POST /criteria/<id>/toggle` | Enable / disable |
| `POST /criteria/<id>/move` | Move up / down |
| `GET /media/<thumb\|preview\|original>/<id>` | Serve image files |

---

## Backups

All your data is in `database/` and `uploads/`. Copy those two folders to back up everything; `previews/` and `thumbnails/` are derived and can be regenerated by re-uploading. `.gitignore` excludes all four so photos never end up in the repository.

## Not included (by design)

AI analysis, keyboard shortcuts, user accounts, REST API, progress bars, photo editing, tags/collections, duplicate detection, multiple rubrics and score history. The design leaves room for them, but the goal is a fast, simple evaluate-and-rank loop.

## License

No license specified yet. Add one (for example MIT) before publishing if you want others to reuse the code.
