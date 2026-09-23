#!/usr/bin/env python3
"""
Photo Evaluator - a small local Flask app for manually scoring photographs.

Setup:
    pip install flask pillow rawpy
    python app.py
    -> open http://127.0.0.1:5000

(rawpy is optional: without it JPEG/PNG/WebP still work, RAW uploads are rejected
with a clear message.)

Everything lives in this one file: routes, database code, image/RAW processing,
Jinja templates and CSS. Data is created next to this file:

    database/app.db      SQLite (metadata, criteria, evaluations)
    uploads/             untouched originals (failed imports are kept in uploads/_failed/)
    previews/            JPEG previews used on the evaluation page
    thumbnails/          JPEG thumbnails used in the library

Optional environment variables: PHOTO_EVAL_HOST, PHOTO_EVAL_PORT, PAGE_SIZE, SECRET_KEY.
"""
import io
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, url_for)
from jinja2 import DictLoader
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    import rawpy
except ImportError:  # RAW support is optional at import time
    rawpy = None

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "app.db"
UPLOAD_DIR = BASE_DIR / "uploads"
FAILED_DIR = UPLOAD_DIR / "_failed"
PREVIEW_DIR = BASE_DIR / "previews"
THUMB_DIR = BASE_DIR / "thumbnails"

HOST = os.environ.get("PHOTO_EVAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PHOTO_EVAL_PORT", "5000"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "24"))

THUMB_MAX = 400            # px, longest side
PREVIEW_MAX = 2000         # px, longest side
THUMB_QUALITY = 82
PREVIEW_QUALITY = 88
MAX_UPLOAD_BYTES = 4 * 1024 ** 3   # total size of one upload request
RAW_MIN_EMBEDDED_PX = 1200         # embedded RAW previews smaller than this trigger a real decode

STANDARD_EXT = {"jpg", "jpeg", "png", "webp"}
RAW_EXT = {"cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "raf", "orf", "rw2",
           "pef", "dng", "srw", "3fr", "erf", "kdc", "mef", "mos", "mrw", "iiq"}
ALLOWED_EXT = STANDARD_EXT | RAW_EXT
STANDARD_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}   # MPO = multi-picture JPEG from some cameras

Image.MAX_IMAGE_PIXELS = 400_000_000

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "photo-evaluator-local")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["MAX_FORM_PARTS"] = 100_000   # Flask/Werkzeug default (1000) would cap multi-file uploads


class ImportProblem(Exception):
    """A user-facing reason why a file could not be imported."""


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    preview_path TEXT NOT NULL,
    thumbnail_path TEXT NOT NULL,
    mime_type TEXT,
    file_size INTEGER,
    width INTEGER,
    height INTEGER,
    is_raw INTEGER NOT NULL DEFAULT 0,
    uploaded_at DATETIME NOT NULL,
    captured_at DATETIME,
    camera TEXT,
    lens TEXT,
    focal_length REAL,
    aperture REAL,
    shutter_speed TEXT,
    iso INTEGER,
    score INTEGER NOT NULL DEFAULT 0,
    evaluated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    yes_score INTEGER NOT NULL DEFAULT 0,
    no_score INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id INTEGER NOT NULL,
    criterion_id INTEGER NOT NULL,
    answer INTEGER NOT NULL,
    score INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(image_id, criterion_id),
    FOREIGN KEY(image_id) REFERENCES images(id),
    FOREIGN KEY(criterion_id) REFERENCES criteria(id)
);

