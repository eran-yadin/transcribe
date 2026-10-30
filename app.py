#!/usr/bin/env python3
"""
Transcribe — self-hosted MP3 -> text for phone calls & meetings.
Single-process Flask app with one background transcription worker.
Designed for the NUC (i3-7100U): one job at a time, limited CPU threads,
so it never stacks on top of Paperless OCR / Ollama.
"""

import os
import sys
import json
import time
import signal
import queue
import logging
import sqlite3
import threading
import datetime
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, render_template,
    jsonify, abort, send_from_directory
)
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------------
# Config (all overridable via env — same pattern as the Paperless classifier)
# ----------------------------------------------------------------------------
DATA_DIR        = Path(os.environ.get("DATA_DIR", "/data"))
UPLOAD_DIR      = DATA_DIR / "uploads"
TRANSCRIPT_DIR  = DATA_DIR / "transcripts"
DB_PATH         = DATA_DIR / "transcribe.sqlite"

# The model cache gets its own directory so it can get its own volume. It is
# ~3.2 GB of re-downloadable files guarding ~35 MB of real data, and homelab's
# pre-update backup archives a whole volume -- so with the cache inside
# DATA_DIR, every update spends minutes tarring 99% cache. A separate volume can
# be declared `bulk_volumes` and skipped.
#
# Defaults to DATA_DIR/models, so an install that does not set it keeps working
# exactly as before. HF_HOME is derived from it rather than set independently,
# so there is one knob and not two that can disagree.
MODEL_DIR = Path(os.environ.get("MODEL_DIR", str(DATA_DIR / "models")))
os.environ["HF_HOME"] = str(MODEL_DIR)

WHISPER_MODEL    = os.environ.get("WHISPER_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2")
WHISPER_COMPUTE  = os.environ.get("WHISPER_COMPUTE", "int8")          # int8 = lean on CPU
WHISPER_THREADS  = int(os.environ.get("WHISPER_THREADS", "3"))        # leave 1 thread for the system
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "auto")         # auto | he | en
WHISPER_BEAM     = int(os.environ.get("WHISPER_BEAM", "5"))
PORT             = int(os.environ.get("PORT", "8011"))

ALLOWED_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm"}
MAX_MB      = int(os.environ.get("MAX_UPLOAD_MB", "300"))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

# Flask's logger sits at WARNING outside debug mode, so every app.logger.info()
# in here -- "Loading model", "Job N done", the shutdown line -- was being
# discarded. The worker is the part you most want to watch, and it was the part
# you could not see at all.
# Root stays at WARNING, because turning INFO on globally also turns on
# huggingface_hub's per-request HTTP logging and buries the four lines worth
# reading. A logger's own level is what filters, and records still reach the
# root handler, so raising these two is enough.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
app.logger.setLevel(logging.INFO)
logging.getLogger("faster_whisper").setLevel(logging.INFO)

job_queue: "queue.Queue[int]" = queue.Queue()

# What the worker is doing right now, as opposed to what the row says. A row
# sits at status='processing' for the whole of the first job after a restart,
# including the minutes spent downloading a 1.6 GB model -- so "processing" on
# its own cannot tell "working" from "downloading". Only the worker thread
# writes this; a string rebind is atomic under the GIL, so no lock is needed.
worker_phase = "idle"          # idle | loading-model | transcribing

# ----------------------------------------------------------------------------
# Database helpers (one connection per thread; WAL so reader+worker coexist)
# ----------------------------------------------------------------------------
_local = threading.local()


def db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        _local.conn = conn
    return conn


# The index is an EXTERNAL CONTENT fts5 table: it stores only the index and
# reads the text back out of `recordings` when it needs it. The column names
# must match that table's, because that is literally what fts5 selects.
#
# The first version used content='' (contentless), which cost two defects for
# one decision: snippet() returned NULL because there was no text to build a
# snippet from, and a row could not be removed at all -- "cannot DELETE from
# contentless fts5 table". External content fixes both.
#
# Triggers keep it in sync, so nothing in the app maintains the index by hand.
# The update trigger names its columns so a status change (pending ->
# processing -> done) does not re-index a 4 kB transcript three times.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE search USING fts5(
    transcript, original_name,
    content='recordings', content_rowid='id'
);

