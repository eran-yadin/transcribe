# Transcribe

Self-hosted audio → text. Upload phone calls and meetings, get searchable
transcripts. Hebrew-first: the default model is
[`ivrit-ai/whisper-large-v3-turbo-ct2`](https://huggingface.co/ivrit-ai/whisper-large-v3-turbo-ct2),
the Hebrew ASR community's current best, already published in faster-whisper /
CTranslate2 format. It is multilingual, so English mixed into Hebrew recordings
transcribes fine.

Runs on a CPU. It was built for a 2-core i3 NUC alongside other services, so it
transcribes **one job at a time** with a capped thread count rather than
saturating the box.

## Install

```sh
mkdir transcribe && cd transcribe
curl -O https://raw.githubusercontent.com/eran-yadin/transcribe/main/compose.yml
docker compose up -d
```

Then open <http://localhost:8011>.

The model (~1.6 GB) downloads on the **first transcription**, not at startup, and
is cached in the data volume — so it needs internet once and is offline after
that. Don't delete the volume.

## Configuration

All via environment variables; defaults suit a small CPU-only box.

| Var | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `ivrit-ai/whisper-large-v3-turbo-ct2` | `large-v3-turbo` for English-dominant audio, `tiny` for testing |
| `WHISPER_COMPUTE` | `int8` | `int8_float32` is slightly more accurate, more RAM |
| `WHISPER_LANGUAGE` | `auto` | `auto` / `he` / `en` |
| `WHISPER_THREADS` | `3` | leave the host a core |
| `WHISPER_BEAM` | `5` | lower is faster, slightly less accurate |
| `MAX_UPLOAD_MB` | `300` | upload size cap |
| `DATA_DIR` | `/data` | uploads, transcripts, SQLite db |
| `MODEL_DIR` | `$DATA_DIR/models` | the model cache. Point it at its own volume: it is ~1.5 GB of re-downloadable files, and a backup of `DATA_DIR` should not have to carry them |
| `PORT` | `8011` | |

## API

| Endpoint | Returns |
|---|---|
| `GET /api/status` | `{"queue": n, "phase": …, "model_cached": …, "items": {id: {status, language, duration}}}`. `phase` is `idle` / `loading-model` / `transcribing` — what the worker is doing, as distinct from what a row says |
| `GET /api/transcript/<id>` | `id`, `name`, `status`, `language`, `duration`, `transcript` |
| `GET /audio/<id>` | the original audio file |
| `POST /delete/<id>` | removes the row, its index entry, its audio and its json. Refuses with 409 while that recording is being transcribed |

## Development

```sh
make venv     # python 3.11 via mise, then .venv
make dev      # run on :8011 with WHISPER_MODEL=tiny
make test     # smoke + fts
make smoke    # end-to-end: upload a generated clip, assert it transcribes
make fts      # the search-index migration, snippet() and delete
make lint
```

`make dev` uses the tiny model so a transcription takes seconds. The docker path
(`make docker-dev`) mirrors what ships, and needs your user in the `docker` group.

## How it stores things

```
$DATA_DIR/transcribe.sqlite      rows, full text, and the search index
$DATA_DIR/uploads/<ms>_<name>    the original audio, untouched
$DATA_DIR/transcripts/<id>.json  timestamped segments
$MODEL_DIR/                      the model cache (HF_HOME points here)
```

One row per recording in `recordings`; the plain transcript lives in the row
because that is what search and the API need, while the per-segment timestamps
live in the JSON because they are only needed when you open one recording.

`search` is an **external-content** FTS5 table (`content='recordings'`), kept in
step by triggers. It was contentless until v0.4.0, which cost two defects for
one decision: `snippet()` returned NULL, and a row could not be deleted at all.

`recordings` also carries `brief*` and `embedding*` columns, both unused so far.
The brief ones are a denormalised copy for display — a brief is owned by a
separate service, so that apps nobody here wrote can have briefs too. The
embedding is this app's own, for semantic search over its transcripts, since
FTS5 matches surface forms and Hebrew inflects heavily.

## Notes

- **No authentication.** Put it on a private network — a LAN or a tailnet — not
  the public internet.
- **Single process by design.** The worker is an in-process thread. Do not run
  this under gunicorn with multiple workers: it would load the model once per
  worker and transcribe N files at once. Use `--workers 1` or `waitress` if you
  need a production server.
- **Long silences** can make Whisper loop or repeat. `vad_filter` is on to trim
  them; on a noisy call, try `WHISPER_BEAM=1`.
- **RAM:** the model is ~1.6–2 GB resident while loaded.

## License

MIT