CREATE INDEX IF NOT EXISTS idx_images_score ON images(evaluated, score);
CREATE INDEX IF NOT EXISTS idx_images_uploaded ON images(uploaded_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_image ON evaluations(image_id);
"""

DEFAULT_CRITERIA = [
    ("Subject", "Does the photograph have a clear subject?", 5, 0),
    ("Centered", "Is the subject centered?", -2, 0),
    ("Triangles", "Are there triangles?", 5, 0),
    ("Framing", "Does it use framing?", 5, 0),
    ("Foreground", "Does it have foreground?", 2, 0),
    ("Depth", "Does it have depth?", 5, 0),
    ("Layout", "Does the layout work?", 5, 0),
]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app_storage():
    for d in (DB_PATH.parent, UPLOAD_DIR, FAILED_DIR, PREVIEW_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM criteria").fetchone()[0] == 0:
            ts = now()
            for i, (name, desc, yes, no) in enumerate(DEFAULT_CRITERIA, 1):
                conn.execute(
                    "INSERT INTO criteria (name, description, yes_score, no_score, active, sort_order,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (name, desc, yes, no, i, ts, ts))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def recalc_image_score(db, image_id):
    """Cached image score = sum of stored evaluation scores for ACTIVE criteria."""
    db.execute(
        "UPDATE images SET score = COALESCE((SELECT SUM(e.score) FROM evaluations e"
        " JOIN criteria c ON c.id = e.criterion_id"
        " WHERE e.image_id = images.id AND c.active = 1), 0) WHERE id = ?", (image_id,))


def rescore_everything(db):
    """Re-apply the current criterion points to every stored answer, then refresh cached scores."""
    db.execute(
        "UPDATE evaluations SET score = (SELECT CASE WHEN evaluations.answer = 1"
        " THEN c.yes_score ELSE c.no_score END FROM criteria c WHERE c.id = evaluations.criterion_id)")
    db.execute(
        "UPDATE images SET score = COALESCE((SELECT SUM(e.score) FROM evaluations e"
        " JOIN criteria c ON c.id = e.criterion_id"
        " WHERE e.image_id = images.id AND c.active = 1), 0)")


def next_unevaluated_id(db):
    row = db.execute("SELECT id FROM images WHERE evaluated = 0 ORDER BY id ASC LIMIT 1").fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------------------- #
# Image processing (Pillow + optional rawpy)
# --------------------------------------------------------------------------- #
def clean_filename(name):
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:255] or "unnamed"


def to_rgb(img):
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _text(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    v = str(v).replace("\x00", "").strip()
    return v or None


def _num(v):
    try:
        if isinstance(v, (tuple, list)):
            v = v[0]
        v = float(v)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def empty_meta():
    return dict.fromkeys(("captured_at", "camera", "lens", "focal_length", "aperture",
                          "shutter_speed", "iso"))


def extract_exif(img):
    """Best-effort EXIF read. Never raises; missing values stay None."""
    out = empty_meta()
    try:
        ex = img.getexif()
        if not ex:
            return out
        try:
            sub = dict(ex.get_ifd(0x8769))
        except Exception:
            sub = {}

        dt = _text(sub.get(0x9003) or ex.get(0x0132))
        if dt:
            m = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}:\d{2}:\d{2})", dt)
            if m and m.group(1) != "0000":
                out["captured_at"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}"

        make, model = _text(ex.get(0x010F)), _text(ex.get(0x0110))
        if model and make and not model.lower().startswith(make.lower()):
            out["camera"] = f"{make} {model}"
        else:
            out["camera"] = model or make

        out["lens"] = _text(sub.get(0xA434))
        out["focal_length"] = _num(sub.get(0x920A))
        out["aperture"] = _num(sub.get(0x829D))

        t = _num(sub.get(0x829A))
        if t and t > 0:
            out["shutter_speed"] = f"{t:g}" if t >= 1 else f"1/{round(1 / t)}"

        iso = _num(sub.get(0x8827))
        out["iso"] = int(iso) if iso else None
    except Exception:
        pass
    return out


def process_standard(path):
    """Return (RGB image, metadata dict) for a JPEG/PNG/WebP."""
    with Image.open(path) as im:
        fmt = im.format
        if fmt not in STANDARD_FORMATS:
            raise ImportProblem(f"Unsupported image format ({fmt}).")
        meta = extract_exif(im)
        w, h = im.size
        if im.getexif().get(0x0112) in (5, 6, 7, 8):
            w, h = h, w
        if fmt in ("JPEG", "MPO"):
            im.draft("RGB", (PREVIEW_MAX, PREVIEW_MAX))   # much faster decode for big JPEGs
        im.load()
        img = to_rgb(ImageOps.exif_transpose(im))
    mime = "image/jpeg" if fmt in ("JPEG", "MPO") else Image.MIME.get(fmt, "image/" + fmt.lower())
    meta.update(width=w, height=h, mime_type=mime, is_raw=0)
    return img, meta


def _apply_raw_flip(img, flip, native_landscape):
    """Orient an embedded preview that carries no EXIF orientation, using LibRaw's flip value."""
    T = Image.Transpose
    if flip == 3:
        return img.transpose(T.ROTATE_180)
    if flip in (5, 6):
        if (img.width >= img.height) != native_landscape:
            return img   # preview is already upright
        return img.transpose(T.ROTATE_90 if flip == 5 else T.ROTATE_270)
    return img


def process_raw(path):
    """Return (RGB image, metadata dict) for a RAW file. Original is never touched."""
    if rawpy is None:
        raise ImportProblem("RAW support is unavailable because rawpy is not installed "
                            "(pip install rawpy).")
    try:
        raw = rawpy.imread(str(path))
    except Exception as e:
        raise ImportProblem(f"RAW decode failed: unsupported or corrupt RAW file "
                            f"({type(e).__name__}).")
    with raw:
        s = raw.sizes
        flip = getattr(s, "flip", 0)
        native_w, native_h = s.width, s.height
        w, h = (native_h, native_w) if flip in (5, 6) else (native_w, native_h)
        native_landscape = native_w >= native_h

        meta = empty_meta()
        img = None
        # 1) Preferred: the embedded JPEG preview (fast).
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as t:
                    meta = extract_exif(t)
                    t.load()
                    if 0x0112 in t.getexif():
                        img = to_rgb(ImageOps.exif_transpose(t))
                    else:
                        img = _apply_raw_flip(to_rgb(t.copy()), flip, native_landscape)
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                img = _apply_raw_flip(to_rgb(Image.fromarray(thumb.data)), flip, native_landscape)
        except Exception:
            img = None

        # 2) Fallback: decode the RAW with LibRaw (half-size for speed).
        if img is None or max(img.size) < RAW_MIN_EMBEDDED_PX:
            try:
                dev = Image.fromarray(raw.postprocess(use_camera_wb=True, half_size=True,
                                                      output_bps=8))
                if img is None or max(dev.size) > max(img.size):
                    img = dev
            except Exception as e:
                if img is None:
                    raise ImportProblem(f"RAW decode failed ({type(e).__name__}: {e}).")

    # Metadata fallback: many TIFF-based RAWs (DNG, NEF, ARW...) can be read by Pillow directly.
    if not meta["camera"] or not meta["captured_at"]:
        try:
            with Image.open(path) as f:
                for k, v in extract_exif(f).items():
                    if meta.get(k) is None:
                        meta[k] = v
        except Exception:
            pass

    meta.update(width=w, height=h, mime_type="image/x-raw", is_raw=1)
    return img, meta


def write_derivatives(img, uid):
    """Write preview + thumbnail JPEGs. Returns their paths relative to BASE_DIR."""
    preview = img.copy()
    preview.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.Resampling.LANCZOS)   # never upscales
    preview.save(PREVIEW_DIR / f"{uid}.jpg", "JPEG", quality=PREVIEW_QUALITY, optimize=True)
    thumb = preview.copy()
    thumb.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.LANCZOS)
    thumb.save(THUMB_DIR / f"{uid}.jpg", "JPEG", quality=THUMB_QUALITY, optimize=True)
    return f"previews/{uid}.jpg", f"thumbnails/{uid}.jpg"


def import_upload(db, storage):
    """Import one uploaded file. Returns (original_name, error_or_None, kept_failed_file)."""
    original = clean_filename(storage.filename)
    ext = os.path.splitext(original)[1].lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return original, "Unsupported format.", False

    uid = uuid.uuid4().hex
    stored_name = f"{uid}.{ext}"
    stored_path = UPLOAD_DIR / stored_name
    storage.save(stored_path)

    try:
        img, meta = process_raw(stored_path) if ext in RAW_EXT else process_standard(stored_path)
        preview_rel, thumb_rel = write_derivatives(img, uid)
        db.execute(
            "INSERT INTO images (filename, original_filename, filepath, preview_path, thumbnail_path,"
            " mime_type, file_size, width, height, is_raw, uploaded_at, captured_at, camera, lens,"
            " focal_length, aperture, shutter_speed, iso, score, evaluated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (stored_name, original, f"uploads/{stored_name}", preview_rel, thumb_rel,
             meta["mime_type"], stored_path.stat().st_size, meta["width"], meta["height"],
             meta["is_raw"], now(), meta["captured_at"], meta["camera"], meta["lens"],
             meta["focal_length"], meta["aperture"], meta["shutter_speed"], meta["iso"]))
        db.commit()
        return original, None, False
    except Exception as e:
        db.rollback()
        for d in (PREVIEW_DIR, THUMB_DIR):
            (d / f"{uid}.jpg").unlink(missing_ok=True)
        if isinstance(e, ImportProblem):
            reason = str(e)
        elif isinstance(e, UnidentifiedImageError):
            reason = "Not a valid or supported image file."
        else:
            reason = f"Could not process file ({type(e).__name__}: {e})."
        # Never silently delete the user's file: park it in uploads/_failed/.
        kept = False
        try:
            shutil.move(str(stored_path), str(FAILED_DIR / stored_name))
            kept = True
        except OSError:
            pass
        return original, reason, kept


