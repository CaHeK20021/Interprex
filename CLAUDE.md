# Interprex — CLAUDE.md

Desktop tool that **auto-discovers translatable strings in game-engine files,
translates them via an LLM, and writes them back**. Must stay a lightweight exe.

When working in this repo, read this file first, then the relevant module. Keep
this file current when architecture or plans change.

## Stack

```
Tauri (thin Rust shell)         src-tauri/    window + launches the sidecar
├── React + TypeScript          src/          UI, orchestration, state
└── Python sidecar (FastAPI)    python-core/   ALL engine parsing + LLM calls
```

- Frontend talks to the sidecar over localhost HTTP (`:8723`) through ONE seam:
  `callPython()` in `src/lib/ipc.ts`. Never `fetch` the sidecar elsewhere.
- LLM calls happen IN THE SIDECAR (keeps API keys out of the browser, no cloud
  CORS). The frontend's `src/lib/llm.ts` is a thin pass-through to it.

## Load-bearing invariants (break these and saved work is lost)

1. **Stable id** = `hash(engine + file + path + original)`, FNV-1a. The TS
   (`src/lib/types.ts` `makeId`) and Python (`python-core/parsers/base.py`
   `make_id`) versions MUST stay byte-identical — both emit `1ae1e0a2` for the
   sample input. Never use array index / line number as id.
2. **`path[]`** on every string is its address inside the file: used for
   write-back AND as part of the id. Don't drop it or replace with a line no.
3. **Project file** `Interprex/project.json` (in the game folder) is versioned
   (`version: 1`). On a breaking change, bump + migrate; never read an unknown
   version. All Interprex data — project + the two Ren'Py inline caches
   (`classify_cache.json`, `python_translations.json`) — lives in the
   `Interprex/` folder (const `INTERPREX_DIR`, defined in BOTH `src/lib/types.ts`
   and `python-core/parsers/base.py` — keep in sync), NOT as root dotfiles. The
   old `.interprex*.json` dotfile layout (pre-2026-06) is NOT migrated. Caches are
   intentionally KEPT on backup restore (only translation output is reverted).
4. **Three-stage wall**: parser ⇄ `TranslationString` ⇄ LLM. Parsers know
   nothing about LLMs; the LLM knows nothing about engine formats. A new engine
   never touches translation code; a new provider never touches parsers.

## Layout

