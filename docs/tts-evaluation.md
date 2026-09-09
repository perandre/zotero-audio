# Kokoro engine evaluation

Evaluation date: 2026-09-01. Target: MacBook Air M2 with 16 GB unified memory.

## Decision

The default is **Kokoro-82M BF16 through MLX-Audio** using `am_michael` for
English. It keeps the neural model at BF16 precision, runs locally on Apple
Silicon, and is faster on the target machine than the quantized ONNX path.
Only the Kokoro engines are supported; selecting an unknown engine fails.

The target-machine measurements below predate this default-voice change and
retain `af_heart` as the benchmark voice; they describe backend performance,
not the current voice preference.

Kokoro-82M does not provide a Norwegian language frontend. For Norwegian source
material the pipeline uses Kokoro's British-English frontend and `bf_emma`, so
proper nouns and Norwegian phonemes may be imperfect. This limitation is made
explicit in the run manifest rather than silently switching engines.

## Primary-source review

- Kokoro's upstream model card describes an 82-million-parameter model under
  Apache-2.0. Source: [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M).
- MLX-Audio is optimized for Apple Silicon, supports BF16 Kokoro checkpoints,
  and identifies Kokoro as its fastest and smallest TTS option. Source:
  [MLX-Audio](https://github.com/Blaizzy/mlx-audio).
- `kokoro-onnx` remains available as an explicit compatibility backend. Its
  quantized runtime is practical but loses precision relative to BF16. Source:
  [`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx).
- MLX-Audio 0.4.4 had a documented Kokoro regression while 0.4.1 remained
  functional, so the environment pins 0.4.1 until that path is revalidated.
  Source: [MLX-Audio issue #784](https://github.com/Blaizzy/mlx-audio/issues/784).

## Target-machine measurements

The same fixed 86-word passage was measured in fresh local processes.

| Kokoro path | Precision | Generation | Audio | Real-time factor | Peak RSS |
| --- | --- | ---: | ---: | ---: | ---: |
| MLX-Audio / `af_heart` | BF16 | 18.14 s | 37.00 s | 0.490 | 527 MiB |
| ONNX / `af_heart` | INT8 | 23.84 s | 38.74 s | 0.616 | 574 MiB |

Both paths generate faster than playback and fit comfortably in 16 GB. BF16
MLX was selected because it preserves model precision while also improving
throughput. The segment cache records text, voice, model, configuration, and
audio hashes; fresh generation is not assumed to be sample-identical.

## Full-paper worker benchmark

On 2026-09-02, the complete 8HK8HFKA paper was rendered with the persistent
BF16 model in the external Sites runtime. One worker was fastest at the 900
character segmentation setting; two workers added Metal contention and were
slower at the two larger settings.

| Segmentation | One worker | Two workers | Decision |
| --- | ---: | ---: | --- |
| 600 chars | 349.8 s | 279.8 s | not selected |
| 900 chars | **282.8 s** | 328.7 s | **operational default, one worker** |
| 1200 chars | 293.0 s | 319.7 s | not selected |

The raw result and local progress log are retained under
`/Users/pesh/Sites/zotero-audio-runtime/benchmarks/`. Two-worker mode remains a
diagnostic benchmark only because independent MLX processes can contend for
Metal and hang under load ([MLX-Audio issue #733](https://github.com/Blaizzy/mlx-audio/issues/733)).