def remove_image_files(row):
    """Delete original, preview and thumbnail. Returns list of error strings."""
    errors = []
    for root, rel in ((UPLOAD_DIR, row["filepath"]), (PREVIEW_DIR, row["preview_path"]),
                      (THUMB_DIR, row["thumbnail_path"])):
        if not rel:
            continue
        target = root / os.path.basename(rel)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append(f"{target.name}: {e.strerror or e}")
    return errors


# --------------------------------------------------------------------------- #
# Template helpers
# --------------------------------------------------------------------------- #
@app.template_filter("signed")
def signed_filter(v):
    v = int(v or 0)
    return f"+{v}" if v > 0 else str(v)


@app.template_filter("filesize")
def filesize_filter(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def safe_return_path(value, fallback):
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return fallback


# --------------------------------------------------------------------------- #
# Routes: media
# --------------------------------------------------------------------------- #
@app.route("/media/<kind>/<int:image_id>")
def media(kind, image_id):
    row = get_db().execute(
        "SELECT filepath, preview_path, thumbnail_path, original_filename FROM images WHERE id = ?",
        (image_id,)).fetchone()
    if row is None:
        abort(404)
    if kind == "thumb":
        return send_from_directory(THUMB_DIR, os.path.basename(row["thumbnail_path"]), max_age=3600)
    if kind == "preview":
        return send_from_directory(PREVIEW_DIR, os.path.basename(row["preview_path"]), max_age=3600)
    if kind == "original":
        return send_from_directory(UPLOAD_DIR, os.path.basename(row["filepath"]),
                                   as_attachment=True, download_name=row["original_filename"])
    abort(404)


# --------------------------------------------------------------------------- #
# Routes: library
# --------------------------------------------------------------------------- #
def parse_date_arg(name):
    value = (request.args.get(name) or "").strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return ""


@app.route("/")
def library():
    db = get_db()
    sort = request.args.get("sort", "date")
    if sort not in ("date", "score"):
        sort = "date"
    status = request.args.get("status", "all")
    if status not in ("all", "evaluated", "unevaluated"):
        status = "all"
    direction = request.args.get("dir", "desc").lower()
    if direction not in ("asc", "desc"):
        direction = "desc"
    page = max(1, request.args.get("page", 1, type=int) or 1)

    # Date range = capture date from the photo's metadata (upload date if a photo has none).
    date_from, date_to = parse_date_arg("date_from"), parse_date_arg("date_to")
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    conds, params = [], []
    if status == "evaluated":
        conds.append("evaluated = 1")
    elif status == "unevaluated":
        conds.append("evaluated = 0")
    if date_from:
        conds.append("date(COALESCE(captured_at, uploaded_at)) >= ?")
        params.append(date_from)
    if date_to:
        conds.append("date(COALESCE(captured_at, uploaded_at)) <= ?")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    fargs = {k: v for k, v in (("date_from", date_from), ("date_to", date_to)) if v}
    d = "ASC" if direction == "asc" else "DESC"
    if sort == "date":
        order = f"COALESCE(captured_at, uploaded_at) {d}, id {d}"
    else:  # unevaluated photographs always sink to the end when ranking by score
        order = f"evaluated DESC, score {d}, COALESCE(captured_at, uploaded_at) DESC, id DESC"

    total = db.execute(f"SELECT COUNT(*) FROM images {where}", params).fetchone()[0]
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    images = db.execute(
        f"SELECT id, original_filename, score, evaluated, is_raw FROM images {where}"
        f" ORDER BY {order} LIMIT ? OFFSET ?", params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()

    start = max(1, page - 2)
    end = min(pages, start + 4)
    start = max(1, end - 4)

    return render_template("library.html", images=images, sort=sort, status=status,
                           direction=direction, page=page, pages=pages, total=total,
                           window=range(start, end + 1), date_from=date_from, date_to=date_to,
                           fargs=fargs)


@app.post("/images/download")
def download_images():
    db = get_db()
    back = safe_return_path(request.form.get("return_to"), url_for("library"))
    ids = sorted({int(i) for i in request.form.getlist("ids") if i.isdigit()})[:900]
    if not ids:
        flash("No photographs were selected.", "error")
        return redirect(back)

    rows = db.execute(
        f"SELECT filepath, original_filename FROM images WHERE id IN ({','.join('?' * len(ids))})"
        " ORDER BY id", ids).fetchall()
    files = []
    for r in rows:
        path = UPLOAD_DIR / os.path.basename(r["filepath"])
        if path.is_file():
            files.append((path, r["original_filename"]))
    if not files:
        flash("The original files could not be found on disk.", "error")
        return redirect(back)

    if len(files) == 1:   # one photo: serve the original directly
        path, name = files[0]
        return send_from_directory(UPLOAD_DIR, path.name, as_attachment=True, download_name=name)

    used = set()

    def unique(name):
        base, ext = os.path.splitext(name)
        cand, n = name, 2
        while cand.lower() in used:
            cand = f"{base} ({n}){ext}"
            n += 1
        used.add(cand.lower())
        return cand

    tmp = tempfile.TemporaryFile()   # on disk, removed automatically when closed
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:   # photos are already compressed
        for path, name in files:
            zf.write(path, unique(name))
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name=f"photos-{datetime.now():%Y%m%d-%H%M%S}.zip")


@app.post("/images/delete")
def delete_images():
    db = get_db()
    back = safe_return_path(request.form.get("return_to"), url_for("library"))
    ids = sorted({int(i) for i in request.form.getlist("ids") if i.isdigit()})
    if not ids:
        flash("No photographs were selected.", "error")
        return redirect(back)

    rows = []
    for image_id in ids:
        row = db.execute("SELECT id, filepath, preview_path, thumbnail_path FROM images WHERE id = ?",
                         (image_id,)).fetchone()
        if row:
            rows.append(row)
    with db:  # single transaction for the DB part
        for row in rows:
            db.execute("DELETE FROM evaluations WHERE image_id = ?", (row["id"],))
            db.execute("DELETE FROM images WHERE id = ?", (row["id"],))
    problems = []
    for row in rows:
        problems += remove_image_files(row)

    flash(f"Deleted {len(rows)} photograph{'s' if len(rows) != 1 else ''}.", "ok")
    for p in problems:
        flash(f"Could not remove a file: {p}", "error")
    return redirect(back)


# --------------------------------------------------------------------------- #
# Routes: upload
# --------------------------------------------------------------------------- #
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html", accept=",".join("." + e for e in sorted(ALLOWED_EXT)),
                               raw_ok=rawpy is not None)

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Choose at least one file to upload.", "error")
        return redirect(url_for("upload"))

    db = get_db()
    ok, failures, kept_any = 0, [], False
    for f in files:
        name, error, kept = import_upload(db, f)
        if error:
            failures.append((name, error))
            kept_any = kept_any or kept
        else:
            ok += 1

    if ok:
        flash(f"{ok} image{'s' if ok != 1 else ''} uploaded successfully.", "ok")
    if failures:
        flash(f"{len(failures)} image{'s' if len(failures) != 1 else ''} failed.", "error")
        for name, reason in failures[:20]:
            flash(f"{name} - {reason}", "error")
        if len(failures) > 20:
            flash(f"...and {len(failures) - 20} more.", "error")
        if kept_any:
            flash("Files that failed after upload were not deleted; they are in uploads/_failed/.", "info")
    return redirect(url_for("library") if ok else url_for("upload"))


