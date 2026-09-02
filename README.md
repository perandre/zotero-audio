# Zotero Audio

Zotero Audio is a local macOS pipeline that turns a Zotero PDF attachment into
TTS-friendly Markdown and an AAC-encoded M4A file without sending article text
to a service. It targets Apple Silicon Macs with 16 GB of unified memory.

The generated bundle preserves the source PDF SHA-256, Zotero key, PDF page
markers, block-to-page mapping, segment text hashes, exact TTS configuration,
per-segment audio hashes, final output hash, and QA results. Synthesis is
content-addressed: rerunning the same plan and voice reuses verified segments.

## Requirements

- macOS on Apple Silicon
- Python 3.11 or newer
- Built-in `/usr/bin/afconvert` for AAC encoding
- Local storage for the Kokoro-82M BF16 model and Python environment

## Install

```bash
REPO=/Users/pesh/Sites/zotero-audio
RUNTIME=/Users/pesh/Sites/zotero-audio-runtime
mkdir -p "$RUNTIME"
python3.12 -m venv "$RUNTIME/venv"
"$RUNTIME/venv/bin/pip" install -e "${REPO}[mlx,dev]"
```

Keep `HF_HOME`, the virtual environment, segment cache, manifests, and logs
under `RUNTIME`, outside iCloud-backed Documents:

```bash
export HF_HOME="$RUNTIME/huggingface"
mkdir -p "$HF_HOME"
```

## Run

From a direct PDF path:

```bash
"$RUNTIME/venv/bin/zotero-audio" run \
  --pdf '/path/to/article.pdf' \
  --output-root outputs
```

From a Zotero attachment key:

```bash
"$RUNTIME/venv/bin/zotero-audio" run \
  --zotero-key ABCD1234 \
  --zotero-storage "$HOME/Zotero/storage" \
  --output-root outputs
```

For a complete local Zotero library, the resumable batch runner delivers only
finished M4A files to the requested destination while retaining manifests and
segment caches under the ignored state directory:

```bash
"$RUNTIME/venv/bin/python" "$REPO/scripts/batch_library.py" \
  --zotero-storage "$HOME/Zotero/storage" \
  --destination /path/to/audio-library \
  --state-dir "$RUNTIME/full-library" \
  --log-file "$RUNTIME/full-library/batch.log"
```

The batch runner keeps one BF16 Kokoro model loaded for the run and writes
progress to the local batch log. It writes only completed M4As to the
destination; extraction, segmentation, PCM, manifests, and caches stay under
`RUNTIME`. English defaults to `af_heart`; the operational segmentation default
is 900 characters, selected from the included full-paper benchmark.
Because Kokoro-82M has no Norwegian frontend, Norwegian material is rendered
with the British-English frontend and `bf_emma`; no system TTS is used.

## Automatic library synchronization

For a zero-click workflow, `auto_sync.py` runs the batch runner incrementally
and then applies the title, author, year, and Zotero-key metadata. It scans the
whole local Zotero library, so every new PDF is processed automatically. Source
and output hashes keep unchanged items out of the synthesis queue, and a lock
prevents overlapping runs. The Kokoro model is loaded only when a new or changed
PDF needs audio.

The included macOS `launchd` job checks every five minutes and once at login:

```bash
/Users/pesh/Sites/zotero-audio/scripts/install_launchd.sh
```

The supplied plist assumes the paths used by this installation:

- Zotero storage: `/Users/pesh/Zotero/storage`
- Zotero database: `/Users/pesh/Zotero/zotero.sqlite`
- Audio destination: `/Users/pesh/Music/Zotero Audio`
- Runtime and logs: `/Users/pesh/Sites/zotero-audio-runtime/full-library`

Edit `launchd/com.pesh.zotero-audio.plist` before installation if any path is
different. The job can be inspected with:

```bash
launchctl print "gui/$(id -u)/com.pesh.zotero-audio"
tail -f /Users/pesh/Sites/zotero-audio-runtime/full-library/auto-sync.stdout.log
```

Completed files trigger a macOS notification. Failed or scanned (OCR-required)
PDFs remain in the batch manifest and trigger an action-required notification;
the next scheduled run retries them. To stop the automation, unload the plist:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.pesh.zotero-audio.plist"
```

Each stage can also be run separately:

```bash
"$RUNTIME/venv/bin/zotero-audio" prepare --pdf article.pdf --output-root "$RUNTIME/work"
"$RUNTIME/venv/bin/zotero-audio" synthesize "$RUNTIME/work/<bundle>" --engine kokoro-mlx
"$RUNTIME/venv/bin/zotero-audio" assemble "$RUNTIME/work/<bundle>" --bitrate 64000
```

The default excludes the reference list from speech. Add
`--include-references` when references are part of the intended listening copy.
Scanned PDFs with no embedded text fail explicitly; preprocess those with OCR or
Marker rather than silently producing an empty recording.

## Reproduce the Kokoro benchmark

```bash
"$RUNTIME/venv/bin/python" "$REPO/scripts/benchmark_workers.py" \
  --pdf "$HOME/Zotero/storage/8HK8HFKA/8HK8HFKA.pdf" \
  --runtime-dir "$RUNTIME" \
  --segment-chars 600 900 1200
```

This runs a whole paper with one and two persistent workers at each segment
size, records per-worker load/synthesis time and peak memory in a local JSON
result plus log, and stops a hung run at the timeout. Treat two-worker mode as
diagnostic: independent MLX processes can contend for Metal and hang under load
([MLX-Audio issue #733](https://github.com/Blaizzy/mlx-audio/issues/733)). Use
the fastest safe segment size from the result as `--max-chars`; do not assume
900 is optimal. See [the engine evaluation](docs/tts-evaluation.md) for the
quality rationale.

## Repository safety

`outputs/`, `work/`, `models/`, `.cache/`, and virtual environments are ignored.
Do not use `git add -f` on those paths: article Markdown, audio, and model weights
are intentionally local artifacts. Zotero storage is read-only to the pipeline.