CREATE TRIGGER recordings_fts_insert AFTER INSERT ON recordings BEGIN
    INSERT INTO search(rowid, transcript, original_name)
    VALUES (new.id, new.transcript, new.original_name);
END;

CREATE TRIGGER recordings_fts_delete AFTER DELETE ON recordings BEGIN
    INSERT INTO search(search, rowid, transcript, original_name)
    VALUES ('delete', old.id, old.transcript, old.original_name);
END;

CREATE TRIGGER recordings_fts_update
AFTER UPDATE OF transcript, original_name ON recordings BEGIN
    INSERT INTO search(search, rowid, transcript, original_name)
    VALUES ('delete', old.id, old.transcript, old.original_name);
    INSERT INTO search(rowid, transcript, original_name)
    VALUES (new.id, new.transcript, new.original_name);
END;
"""


def migrate_fts(conn) -> bool:
    """Move a contentless index to external content. Idempotent."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='search'"
    ).fetchone()
    if row is not None and "content=''" not in (row[0] or ""):
        return False                      # already migrated

    if row is not None:
        app.logger.info("migrating the search index to external content...")
        conn.execute("DROP TABLE search")  # takes its shadow tables with it
    for t in ("insert", "delete", "update"):
        conn.execute(f"DROP TRIGGER IF EXISTS recordings_fts_{t}")
    conn.executescript(FTS_SCHEMA)
    # 'rebuild' re-reads every row from `recordings`, so the index is rebuilt
    # from the source of truth rather than from whatever the old one held.
    conn.execute("INSERT INTO search(search) VALUES('rebuild')")
    return row is not None


# Columns added after the first release. SQLite's ADD COLUMN is cheap, and
# doing it this way means an existing database upgrades itself.
#
# `brief_*` is a DENORMALISED COPY and is not authoritative. A brief is owned by
# the briefs service (docs/BRIEF.md in the homelab repo), because apps we did
# not write -- paperless, immich -- must be able to have briefs too, and they
# cannot grow a column. This copy exists only so the library list can show a
# TL;DR without a round trip. It may be absent or stale; never treat it as truth.
#
# `embedding` is genuinely ours. Semantic search over *this app's* transcripts is
# transcribe's own problem: FTS5 matches surface forms, so in Hebrew `פגישה`
# misses `לפגישה`. The dimension is recorded rather than assumed, because it
# belongs to whichever model produced the vector.
LATER_COLUMNS = [
    ("brief", "TEXT"),
    ("brief_model", "TEXT"),
    ("brief_at", "TEXT"),
    ("embedding", "BLOB"),
    ("embedding_model", "TEXT"),
    ("embedding_dim", "INTEGER"),
]