@app.errorhandler(413)
def too_large(_e):
    flash(f"Upload too large (limit {MAX_UPLOAD_BYTES // 1024 ** 3} GB per upload). "
          "Try fewer files at a time.", "error")
    return redirect(url_for("upload"))


# --------------------------------------------------------------------------- #
# Routes: image detail
# --------------------------------------------------------------------------- #
@app.route("/images/<int:image_id>")
def image_detail(image_id):
    db = get_db()
    image = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if image is None:
        abort(404)
    evals = db.execute(
        "SELECT c.name, c.active, e.answer, e.score FROM evaluations e"
        " JOIN criteria c ON c.id = e.criterion_id WHERE e.image_id = ?"
        " ORDER BY c.sort_order, c.id", (image_id,)).fetchall()
    return render_template("image.html", image=image, evals=evals)


# --------------------------------------------------------------------------- #
# Routes: evaluation
# --------------------------------------------------------------------------- #
def active_criteria(db):
    return db.execute("SELECT * FROM criteria WHERE active = 1 ORDER BY sort_order, id").fetchall()


@app.route("/evaluate")
def evaluate_next():
    db = get_db()
    nxt = next_unevaluated_id(db)
    if nxt:
        return redirect(url_for("evaluate_image", image_id=nxt))
    has_images = db.execute("SELECT 1 FROM images LIMIT 1").fetchone() is not None
    return render_template("evaluate_done.html", has_images=has_images)


@app.route("/evaluate/<int:image_id>", methods=["GET", "POST"])
def evaluate_image(image_id):
    db = get_db()
    image = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if image is None:
        abort(404)
    criteria = active_criteria(db)

    if request.method == "POST":
        if not criteria:
            flash("There are no active criteria. Add or enable one first.", "error")
            return redirect(url_for("criteria_list"))
        answers, missing = {}, []
        for c in criteria:
            v = request.form.get(f"answer_{c['id']}")
            if v in ("0", "1"):
                answers[c["id"]] = int(v)
            else:
                missing.append(c["name"])
        if missing:
            flash("Answer every question before saving. Missing: " + ", ".join(missing), "error")
            return render_template("evaluate.html", image=image, criteria=criteria, existing=answers)

        ts = now()
        with db:
            for c in criteria:
                ans = answers[c["id"]]
                pts = c["yes_score"] if ans == 1 else c["no_score"]
                db.execute(
                    "INSERT INTO evaluations (image_id, criterion_id, answer, score, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(image_id, criterion_id) DO UPDATE SET"
                    " answer = excluded.answer, score = excluded.score, updated_at = excluded.updated_at",
                    (image_id, c["id"], ans, pts, ts, ts))
            recalc_image_score(db, image_id)
            db.execute("UPDATE images SET evaluated = 1 WHERE id = ?", (image_id,))
        return redirect(url_for("evaluate_next"))

    existing = {r["criterion_id"]: r["answer"] for r in db.execute(
        "SELECT criterion_id, answer FROM evaluations WHERE image_id = ?", (image_id,))}
    return render_template("evaluate.html", image=image, criteria=criteria, existing=existing)


# --------------------------------------------------------------------------- #
# Routes: criteria
# --------------------------------------------------------------------------- #
def parse_criterion_form(form):
    errors = []
    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Name is required.")
    elif len(name) > 100:
        errors.append("Name must be 100 characters or fewer.")
    description = (form.get("description") or "").strip()
    if len(description) > 500:
        errors.append("Description must be 500 characters or fewer.")
    points = {}
    for key, label in (("yes_score", "Yes points"), ("no_score", "No points")):
        try:
            points[key] = int((form.get(key) or "").strip())
            if abs(points[key]) > 10000:
                raise ValueError
        except ValueError:
            errors.append(f"{label} must be a whole number between -10000 and 10000.")
    return errors, {"name": name, "description": description, **points}


@app.route("/criteria")
def criteria_list():
    rows = get_db().execute("SELECT * FROM criteria ORDER BY sort_order, id").fetchall()
    return render_template("criteria.html", criteria=rows)


