#!/usr/bin/env bash
# Prove the fts migration on a copy of the REAL contentless database: that it
# migrates, that snippet() stops returning NULL, and that a recording can be
# deleted -- which the old schema refused outright.
set -euo pipefail
cd /home/eran/Work/coding/transcribe

PY=.venv/bin/python
D=.devdata-fts
PORT=8014
BASE="http://127.0.0.1:$PORT"
LOG=/tmp/claude-1000/-home-eran-Work/02e56a3f-5b49-4f35-b6d8-595221ee9866/scratchpad/fts-test.log
APP_PID=""

cleanup() { [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true; rm -rf "$D"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok — $*"; }

# Build a database with the OLD contentless schema, so the migration has
# something real to migrate. This is what shipped in v0.1.0 - v0.3.0.
rm -rf "$D"; mkdir -p "$D/uploads" "$D/transcripts"
$PY - "$D" <<'EOF'
import json, pathlib, sqlite3, sys
d = pathlib.Path(sys.argv[1])
(d / "uploads" / "fixture.mp3").write_bytes(b"not really audio")
(d / "transcripts" / "1.json").write_text(json.dumps(
    {"id": 1, "original_name": "fixture.mp3", "language": "he", "duration": 12.0,
     "segments": [{"start": 0.0, "end": 2.0, "text": "shalom olam"}]}))
c = sqlite3.connect(d / "transcribe.sqlite")
c.executescript("""
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    language TEXT, duration REAL, error TEXT, created_at TEXT NOT NULL,
    completed_at TEXT, transcript TEXT);
CREATE VIRTUAL TABLE search USING fts5(transcript, name, content='');
""")
c.execute("INSERT INTO recordings(id, original_name, stored_name, status, language,"
          " duration, created_at, completed_at, transcript) VALUES"
          " (1,'fixture.mp3','fixture.mp3','done','he',12.0,'2026-01-01T00:00:00',"
          "  '2026-01-01T00:01:00','shalom olam pgisha machar beseder gamur')")
c.execute("INSERT INTO search(rowid, transcript, name) VALUES"
          " (1,'shalom olam pgisha machar beseder gamur','fixture.mp3')")
c.commit()
EOF

echo "=== before: the real database as it ships today"
$PY - "$D/transcribe.sqlite" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
sql = c.execute("select sql from sqlite_master where name='search'").fetchone()[0]
assert "content=''" in sql, "expected the contentless schema"
print("  contentless index confirmed:", " ".join(sql.split())[:72])
try:
    c.execute("delete from search where rowid=1")
    print("  DELETE unexpectedly allowed")
except sqlite3.OperationalError as e:
    print("  DELETE refused, as expected:", e)
EOF

echo "=== refusing to run against a busy port"
if curl -fsS --max-time 2 "$BASE/api/status" >/dev/null 2>&1; then
    fail "something already answers on :$PORT"
fi

echo "=== starting the app (migration runs at init)"
DATA_DIR=$D WHISPER_MODEL=tiny WHISPER_THREADS=2 PORT=$PORT $PY app.py > "$LOG" 2>&1 &
APP_PID=$!
for i in $(seq 1 40); do curl -fsS "$BASE/api/status" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "$BASE/api/status" >/dev/null || { cat "$LOG"; fail "app never came up"; }
grep -q "migrating the search index" "$LOG" || fail "migration did not run (or did not log)"
ok "migration ran: $(grep -m1 'index rebuilt' "$LOG" | sed 's/.*INFO //')"

echo "=== after: schema, triggers, index"
$PY - "$D/transcribe.sqlite" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
sql = c.execute("select sql from sqlite_master where name='search'").fetchone()[0]
assert "content='recordings'" in sql and "content_rowid='id'" in sql, sql
print("  external content:", " ".join(sql.split())[:78])
trg = [r[0] for r in c.execute(
    "select name from sqlite_master where type='trigger' order by name")]
assert len(trg) == 3, trg
print("  triggers:", ", ".join(trg))
rows = c.execute("select count(*) from recordings").fetchone()[0]
idx  = c.execute("select count(*) from search").fetchone()[0]
assert rows == idx, f"{rows} rows but {idx} indexed"
print(f"  rows {rows} == indexed {idx}")
EOF

echo "=== snippet() actually returns text now"
WORD=pgis
$PY - "$D/transcribe.sqlite" "$WORD" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
r = c.execute("""SELECT snippet(search, 0, '[', ']', ' … ', 12) AS snip
                   FROM search s JOIN recordings r ON r.id = s.rowid
                  WHERE search MATCH ? ORDER BY rank""",
              (sys.argv[2] + "*",)).fetchone()
assert r is not None, "no match at all"
assert r["snip"], "snippet() is still NULL - the migration did not take"
assert "[" in r["snip"], f"no highlight markers in: {r['snip']!r}"
print(f"  snippet: {len(r['snip'])} chars, highlighted")
EOF
code=$(curl -s -o /dev/null -w '%{http_code}' --get --data-urlencode "q=$WORD" "$BASE/search")
[ "$code" = "200" ] && ok "search page renders the result" || fail "/search returned $code"

echo "=== delete, which the old schema made impossible"
[ -f "$D/transcripts/1.json" ] || fail "fixture json missing"
MP3=fixture.mp3
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/delete/1")
[ "$code" = "302" ] || fail "POST /delete/1 returned $code, expected a redirect"
ok "delete returned 302"
$PY - "$D/transcribe.sqlite" "$WORD" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("select count(*) from recordings").fetchone()[0] == 0, "row survived"
assert c.execute("select count(*) from search").fetchone()[0] == 0, "index entry survived"
hits = c.execute("select rowid from search where search match ?",
                 (sys.argv[2] + "*",)).fetchall()
assert not hits, f"deleted row still matches searches: {hits}"
print("  row gone, index entry gone, no longer matches a search")
EOF
[ -f "$D/transcripts/1.json" ] && fail "json left behind" || ok "json removed"
[ -f "$D/uploads/$MP3" ] && fail "audio left behind" || ok "audio removed"
grep -q "Recording 1 deleted" "$LOG" || fail "delete was not logged"
ok "logged the deletion"

echo "=== a second start must NOT re-migrate"
kill "$APP_PID"; sleep 2; APP_PID=""
DATA_DIR=$D WHISPER_MODEL=tiny PORT=$PORT $PY app.py > "$LOG.2" 2>&1 &
APP_PID=$!
for i in $(seq 1 40); do curl -fsS "$BASE/api/status" >/dev/null 2>&1 && break; sleep 1; done
grep -q "migrating the search index" "$LOG.2" && fail "migrated twice - not idempotent"
ok "idempotent: second start left the schema alone"

echo "FTS MIGRATION PROVEN"