def add_missing_columns(conn) -> list:
    have = {r[1] for r in conn.execute("PRAGMA table_info(recordings)")}
    added = []
    for name, decl in LATER_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE recordings ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recordings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            original_name TEXT NOT NULL,
            stored_name   TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|error
            language      TEXT,
            duration      REAL,
            error         TEXT,
            created_at    TEXT NOT NULL,
            completed_at  TEXT,
            transcript    TEXT
        );
        """
    )
    added = add_missing_columns(conn)
    migrated = migrate_fts(conn)
    # Any job left mid-flight by a crash/restart goes back in the queue.
    conn.execute("UPDATE recordings SET status='pending' WHERE status='processing';")
    conn.commit()
    if added:
        app.logger.info("added columns: %s", ", ".join(added))
    if migrated:
        n = conn.execute("SELECT count(*) FROM search").fetchone()[0]
        app.logger.info("search index rebuilt: %d row(s)", n)
    conn.close()


def requeue_pending():
    for row in db().execute(
        "SELECT id FROM recordings WHERE status='pending' ORDER BY id ASC"
    ).fetchall():
        job_queue.put(row["id"])


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Transcription worker — loads the model lazily on first job, stays resident.
# ----------------------------------------------------------------------------
class Worker(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="transcribe-worker")
        self.model = None

    def load_model(self):
        global worker_phase
        if self.model is not None:
            return
        worker_phase = "loading-model"
        # Imported here so the web process starts instantly and only pulls
        # ctranslate2 + the ~1.6GB model into RAM when there's actual work.
        from faster_whisper import WhisperModel
        app.logger.info("Loading model %s (%s, %d threads)...",
                         WHISPER_MODEL, WHISPER_COMPUTE, WHISPER_THREADS)
        self.model = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type=WHISPER_COMPUTE,
            cpu_threads=WHISPER_THREADS,
        )
        app.logger.info("Model loaded.")

    def run(self):
        global worker_phase
        while True:
            rec_id = job_queue.get()
            try:
                self.process(rec_id)
            except Exception as e:  # noqa: BLE001 — never let the worker die
                app.logger.exception("Job %s failed", rec_id)
                conn = db()
                conn.execute(
                    "UPDATE recordings SET status='error', error=? WHERE id=?",
                    (str(e), rec_id),
                )
                conn.commit()
            finally:
                worker_phase = "idle"
                job_queue.task_done()

    def process(self, rec_id: int):
        conn = db()
        row = conn.execute(
            "SELECT * FROM recordings WHERE id=?", (rec_id,)
        ).fetchone()
        if row is None or row["status"] == "done":
            return

        conn.execute("UPDATE recordings SET status='processing' WHERE id=?", (rec_id,))
        conn.commit()

        self.load_model()
        global worker_phase
        worker_phase = "transcribing"
        audio_path = str(UPLOAD_DIR / row["stored_name"])
        lang = None if WHISPER_LANGUAGE == "auto" else WHISPER_LANGUAGE

        segments, info = self.model.transcribe(
            audio_path,
            language=lang,
            beam_size=WHISPER_BEAM,
            vad_filter=True,                 # trims silence — big help on phone calls
            vad_parameters={"min_silence_duration_ms": 500},
        )

        seg_list = []
        text_parts = []
        for seg in segments:                 # generator: this is where the work happens
            seg_list.append({
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            })
            text_parts.append(seg.text.strip())

        full_text = "\n".join(text_parts).strip()

        # Persist the rich version (timestamps) to disk, plain text to the DB.
        with open(TRANSCRIPT_DIR / f"{rec_id}.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "id": rec_id,
                    "original_name": row["original_name"],
                    "language": info.language,
                    "duration": info.duration,
                    "segments": seg_list,
                },
                f, ensure_ascii=False, indent=2,
            )

        conn.execute(
            """UPDATE recordings
                  SET status='done', language=?, duration=?,
                      transcript=?, completed_at=?, error=NULL
                WHERE id=?""",
            (info.language, info.duration, full_text, now_iso(), rec_id),
        )
        # No index maintenance here: the triggers on `recordings` do it, so the
        # index cannot drift from the rows just because a code path forgot.
        conn.commit()
        app.logger.info("Job %s done — %s, %.0fs of audio",
                        rec_id, info.language, info.duration or 0)


# ----------------------------------------------------------------------------
# Template context
# ----------------------------------------------------------------------------
@app.context_processor
def shell_context():
    """What the shared layout needs on every page: the settings strip and
    whether anything is working, which is what decides if the page polls."""
    try:
        active = db().execute(
            "SELECT COUNT(*) AS c FROM recordings WHERE status IN ('pending','processing')"
        ).fetchone()["c"]
    except sqlite3.Error:
        active = 0
    return {
        "active": active,
        "phase": worker_phase,
        "model_cached": MODEL_DIR.exists() and any(MODEL_DIR.iterdir()),
        "cfg": {
            "model": WHISPER_MODEL,
            "compute": WHISPER_COMPUTE,
            "language": WHISPER_LANGUAGE,
            "threads": WHISPER_THREADS,
            "beam": WHISPER_BEAM,
            "max_mb": MAX_MB,
        },
    }


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    rows = db().execute(
        "SELECT id, original_name, status, language, duration, created_at "
        "FROM recordings ORDER BY id DESC"
    ).fetchall()
    return render_template("index.html", rows=rows, model=WHISPER_MODEL)


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("audio")
    accepted = 0
    for f in files:
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            continue
        safe = secure_filename(f.filename) or f"recording{ext}"
        stored = f"{int(time.time()*1000)}_{safe}"
        f.save(UPLOAD_DIR / stored)
        conn = db()
        cur = conn.execute(
            "INSERT INTO recordings(original_name, stored_name, created_at) "
            "VALUES (?,?,?)",
            (f.filename, stored, now_iso()),
        )
        conn.commit()
        job_queue.put(cur.lastrowid)
        accepted += 1
    return redirect(url_for("index"))


@app.route("/delete/<int:rec_id>", methods=["POST"])
def delete(rec_id):
    """Remove a recording: its row, its index entry, its audio and its json.

    Only possible at all since the index became external-content -- a
    contentless fts5 table refuses DELETE outright, so until now removing one
    meant hand-editing SQLite with the fts5 'delete' command.
    """
    conn = db()
    row = conn.execute(
        "SELECT stored_name, status FROM recordings WHERE id=?", (rec_id,)
    ).fetchone()
    if row is None:
        abort(404)
    if row["status"] == "processing":
        # The worker holds this one. Deleting the row under it would leave the
        # job writing to a transcript nobody can reach.
        abort(409, "that recording is being transcribed right now")

    conn.execute("DELETE FROM recordings WHERE id=?", (rec_id,))  # trigger un-indexes
    conn.commit()
    for path in (UPLOAD_DIR / row["stored_name"], TRANSCRIPT_DIR / f"{rec_id}.json"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    app.logger.info("Recording %s deleted", rec_id)
    return redirect(url_for("index"))


@app.route("/transcript/<int:rec_id>")
def transcript(rec_id):
    row = db().execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        abort(404)
    segments = []
    jpath = TRANSCRIPT_DIR / f"{rec_id}.json"
    if jpath.exists():
        with open(jpath, encoding="utf-8") as f:
            segments = json.load(f).get("segments", [])
    return render_template("transcript.html", row=row, segments=segments)


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    results = []
    if q:
        # FTS5 MATCH; wrap the term so partial words still hit.
        try:
            rows = db().execute(
                # snippet() works now that the index is external-content: fts5
                # reads the text back out of `recordings` to build it. `head` is
                # still the fallback for a hit that matched the filename only,
                # where there is no transcript span to quote.
                """SELECT r.id, r.original_name, r.created_at,
                          snippet(search, 0, '[', ']', ' … ', 12) AS snip,
                          substr(r.transcript, 1, 240) AS head
                     FROM search s JOIN recordings r ON r.id = s.rowid
                    WHERE search MATCH ?
                    ORDER BY rank""",
                (q + "*",),
            ).fetchall()
            results = rows
        except sqlite3.OperationalError:
            results = []
    return render_template("search.html", q=q, results=results)


@app.route("/audio/<int:rec_id>")
def audio(rec_id):
    row = db().execute(
        "SELECT stored_name FROM recordings WHERE id=?", (rec_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["stored_name"])


@app.route("/api/status")
def api_status():
    rows = db().execute(
        "SELECT id, status, language, duration FROM recordings"
    ).fetchall()
    qlen = job_queue.qsize()
    return jsonify({
        "queue": qlen,
        # `phase` is what the worker is doing; an item's `status` is what the
        # row says. They differ in exactly the case that used to be invisible:
        # phase="loading-model" while the item still reads "processing".
        "phase": worker_phase,
        "model_cached": MODEL_DIR.exists() and any(MODEL_DIR.iterdir()),
        "items": {r["id"]: {
            "status": r["status"],
            "language": r["language"],
            "duration": r["duration"],
        } for r in rows},
    })


@app.route("/api/transcript/<int:rec_id>")
def api_transcript(rec_id):
    """Plain JSON — the future AI search/Q&A layer will read from here."""
    row = db().execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        abort(404)
    return jsonify({
        "id": row["id"],
        "name": row["original_name"],
        "status": row["status"],
        "language": row["language"],
        "duration": row["duration"],
        "transcript": row["transcript"],
    })


# ----------------------------------------------------------------------------
# Boot
# ----------------------------------------------------------------------------
def _shutdown(signum, _frame):
    # Without this handler the container can only ever be SIGKILLed. Python runs
    # as PID 1 here, and PID 1 ignores any signal that has no registered handler
    # — so `docker stop` sent SIGTERM, nothing happened, and after the 10s grace
    # period the kernel killed it: exit 137 every single time. Registering a
    # handler at all is what fixes that; exiting immediately is safe because
    # init_db() resets status='processing' back to 'pending' on the next boot,
    # so an interrupted job is requeued rather than lost.
    app.logger.info("Received %s — exiting.", signal.Signals(signum).name)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    init_db()
    requeue_pending()
    Worker().start()
    # use_reloader=False so the worker thread isn't duplicated.
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
