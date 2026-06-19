# Interprex

Desktop tool that auto-discovers translatable strings in game-engine files,
translates them via an LLM, and writes them back. Lightweight Tauri app.

## Architecture

```
Tauri (thin Rust shell)
├── React + TypeScript   src/          UI, LLM calls, orchestration, state
└── Python sidecar       python-core/  all engine file parsing
```

The two sides only ever exchange one shape — `TranslationString` (see
`src/lib/types.ts`). Parsers never touch the LLM; the LLM never touches engine
formats. A new engine = one new `BaseParser` subclass; a new LLM = one edit to
`src/lib/llm.ts`.

### Load-bearing pieces (don't break these)
- **`makeId()`** — stable string id = `hash(engine+file+path+original)`. The
  TS version (`src/lib/types.ts`) and Python version
  (`python-core/parsers/base.py`) MUST stay byte-identical. Verified: both emit
  `1d3f64da` for the sample input.
- **`path[]`** on every string — its address inside the file, used for
  write-back AND as part of the id.
- **`.interprex.json`** project file (versioned) saved next to the game — the
  translation memory, so nothing is translated twice.

## Running (dev)

Two long-running processes, one per terminal. Leave both up while you work.

```bash
npm run sidecar    # terminal A — Python sidecar on :8723, auto-reloads on .py edits
npm run app        # terminal B — Tauri app (first run compiles Rust: slow once, then instant)
```

The header shows "sidecar online" once it can reach the Python process.

### Fast feedback loops (don't always launch the whole app)

```bash
npm run check      # ~1s — TypeScript types + typos
npm run test:py    # ~1s — parser logic offline (detect/extract/id-stability/inject/parity)
```

Rule of thumb:
- changed **TS UI** → `npm run app` is already hot-reloading, just look.
- changed **TS types/logic** → `npm run check`.
- changed a **parser** → `npm run test:py` (the sidecar also auto-reloaded).
- only reach for the full app when you actually need to *see* it.

First `npm run app` compiles Rust (minutes, once). After that, don't close that
terminal — frontend edits hot-reload in milliseconds.

## Status

Phase 1 — RPG Maker MV/MZ end-to-end:
- [x] data contract + stable id (TS↔Python parity verified)
- [x] RPG Maker parser: extract + inject (round-trip self-tested)
- [x] sidecar (FastAPI) + IPC seam + project store + minimal UI
- [x] real LLM translation via pluggable providers (Ollama, LM Studio, Gemini),
      batched by file + glossary, picked in the UI (e2e tested against a fake
      OpenAI server)
- [ ] bundle Python as a Tauri sidecar binary so the final `.exe` auto-starts it
      (PyInstaller + `externalBin` in tauri.conf.json) — packaging step, later

## Adding an engine

1. New file `python-core/parsers/<engine>.py`, subclass `BaseParser`, set
   `engine = "<name>"`, implement `detect` / `extract` / `inject`.
2. Add the class to `REGISTRY` in `python-core/parsers/__init__.py`.
3. Add the name to the `Engine` union in `src/lib/types.ts`.

Detection and dispatch pick it up automatically.
