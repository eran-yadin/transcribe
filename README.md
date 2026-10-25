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
| `DATA_DIR` | `/data` | uploads, transcripts, SQLite db, model cache |
| `PORT` | `8011` | |

## API

| Endpoint | Returns |
|---|---|
| `GET /api/status` | `{"queue": n, "items": {id: {status, language, duration}}}` |
| `GET /api/transcript/<id>` | text, language, duration, status |
| `GET /audio/<id>` | the original audio file |

## Development

```sh
make venv     # python 3.11 via mise, then .venv
make dev      # run on :8011 with WHISPER_MODEL=tiny
make smoke    # end-to-end: upload a generated clip, assert it transcribes
make lint
```

`make dev` uses the tiny model so a transcription takes seconds. The docker path
(`make docker-dev`) mirrors what ships, and needs your user in the `docker` group.

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
