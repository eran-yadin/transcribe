#!/usr/bin/env bash
# End-to-end smoke test: start the app, upload a generated clip, assert it
# transcribes. Driven by `make smoke`, which supplies the DATA_DIR/WHISPER_*
# environment. The first run downloads the tiny model (~75MB) and needs
# internet once; after that it is offline.
set -euo pipefail

PY="${PY:-.venv/bin/python}"
PORT="${PORT:-8011}"
DATA_DIR="${DATA_DIR:-.devdata}"
BASE="http://127.0.0.1:${PORT}"
TMP="$(mktemp -d)"
APP_PID=""

cleanup() {
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok — $*"; }

command -v curl >/dev/null || fail "curl not found"
command -v "$PY" >/dev/null || fail "no python at $PY — run 'make venv'"

mkdir -p "$DATA_DIR"
echo "starting app on :$PORT (data in $DATA_DIR)"
"$PY" app.py >"$TMP/app.log" 2>&1 &
APP_PID=$!

for _ in $(seq 1 60); do
    curl -fsS "$BASE/api/status" >/dev/null 2>&1 && break
    kill -0 "$APP_PID" 2>/dev/null || { cat "$TMP/app.log"; fail "app exited during startup"; }
    sleep 1
done
curl -fsS "$BASE/api/status" >/dev/null || { cat "$TMP/app.log"; fail "app never answered /api/status"; }
ok "app is up"

for path in / /search; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")
    [ "$code" = "200" ] || fail "GET $path returned $code"
done
ok "GET / and /search return 200"

# A 3-second tone, written with the stdlib so the test needs no ffmpeg.
"$PY" - "$TMP/clip.wav" <<'PYEOF'
import math, struct, sys, wave
rate, secs = 16000, 3
with wave.open(sys.argv[1], "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 220 * t / rate)))
        for t in range(rate * secs)))
PYEOF
ok "generated a 3s test clip"

curl -fsS -F "audio=@$TMP/clip.wav" "$BASE/upload" -o /dev/null
ok "uploaded"

echo "waiting for transcription (first run downloads the tiny model)"
for i in $(seq 1 300); do
    status=$("$PY" -c "
import json, urllib.request
items = json.load(urllib.request.urlopen('$BASE/api/status'))['items']
print(items[min(items, key=int)]['status'] if items else 'none')
" 2>/dev/null || echo err)
    case "$status" in
        done)  ok "transcribed after ${i}s"; break ;;
        error) cat "$TMP/app.log"; fail "job reported status=error" ;;
        none)  fail "upload produced no row" ;;
    esac
    kill -0 "$APP_PID" 2>/dev/null || { cat "$TMP/app.log"; fail "app died while transcribing"; }
    [ "$i" = 300 ] && { cat "$TMP/app.log"; fail "timed out waiting for transcription"; }
    sleep 1
done

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/transcript/1")
[ "$code" = "200" ] || fail "GET /transcript/1 returned $code"
ok "transcript page renders"

# The handler added in app.py is what makes this exit promptly instead of
# needing SIGKILL. If this hangs, that regressed.
kill -TERM "$APP_PID"
for _ in $(seq 1 10); do
    kill -0 "$APP_PID" 2>/dev/null || break
    sleep 1
done
kill -0 "$APP_PID" 2>/dev/null && fail "did not exit on SIGTERM (the PID-1 bug is back)"
APP_PID=""
ok "exits on SIGTERM"

echo "SMOKE PASSED"