@app.route("/criteria/new", methods=["GET", "POST"])
def criterion_new():
    form_values = {"name": "", "description": "", "yes_score": 5, "no_score": 0}
    if request.method == "POST":
        errors, data = parse_criterion_form(request.form)
        form_values.update(request.form.to_dict())
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db = get_db()
            nxt = db.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM criteria").fetchone()[0]
            ts = now()
            with db:
                db.execute(
                    "INSERT INTO criteria (name, description, yes_score, no_score, active, sort_order,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (data["name"], data["description"], data["yes_score"], data["no_score"], nxt, ts, ts))
            flash(f"Added criterion “{data['name']}”.", "ok")
            return redirect(url_for("criteria_list"))
    return render_template("criterion_form.html", c=form_values, editing=False)


@app.route("/criteria/<int:cid>/edit", methods=["GET", "POST"])
def criterion_edit(cid):
    db = get_db()
    row = db.execute("SELECT * FROM criteria WHERE id = ?", (cid,)).fetchone()
    if row is None:
        abort(404)
    values = dict(row)
    if request.method == "POST":
        errors, data = parse_criterion_form(request.form)
        values.update(request.form.to_dict())
        values["active"] = 1 if "active" in request.form else 0
        try:
            sort_order = int((request.form.get("sort_order") or "").strip())
        except ValueError:
            errors.append("Sort order must be a whole number.")
            sort_order = row["sort_order"]
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with db:
                db.execute(
                    "UPDATE criteria SET name = ?, description = ?, yes_score = ?, no_score = ?,"
                    " sort_order = ?, active = ?, updated_at = ? WHERE id = ?",
                    (data["name"], data["description"], data["yes_score"], data["no_score"],
                     sort_order, values["active"], now(), cid))
                rescore_everything(db)
            flash(f"Saved “{data['name']}”. Existing scores were recalculated.", "ok")
            return redirect(url_for("criteria_list"))
    return render_template("criterion_form.html", c=values, editing=True)


@app.post("/criteria/<int:cid>/toggle")
def criterion_toggle(cid):
    db = get_db()
    row = db.execute("SELECT id, name, active FROM criteria WHERE id = ?", (cid,)).fetchone()
    if row is None:
        abort(404)
    with db:
        db.execute("UPDATE criteria SET active = ?, updated_at = ? WHERE id = ?",
                   (0 if row["active"] else 1, now(), cid))
        rescore_everything(db)
    flash(f"{'Disabled' if row['active'] else 'Enabled'} “{row['name']}”. Scores were recalculated.", "ok")
    return redirect(url_for("criteria_list"))


@app.post("/criteria/<int:cid>/move")
def criterion_move(cid):
    db = get_db()
    ids = [r["id"] for r in db.execute("SELECT id FROM criteria ORDER BY sort_order, id")]
    if cid not in ids:
        abort(404)
    i = ids.index(cid)
    j = i - 1 if request.form.get("direction") == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    with db:
        for n, criterion_id in enumerate(ids, 1):
            db.execute("UPDATE criteria SET sort_order = ? WHERE id = ?", (n, criterion_id))
    return redirect(url_for("criteria_list"))


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
BASE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Photo Evaluator{% endblock %}</title>
<style>
:root{color-scheme:dark;--bg:#242424;--stage:#1b1b1b;--panel:#2e2e2e;--panel2:#3a3a3a;--line:#454545;
--text:#dedede;--muted:#9a9a9a;--accent:#8ab4e8;--accent-ink:#10233a;--pos:#8fc79a;--neg:#e39a9a;--danger:#b85555}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:1.35rem;margin:0 0 16px;font-weight:600}
h2{font-size:1.05rem;margin:22px 0 8px;font-weight:600}
.muted{color:var(--muted)}
.pos{color:var(--pos)}.neg{color:var(--neg)}
nav.top{display:flex;align-items:center;gap:6px;padding:0 16px;height:48px;background:var(--panel);border-bottom:1px solid var(--line);overflow-x:auto}
nav.top .brand{font-weight:700;color:var(--text);margin-right:18px;white-space:nowrap}
nav.top a.link{color:var(--muted);padding:6px 12px;border-radius:6px}
nav.top a.link:hover{color:var(--text);text-decoration:none;background:var(--panel2)}
nav.top a.link.on{color:var(--text);background:var(--panel2)}
.wrap{max-width:1400px;margin:0 auto;padding:20px 16px 48px}
.flashes{max-width:1400px;margin:12px auto 0;padding:0 16px}
.flash{padding:9px 12px;border-radius:6px;margin-bottom:6px;background:var(--panel);border-left:4px solid var(--muted)}
.flash.ok{border-color:var(--pos)}.flash.error{border-color:var(--neg)}.flash.info{border-color:var(--accent)}
.btn{display:inline-block;font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);padding:7px 14px;border-radius:6px;cursor:pointer}
.btn:hover{background:#444;text-decoration:none}
.btn:disabled{opacity:.5;cursor:default}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn.primary:hover{background:#a3c4ee}
.btn.danger{border-color:var(--danger);color:var(--neg)}
.btn.danger:hover{background:#4a2b2b}
.btn.small{padding:3px 10px;font-size:.88rem}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.toolbar{display:flex;flex-wrap:wrap;gap:8px 24px;align-items:center;margin-bottom:14px}
.toolbar .group{display:flex;gap:4px;align-items:center}
.toolbar .lbl{color:var(--muted);margin-right:6px}
.toolbar .group a{color:var(--muted);padding:4px 10px;border-radius:6px;border:1px solid transparent}
.toolbar .group a:hover{text-decoration:none;color:var(--text)}
.toolbar .group a.on{color:var(--text);border-color:var(--line);background:var(--panel)}
.toolbar input[type=date]{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.actions{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.actions .count{margin-left:auto;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;display:flex;flex-direction:column}
.card .thumb{display:flex;align-items:center;justify-content:center;aspect-ratio:1/1;background:var(--stage)}
.card .thumb img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.card .pick{display:flex;gap:8px;align-items:center;padding:8px 10px 0;cursor:pointer}
.card .pick .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.card .meta{padding:2px 10px 9px 34px;display:flex;justify-content:space-between;align-items:center}
.score{font-variant-numeric:tabular-nums}
.tag{font-size:.75rem;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 5px}
.pager{display:flex;gap:6px;justify-content:center;align-items:center;margin-top:22px;flex-wrap:wrap}
.pager a,.pager span{padding:5px 11px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--text)}
.pager a:hover{text-decoration:none;background:var(--panel2)}
.pager .cur{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.pager .off{color:var(--muted);opacity:.5}
.empty{padding:40px 0;color:var(--muted)}
.form{max-width:560px}
.form label.f{display:block;margin:0 0 14px}
.form label.f span{display:block;margin-bottom:4px;color:var(--muted)}
.form input[type=text],.form input[type=number],.form textarea{width:100%;font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.form .row{display:flex;gap:14px}.form .row label.f{flex:1}
.form .check{display:flex;gap:8px;align-items:center;margin-bottom:14px}
.form .hint{color:var(--muted);font-size:.9rem;margin:-6px 0 14px}
.crit{display:flex;gap:14px;align-items:center;padding:12px 14px;background:var(--panel);border:1px solid var(--line);border-radius:6px;margin-bottom:8px}
.crit.off{opacity:.55}
.crit .body{flex:1;min-width:0}
.crit .title{font-weight:600}
.crit .btns{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.crit form{margin:0}
table.kv{border-collapse:collapse;width:100%}
table.kv td,table.kv th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
table.kv th{color:var(--muted);font-weight:400;width:38%}
table.kv td.num{text-align:right;font-variant-numeric:tabular-nums}
table.kv tr.dim td{color:var(--muted)}
.detail{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:22px;align-items:start}
.detail .photo{background:var(--stage);border-radius:6px;display:flex;justify-content:center}
.detail .photo img{max-width:100%;max-height:calc(100vh - 110px);object-fit:contain;display:block}
.bigscore{font-size:2rem;font-weight:700;font-variant-numeric:tabular-nums}
.eval{display:grid;grid-template-columns:minmax(0,1fr) 380px;height:calc(100vh - 49px)}
.eval .stage{background:var(--stage);display:flex;align-items:center;justify-content:center;padding:12px;overflow:hidden}
.eval .stage img{max-width:100%;max-height:calc(100vh - 49px - 24px);object-fit:contain;display:block}
.eval .panel{display:flex;flex-direction:column;min-height:0;background:var(--panel);border-left:1px solid var(--line)}
.eval .panel header{padding:12px 16px;border-bottom:1px solid var(--line)}
.eval .panel header .fname{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.eval .qs{flex:1;overflow:auto;padding:6px 16px}
.q{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
.q .txt{flex:1;min-width:0}
.q .qn{font-weight:600}
.q .qd{color:var(--muted);font-size:.86rem}
.seg{display:flex;flex:none}
.seg input{position:absolute;opacity:0;width:1px;height:1px}
.seg label{min-width:74px;text-align:center;padding:7px 10px;border:1px solid var(--line);background:var(--panel2);cursor:pointer;user-select:none}
.seg label:first-of-type{border-radius:6px 0 0 6px}
.seg label:last-of-type{border-radius:0 6px 6px 0;margin-left:-1px}
.seg label small{color:var(--muted);margin-left:4px;font-variant-numeric:tabular-nums}
.seg input:checked + label.yes{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.seg input:checked + label.yes small{color:var(--accent-ink)}
.seg input:checked + label.no{background:#5a5a5a;border-color:#7a7a7a;font-weight:600}
.seg input:focus-visible + label{outline:2px solid var(--accent);outline-offset:2px;position:relative}
.eval .panel footer{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border-top:1px solid var(--line)}
.eval .panel footer .total{font-size:1.1rem}
.eval .panel footer b{font-size:1.5rem;font-variant-numeric:tabular-nums}
.eval .panel .flash{margin:8px 0 0}
.done{max-width:480px;margin:80px auto;text-align:center}
@media (max-width:820px){
 .detail{grid-template-columns:1fr}
 .eval{grid-template-columns:1fr;height:auto}
 .eval .stage img{max-height:65vh}
 .eval .panel{border-left:0;border-top:1px solid var(--line)}
 .crit{flex-wrap:wrap}
}
</style>
</head>
<body>
{% set ep = request.endpoint %}
<nav class="top">
  <a class="brand" href="{{ url_for('library') }}">Photo Evaluator</a>
  <a class="link {{ 'on' if ep in ('library', 'image_detail') }}" href="{{ url_for('library') }}">Library</a>
  <a class="link {{ 'on' if ep == 'upload' }}" href="{{ url_for('upload') }}">Upload</a>
  <a class="link {{ 'on' if ep in ('evaluate_next', 'evaluate_image') }}" href="{{ url_for('evaluate_next') }}">Evaluate</a>
  <a class="link {{ 'on' if ep in ('criteria_list', 'criterion_new', 'criterion_edit') }}" href="{{ url_for('criteria_list') }}">Criteria</a>
</nav>
{% block flashes %}
{% with msgs = get_flashed_messages(with_categories=true) %}
{% if msgs %}<div class="flashes">{% for cat, msg in msgs %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}</div>{% endif %}
{% endwith %}
{% endblock %}
{% block content %}{% endblock %}
<script>
(function () {
  var selAll = document.getElementById('select-all');
  if (selAll) {
    selAll.addEventListener('click', function () {
      var boxes = [].slice.call(document.querySelectorAll('input[name="ids"]'));
      var all = boxes.length > 0 && boxes.every(function (b) { return b.checked; });
      boxes.forEach(function (b) { b.checked = !all; });
      selAll.textContent = all ? 'Select all' : 'Clear selection';
    });
  }
  var del = document.getElementById('delete-form');
  if (del) {
    del.addEventListener('submit', function (e) {
      var n = del.querySelectorAll('input[name="ids"]:checked').length;
      if (!n) { e.preventDefault(); alert('Select at least one photograph first.'); return; }
      var action = e.submitter && e.submitter.getAttribute('data-action');
      if (action !== 'download' &&
          !confirm('Permanently delete ' + n + ' photograph' + (n === 1 ? '' : 's') +
                   ', including the original files? This cannot be undone.')) { e.preventDefault(); }
    });
  }
  var up = document.getElementById('upload-form');
  if (up) {
    up.addEventListener('submit', function () {
      var b = up.querySelector('button[type=submit]');
      b.disabled = true; b.textContent = 'Uploading and processing...';
    });
  }
  var ev = document.getElementById('eval-form');
  if (ev) {
    var total = document.getElementById('live-total');
    var update = function () {
      var sum = 0;
      [].forEach.call(ev.querySelectorAll('input[type=radio]:checked'), function (r) {
        sum += parseInt(r.getAttribute('data-pts'), 10) || 0;
      });
      total.textContent = sum;
    };
    ev.addEventListener('change', update);
    update();
  }
})();
</script>
</body>
</html>
"""

LIBRARY_HTML = r"""{% extends "base.html" %}
{% block title %}Library - Photo Evaluator{% endblock %}
{% block content %}
<div class="wrap">
  <div class="toolbar">
    <div class="group"><span class="lbl">Sort</span>
      {% for key, label in (('date', 'Date'), ('score', 'Score')) %}
        {% if sort == key %}
          <a class="on" href="{{ url_for('library', sort=key, dir=('asc' if direction == 'desc' else 'desc'), status=status, **fargs) }}">{{ label }} {{ '↓' if direction == 'desc' else '↑' }}</a>
        {% else %}
          <a href="{{ url_for('library', sort=key, status=status, **fargs) }}">{{ label }}</a>
        {% endif %}
      {% endfor %}
    </div>
    <div class="group"><span class="lbl">Show</span>
      {% for key, label in (('all', 'All'), ('evaluated', 'Evaluated'), ('unevaluated', 'Not evaluated')) %}
        <a class="{{ 'on' if status == key }}" href="{{ url_for('library', sort=sort, dir=direction, status=key, **fargs) }}">{{ label }}</a>
      {% endfor %}
    </div>
    <form class="group" method="get" action="{{ url_for('library') }}">
      <input type="hidden" name="sort" value="{{ sort }}">
      <input type="hidden" name="dir" value="{{ direction }}">
      <input type="hidden" name="status" value="{{ status }}">
      <span class="lbl" title="Capture date from the photo metadata (upload date if a photo has none)">Taken</span>
      <input type="date" name="date_from" value="{{ date_from }}" aria-label="From date">
      <span class="muted">to</span>
      <input type="date" name="date_to" value="{{ date_to }}" aria-label="To date">
      <button class="btn small" type="submit">Apply</button>
      {% if fargs %}<a href="{{ url_for('library', sort=sort, dir=direction, status=status) }}">Clear</a>{% endif %}
    </form>
  </div>

  <form id="delete-form" method="post" action="{{ url_for('delete_images') }}">
    <input type="hidden" name="return_to" value="{{ request.full_path.rstrip('?') }}">
    <div class="actions">
      <button type="button" id="select-all" class="btn">Select all</button>
      <button type="submit" class="btn" formaction="{{ url_for('download_images') }}" data-action="download">Download selected</button>
      <button type="submit" class="btn danger" data-action="delete">Delete selected</button>
      <span class="count">{{ total }} photograph{{ '' if total == 1 else 's' }}</span>
    </div>

    {% if images %}
    <div class="grid">
      {% for im in images %}
      <div class="card">
        <a class="thumb" href="{{ url_for('image_detail', image_id=im.id) }}" title="{{ im.original_filename }}">
          <img loading="lazy" src="{{ url_for('media', kind='thumb', image_id=im.id) }}" alt="{{ im.original_filename }}">
        </a>
        <label class="pick"><input type="checkbox" name="ids" value="{{ im.id }}"><span class="name" title="{{ im.original_filename }}">{{ im.original_filename }}</span></label>
        <div class="meta">
          {% if im.evaluated %}<span>Score: <b class="score {{ 'neg' if im.score < 0 }}">{{ im.score }}</b></span>{% else %}<span class="muted">Not evaluated</span>{% endif %}
          {% if im.is_raw %}<span class="tag">RAW</span>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">
      {% if status == 'all' and not fargs %}No photographs yet. <a href="{{ url_for('upload') }}">Upload some</a> to get started.
      {% else %}No photographs match this filter.{% endif %}
    </div>
    {% endif %}
  </form>

  {% if pages > 1 %}
  <nav class="pager" aria-label="Pages">
    {% if page > 1 %}<a href="{{ url_for('library', sort=sort, dir=direction, status=status, page=page - 1, **fargs) }}">&lt; Previous</a>{% else %}<span class="off">&lt; Previous</span>{% endif %}
    {% for n in window %}
      {% if n == page %}<span class="cur">{{ n }}</span>{% else %}<a href="{{ url_for('library', sort=sort, dir=direction, status=status, page=n, **fargs) }}">{{ n }}</a>{% endif %}
    {% endfor %}
    {% if page < pages %}<a href="{{ url_for('library', sort=sort, dir=direction, status=status, page=page + 1, **fargs) }}">Next &gt;</a>{% else %}<span class="off">Next &gt;</span>{% endif %}
  </nav>
  {% endif %}
</div>
{% endblock %}
"""

UPLOAD_HTML = r"""{% extends "base.html" %}
{% block title %}Upload - Photo Evaluator{% endblock %}
{% block content %}
<div class="wrap form">
  <h1>Upload photographs</h1>
  <form id="upload-form" method="post" enctype="multipart/form-data">
    <label class="f"><span>Choose one or more files</span>
      <input type="file" name="files" accept="{{ accept }}" multiple required>
    </label>
    <button type="submit" class="btn primary">Upload</button>
  </form>
  <p class="muted">JPEG, PNG and WebP, plus camera RAW files (CR2, CR3, NEF, ARW, RAF, ORF, RW2, PEF, DNG and others LibRaw can read). Originals are never modified; previews and thumbnails are generated separately.</p>
  {% if not raw_ok %}<div class="flash error">RAW support is off because the <code>rawpy</code> package is not installed. Run <code>pip install rawpy</code> and restart.</div>{% endif %}
</div>
{% endblock %}
"""

IMAGE_HTML = r"""{% extends "base.html" %}
{% block title %}{{ image.original_filename }} - Photo Evaluator{% endblock %}
{% block content %}
<div class="wrap">
  <div class="detail">
    <div class="photo"><img src="{{ url_for('media', kind='preview', image_id=image.id) }}" alt="{{ image.original_filename }}"></div>
    <div>
      <h1 style="word-break:break-all">{{ image.original_filename }}</h1>
      {% if image.evaluated %}
        <div class="bigscore {{ 'neg' if image.score < 0 }}">{{ image.score }}</div>
        <div class="muted">Evaluated</div>
      {% else %}
        <div class="muted">Not evaluated</div>
      {% endif %}
      <p style="margin:14px 0">
        <a class="btn primary" href="{{ url_for('evaluate_image', image_id=image.id) }}">{{ 'Change evaluation' if image.evaluated else 'Evaluate' }}</a>
        <a class="btn" href="{{ url_for('media', kind='original', image_id=image.id) }}">Download original</a>
        <a class="btn" href="{{ url_for('library') }}">Back to library</a>
      </p>

      {% if evals %}
      <h2>Evaluation</h2>
      <table class="kv">
        {% for e in evals %}
        <tr class="{{ 'dim' if not e.active }}">
          <td>{{ e.name }}{% if not e.active %} <span class="tag">disabled</span>{% endif %}</td>
          <td>{{ 'Yes' if e.answer else 'No' }}</td>
          <td class="num {{ 'pos' if e.score > 0 and e.active }}{{ 'neg' if e.score < 0 and e.active }}">{{ e.score|signed }}</td>
        </tr>
        {% endfor %}
      </table>
      {% endif %}

      <h2>Details</h2>
      <table class="kv">
        <tr><th>Type</th><td>{{ 'RAW' if image.is_raw else (image.mime_type or 'Image') }}</td></tr>
        <tr><th>Dimensions</th><td>{% if image.width %}{{ image.width }} × {{ image.height }} px{% else %}-{% endif %}</td></tr>
        <tr><th>File size</th><td>{{ image.file_size|filesize }}</td></tr>
        <tr><th>Captured</th><td>{{ image.captured_at or '-' }}</td></tr>
        <tr><th>Camera</th><td>{{ image.camera or '-' }}</td></tr>
        <tr><th>Lens</th><td>{{ image.lens or '-' }}</td></tr>
        <tr><th>Focal length</th><td>{% if image.focal_length %}{{ '%g' % image.focal_length }} mm{% else %}-{% endif %}</td></tr>
        <tr><th>Aperture</th><td>{% if image.aperture %}f/{{ '%g' % image.aperture }}{% else %}-{% endif %}</td></tr>
        <tr><th>Shutter</th><td>{% if image.shutter_speed %}{{ image.shutter_speed }} s{% else %}-{% endif %}</td></tr>
        <tr><th>ISO</th><td>{{ image.iso or '-' }}</td></tr>
        <tr><th>Uploaded</th><td>{{ image.uploaded_at }}</td></tr>
      </table>
    </div>
  </div>
</div>
{% endblock %}
"""

EVALUATE_HTML = r"""{% extends "base.html" %}
{% block title %}Evaluate - Photo Evaluator{% endblock %}
{% block flashes %}{% endblock %}
{% block content %}
<div class="eval">
  <div class="stage"><img src="{{ url_for('media', kind='preview', image_id=image.id) }}" alt="{{ image.original_filename }}"></div>
  <form id="eval-form" class="panel" method="post" action="{{ url_for('evaluate_image', image_id=image.id) }}">
    <header>
      <div class="fname" title="{{ image.original_filename }}">{{ image.original_filename }}</div>
      {% if image.evaluated %}<div class="muted">Already evaluated. Saving replaces the previous answers.</div>{% endif %}
      {% for cat, msg in get_flashed_messages(with_categories=true) %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}
    </header>
    {% if criteria %}
    <div class="qs">
      {% for c in criteria %}
      <div class="q">
        <div class="txt">
          <div class="qn">{{ c.name }}</div>
          {% if c.description %}<div class="qd">{{ c.description }}</div>{% endif %}
        </div>
        <div class="seg" role="radiogroup" aria-label="{{ c.name }}">
          <input type="radio" id="a{{ c.id }}y" name="answer_{{ c.id }}" value="1" data-pts="{{ c.yes_score }}" required {{ 'checked' if existing.get(c.id) == 1 }}>
          <label class="yes" for="a{{ c.id }}y">Yes<small>{{ c.yes_score|signed }}</small></label>
          <input type="radio" id="a{{ c.id }}n" name="answer_{{ c.id }}" value="0" data-pts="{{ c.no_score }}" required {{ 'checked' if existing.get(c.id) == 0 }}>
          <label class="no" for="a{{ c.id }}n">No<small>{{ c.no_score|signed }}</small></label>
        </div>
      </div>
      {% endfor %}
    </div>
    <footer>
      <div class="total">Total <b id="live-total">0</b></div>
      <button type="submit" class="btn primary">Save &amp; Next</button>
    </footer>
    {% else %}
    <div class="qs"><p class="muted">There are no active criteria to answer. <a href="{{ url_for('criteria_list') }}">Add or enable criteria</a> first.</p></div>
    {% endif %}
  </form>
</div>
{% endblock %}
"""

EVALUATE_DONE_HTML = r"""{% extends "base.html" %}
{% block title %}Evaluate - Photo Evaluator{% endblock %}
{% block content %}
<div class="done">
  {% if has_images %}
    <h1>All photographs have been evaluated.</h1>
    <p><a class="btn primary" href="{{ url_for('library', sort='score') }}">Return to library</a></p>
  {% else %}
    <h1>There is nothing to evaluate yet.</h1>
    <p><a class="btn primary" href="{{ url_for('upload') }}">Upload photographs</a></p>
  {% endif %}
</div>
{% endblock %}
"""

CRITERIA_HTML = r"""{% extends "base.html" %}
{% block title %}Criteria - Photo Evaluator{% endblock %}
{% block content %}
<div class="wrap" style="max-width:860px">
  <h1>Criteria</h1>
  <p><a class="btn primary" href="{{ url_for('criterion_new') }}">Add criterion</a></p>
  {% for c in criteria %}
  <div class="crit {{ 'off' if not c.active }}">
    <div class="body">
      <div class="title">{{ loop.index }}. {{ c.name }} {% if not c.active %}<span class="tag">disabled</span>{% endif %}</div>
      {% if c.description %}<div class="muted">{{ c.description }}</div>{% endif %}
      <div>Yes: <b class="{{ 'pos' if c.yes_score > 0 }}{{ 'neg' if c.yes_score < 0 }}">{{ c.yes_score|signed }}</b> &nbsp; No: <b class="{{ 'pos' if c.no_score > 0 }}{{ 'neg' if c.no_score < 0 }}">{{ c.no_score|signed }}</b></div>
    </div>
    <div class="btns">
      <form method="post" action="{{ url_for('criterion_move', cid=c.id) }}"><input type="hidden" name="direction" value="up"><button class="btn small" type="submit" title="Move up" aria-label="Move {{ c.name }} up" {{ 'disabled' if loop.first }}>Up</button></form>
      <form method="post" action="{{ url_for('criterion_move', cid=c.id) }}"><input type="hidden" name="direction" value="down"><button class="btn small" type="submit" title="Move down" aria-label="Move {{ c.name }} down" {{ 'disabled' if loop.last }}>Down</button></form>
      <a class="btn small" href="{{ url_for('criterion_edit', cid=c.id) }}">Edit</a>
      <form method="post" action="{{ url_for('criterion_toggle', cid=c.id) }}"><button class="btn small" type="submit">{{ 'Disable' if c.active else 'Enable' }}</button></form>
    </div>
  </div>
  {% else %}
  <div class="empty">No criteria yet.</div>
  {% endfor %}
  <p class="muted">Disabled criteria stay in the database but are not asked during evaluation and do not count toward scores.</p>
</div>
{% endblock %}
"""

CRITERION_FORM_HTML = r"""{% extends "base.html" %}
{% block title %}{{ 'Edit' if editing else 'New' }} criterion - Photo Evaluator{% endblock %}
{% block content %}
<div class="wrap form">
  <h1>{{ 'Edit criterion' if editing else 'Add criterion' }}</h1>
  <form method="post">
    <label class="f"><span>Name</span><input type="text" name="name" value="{{ c.name }}" maxlength="100" required></label>
    <label class="f"><span>Description (shown under the name while evaluating)</span><textarea name="description" rows="2" maxlength="500">{{ c.description or '' }}</textarea></label>
    <div class="row">
      <label class="f"><span>Yes points</span><input type="number" name="yes_score" value="{{ c.yes_score }}" step="1" required></label>
      <label class="f"><span>No points</span><input type="number" name="no_score" value="{{ c.no_score }}" step="1" required></label>
    </div>
    {% if editing %}
    <label class="f"><span>Sort order (lower comes first)</span><input type="number" name="sort_order" value="{{ c.sort_order }}" step="1" required></label>
    <label class="check"><input type="checkbox" name="active" value="1" {{ 'checked' if c.active }}> Active</label>
    <p class="hint">Saving recalculates the scores of photographs that were already evaluated.</p>
    {% endif %}
    <button type="submit" class="btn primary">{{ 'Save changes' if editing else 'Add criterion' }}</button>
    <a class="btn" href="{{ url_for('criteria_list') }}">Cancel</a>
  </form>
</div>
{% endblock %}
"""

app.jinja_loader = DictLoader({
    "base.html": BASE_HTML,
    "library.html": LIBRARY_HTML,
    "upload.html": UPLOAD_HTML,
    "image.html": IMAGE_HTML,
    "evaluate.html": EVALUATE_HTML,
    "evaluate_done.html": EVALUATE_DONE_HTML,
    "criteria.html": CRITERIA_HTML,
    "criterion_form.html": CRITERION_FORM_HTML,
})

init_app_storage()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