| Path | Role |
|---|---|
| `src/lib/types.ts` | `TranslationString`, `ProjectFile`, `makeId` — the data contract |
| `src/lib/ipc.ts` | the only transport seam to the sidecar |
| `src/lib/llm.ts` | provider list + `translateBatch` (pass-through to sidecar) |
| `src/lib/project.ts` | load/save `.interprex.json`, translation-memory merge |
| `src/lib/settings.ts` | persisted prefs (localStorage): UI lang, target lang, provider config, max context |
| `src/i18n/` | UI localization: `en.ts`/`ru.ts` (same keys, enforced by `Strings` type), `index.ts` store + `t()` |
| `python-core/main.py` | sidecar endpoints: ping, detect, extract, inject, providers, translate, validate, autofix, backup/*, fs/* |
| `python-core/parsers/` | `base.py` (`BaseParser`, `make_id`, `read_backup_original`), per-engine modules (`rpgmaker`, `renpy`, `csharp`, `unity`, `i18n`, `fusion`, `mmf2`, `qsp`, `unreal`, `unreal4`), container readers (`rpa` for Ren'Py archives, `pak` for UE4/5), `__init__.py` registry |
| `python-core/providers/` | `base.py` (provider ABC, prompt, parse, **Calibrator**, batching), `openai_compat.py` (Ollama + LM Studio), `gemini.py`, registry |
| `python-core/validators/` | post-translation validators (`get_validator(engine)`); `renpy.py` `ast.parse`-checks `python:`/`$` blocks — catches a broken translated literal WITHOUT the `renpy` binary (works on a Steam player's machine) |

## Auto-update (tauri-plugin-updater)

Built-in updater checks GitHub releases on launch (`UpdateOverlay.tsx`):
- **Endpoint**: `https://api.github.com/repos/CaHeK20021/interprex/releases/latest`
- **Flow**: `check()` → `downloadAndInstall()` (NSIS installer) → `relaunch()`
- **Signing**: `TAURI_SIGNING_PRIVATE_KEY` + password in GitHub Secrets
- **Release pipeline**: push `v*` tag → `.github/workflows/release.yml` → builds sidecar + NSIS + signs + uploads to GitHub release with `latest.json`
- **UX**: overlay with spinner + progress bar (MB downloaded), blocks window close during download
- **Single-instance**: `tauri-plugin-single-instance` prevents duplicate windows; second launch focuses existing
- **Plugins chain** (lib.rs): `opener → dialog → fs → process → updater → single-instance`
- **Close blocking**: `updateBusyRef` in App.tsx prevents user from closing during download

## Two separate language axes (do not conflate)

- **UI language** — language of Interprex itself (`src/i18n/`, en + ru).
- **Target language** — language games are translated INTO (`TARGET_LANGS` in
  `src/i18n/index.ts`, passed to the LLM).

## Token budgeting (why it's the way it is)

Small-VRAM local models (8 GB) run tight context windows (2k–4k). An overflowing
prompt is **silently truncated** — strings vanish, no error. So:

- Batches are **packed to fit the window**, not a fixed count.
- Output is reserved for by a **language-aware ratio** (Cyrillic/CJK tokenize
  denser → bigger reserve) — the translation doesn't exist yet at pack time, so
  it can only be reserved for, never measured.
- **`Calibrator`** (`providers/base.py`) learns each model's real
  `chars_per_token` and `output_ratio` from the **exact `usage`** every provider
  returns after each batch (OpenAI `usage`, Gemini `usageMetadata`). First batch
  uses language defaults; from the 2nd it's sized to the model's own tokenizer.
  No model-name→tokenizer table to maintain.
- Ollama also gets `num_ctx` (smaller window = less KV-cache VRAM) and an
  optional exact `/api/tokenize` pre-send guard. All paths **degrade gracefully**
  to the heuristic if a tokenizer/usage is unavailable.

## UI-width fitting for menu choices (Ren'Py) — `parsers/renpy.py` + `scheduler.py`

Ren'Py choice/menu captions live in fixed-width buttons; a too-long translation
overflows and **breaks the UI**. We never truncate — the goal is to make the
**model itself** return text that fits. Pixels are the ground truth; char counts
are only a hint we hand the model.

- **Budget = the ORIGINAL's rendered width.** `measure_original_px()` measures
  the widest line (style tags `{...}` stripped) of the source in the GAME's font
  (resolved from `gui.rpy` via `get_source_font_and_size`). That width is the
  hard limit the translation must fit into.
- **Char hint** (`get_char_limit`): converts that pixel width into an approximate
  character count using a **frequency-weighted average glyph width** for the
  target script (`_avg_char_width`) — letters weighted together, the narrow
  space blended in at its real prose frequency (`_SPACE_FRACTION`, ~16% Latin/
  Cyrillic, ~0% CJK). This is a HINT only; LLMs can't reliably count to N.
- **Prompt anchors on the original, not an abstract number** (`scheduler.py`
  prep): the model is told "this is a fixed-width caption, the original is X
  chars, fit the same width (~N), rephrase shorter if needed" — it reasons about
  matching a string it can see far better than counting characters.
- **Overflow check is PIXEL-based** (`_overflows` in `scheduler.py`): the
  candidate is re-measured in the **target-script** font (`measure_translation_px`,
  tags stripped) against the original's width, tolerance `1.03` (kerning slack
  only — the old `1.1` was compensating for the dropped `len()`/avg approximation).
  On overflow we **re-ask the model** with a tighter instruction; the final text
  is ALWAYS the model's, never cut.
- **Language normalization is load-bearing** (`_normalize_lang`): the UI sends
  DISPLAY names (`"Russian"`, `"Chinese (Simplified)"`, `"Portuguese (Brazil)"`)
  but the per-script tables (`ALPHABET_SAMPLES`, `LANG_FONTS`, `_SPACE_FRACTION`)
  key on bare lowercase script names. Without normalization every non-English
  target silently fell back to the English sample + Latin font — catastrophic for
  CJK (wrong glyphs AND wrong widths). Region variants resolve via aliases or the
  part before `(`. The target font is our bundled Noto for the script, matching
  what inject swaps the game font to.
- **Verified** by `check_char_limit` (`selftest.py`): all 9 target languages
  normalize to the right script + font (display name yields the same limit as the
  bare key → no English fallback), CJK limits differ from Latin (proving the CJK
  font loaded), pixel ground-truth (wide translation measures wider, short fits),
  tags excluded from both measure sides, empty/unknown inputs degrade.

## Text fitting: shrink the FONT, NEVER abbreviate a word (Ren'Py)

**⚠️ BUILD TRAP — PIL/Pillow MUST be bundled in the sidecar.** ALL Ren'Py
text-fitting measurement (`_line_height`, `_wrapped_line_count`, `fit_scale_for_box`,
`measure_*_px`) uses PIL, and every one DEGRADES GRACEFULLY to "don't shrink" if PIL
import fails. So if `python-core/sidecar.spec` lists `"PIL"` in `excludes` (it used to),
the built exe has no PIL → box-fit silently does NOTHING in the shipped app, while it
works fine in `npm run sidecar` (dev venv has PIL). Symptom: "autosize works when I run
it in dev but the build doesn't shrink anything." Pillow is in `requirements.txt` and
MUST stay OUT of the spec `excludes`. ALSO: a stale `sidecar.exe` from a previous run
can keep owning port 8723 (two `sidecar.exe` in tasklist) so the app talks to OLD code —
`taskkill /F /IM sidecar.exe` before testing a rebuild.

**Load-bearing user rule:** a translated word must never be butchered to fit a box —
"Сохранение" must stay "Сохранение", not become "Сох". We fit by shrinking the
FONT and keeping the word whole. Two mechanisms, both preserve the full text:

1. **Buttons / menu captions** (`scheduler.py::_enforce_char_limits`): a word-count
   gate `_MAX_WORDS_FONT_FIT = 2`. A translation of **≤2 words is NEVER re-asked
   "shorter"** (re-asking only yields "Сох") — instead `_record_font_shrink` records
   a shrink factor into `size_overrides` (floor `_FONT_SHRINK_FLOOR = 0.6`), consumed
   by inject as a `choice_button` style shrink. Only **3+ word** captions may be
   re-asked (a synonym can shorten honestly). The prompt (`providers/base.py`) also
   forbids abbreviating a single word and permits rephrasing only for 3+ words.
   The earlier blanket "REPHRASE shorter / abbreviate" prompt was the "Сох" cause.

2. **Dialogue (say lines)** (`parsers/renpy.py::_fit_dialogue` + `fit_scale_for_box`):
   if `gui.rpy` declares a **FIXED** box (`dialogue_width` AND `textbox_height` both
   present as numbers — read via `parse_gui_rpy`, **last-wins** like the engine: Watch
   the Road redefines `textbox_height` 278→360, both we and the engine take 360) and
   the full translation would overflow it (greedy word-wrap height measure,
   `_wrapped_line_count`, line-height `_LINE_HEIGHT_MULT`), wrap the text in the
   engine-native `{size=*scale}…{/size}` so Ren'Py renders the WHOLE text smaller.
   Floor `_FIT_FONT_FLOOR = 0.6`. **CONTRACT: where the box is UNKNOWN (no gui
   numbers — chat/scroll/auto-grow windows like Killer Chat), DON'T TOUCH the text** —
   long messages there are normal. The `{size=*}` tag goes only in displayed text
   (`new`/say body), never in `old`/identifier, so it can't affect hashes or
   code comparisons.

3. **Screen captions (`text` / `_()` / `label` / `textbutton`)** (`parsers/renpy.py::
   _parse_style_boxes` + `_screen_widget_boxes`, applied via `_fit_dialogue` in the
   inject `string` branch): a `string`-kind caption that lives in a **fixed box** used
   to get NO fitting — only say lines (2) and menu choices got it — so a multi-line
   screen button overflowed unchecked. Real Killer Chat bug: `error_screen.rpy:117`
   `textbutton _("THE RIGHT BUTTON?\nOR THE RIGHT BUTTON?")` in a 350×81 box — the
   wider RU line wrapped to a 3rd line and **clipped at the top**. Fix: resolve each
   screen widget's box (inline `xsize/ysize/xysize` wins, else its `style_prefix`'s
   `<prefix>_button`/`<prefix>` style box, with **one-level `is` inheritance** so a
   child that only adds geometry still inherits the parent's box), then box-fit the
   SAME way as dialogue: `{size=*scale}` so the engine renders the WHOLE caption
   smaller. Point-fix per widget (a fitting sibling button is left untouched — better
   than shrinking the whole style). **Same CONTRACT: box must be KNOWN on BOTH axes
   (width AND height); unknown → widget is absent from the map → text untouched**
   (auto-grow chat captions like "is typing…" never get a box, stay full size). The
   `style_prefix` scope follows the engine: it applies to its SIBLINGS at its own
   indent, so the pop rule is **strict dedent** (a same-indent later prefix replaces
   it) — otherwise the textbutton next to the prefix loses it.

**Predicting the line count EXACTLY (why the box-fit is trustworthy).** Whether a
caption needs shrinking comes down to *how many lines the engine wraps it to* — get
that wrong by one and we either over-shrink or still clip. So the offline measure
mirrors the engine's real layout (`renpy/text/text.py::Layout`), not an approximation:
- **Line height** = the font's FreeType `ascent + descent` (`_line_height`, via PIL
  `getmetrics()` — the SAME metrics Ren'Py's `place_vertical` stacks by), NOT the old
  `font_size * 1.25` (which under-counted height ~12% and was the clip cause). Plus the
  style's `line_spacing` + `line_leading` (`_parse_style_line_spacing`), which DEFAULT
  0 but can be **negative** (a tightened line box, e.g. `line_spacing -8`) — the engine
  adds them per line, so we add them too (clamping only the FINAL height to ≥1), and
  they scale WITH the `{size=*}` factor.
- **Line count** = `_wrapped_line_count` emulates the engine's DEFAULT `style.layout =
  "tex"` (Knuth-Plass optimal break, `_tex_line_count`), not pure greedy. On the 199
  real Killer Chat captions greedy and tex agree on the count, but tex can place a
  break greedy wouldn't, and the engine uses tex — so we compute the true optimum and
  never under-count. An over-wide single word stands alone (the engine can't break
  inside a word either), so it never loops.
- **Wrap width** = the box's INNER text width = `box_w − xpadding` (`_parse_style_boxes`
  also returns `_style_box_padding`). We do NOT subtract a `Frame(img, 8, 8)`
  background's border — those numbers are 9-slice image scaling, not a text inset
  (verified in `display/imagelike.py`). Padding defaults 0, so usually a no-op.

Most dialogue boxes auto-grow or scroll, so a true fixed-box overflow is rare — a
runtime per-frame auto-fit was considered and **rejected** as too risky for too
little gain; the offline `{size=*}` approach covers the real cases with zero
runtime cost. Verified end-to-end: real inject on Watch the Road (box 1650×360) wraps
a 750-char RU line in `{size=*0.65}` (full text intact), leaves a 292-char line
untouched; Killer Chat (no gui box) → dialogue fit OFF, but the fixed-box screen
button (350×81) wraps `{size=*0.8}` (full RU text intact) while its fitting siblings
stay full size. Offline guards: `check_renpy` dialogue-auto-fit + menu-choice-fit +
**screen-caption box-fit** (incl. padding + negative line_spacing) + **wrap-measure**
(real-metric line height, tex line count) cases + scheduler 1-word-no-reask case in
`selftest.py`.

## Engine-oracle lint — validate our `tl/` with the game's OWN Ren'Py

Every shipped Ren'Py game bundles its engine + a launcher exe. `<Game>.exe <basedir>
lint` makes the ENGINE validate the WHOLE project INCLUDING our injected
`tl/<lang>/` — same "engine oracle" stance as translate-id verification. Turns "will
our injection break the game?" into a measured fact, catching defects static
validators miss. `parsers/renpy.py::lint_with_engine` (endpoint `/renpy/lint`, step 5
of `handleTranslate`, renpy non-mods; surfaced in the UI when `actionable_count > 0`):

- `_find_engine_exe`: the `<Name>.exe` with a paired `<Name>.py` bootstrap.
- lint **exits 0 even with findings** → parse stdout/`lint.txt`, never the exit code.
- Splits OURS (`tl/<lang>/`, normalized via `_lang_dir`) from pre-existing game
  findings; `_lint_is_actionable` filters benign tl/-lint noise ("Could not evaluate
  X in the who part" = runtime var) from REAL hazards (text-tag mismatch, %-format
  error, dup key). On Killer Chat: 81 ours → only 3 actionable.
- Degrades gracefully (`available=False`) without the bundled SDK.

This is how we found the **%-format bug**: Ren'Py runs displayed text through
`%`-substitution (`config.old_substitutions`, default ON), so `%s`/`%(name)s` are
format specs and a literal `%` must be `%%`. The LLM drops the escaping
(`100%% ASSIGNMENT` → `НА 100%`) → "Unterminated string format code", a crash-class
error. Fixed deterministically (no API) by `_escape_bad_percent` (mirrors the engine's
`lint.py` state machine: escapes only a `%` beginning an invalid/unterminated spec,
leaves valid specs and `%%` byte-verbatim, idempotent), applied at inject to `tr` AND
to inline-Python `new` strings (never `old`). Verified: engine lint 3→0 actionable on
Killer Chat; `check_renpy` %-format case in `selftest.py`.

**Stray-newline fixup** (`_match_newlines`, applied at inject right after
`_repair_text_tags`): the LLM often collapses a 2-line source to one line but leaves
the trailing `\n` (`"I DIDN'T SIGN UP\nFOR THIS"` → `"Я НА ЭТО НЕ ПОДПИСЫВАЛСЯ\n"`).
That phantom empty line makes a fixed-height button reserve a SECOND line, so the
engine centres the visible text against two lines and it sits jammed at the TOP (real
Killer Chat bug) — AND it falsely triggers the box-fit `{size=*}` shrink. The fix
strips ONLY leading/trailing fully-EMPTY lines the original itself lacks (interior
paragraph blanks and any content line are byte-verbatim; if the original ends with a
newline the translation's is kept), compared against the UNESCAPED original,
idempotent. After it: the SIGN UP button is one centred line with no shrink; the
genuinely-2-line RIGHT BUTTON still gets `{size=*0.8}`. Guard: `check_renpy` newline-
match case in `selftest.py`.

## Overflow risk analyzer (`parsers/renpy_risk.py`, endpoint `/renpy/risk`)

Data-driven verdict (READ-ONLY, no engine run) on whether a game can overflow:
reads say-line lengths + `{p}/{w}/{nw}`, custom-vs-stock say screen, fixed vs
None/expr `textbox_height`, `calculate_dialogue_height`-style auto-height, and
viewport/vpgrid **scoped to the say-screen body** (a viewport in another screen ≠
dialogue scroll). Verdict `dialogue_overflow_risk: none|low|high`. Surfaced in the
UI (banner) only for high/low (silent on none). Key finding: overflow is mostly a
non-problem — Killer Chat → none (auto-grows), Watch the Road → high (fixed 278/360 +
97 long lines). Verified `check_renpy_risk` in `selftest.py` + both real games.

## Parallel translation scheduler (`python-core/scheduler.py`)

`TranslationScheduler` is the worker pool behind one `/translate` run (the old
inline Gemini-only loop in `main.py` is gone — `_translate_stream` is now a thin
wrapper). Engine-agnostic by construction: it only sees `TranslateItem`, so the
SAME pool drives every engine. Verified by `check_scheduler` in `selftest.py`
(140+ file×string×thread combos, dead-key failover, rate recovery, pause).

- **Workers = `threads × #keys`**, grouped `keys[i // threads]` so a dead key
  cleanly retires its whole group and its strings reclaim to a surviving key.
  Keys come from `api_keys[]` (any count) or legacy `api_key`/`api_key_2`.
- **Shared pool** (`dict[file → items]`) under ONE `threading.Condition`. A
  worker claims ONE file's token-packed batch and OWNS it until success or its
  key dies. `done = len(result)` is the single source of truth (no double-count).
- **Priority ramp-down**: gate `effective_rank(worker) >= ceil(remaining/avg_batch)`
  idles workers from the BACK as the pool drains — thread 1 is the last working.
  Rank counts only **FREE** workers (alive AND not in-flight, tracked by
  `in_flight_workers`): a worker that's already busy can't take the next batch, so
  it must NOT make a free lower-index worker yield — otherwise the tail serializes
  and threads rest while strings wait. The lowest-index alive worker is always
  rank 0, so the pool never deadlocks. `b_est` is unchanged, so batch sizes /
  token packing / request count are identical — only idle workers re-engage.
- **Pause** is checked only at claim/retry-sleep boundaries, never mid-request:
  an in-flight batch always completes and EMITS its translations (frontend
  auto-saves) before the worker blocks. This is the file-integrity guarantee.
- **Pacing** (`delay_seconds`): a request occupies ≥ delay of wall-clock; faster
  replies sleep the remainder before the next claim. Cloud-only (UI hides it for
  local). Retry back-off is staggered per-thread (`delay × rank/threads`) so
  threads that all error at once don't re-fire in lockstep — the final sleep tick
  must be the EXACT remainder, not a flat 1s, or sub-second offsets round up and
  re-synchronize.
- **Error classes** (`_classify_error`): `rate` (429/503/overload) → retry with
  back-off ≥ delay + per-key `key_cooldown` (siblings on that key wait, other
  keys keep going); `auth` (401/403/invalid key) → fail the key fast (2-try
  grace) so work fails over instead of burning ~26 min of retries; `other` →
  normal retry. **On every cloud API an error RESPONSE still spends quota** — only
  a request that never reached the server is free (`_reached_server`); the
  OpenRouter daily counter ticks per request that got an HTTP status.

OpenRouter daily free-request budget: the API gives the cap (50 free tier / 1000
once ≥$10 bought, via `OpenRouterProvider.key_limits` → `/auth/key`) but NOT
requests-spent-today, so the frontend counts locally (`openrouterUsageCount`,
reset at UTC midnight) from the scheduler's `requests_sent`.

### Worker-status UI
The live grid (`App.tsx`, collapsible, toggle enabled only when threads×keys > 2)
colours each thread as a traffic light: 🟡 request in flight (pulsing) · 🟢 batch
landed · 🔴 key failed · ⚪ stopped/resting · 🟣 paused. Tone is derived from the
per-worker `phase` event. API keys are a **dynamic list** (`apiKeys[]`, “+ add
key”), stored per-provider as a JSON array under `providerApiKey` with migration
from the old single `providerApiKey`/`providerApiKey2`. `nonEmptyKeys` is deduped
to match the scheduler so the grid never shows phantom workers. `threads` (1–10)
and `rpmLimit` (`providerRpm`, requests-per-minute cap PER KEY, 0 = none) are
per-provider, cloud-only. **The user enters their RPM cap, not seconds** — the
pacing `delay_seconds` the scheduler consumes is DERIVED in the UI via
`rpmToDelay(rpm, threads) = threads*60/rpm` (the cap is shared by the threads on
one key). The sidecar still only knows `delay_seconds`. The RPM field is a
`type=text` digit-filtered input with `.no-spin` (no native number-spinner
arrows).

## Ren'Py inline-Python translator (`python-core/renpy_python_translator.py`)

The SECOND Ren'Py translation path (the first is the archive-safe `tl/` writer in
`parsers/renpy.py`). It translates string literals living INSIDE `python:`/`$`
blocks AND `show/call screen NAME(...)` args — chat inserts via `append()`, status
text, blog messages, search-history lists — that the dialogue `tl/` format can't
reach because they're code, not say-lines. Two stages: **classify** (Gemini
decides translate vs skip per candidate, cached) then **translate** (also cached).
Driven by `/renpy/translate_python`; runs as step 3 of `handleTranslate`, strictly
AFTER the main `tl/` translate hits 100% (both paths share one RPM budget per key).

### ⚠️ NON-FRAGILE delivery: native `translate <lang> strings:` — NOT file edits
**This is the load-bearing design decision. Do NOT revert to editing archived
`.rpy` / recompiling `.rpyc`.** Output is ONE file `game/tl/<lang>/_interprex_inline.rpy`
holding a `translate <lang> strings:` block of `old`/`new` pairs
(`_write_inline_strings_file`). The engine runs every DISPLAYED string through
`translate_string()` — a runtime old→new dict lookup — at render time, BEFORE
`[var]` interpolation. Proven in the bundled engine source:
`text.py` `set_text` (substitute on by default) → `substitutions.py:substitute`
(`translate=True`, translate happens BEFORE interpolate) → `translation/__init__.py`
`StringTranslator.translate` (exact-match dict). Consequences that make this the
right path:
- **`old` key = the original string EXACTLY as written in code** (the template,
  WITH any `[var]` — interpolation runs after the lookup). That's `entry["value"]`.
- **Archives never touched, no `.rpyc` compile, no game runtime needed** → the
  double-load crash is IMPOSSIBLE by construction. Works on any game/version.
- **Cannot break a code comparison.** `if x == "home"` reads the raw Python value,
  which never goes through the dict — so translating a displayed key can't kill a
  click/branch. (This retired the old `apply_global_replacement` promotion hack.)
- **`load_all_sources` is READ-ONLY**: archived `.rpy` are read straight from
  `.rpa` into memory (synthetic path keys), NEVER written to disk. This is what
  removed the whole extract→edit→recompile→cleanup fragility.
- **MUST exclude `old` keys already in the dialogue `tl/` tree** — Ren'Py crashes
  on a duplicate string-translation key per language (`A translation for "X"
  already exists at ...`). `_existing_tl_string_keys` scans `tl/<lang>/` and seeds
  the dedup set. (Real crash hit: "Enter your Killsong username:" in both.)
- `.lower()`/`.upper()` display transforms (e.g. `text mc.status.lower()`): the
  cased variant is also emitted so the dict still hits after the in-code transform.
- Honest limit: text shown with explicit `substitute=False` won't be translated
  (stays English, never crashes). Verified `substitute=False` = 0 on Killer Chat.
- Verified offline by `check_renpy_python_cache` (strings-file format, escaping,
  dedup, dialogue-tl/ dup exclusion, created-backup) + `check_renpy_python_sources`
  (archives read in-memory, never extracted). The ONLY true correctness test is
  launching the game after a real translate.

### Multi-key worker pool (`_run_batches_over_keypool`)
Both stages fan out over ALL keys with failover, mirroring `scheduler.py` (not the
old single-`primaryKey` `ThreadPoolExecutor`). Workers = `threads × len(keys)`,
worker `i` bound to `keys[i // threads]`. Reuses `_classify_error` /
`_reached_server` from `scheduler.py` (imported, NOT duplicated) so error
bucketing is identical: `auth` → 2-try grace then retire the key + requeue its
batch (siblings on that key stop; last-key-dead accounted so the pool can't hang);
`rate` → per-key cooldown ≥ `delay_seconds`, requeue, key survives; `other` →
requeue with a CUMULATIVE try cap (`BATCH_TRIES × len(keys)`, not reset on key
switch). Pause checked only at claim boundaries. Safety contract: a batch always
finishes classified/translated OR falls to the safe default (classify → `SKIP`;
translate → left untranslated) — never lost silently. Log strings
(`Classified/Translated batch i/M [thread T]`, `T` = global worker index) are a
CONTRACT with the UI grid regexes (`App.tsx`) — keep them exact. `main()` takes
`--api-keys` (JSON array, comma-split fallback) + `--delay-seconds`; legacy
`--api-key` still works. Verified offline by `check_renpy_python_pool` in
`selftest.py` (all-success, auth-failover, rate-recover, all-keys-dead-terminates).

### Translation cache + no-API write-back
- **`_TranslationCache`** (`.interprex_python_translations.json`, keyed by
  `_candidate_cache_key`, versioned by TARGET LANGUAGE) stores the actual inline
  translations. A re-run translates ONLY cache-miss (new) strings; switching
  model/provider does NOT re-translate (only target-language change invalidates).
- **`--apply-cached-only`**: writes the strings-file purely from the cache, ZERO
  API calls. Called automatically by `writeBack()` inside `handleTranslate` (renpy,
  non-mods mode) — pass-through via `main.py` `RenpyPythonReq.apply_cached_only`
  + `ipc.ts`. (The separate "Записать в игру" button was removed from the UI;
  `writeBack()` still exists and is called as step 2 of `handleTranslate`.)
- Model drops a string from its batch → retried one-per-batch, then logged if
  still missing (never silently lost).

### HISTORICAL (REMOVED — do not resurrect): extract→edit→recompile path
The original inline path extracted archived `.rpy` to disk, edited them in place,
and recompiled `.rpyc` via the game's own runtime (`_find_renpy_exe` +
`_compile_loose_rpy`). It fought a **double-load crash** (loose `.rpy` + archived
`.rpyc` don't dedup in the loader's `seen` set → init runs twice →
`'RevertableDict' object is not callable`; see `renpy-inline-python-archive-crash`
memory). This was FRAGILE (needed the runtime, could crash, plenty of orphan-file
edge cases) and is now **fully replaced** by the strings-dict approach above. The
`.rpa` is read-only; nothing is extracted to disk. Correctness is verified by
launching the game on `C:/Program Files (x86)/Steam/steamapps/common/Killer Chat!
- Original Edition` after a real translate.

## In-app folder browser (themed, replaces the native OS dialog)

The native dialog can't be restyled, so `src/FolderPicker.tsx` is a custom modal
that walks the filesystem through the sidecar. Endpoints (all in `main.py`, all
**degrade, never raise** — a locked/missing path returns an empty list or the
drive list, never a 500):

| Endpoint | Returns |
|---|---|
| `/fs/home` | a sensible start folder (user home) |
| `/fs/list` `{path}` | sub-DIRECTORIES of `path` (files irrelevant to a folder picker); `path:""` → drive list; `parent:null` at a filesystem root; hidden/`$`/inaccessible entries skipped |
| `/fs/shortcuts` | game-launcher library folders that EXIST, in priority order: Steam → Epic → GOG. Steam can yield several (one per library across drives, parsed from `steamapps/libraryfolders.vdf`) |

The picker shows launcher quick-jump buttons (logos as inline SVG, not text;
Steam buttons tagged by drive letter), breadcrumbs, up-one-level, a manual
path field, and “Select this folder” (enabled only on a real directory, not the
drive list). `pickFolder`/`pickModsFolder` take a path arg now and are invoked by
the modal's `onPick`; the native `@tauri-apps/plugin-dialog` `open` is gone.

App icon: black/violet themed, regenerate from a 1024² source via
`node_modules/.bin/tauri icon <source.png>` (fans out all platform sizes).

## Dev workflow

```bash
npm run sidecar   # Python sidecar :8723, auto-reloads on .py edits
npm run app       # Tauri app (first run compiles Rust: slow once, then instant)
npm run check     # ~1s  TypeScript types
npm run test:py   # ~1s  parser self-test (detect/extract/id-stability/inject/parity)
```

`start.bat` (double-click) does first-run setup + launches both.

Rule of thumb: changed UI → app is hot-reloading, just look. Changed TS
types/logic → `npm run check`. Changed a parser → `npm run test:py`. Reach for
the full app only to *see* it.

### Conventions
- Verify with a real run/test, not by assertion. Don't mark work done on
  failing output.
- Match surrounding code style; comments explain *why*, not *what*.
- Windows + bash shell: forward slashes in code, but npm scripts invoking the
  venv need backslash paths (cmd).
- Don't commit/push unless asked.
- **React stale closure trap**: `setProject(newProj)` in an `async` function
  does NOT update the `project` variable captured by sibling closures in the
  same render tick. Always pass the updated project explicitly (e.g.
  `translateAll()` returns `{ ok, project }`, caller passes it to
  `writeBack(updatedProject)`). Never read React state after `setProject` in
  the same async chain.
- **Sidecar Python changes need restart**: `npm run sidecar` uses `--reload`
  but it watches files, not in-process imports. After editing `renpy.py` etc.,
  the sidecar process may keep running old bytecode until the file watcher
  fires. If inject output looks wrong, restart the sidecar manually.
- **Sidecar inject ≠ direct Python call**: the `/inject` endpoint calls
  `finalize_backups()` after `parser.inject()`. `finalize_backups` cleans up
  decompiled `.rpy`/`.rpyc` intermediaries — it must NOT delete `tl/` output
  or `interprex_language.rpy`. Selftests that call `parser.inject()` directly
  bypass this; always test through the sidecar for full coverage.

## Status

**Done**
- Tauri + React + TS scaffold; thin Rust shell.
- Data contract + stable id (TS↔Python parity verified).
- Engine parsers (all round-trip self-tested, byte-exact inject):
  - **RPG Maker** MV/MZ: extract + inject.
  - **Ren'Py** (.rpy): say lines + menu choices + `_()` + screen text + character
    names. Inject writes the engine's NATIVE `tl/<lang>/` format (say blocks with
    md5 identifiers verified against an engine oracle; `old/new` strings block,
    globally deduped). Original `.rpy` never modified. Also reads scripts packed
    in **`game/*.rpa`** archives (RPA-2.0/3.0) when no loose `.rpy` exist — see
    `parsers/rpa.py`; loose files override archived ones. Verified on Takei's
    Journey (loose, 63k strings) + Killer Chat! (.rpa, 6.5k strings).
    Supports **dynamic `@[expr]` temporary attributes** in say prefixes (e.g.
    `r @[r.username] "text"`): pre-normalised by `_normalise_line()` before
    `_LINE_RE` matching (fast O(N) char-class prefix, zero backtracking), then
    the original prefix is recovered from the source line for byte-exact hash
    computation (`_say_get_code`). Lines with unbalanced `[` (e.g. `if_any [
    "value"`) are correctly rejected — `[` is not in the char class so the
    regex can't consume it as a prefix token.
  - **C#** (DLL string resources) + **Unity** (UnityPy assets).
  - **i18n** (JSON/key-value locale files).
  - **Fusion/Chowdren** (.dia): ARR1.0 container, number+31 cipher (Iconoclasts).
  - **MMF2** langfile: INI `lang_*.txt` (Baba Is You).
  - **QSP** (.qsp): UTF-16LE, fixed ±5 shift cipher, location records;
    positive-match code-string extraction, surgical per-field re-cipher inject
    (verified on Student_Girl, 12.6 MB / ~30k strings).
  - **Unreal Engine 3** (`unreal`, .INT): `<Game>/Localization/<LANG>/*.INT`
    INI files. Custom state-machine parser (encoding+BOM detect, multiline
    quotes, inline comments, duplicate-key indexing). Handles two escaping
    dialects (real `"` vs escaped `\"`), struct-packed dialogue
    (`Subtitles[N]=(Text=…)` / `(Subtitle=\"…\",Speaker=…)`) via surgical
    per-field inject, and noise filtering (engine boilerplate, bools, object
    literals). Untranslated lines written byte-verbatim. Verified byte-exact on
    Life Is Strange, Borderlands 2, BioShock Infinite (all UE3). See the
    `unreal-ue3-int-format` memory for the gotcha list.
  - **Unreal Engine 4/5** (`unreal4`, .locres): binary `TextLocalizationResource`
    (v0 Legacy / v1 Compact / v2 Optimized CRC32 / v3 CityHash64-UTF16). Pure-
    Python FString codec (positive=ASCII/UTF-8, negative=UTF-16LE) +
    parse/serialize model. Carry-verbatim design: namespace/key tree AND all
    hashes copied byte-for-byte, only string-table VALUES swapped — no need to
    reimplement `FCrc::StrCrc32`/`CityHash64`. Dedup-split on inject (keys sharing
    a slot but wanting different translations get appended slots). Verified
    byte-exact identity round-trip + extract/inject on a real Satisfactory (UE5)
    `.locres` (extracted from its `.pak`). NOTE: shipped UE5 games pack `.locres`
    inside `.pak`/`.utoc`/`.ucas` IoStore containers — the parser works on
    on-disk `.locres`; unpacking the containers is a separate (future) step.
- Sidecar (FastAPI) + IPC seam + project store + minimal UI.
- i18n (en + ru), language switcher, persisted prefs.
- LLM translation via pluggable providers (Ollama, LM Studio, Gemini), batched
  by file + glossary, picked in the UI.
- Token-budgeted batching + per-language output reserve + `num_ctx` (VRAM).
- Adaptive calibration from real `usage`; graceful fallback everywhere.
- Parallel scheduler (`scheduler.py`): N threads × M keys, priority ramp-down,
  key-failover reclaim, request pacing + staggered retries, error-class handling,
  pause that never drops an in-flight batch. Live per-thread status grid +
  resting coverage bar in the UI; OpenRouter daily-quota readout.
- Themed in-app folder browser (`FolderPicker.tsx` + `/fs/*`) with game-launcher
  quick-jumps (Steam/Epic/GOG); replaces the native OS dialog.
- **One "Translate" button = full pipeline** (`App.tsx::handleTranslate`):
  translate → write-back (`tl/`) → [Ren'Py only] Python-block translation →
  autofix. One backup/restore covers all of it (no metadata-schema change).
  The separate "Записать в игру" (write-back only) button has been **removed** —
  it was redundant: "Перевести" skips already-translated strings and always
  runs the full write-back + font inject pipeline.
- **Engine-agnostic post-translation autofix** (`/validate` + `/autofix`,
  `get_validator(engine)`): runs after every translation for ANY engine with a
  validator; the tl/-scoping filter (`_collect_validatable_files`, keep only the
  active `tl/<lang>/`, skip `.interprex_backups` + other langs/`tl/None`) is
  generic — no "renpy" hardcode. Ren'Py validator catches a broken translated
  literal via `ast.parse` WITHOUT the engine binary (works on a Steam machine).
- **UI-width fitting for menu choices**: pixel-accurate budget from the original,
  frequency-weighted char hint, original-anchored prompt, pixel-based overflow
  re-ask (never truncate), per-script language normalization. See the dedicated
  section above; verified by `check_char_limit` across all 9 target languages.

**Next (rough priority)**
1. **Token/cost readout in UI** — `/translate` already returns `usage` +
   `calibration`; surface it (esp. useful for cloud providers).
3. **Editable translations + glossary UI** — let the user edit a cell, mark
   `approved` (translation memory already respects approved), and edit the
   glossary that feeds every prompt.
4. **More engines** (each = one `BaseParser` subclass + registry line + `Engine`
   union entry): done so far — Ren'Py, RPG Maker, C#/DLL, Unity (UnityPy), i18n
   JSON, Fusion/Chowdren, MMF2, QSP, Unreal Engine 3 (.INT), Unreal Engine 4/5
   (.locres). Next candidates: `.pak`/`.utoc` unpacking (to reach packed
   `.locres` in shipped UE5 games like Satisfactory), Godot (.tres/.tscn),
   GameMaker (JSON).
5. **Claude provider** — add Anthropic as a 4th provider (read the claude-api
   skill for current model id/SDK; don't hardcode from memory).
6. **Packaging** — bundle the Python sidecar as a Tauri `externalBin` sidecar
   (PyInstaller) so the shipped exe auto-starts it; today it's two processes.
7. **Robustness** — retry/timeout policy per provider; partial-failure surfacing
   in the UI (errors already returned by `/translate`).

## Unreal Engine 4/5 (`.locres`) — DONE (see `python-core/parsers/unreal4.py`)

UE4/5 is **not** UE3. UE3 ships plain-text `.INT` INI files (handled by `unreal`).
UE4/5 compiles localization into a **binary** `TextLocalizationResource` —
`<Game>/Content/Localization/<Target>/<lang>/<Target>.locres` (e.g.
`Game/Content/Localization/Game/ru/Game.locres`). It's a **separate `unreal4`
engine**, not an extension of `unreal`.

### Format (verified against akintos/UnrealLocres == Epic's source)
- **Magic** (16 bytes): `0E 14 74 75 67 4A 03 FC 4A 15 90 9D C3 37 7F 1B`
  (== `FGuid(0x7574140E, 0xFC034A67, 0x9D90154A, 0x1B7F37C3)`). Absent → Legacy
  (v0), no header. ⚠️ A prior draft of this file listed wrong magic bytes, and
  GameStringer's TS parser reads the magic as a single uint32 — both are bugs;
  trust `unreal4.py::LOCRES_MAGIC`.
- **Version byte** (`ELocResVersion`): 0 Legacy · 1 Compact (string LUT, no
  hashes) · 2 Optimized (CRC32 hashes + refcounts + entry count) · 3
  Optimized_CityHash64_UTF16 (same byte layout as v2, different hash algo).
- **Layout (v1+)**: magic, version, `int64` string-table offset, `[v>=2] int32`
  entry count, `int32` namespace count; per namespace: `[v>=2] uint32` hash +
  namespace `FString` + `int32` key count; per key: `[v>=2] uint32` hash + key
  `FString` + source-string-hash `uint32` + `int32` index into the string table.
  String table @offset: `int32` count, then each entry = `FString` value
  `[+ [v>=2] int32 refcount]`. Legacy (v0): no header/table — value stored inline
  as an `FString` per key.
- **`FString`**: `int32` length. **Positive** = ASCII/UTF-8 (1 byte/char);
  **negative** = UTF-16LE, `abs(len)` = char count. Length **includes** the
  trailing null. Empty string = length 0, no bytes.

### Key design decision (avoids reimplementing hashes)
Translation only ever changes string **values** — namespaces, keys, and every
hash are immutable. So we **carry the whole namespace/key tree AND all hashes
byte-verbatim** (each parsed FString keeps its raw on-disk bytes), and only swap
localized values in the string table. This sidesteps pure-Python `FCrc::StrCrc32`
AND `CityHash64` — the load-bearing reason a `.locres` rewriter is otherwise hard.
We re-emit the same version we read. Bonus: the namespace/key tree is fixed-size
(only an int32 index changes), so the string-table offset never shifts on inject.

### Mapping to our contract
- `path[] = [namespace, key]`; `original` = current localized value. id =
  `hash(engine+file+path+original)` — never the string-table index (impl detail;
  dedup means multiple keys share one slot).
- **Dedup-split** (`_apply` in `unreal4.py`): v1+ table is deduplicated. Group
  keys by the slot they reference; if all sharers want the same text (or only one
  changes), edit the slot in place; if sharers want *different* translations,
  append new slots and repoint just those keys — the original slot and every
  untouched slot stay byte-verbatim. Both branches covered by `check_unreal4`.
- Detect: magic bytes, or (Legacy) a clean parse.

### Verified
- `check_unreal4` (selftest.py): synth v3 fixture with a shared/deduped slot, a
  UTF-16 negative-length value, and an empty value. Byte-exact identity
  round-trip, edit-in-place + dedup-split inject, id-stability, id parity anchor.
- Real UE5 game: byte-exact round-trip + extract/inject on a `.locres` lifted
  from Satisfactory's `FactoryGame-Windows.pak` (v1, mixed ASCII + Arabic UTF-16).

### Not yet
Shipped UE5 games pack `.locres` inside `.pak`/`.utoc`/`.ucas` IoStore
containers (Satisfactory has no loose `.locres` on disk). The parser works on
on-disk `.locres`; unpacking those containers is a separate future step.

### Reference impls
- Epic: `Engine/Source/Runtime/Core/Private/Internationalization/TextLocalizationResource.cpp`
- [akintos/UnrealLocres](https://github.com/akintos/UnrealLocres) (C#, all versions — the authoritative one)
- [CUE4Parse](https://github.com/FabianFG/CUE4Parse) `LocResReader`

## Ren'Py native `tl/` format — DONE (see `parsers/renpy.py`)

`inject()` writes Ren'Py's OWN translation format to `game/tl/<lang>/` as
`translate` blocks and **never touches the original `.rpy`**. Benefits: survives
game updates (a patch re-shipping `.rpy` doesn't wipe the translation), supports
in-game language switching, can't corrupt originals with a bad escape.

The notes below document the algorithm we reproduced (kept for reference — it's
load-bearing and verified against an engine oracle). Verified end-to-end on
Takei's Journey: say-block identifiers 100% precision vs the engine's own
`tl/russian/`, and `old/new` strings — after fixing two bugs found on a real run:
(1) `old` must be the lexer-DECODED source re-quoted, not raw (else `\n`
double-escapes and the binding silently fails); (2) `old` keys are GLOBAL per
language — dedupe across all files or Ren'Py warns on duplicates. Speaker glued
to the quote (`Koji"Hi."`, no space) is valid and must parse, or its hash drifts.

### Reading packed `.rpa` archives — DONE (see `parsers/rpa.py`)
Commercial Ren'Py games often ship `.rpy` ONLY inside `game/*.rpa` (RPA-2.0/3.0),
nothing loose on disk (e.g. Killer Chat!). `parsers/rpa.py` reads `.rpy` straight
out of the archive (pickle+zlib index, XOR-keyed offsets, suffix-filtered so the
300 MB of media is never read). `renpy.py::_iter_sources` merges loose + archived
sources; loose wins on path collision (matches the engine: disk before archive).
Archived file → `file_rel = "game/<inner>"`, byte-identical to its loose id, so
ids stay portable. The `.rpa` is read-only; translations still go to `tl/`. NOTE:
a few `.rpyc` (compiled bytecode) with no paired `.rpy` are NOT read — would need
a decompiler; rare and usually engine plugins, not story.

**Skip shipped translations, by CONTENT not path** (`_is_existing_translation_file`
in `renpy.py`): a game may ship a finished `tl/<lang>/` (e.g. Watch the Road bundles
a complete `tl/chinese/`, ~3k strings). Ingesting that as source would re-translate
Chinese→target — garbage + wasted API. But a blanket "skip `tl/`" rule is WRONG:
some games keep real **source** under `tl/None/` (Ren'Py's "no language" tree).
So `_iter_sources` classifies each file by content — full of `translate <lang> …`
blocks AND no top-level source statements (`label/define/screen/…`) ⇒ existing
translation, skip; otherwise source, read (`tl/None/` code passes). Same seam for
extract + inject, so addressing stays consistent. Verified in `check_renpy_rpa`
(bundles both a `tl/None/` source file and a `tl/chinese/` translation).

### Decompile → backup interaction — CRITICAL
`_decompile_rpyc_files()` extracts `.rpyc` from `.rpa` archives into loose `.rpy`
for the parser to read. These decompiled files must NOT be registered in backup
metadata (`update_metadata`) — they are derived intermediaries, not backed-up
originals. If registered, every `extract()` recreates `.interprex_backups/metadata`
making the "backup exists" flag appear after the user deletes it or reinstalls.

Separately, `finalize_backups()` (`renpy.py:1525`) cleans up decompiled `.rpy`/`.rpyc`
from the game folder after inject so Ren'Py doesn't load duplicate definitions. It
iterates metadata entries with `type == "created"` and deletes them. **It must skip
`game/tl/` files and `game/interprex_language.rpy`** — these are inject output, not
intermediaries. Without this guard, `finalize_backups` silently deletes every
translation file right after inject writes them. Selftests that call `parser.inject()`
directly (bypassing the `/inject` sidecar endpoint) never hit this bug — always test
through the sidecar for full coverage.

**Why we reimplement, not import.** The engine's `renpy/parser.py` + `lexer.py`
are the authoritative parser, but they `import renpy` (whole package: `ast`,
`config`, `script`) and the lexer needs the compiled Cython `renpy.lexersupport`.
Pulling that in would blow up the lightweight sidecar. So we copy the *algorithm*,
not the code — same stance as `make_id`.

**Block identifier algorithm** (verified in `renpy/translation/__init__.py`
`create_translate`/`unique_identifier`, Takei's Journey 0.35):
- `get_code()` of a Say = space-joined: `who`, attributes, `@`+temp-attrs, then
  `encode_say_string(what)`, then any of `nointeract`/`id <id>`/args/`with <x>`.
- `encode_say_string(s)`: `s.replace("\\","\\\\").replace("\n","\\n").replace('"','\\"')`,
  then `re.sub(r'(?<= ) ', '\\ ', s)` (escape a 2nd consecutive space), wrap in `"`.
- `digest = md5((get_code() + "\r\n").encode("utf-8")).hexdigest()[:8]`
- `identifier = digest` if label is None else `label.replace(".","_")+"_"+digest`;
  on collision append `_1`, `_2`, … until unique.

**File format** (`generation.py::write_translates`):
```
# <orig_file>.rpy:<lineno>
translate <lang> <identifier>:

    # <who> "<original what>"
    <who> "<translated what>"
```
Plain strings (`_("...")`, menu choices, screen text) go in a SEPARATE
`translate <lang> strings:` block as `old "..."` / `new "..."` pairs
(`write_strings`/`scanstrings.py`).

**Engine oracle for verification.** Shipped games bundle their own Python runtime
(`lib/py3-windows-x86_64`, `python3.9`) + a launcher exe. Run
`<Game>.exe translate russian` to make the ENGINE emit a reference `tl/russian/`
with its own identifiers, then diff ours against it byte-for-byte — same idea as
the `make_id` TS↔Python parity anchor. (Takei's Journey already ships
`tl/None/common.rpym`: engine-runtime strings in old/new format.)

**Extraction coverage (gaps closed).** Extract now pulls say lines, menu choices,
`_("...")` calls, screen text, and Character names. Two former gaps are fixed:
`_("...")` translatable calls (e.g. Back/Save/History/Skip menu buttons), and
**chat-style menu choices that carry args** — `"Choice"(reacts=[ChatReact("😆",
m,1.0)]):` (`_is_menu_choice_with_args` in `renpy.py`): only the choice TEXT is
taken; the arg list (which may itself contain quoted emoji) is code and stays
verbatim. Detected via balanced-paren scan (a regex can't, args nest), then the
remainder after the outer `)` must match `_MENU_SUFFIX_RE` (optional `if` guard +
the block-opening `:`). menu-choice `path[] = ["label", label, "menu", idx]` where
idx is a per-(label,kind) counter, NEVER a line number.

**`init`-priority screens** (`_SCREEN_RE`). The decompiler (unrpyc) emits a screen
with a non-default init priority as `init -501 screen NAME():`, NOT bare
`screen NAME():`. `_SCREEN_RE` therefore allows an optional `init [+/-N] ` prefix —
without it such screens aren't entered as screen blocks and their bare-string
`textbutton "..."` widgets (labels NOT wrapped in `_()`) go un-extracted. Real bug:
OnlineObsessionDemo's custom main menu (`init -501 screen main_menu():` with
`textbutton "start"/"load"/"prefs"/"help"`) stayed English. Regression-guarded in
`check_renpy` (selftest.py).

**`old`-key decode: lexer vs Python (whitespace).** An `old`/`new` strings-block
entry has TWO different runtime origins, and they decode whitespace differently —
the `old` key MUST be built to match how the engine produces the runtime value:
- **`menu_choice`** → read by the engine's string LEXER (`lexer.py::Lexer.string`),
  which collapses `[ \n]+`→single space. Use `_lexer_decode`.
- **`string`** (screen `text "..."`, `_("...")`, Character names) → the runtime
  value is a Python literal, and the engine ALSO parses the `old "..."` key via
  Python `eval` (`parser.py::translate_strings` → `compile(..., "eval")`), which
  does NOT collapse whitespace. Use `_py_decode` (escapes only, spaces preserved).
Using `_lexer_decode` for a `string` entry collapsed a multi-space source caption
(`text "...quiz!   \n..."`, three spaces) to one space in the key, so the engine's
exact-match `translate_string` dict lookup missed and it rendered English (real
bug: OnlineObsessionDemo StarBlitz quiz caption). Inject branches on
`kind == "menu_choice"`. Regression-guarded in `check_renpy` (selftest.py).

### Identifier algorithm — VERIFIED against engine oracle (Takei's Journey)
Ran `<Game>.exe <gamedir> translate russian` to generate a reference
`tl/russian/`, then checked our recomputed identifiers against it:
- **Digest formula: 56242/56242 exact** — `md5((get_code()+"\r\n").utf8)[:8]` with
  `get_code = " ".join([who] + attrs + [encode_say_string(what)])` is byte-correct.
- **End-to-end from source .rpy: 98.94%** (59496/59576). Every remaining miss is
  one of three KNOWN structural cases (NOT formula errors) — a correct `tl/`
  writer MUST handle all three:
  1. **`nointeract` menu captions.** A say line that is the FIRST statement inside
     a `menu:` block is the menu caption: the engine emits a SECOND translate
     block whose `get_code` appends `" nointeract"` (e.g. `Koji "..." nointeract`),
     changing the hash. So the same line yields two ids — interactive + nointeract.
  2. **Whitespace collapse.** The lexer (`lexer.py::Lexer.string`) runs
     `re.sub(r'[ \n]+', ' ', s)` then expands escapes BEFORE hashing. So a source
     `"foo  bar"` (double space) hashes as `"foo bar"`. We must decode the source
     text exactly as the lexer does, then re-`encode_say_string`, before md5 —
     NOT hash the raw bytes between the quotes.
  3. **Collision suffixes.** Duplicate (label, digest) within a file gets `_1`,
     `_2`, … appended (`unique_identifier`). Our counter must match the engine's
     order (document order). A handful of nested-menu repeats still drift here —
     resolve by mirroring `create_translate`'s grouping exactly when implementing.
  4. **Named-menu label prefix.** A say inside `menu NAME:` takes **NAME** as its
     id label prefix, NOT the enclosing `label` — the engine compiles `menu NAME:`
     to a real `ast.Label(NAME)` (`parser.py::menu_statement`) + `set_global_label`.
     `_MENU_NAME_RE` + the `set_label()` helper in `renpy.py` mirror this (anonymous
     `menu:` leaves the label untouched). This was the *Watch the Road* bug:
     English-after-choice replies because 655/1065 ids used `start_…` not
     `<menuname>_…`. See `[[renpy-named-menu-label-prefix]]` memory.
  Plus bare strings inside non-say contexts (combat captions, etc.) route to the
  `old/new strings` block, not a translate block — classify by context, not by
  "has quotes".

  **Oracle verification trick:** a shipped game's own engine-generated `tl/`
  (e.g. bundled `tl/chinese/`) is a perfect identifier oracle — parse the source,
  compute every say-id, diff against the oracle's id set. Watch for version skew
  (source rewritten after the tl was generated) showing false "extra" ids.

## Ren'Py Runtime Monkey-Patches & Style Overrides (`_interprex_font.rpy`)

Ren'Py games can have customized UI/layout styling or run-time text preprocessing functions that break standard localization lookup (e.g., dynamic variable insertion or inline parsing). To resolve this cleanly without editing the original game files or unpacking `.rpa` archives, we generate a special runtime script `game/tl/<lang>/_interprex_font.rpy` containing a `python init:` block.

### Key Applications of Runtime Overrides
1. **Style Adjustments**:
   - `style.choice_button.ysize = None`: Permits choice/menu buttons to dynamically scale vertically, accommodating longer translated options.
   - `style.choice_button_text.layout = 'subtitle'`: Enables better word-wrapping algorithms on choice captions.

2. **Function Wrapping (e.g., *Killer Chat!*)**:
   - The game pre-processes chat messages at runtime by calling `add_ping_hyperlinks()`, replacing variable pings like `@[username]` with temporary symbols (`<0001>`) and calling `renpy.substitute()`. This means `translate_string` is run on the placeholder-replaced version, which fails to match the original translation key.
   - We patch `add_ping_hyperlinks` to intercept the text and translate it *before* placeholders are injected.

3. **Dynamic Translation of Object Properties (`TranslatingString`)**:
   - Games often instantiate custom classes (e.g., `ServerRole` for chat roles/traits or `ChatCharacter`) containing string attributes (e.g. `role.name` or `mc.dominant_role`). If screens render them via dynamic string expressions like `" " + role.name`, Ren'Py's screen translator doesn't translate them automatically.
   - However, translating them statically in Python breaks code logic (such as pronoun indexing, savegame lookups, or comparison checks like `character.dominant_role == role.name`).
   - We resolve this by introducing a hybrid `TranslatingString(str)` class. It behaves like the English string for all code comparisons (`__eq__`, `__hash__`, `list.index()`, `in` checks) but dynamically returns the translated (Russian) string when formatted, printed, converted via `str()`, or concatenated (like in screens). We patch class attributes via properties (e.g., `ServerRole.name = property(_get_role_name, _set_role_name)`).

4. **Dynamic Translation of Custom UI State Indicators (e.g., "is typing...")**:
   - Complex UI state indicators (like `ChatChannel.get_who_typing`) construct display strings dynamically inside Python property getters (e.g., `return self.people_typing[0].username + " is typing..."` or `return "Several people are typing..."`). Since these are not marked with `_()` or processed as translation variables, they appear in English.
   - We patch the property getter on the corresponding class at runtime to return language-appropriate strings dynamically based on the current active language (`renpy.game.preferences.language`).

### Rules for Safe Monkey-Patching
To prevent crashes, infinite recursion, or issues in game/test environments:
- **Avoid Repeated Application**: Check if the hook has already been set using a sentinel attribute:
  ```python
  if not hasattr(_orig_fn, '_patched_by_interprex'):
      # apply patch
  ```
- **Closure Default Argument Binding**: Capture the original function as a default parameter in the wrapper signature (e.g., `def _patched_fn(text, _orig=_orig_fn):`). This stores the reference at definition time, preventing infinite recursion or `NameError`/`RecursionError` when the code is executed multiple times or inside Python `exec()` blocks.
- **A class defined in the block is NOT a global for its methods — bind it via
  default arg / `type(self)`, NEVER the bare name.** A `translate <lang> python:`
  block runs with the store as GLOBALS and a SEPARATE locals frame, so a
  `class TranslatingString(str)` defined in the block binds into LOCALS, not the
  patched methods' `__globals__`. A method (property getter, `__eq__`) that
  references the bare class name then raises **`NameError: name 'TranslatingString'
  is not defined` at call time** (real Killer Chat 1.4.1 crash in `_get_role_name`
  / `_get_dominant_role` / `__eq__`). Fix: capture the class as a default arg at
  def-time (`def _get_role_name(self, _TS=TranslatingString):` then use `_TS`); and
  inside the class's OWN methods, where the name isn't bound yet, use
  `isinstance(other, type(self))` — zero global lookup. Same closure rule as the
  `add_ping_hyperlinks` wrapper above: never rely on a name being a global inside
  exec'd patch code. **Test trap:** `check_renpy_font` must `exec(body, globals, locals)`
  with DISTINCT dicts (not one dict for both) and THEN call the getters — a
  single-dict exec makes the class findable and hides the bug.
- **Skip during Lint/Prediction**: Check `not (renpy.game.lint or renpy.predicting())` inside wrappers to prevent unnecessary evaluation or potential crashes during game linting/saving/loading.
- **NEVER REBIND a `store` function name — patch it IN PLACE (`__code__` swap).** Ren'Py pickles `store` functions BY REFERENCE on save and verifies identity (`store.<name>` must be the SAME object as the one captured in the save graph). If you do `globals()['fn'] = wrapper` (a NEW object), every save raises `PicklingError: Can't pickle <function fn>: it's not the same object as store.fn` and the player loses ALL progress — saving is impossible. This was a real shipped bug (Killer Chat 1.4.1, `add_ping_hyperlinks`). The fix: keep the SAME function object and swap its behaviour —
  ```python
  import types
  fn = globals()['fn']
  if not getattr(fn, '_patched_by_interprex', False):
      try:
          # The clone MUST get a UNIQUE name, NOT fn.__name__ (see gotcha below).
          orig = types.FunctionType(fn.__code__, fn.__globals__,
                                    '_interprex_orig_fn',   # unique, NOT 'fn'
                                    fn.__defaults__, fn.__closure__)  # clone original
          orig.__qualname__ = '_interprex_orig_fn'
          def _wrap(arg, _orig=orig, _renpy=renpy):  # zero freevars (defaults only)
              ...  # call _orig(arg)
          fn.__code__ = _wrap.__code__            # SAME object, new behaviour
          fn.__defaults__ = _wrap.__defaults__
          fn._patched_by_interprex = True
      except Exception:
          pass   # function has free variables -> swap impossible -> skip, NEVER crash
  ```
  The wrapper must have NO closure free variables (capture everything via default args), or `__code__` assignment fails. Rebinding a `renpy.*` MODULE attribute (e.g. `renpy.translation.translate_string`) is fine — engine modules aren't pickled into saves; only the `store` namespace + log are.
  - **GOTCHA the `__code__`-swap itself created (2nd shipped save crash):** the clone of the original is assigned to a variable INSIDE the `translate <lang> python:` block, so it becomes a **store var**, and it gets captured in the rollback log (a Say's `show_function` → … → the wrapper's `_orig` default). On save, pickle stores a function BY REFERENCE as `getattr(store, fn.__name__)`. If the clone kept the original's `__name__` (`add_ping_hyperlinks`), that lookup returns the PATCHED original (a *different* object) → the SAME `"not the same object as store.add_ping_hyperlinks"` PicklingError, just from the clone instead of a rebind. **Fix: give the clone a UNIQUE `__name__`/`__qualname__`** (e.g. `_interprex_orig_aph`) so pickle-by-reference resolves it to itself. Verified by `check_renpy_font`: asserts the patched fn `is` the original AND that NO leftover store function named `add_ping_hyperlinks` is a different object (mirrors pickle's check), and the renamed clone still calls through.

### Testing Monkey-Patches
All runtime code injected into `_interprex_font.rpy` must be verified via automated self-tests:
- See `check_renpy_font()` in [selftest.py](file:///c:/Users/Alexandr/Desktop/Interprex/python-core/selftest.py). It spins up a mocked Ren'Py environment using local dicts, defines mock target functions/objects, runs `exec()` on the generated `_interprex_font.rpy` content, and validates style changes and wrapper logic.

## Reference
GameStringer — same Tauri + TS + Python-sidecar approach; look there for how it
bundles the sidecar and handles complex binary engines.
