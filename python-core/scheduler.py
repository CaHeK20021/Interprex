"""Parallel translation scheduler — the worker pool behind one /translate run.

Engine-agnostic by construction: it only ever sees TranslateItem / TItem, never
an engine format (the three-stage wall — parser ⇄ TranslationString ⇄ LLM). So
the SAME scheduler drives Ren'Py, RPG Maker, Unreal, QSP, … without a line of
engine-specific code. Stability is the whole point of this module: no source
string may be lost, the project file must always receive complete batches, and a
dead API key must hand its work off cleanly rather than drop it.

Design (plan shiny-yawning-breeze):

  * ONE shared pool of remaining strings, grouped by file (so a batch shares
    scene/context) and ordered, guarded by a single threading.Condition.
  * A worker claims ONE file's token-packed batch at a time and OWNS it until it
    succeeds or its key dies — while it retries (up to BATCH_TRIES) no other
    worker may touch those strings.
  * Priority ramp-down: as the pool depletes, workers idle from the BACK. With
    B_est batches left, only workers 0..B_est-1 keep translating; worker N rests
    first, worker 0 is the last one working. Small remainders never get split
    across all threads into tiny batches.
  * Reclaim: only when a worker's KEY ultimately dies are its un-translated
    strings returned to the pool for a surviving key — guaranteed via finally so
    a crash between pop and push-back can never lose them.
  * Pace: each request occupies at least `delay_seconds` of wall-clock since the
    last fire (RPM). A worker that finished faster sleeps only the REMAINING
    wall-clock time before its next claim — never a full delay restart.
  * TPM (tokens-per-minute, PER KEY): before each send, estimate the batch's
    token cost from our prompt text (Calibrator + output_ratio reserve), then
    wait until a 60s sliding window on that key has room. Threads on the same
    key share one window via reserve-on-send + commit-actual-after-usage so they
    can't all fire fat batches at once and blow a 16k TPM free tier. Other keys
    are independent (3 keys × 16k TPM ≈ 48k aggregate). Every progress event
    carries a live ledger snapshot (`tpm_keys`: spent/reserved/used/free/limit)
    so the UI can show an exact counter. If free tokens remain but the full
    batch would exceed them, the batch is shrunk to fit (leftovers back to the
    pool) — free budget is not left idle waiting for a fat batch.
  * Pause: no NEW batch is claimed (claim gate). An in-flight HTTP request is
    always finished (and on SUCCESS the translations are emitted) so we never
    drop a paid reply — mid-request cards show `finishing_batch`. On FAILURE
    while paused: do NOT auto-retry; hold the owned batch and wait for the
    user to press Continue. After drain, free workers park as `paused`.
    Pause freezes ACTION only (no auto-retry / no new claim). Wait timers use
    absolute wall-clock deadlines: a long pause does NOT re-arm a full 7s RPM
    wait or a full retry backoff — if the deadline already passed, Continue
    fires immediately (the provider's real RPM window advanced while paused).

Everything a worker feeds the model is identical to the legacy Gemini path:
per-string context, the Ren'Py per-line character limit, the glossary, adaptive
token packing via Calibrator, and the oversized-translation re-ask.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from collections import OrderedDict
from typing import Callable

logger = logging.getLogger("interprex")

from providers import Calibrator, ProviderConfig, TranslateItem, get_provider
from providers.base import build_prompt

# A transient batch failure is retried this many times before its key is judged
# dead: a blip (brief network hiccup, model reloading, a momentary 503) must not
# lose the batch — we re-send the SAME batch so every string still gets
# translated, not skipped.
BATCH_TRIES = 100
# Hard cap on workers PER key (UI + /translate clamp to this). Raised 10→40 after
# check_scheduler_high_threads proved no double-claim, key binding, pause, and
# failover under 40 × 1..4 keys. Total workers = threads * #keys.
MAX_THREADS_PER_KEY = 40

# Back-off (seconds) between retries: short on the first miss, longer after. For
# RATE/OVERLOAD errors the effective wait is raised to at least `delay_seconds`
# (see _classify_error) — on every cloud API a 429/503/"overloaded" reply STILL
# counts against the per-minute request quota, so hammering retries would only
# dig the hole deeper.
_RETRY_BACKOFF_FIRST = 8
_RETRY_BACKOFF_REST = 16

# Default window when the UI doesn't constrain it (cloud models, big local ones).
DEFAULT_CONTEXT_TOKENS = 8192

# Sliding window for tokens-per-minute pacing (provider TPM is almost always a
# rolling 60s bucket). Events older than this are dropped from the per-key ledger.
_TPM_WINDOW_S = 60.0
# Small safety margin on pre-send estimates so a slightly dense tokenizer doesn't
# overshoot the real TPM cap before usage comes back. Pre-calibration (0 samples)
# uses a higher margin — default chars/token under-counts Gemini/OpenAI denser
# tokenizers and would let N fat batches reserve "cheap" then blow the real TPM.
_TPM_EST_SAFETY = 1.08
_TPM_EST_SAFETY_COLD = 1.35
# When the user set TPM but left RPM empty (delay_seconds=0), a 429 still needs
# a real multi-thread cooldown. Without this, only the failing worker backs off
# 8s while siblings keep firing → "TPM on, still hammering errors".
_RATE_COOLDOWN_NO_RPM_S = 20.0


def _classify_error(msg: str) -> str:
    """Bucket a provider error so the retry loop can react correctly.

      "rate"  — 429 / 503 / overloaded / quota / temporarily-unavailable. The
                request still consumed the provider's per-minute quota, so the
                retry must wait at least the pacing delay. These often recover,
                so keep retrying up to BATCH_TRIES.
      "auth"  — invalid / expired / unauthorized key, billing. Will NOT recover
                this run: fail the key fast so its work fails over to a surviving
                key instead of burning ~26 minutes of pointless retries.
      "other" — anything else (parse hiccup, odd 500): retry normally.
    """
    m = (msg or "").lower()
    rate_markers = (
        "429", "too many", "rate limit", "rate-limit", "resource exhausted",
        "resource_exhausted", "quota", "503", "overloaded", "unavailable",
        "try again", "temporarily", "capacity", "server is busy", "busy",
        "provider returned error", "no available provider",
    )
    if any(k in m for k in rate_markers):
        return "rate"
    auth_markers = (
        "401", "403", "api key not valid", "invalid api key", "invalid_api_key",
        "api_key_invalid", "permission", "unauthor", "expired", "billing",
        "no gemini api key", "no openrouter", "credentials",
        # OpenRouter returns plain "User not found" (often HTTP 404) for a
        # deleted/invalid account or key — permanent for this run, fail the
        # key fast instead of burning BATCH_TRIES (=100) "other" retries.
        "user not found", "invalid key", "key not found", "no credits",
        "insufficient credits", "insufficient funds",
    )
    if any(k in m for k in auth_markers):
        return "auth"
    return "other"


def _reached_server(msg: str) -> bool:
    """True if a failed request still hit the provider's server (so it spent the
    quota), False for a pure connection failure that never arrived. On every
    cloud API an error RESPONSE (429/503/…) counts against the daily/per-minute
    quota; only a request that never reached the server is free."""
    m = (msg or "").lower()
    connection_markers = (
        "failed to connect", "connection", "не удалось подключиться",
        "connect timeout", "connecterror", "name or service not known",
        "getaddrinfo", "dns", "ssl", "max retries exceeded",
        "terminated without result",
        "network", "winerror", "10054", "10053", "10060",
        "reset by peer", "broken pipe", "unreachable",
    )
    return not any(k in m for k in connection_markers)

# claim_batch outcomes.
_CLAIM = "claim"
_REST = "rest"
_DONE = "done"


class TranslationScheduler:
    def __init__(self, req, should_pause: Callable[[], bool]):
        self.req = req
        self.should_pause = should_pause
        self.provider = get_provider(req.provider)
        self.window = req.max_context_tokens or DEFAULT_CONTEXT_TOKENS

        # --- dedup identical strings (same as the legacy path) ------------------
        # Translate each unique (text, context) ONCE, fan the result out to every
        # id that shares it. A typical RPG repeats "Yes"/"No"/"HP" dozens of times.
        self.groups: dict[tuple[str, str], list[str]] = {}
        reps: list = []
        for it in req.items:
            key = (it.text, it.context)
            if key in self.groups:
                self.groups[key].append(it.id)
            else:
                self.groups[key] = [it.id]
                reps.append(it)
        self.reps = reps
        self.rep_key: dict[str, tuple[str, str]] = {
            it.id: (it.text, it.context) for it in reps
        }
        self.total = len(reps)

        # --- Ren'Py choice-font character limits (same as legacy) ---------------
        self.is_renpy = (req.engine == "renpy") or any(
            it.file.endswith(".rpy") for it in reps
        )
        self.source_font_path = None
        self.font_size = 32
        # Measure UI-fit against the SAME font inject will write (smooth/pixel).
        self._font_style = getattr(req, "font_style", "smooth") or "smooth"
        if self.is_renpy and req.root:
            try:
                from parsers.renpy import get_source_font_and_size

                self.source_font_path, self.font_size = get_source_font_and_size(
                    req.root
                )
                logger.info(
                    "Resolved Ren'Py choice font: %s, size: %d",
                    self.source_font_path,
                    self.font_size,
                )
            except Exception as e_font:
                logger.error("Failed to resolve choice font from gui.rpy: %s", e_font)

        # --- prepare each rep into a TranslateItem (context + char-limit) -------
        # Done ONCE up front so the exact same payload survives a reclaim, and the
        # pool can be a plain map of file -> ready-to-send items.
        self.item_limits: dict[str, int] = {}
        # Pixel budget (the GROUND TRUTH the translation must fit) per item, so
        # the oversize check measures real rendered width, not len()*avg.
        self.item_orig_px: dict[str, float] = {}
        # RimWorld research-tree buttons: max wrapped lines (ground truth) + lang
        # used for the offline Arial/Noto measure. Empty for non-research items.
        self.item_max_lines: dict[str, int] = {}
        self.item_line_lang: dict[str, str] = {}
        # rep id -> font-shrink factor (<1.0) for captions that STILL overflow
        # after all re-asks; consumed by inject to reduce that style's font. Empty
        # for the common case (shortening was enough). Guarded by self.cond.
        self.size_overrides: dict[str, float] = {}
        self.item_file: dict[str, str] = {}
        prepared_by_file: "OrderedDict[str, list]" = OrderedDict()
        from parsers.i18n import is_research_project_label
        for c in reps:
            context = c.context
            is_menu = "menu" in c.path
            # A screen widget's path is ["screen", name, kind, idx] (see
            # parsers/renpy.py). Only `textbutton`/`label` are genuine clickable
            # captions that live in a fixed-width box; a freestanding `text` or
            # `tooltip` usually has room and is exactly what the old blanket
            # `len(text) < 50` rule was wrongly crushing (e.g. a short status line
            # squeezed as if it were a button). So scope the width budget to button
            # kinds. NOTE: this is still a HEURISTIC fallback used when we can't ask
            # the engine itself — the authoritative path is the runtime auto-fit /
            # risk analyzer that inherits the real box geometry.
            is_screen_button = (
                len(c.path) >= 3
                and c.path[0] == "screen"
                and c.path[2] in ("textbutton", "label")
                and len(c.text) < 50
            )
            # Per-item fixed-width budget. The char limit is passed to the model
            # as TranslateItem.max_chars (a first-class field build_prompt surfaces
            # prominently), NOT stuffed into `context` — the system prompt tells the
            # model context is ignorable metadata, so a width limit buried there was
            # silently disobeyed (EN 95 chars -> RU 120). Only genuine metadata
            # (multi-line note) stays in context.
            max_chars = 0
            max_pixels = 0
            max_lines = 0
            if (is_menu or is_screen_button) and self.is_renpy and req.root and self.source_font_path:
                try:
                    from parsers.renpy import (
                        get_char_limit, measure_original_px, _avg_char_width,
                    )

                    orig_px = measure_original_px(
                        c.text, self.source_font_path, self.font_size
                    )
                    # The ORIGINAL word's width is a POOR estimate of the box it
                    # lives in: buttons have padding, and menu choices wrap (we set
                    # choice_button ysize=None + 'subtitle' layout in
                    # _interprex_font.rpy). So a short source like "Save" must NOT
                    # crush "Сохранение" down to "Сох". Widen the budget by a slack
                    # factor and never let it fall below MIN_CAPTION_CHARS worth of
                    # width, so standard menu words (Сохранение / Настройки /
                    # Продолжить, ~10 chars) always fit. This relaxed budget is the
                    # ONE ground truth used both for the prompt hint AND the overflow
                    # re-ask check (item_orig_px), so the two stay consistent.
                    avg_w = _avg_char_width(
                        req.target_lang, self.font_size, self._font_style
                    )
                    floor_px = self._MIN_CAPTION_CHARS * avg_w
                    budget_px = max(orig_px * self._UI_WIDTH_SLACK, floor_px)
                    max_chars = max(
                        get_char_limit(
                            c.text, self.source_font_path, req.target_lang,
                            self.font_size, self._font_style,
                        ),
                        int(budget_px / avg_w) if avg_w > 0 else self._MIN_CAPTION_CHARS,
                        self._MIN_CAPTION_CHARS,
                    )
                    self.item_orig_px[c.id] = budget_px
                    max_pixels = int(budget_px)

                    if not is_menu:
                        line_count = c.text.count("\\n") + 1
                        if line_count > 1:
                            note = (
                                f"Keep exactly {line_count} lines "
                                f"(use \\n for line breaks)."
                            )
                            context = f"{note} | {context}" if context else note
                    self.item_limits[c.id] = max_chars
                except Exception as e_limit:
                    logger.error(
                        "Error calculating char limit for '%s': %s", c.text, e_limit
                    )
            # RimWorld research-tree buttons (ResearchProjectDef.label only):
            # fixed 140px cell, height grows with wrap → limit by LINE COUNT so
            # a long RU name cannot cover the node below. Ground truth = measured
            # wrap lines; max_chars is only a model hint. No font-shrink path
            # (engine has none for research cells).
            elif is_research_project_label(
                getattr(c, "file", "") or "",
                getattr(c, "path", None) or [],
                context or getattr(c, "context", "") or "",
            ):
                try:
                    from parsers.i18n import (
                        RESEARCH_BUTTON_MAX_LINES,
                        RESEARCH_BUTTON_WIDTH_PX,
                        research_label_char_limit,
                    )
                    max_lines = RESEARCH_BUTTON_MAX_LINES
                    max_chars = research_label_char_limit(
                        req.target_lang, max_lines, RESEARCH_BUTTON_WIDTH_PX
                    )
                    self.item_limits[c.id] = max_chars
                    self.item_max_lines[c.id] = max_lines
                    self.item_line_lang[c.id] = req.target_lang
                except Exception as e_limit:
                    logger.error(
                        "Error calculating research line limit for '%s': %s",
                        c.text, e_limit,
                    )
            prepared_by_file.setdefault(c.file, []).append(
                TranslateItem(
                    id=c.id, text=c.text, context=context,
                    max_chars=max_chars, max_pixels=max_pixels, max_lines=max_lines,
                )
            )
            self.item_file[c.id] = c.file
        self._prepared_by_file = prepared_by_file

        group_mode = getattr(req, "group_small_files", "auto")
        if group_mode == "auto":
            # Смарт-автогруппировка: включаем, если мелких файлов (менее 5 строк) как минимум 8
            # И они составляют большую часть проекта (не менее 70% всех файлов в текущем запуске).
            # Это предотвращает группировку сюжетных диалогов больших файлов из-за пары мелких файлов настроек.
            small_files_count = sum(1 for items in prepared_by_file.values() if len(items) < 5)
            self.auto_group_small_files = (
                small_files_count >= 8
                and small_files_count >= len(prepared_by_file) * 0.7
            )
        elif group_mode == "on":
            self.auto_group_small_files = True
        else:
            self.auto_group_small_files = False

        # --- worker → key assignment --------------------------------------------
        # Grouped: keys[i // threads]. Killing a key then cleanly retires that
        # key's whole worker group and reclaims their strings to a surviving key.
        # A provider that can rotate keys (cloud) supplies them via api_keys (any
        # count) and/or the legacy api_key/api_key_2 fields; we dedupe-preserve
        # order. Single-key/local providers collapse to one (possibly empty) key.
        multi = list(getattr(req, "api_keys", None) or [])
        candidates = multi + [getattr(req, "api_key", ""), getattr(req, "api_key_2", "")]
        seen_keys: set[str] = set()
        keys = []
        for k in candidates:
            if k and k not in seen_keys:
                seen_keys.add(k)
                keys.append(k)
        if not keys:
            keys = [getattr(req, "api_key", "") or ""]
        self.keys_to_use = keys
        self.threads = max(1, min(MAX_THREADS_PER_KEY, int(getattr(req, "threads", 1) or 1)))
        self.worker_count = self.threads * len(keys)
        self.delay_seconds = max(0.0, float(getattr(req, "delay_seconds", 0.0) or 0.0))
        # Tokens-per-minute cap PER KEY (0 = off). Shared by all threads on that
        # key via a sliding 60s ledger; independent across keys.
        self.tpm_limit = max(0, int(getattr(req, "tpm_limit", 0) or 0))
        # ONE calibrator for the whole run. A per-worker Calibrator left every
        # thread "cold" on the first burst (5× default chars/token) so TPM
        # reserved 2k while Gemini billed 5k → 429 with the cap "respected".
        self.cal = Calibrator(req.target_lang)

        # --- shared state, all guarded by self.cond -----------------------------
        self.cond = threading.Condition()
        self.pool: "OrderedDict[str, list]" = OrderedDict()  # filled in run()
        self.in_flight = 0
        # Which worker indices are CURRENTLY holding a batch (busy). The ramp-down
        # rank counts only FREE workers, so a busy higher-priority worker doesn't
        # make a free lower-priority one defer to it — that deferral left claimable
        # work idle in the pool and serialized the tail (see _claim_rank_locked).
        self.in_flight_workers: set[int] = set()
        self.avg_batch_items = float(req.max_batch_size or 30)
        self.dead_keys: set[str] = set()
        # Per-key cooldown: monotonic wall-clock time until which this key should
        # not be hit again, set when ANY worker on the key sees a rate/overload
        # error. Sibling workers on the SAME key honour it (and workers on other
        # keys are unaffected) — so with 2+ keys each waits exactly as long as ITS
        # OWN provider quota demands, no more. Guarded by self.cond.
        self.key_cooldown: dict[str, float] = {}
        # Per-key TPM ledger: committed (ts, tokens) spends in the last 60s, plus
        # reserved tokens for in-flight sends so sibling threads can't over-book.
        self.key_token_events: dict[str, list[tuple[float, int]]] = {}
        self.key_token_reserved: dict[str, int] = {}
        # How many tokens THIS worker currently has reserved (for clean unreserve).
        self.worker_tpm_reserve: dict[int, int] = {}
        self.reclaim_count: dict[str, int] = {}
        self.result: dict[str, str] = {}  # rep id -> translation (authoritative)
        self.errors: list[str] = []
        self.aborted = False
        self.tok_in = 0
        self.tok_out = 0
        self.batches = 0
        # Per-worker batch numbering for the UI grid. `batch_seq` is a global
        # monotonic ticket (logging). `worker_batch_no` is THIS worker's Nth
        # claim (1, 2, 3…) — a global 4→81 on the same card looked like the
        # thread was jumping around. Failover re-claim just increments again.
        self.batch_seq = 0
        self.worker_batch_no: dict[int, int] = {}
        self.worker_claim_n: dict[int, int] = {}
        # Count of requests that REACHED the provider (success + error responses),
        # for the OpenRouter daily-quota readout. Connection failures don't count.
        self.requests_sent = 0
        # Per-thread timestamp of the LAST request that reached the provider.
        # Used by _pace_delay to space requests evenly across the delay window
        # instead of measuring from batch-start (which causes all threads on a
        # key to fire simultaneously after the first cycle).
        self.worker_last_request: dict[int, float] = {}
        self.chars_per_token = 3.0
        self.output_ratio = 1.2

        # event_queue is its own thread-safe channel (not under cond).
        self.event_queue: "queue.Queue[str]" = queue.Queue()

    # -- small locked helpers ---------------------------------------------------

    def _is_aborted(self) -> bool:
        with self.cond:
            return self.aborted

    def set_aborted(self) -> None:
        with self.cond:
            self.aborted = True
            self.cond.notify_all()

    def _done_count(self) -> int:
        # done == strings actually in the result map. Single source of truth, so
        # it can never exceed total or double-count across retries/sweeps.
        return len(self.result)

    def _fan_out(self, rep_tr: dict[str, str]) -> dict[str, str]:
        """Expand {rep id -> translation} to {every sharing id -> translation}."""
        out: dict[str, str] = {}
        for rid, tr in rep_tr.items():
            for sid in self.groups[self.rep_key[rid]]:
                out[sid] = tr
        return out

    def _emit(self, worker_idx, phase, status="", **extra) -> None:
        """Queue one NDJSON progress event for the stream consumer."""
        with self.cond:
            done = len(self.result)
            batches = self.batches
            requests_sent = self.requests_sent
            tpm_snap = (
                self._tpm_snapshot_locked(now=time.time())
                if self.tpm_limit > 0 else None
            )
        evt = {
            "type": "progress",
            "done": done,
            "total": self.total,
            "batches": batches,
            "requests_sent": requests_sent,
            "status": status,
            "phase": phase,
            "worker_idx": worker_idx,
            # legacy alias so an un-migrated frontend still reads it.
            "key_idx": worker_idx,
            "translations": {},
        }
        if tpm_snap is not None:
            evt["tpm_limit"] = tpm_snap["limit"]
            evt["tpm_keys"] = tpm_snap["keys"]
            # Flat fields for THIS worker's key (handy single-key UI + logs).
            k_i = 0
            if self.threads > 0 and self.keys_to_use:
                k_i = min(
                    worker_idx // self.threads,
                    len(self.keys_to_use) - 1,
                )
            if 0 <= k_i < len(tpm_snap["keys"]):
                row = tpm_snap["keys"][k_i]
                evt["tpm_used"] = row["used"]
                evt["tpm_spent"] = row["spent"]
                evt["tpm_reserved"] = row["reserved"]
                evt["tpm_free"] = row["free"]
        evt.update(extra)
        self.event_queue.put(json.dumps(evt, ensure_ascii=False))

    # -- TPM (tokens-per-minute) pacing, per key --------------------------------

    def _est_batch_tokens(self, batch: list, cal: Calibrator | None = None) -> int:
        """Pre-send estimate of total tokens this batch will cost (input+output).

        Built from the exact prompt we will send: Calibrator chars/token (refined
        from real usage after the first batch) × (1 + out_ratio) with a small
        safety margin. Output is reserved, never measured — the translation does
        not exist yet. Used only for TPM gating; packing still uses input_budget.
        Always reads the SHARED run calibrator so thread 5 inherits thread 1's
        first usage sample instead of staying on language defaults.
        """
        prompt = build_prompt(
            batch, self.req.target_lang, self.req.glossary,
            self.req.engine, getattr(self.req, "extra_instruction", "") or "",
        )
        c = cal if cal is not None else self.cal
        with self.cond:
            prompt_tok = max(1, c.est_tokens(prompt))
            out_ratio = max(0.1, float(c.out_ratio))
            samples = int(getattr(c, "_samples", 0) or 0)
        # Cold start (no usage samples yet): pad harder so concurrent threads
        # cannot under-reserve and dump the free-tier TPM in one burst.
        safety = _TPM_EST_SAFETY_COLD if samples <= 0 else _TPM_EST_SAFETY
        return max(1, int(prompt_tok * (1.0 + out_ratio) * safety))

    @staticmethod
    def _tpm_key_label(key: str, key_idx: int) -> str:
        """Short stable label for the UI (never the full secret)."""
        k = (key or "").strip()
        if len(k) >= 4:
            return f"K{key_idx + 1}…{k[-4:]}"
        return f"K{key_idx + 1}"

    def _tpm_snapshot_locked(self, now: float | None = None) -> dict:
        """Exact per-key ledger for the UI counter. Caller holds self.cond."""
        now = time.time() if now is None else now
        keys_out: list[dict] = []
        for i, k in enumerate(self.keys_to_use):
            self._tpm_prune_locked(k, now)
            spent = sum(t for _, t in self.key_token_events.get(k, ()))
            reserved = int(self.key_token_reserved.get(k, 0) or 0)
            used = spent + reserved
            keys_out.append({
                "key_idx": i,
                "label": self._tpm_key_label(k, i),
                "spent": spent,
                "reserved": reserved,
                "used": used,
                "free": max(0, self.tpm_limit - used),
                "limit": self.tpm_limit,
            })
        return {"limit": self.tpm_limit, "keys": keys_out}

    def _tpm_prune_locked(self, key: str, now: float) -> None:
        ev = self.key_token_events.get(key)
        if not ev:
            return
        cutoff = now - _TPM_WINDOW_S
        i = 0
        n = len(ev)
        while i < n and ev[i][0] <= cutoff:
            i += 1
        if i:
            del ev[:i]

    def _tpm_used_locked(self, key: str, now: float) -> int:
        """Committed spends in the window + in-flight reserves for this key."""
        self._tpm_prune_locked(key, now)
        spent = sum(t for _, t in self.key_token_events.get(key, ()))
        reserved = self.key_token_reserved.get(key, 0)
        return spent + reserved

    def _tpm_earliest_fit_locked(self, key: str, est: int, now: float) -> float:
        """Wall-clock time when `est` tokens fit under this key's TPM, or `now`."""
        if self.tpm_limit <= 0:
            return now
        # Cold start: serialize the FIRST send per key until we have a real
        # usage sample. Five threads each reserving a default estimate is how
        # 16k TPM still 429'd on Gemini's denser tokenizer.
        if int(getattr(self.cal, "_samples", 0) or 0) <= 0:
            if int(self.key_token_reserved.get(key, 0) or 0) > 0:
                return now + 0.15
        used = self._tpm_used_locked(key, now)
        # A single batch larger than the whole minute budget: only fire when the
        # key is idle — we cannot stay under the cap, but we can avoid stacking.
        if est > self.tpm_limit:
            if used == 0:
                return now
            events = self.key_token_events.get(key) or []
            if events:
                return events[0][0] + _TPM_WINDOW_S
            return now + 0.15  # waiting on another worker's reserve
        if used + est <= self.tpm_limit:
            return now
        free_needed = used + est - self.tpm_limit
        events = self.key_token_events.get(key) or []
        spent_only = sum(t for _, t in events)
        if free_needed > spent_only:
            # Need siblings to finish/unreserve — short poll.
            return now + 0.15
        freed = 0
        for ts, tok in events:
            freed += tok
            if freed >= free_needed:
                return ts + _TPM_WINDOW_S
        return now + 0.15

    def _tpm_reserve_locked(self, worker_idx: int, key: str, est: int) -> None:
        self.key_token_reserved[key] = self.key_token_reserved.get(key, 0) + est
        self.worker_tpm_reserve[worker_idx] = (
            self.worker_tpm_reserve.get(worker_idx, 0) + est
        )

    def _tpm_unreserve_locked(self, worker_idx: int, key: str,
                              amount: int | None = None) -> None:
        amt = (
            self.worker_tpm_reserve.get(worker_idx, 0)
            if amount is None else max(0, int(amount))
        )
        if amt <= 0:
            return
        self.key_token_reserved[key] = max(
            0, self.key_token_reserved.get(key, 0) - amt
        )
        left = self.worker_tpm_reserve.get(worker_idx, 0) - amt
        if left > 0:
            self.worker_tpm_reserve[worker_idx] = left
        else:
            self.worker_tpm_reserve.pop(worker_idx, None)

    def _tpm_commit(self, worker_idx: int, key: str, tokens: int) -> None:
        """Drop this worker's reserve and record a committed spend (if any)."""
        now = time.time()
        with self.cond:
            self._tpm_unreserve_locked(worker_idx, key)
            if tokens > 0:
                self.key_token_events.setdefault(key, []).append((now, int(tokens)))
                self._tpm_prune_locked(key, now)
            self.cond.notify_all()

    def _tpm_shrink_to_free(
        self, batch: list, cal: Calibrator, worker_key: str, fname: str,
    ) -> list:
        """If free TPM is partial, cut the batch so est fits NOW (use free budget).

        Fat batches that wait while free tokens sit idle is the usual "threads
        look stuck but TPM isn't full" symptom. Leftovers go back to the front of
        the file's pool under the same claim ownership model (this worker already
        popped them; we reinsert the tail only).
        """
        if self.tpm_limit <= 0 or len(batch) <= 1:
            return batch
        with self.cond:
            free = max(
                0,
                self.tpm_limit - self._tpm_used_locked(worker_key, time.time()),
            )
        if free <= 0:
            return batch  # nothing free — full wait, don't splinter
        est = self._est_batch_tokens(batch, cal)
        if est <= free:
            return batch
        # Largest prefix whose estimate fits free (at least 1 item — a single
        # oversized item still waits the normal gate).
        lo, hi = 1, len(batch)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            e = self._est_batch_tokens(batch[:mid], cal)
            if e <= free:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best >= len(batch):
            return batch
        leftover = batch[best:]
        kept = batch[:best]
        with self.cond:
            cur = self.pool.get(fname)
            if cur is None:
                self.pool[fname] = list(leftover)
            else:
                # Prefer prepend so the same scene continues next claim.
                self.pool[fname] = list(leftover) + list(cur)
            self.pool.move_to_end(fname, last=False)
            self.cond.notify_all()
        logger.info(
            "TPM shrink: free=%d est=%d → batch %d→%d (rest requeued)",
            free, est, len(batch), len(kept),
        )
        return kept

    def _wait_tpm(self, worker_idx: int, worker_key: str, est: int) -> bool:
        """Block until this key's TPM window can absorb `est` tokens, then reserve.

        Returns False if aborted / key died (no reserve held). Pause parks without
        burning the window; Continue rechecks immediately.

        UI contract: do NOT spam `waiting_tpm` every poll tick. Micro-waits for a
        sibling to unreserve (<0.5s ready_at) stay silent so worker cards don't
        blink "TPM" every 150–350ms. Longer drain waits emit once per changed
        `wait_left` (second-granularity), not 3×/s.
        """
        if self.tpm_limit <= 0 or est <= 0:
            return True
        # (kind, wait_left_or_free_bucket) of the last waiting_tpm we emitted —
        # re-emit only when the user-visible text would change.
        last_emit_key: tuple | None = None
        waiting_since: float | None = None
        while not self._is_aborted():
            if not self._park_while_paused(worker_idx):
                return False
            with self.cond:
                if worker_key in self.dead_keys:
                    return False
                now = time.time()
                ready_at = self._tpm_earliest_fit_locked(worker_key, est, now)
                if ready_at <= now + 0.001:
                    self._tpm_reserve_locked(worker_idx, worker_key, est)
                    return True
                wait_s = max(0.0, ready_at - now)
                bnum = self.worker_batch_no.get(worker_idx, self.batch_seq + 1)
                used = self._tpm_used_locked(worker_key, now)
                free = max(0, self.tpm_limit - used)
                spent = sum(t for _, t in self.key_token_events.get(worker_key, ()))
                reserved = int(self.key_token_reserved.get(worker_key, 0) or 0)
            if waiting_since is None:
                waiting_since = now
            # Short poll (sibling still holding a reserve): ready_at is ~now+0.15.
            # Real window drain has a multi-second ready_at from event timestamps.
            is_drain = wait_s >= 0.5
            waited = now - waiting_since
            # Stay silent for the first half-second of a pure sibling-poll so a
            # 200ms gap between unreserve and next send never paints "TPM wait".
            if is_drain or waited >= 0.5:
                if is_drain:
                    wait_left = int(math.ceil(wait_s))
                    emit_key: tuple = ("drain", wait_left)
                else:
                    # Slot wait: no honest countdown — free bucket (~1k) is enough
                    # so the card text stays put while free wiggles a few hundred tok.
                    wait_left = 0
                    emit_key = ("slot", free // 1000)
                if emit_key != last_emit_key:
                    last_emit_key = emit_key
                    self._emit(
                        worker_idx,
                        "waiting_tpm",
                        status=(
                            f"TPM {used}/{self.tpm_limit} "
                            f"(spent {spent} + R {reserved}, free {free}) — "
                            + (
                                f"wait {wait_left}s for ~{est} tok..."
                                if is_drain
                                else f"waiting slot for ~{est} tok (free {free})..."
                            )
                        ),
                        wait_left=wait_left,
                        batch_num=bnum,
                        tpm_est=est,
                    )
            # Sleep the remainder (capped) so we recheck soon when a sibling
            # unreserves; never a flat 1s that would re-lockstep threads.
            time.sleep(min(0.35, max(0.05, wait_s)))
        return False

    # -- pool primitives (call under self.cond) ---------------------------------

    def _remaining_locked(self) -> int:
        return sum(len(v) for v in self.pool.values())

    def _run_done_locked(self) -> bool:
        # Truly finished: nothing left AND nobody still holds a batch that could
        # be reclaimed back into the pool.
        return self._remaining_locked() == 0 and self.in_flight == 0

    def _next_file_locked(self):
        for fname, items in self.pool.items():
            if items:
                return fname, items
        return None, None

    def _effective_rank_locked(self, worker_idx: int) -> int:
        """Rank of this worker among the workers that are FREE TO CLAIM right now —
        alive (key not dead) AND not already holding a batch (not in-flight). Lower
        rank = higher priority. The ramp-down gate compares this to b_est (batches
        left in the POOL, which also excludes in-flight work), so the two sides are
        consistent: with B batches claimable, the B highest-priority FREE workers
        engage.

        Counting only free workers fixes two problems:
          * Dead key: its slots don't pin live workers in permanent rest (the
            original reason this wasn't the raw index — a real deadlock we hit).
          * Busy higher-priority worker: a free lower-priority worker no longer
            defers to a peer that's mid-request and can't take the work, which
            used to leave claimable strings idle and serialize the tail.

        The lowest-index ALIVE worker always has rank 0 while it's the one
        claiming, so the pool can never stall with work remaining."""
        rank = 0
        for j in range(worker_idx):
            kj = self.keys_to_use[j // self.threads]
            if kj in self.dead_keys:
                continue
            if j in self.in_flight_workers:
                continue
            rank += 1
        return rank

    # -- claim / finish ---------------------------------------------------------

    def claim_batch(self, worker_idx: int, cal: Calibrator, worker_key: str):
        """Atomically decide this worker's fate and, if claiming, pop one batch.

        Returns (_CLAIM, (fname, batch)) | (_REST, None) | (_DONE, None). Gate
        check and pop happen under ONE lock hold so the priority decision can't
        race the pool draining underneath it.

        While paused: never claim a NEW batch (that used to race the UI into
        "paused" cards that immediately flipped back to translating). Wait at
        this gate and emit `paused` with the lock released so `_emit` can't
        deadlock on `self.cond`."""
        req = self.req
        while True:
            # Pause gate OUTSIDE the pool lock — sleep + emit need the lock free
            # (`_emit` itself takes cond briefly for the done counter).
            if self.should_pause() and not self._is_aborted():
                with self.cond:
                    if self.aborted or worker_key in self.dead_keys:
                        return (_DONE, None)
                if not self._park_while_paused(
                    worker_idx,
                    batch_num=self.worker_batch_no.get(worker_idx, 0),
                    batch_size=0,
                ):
                    return (_DONE, None)
                continue

            with self.cond:
                while True:
                    if self.aborted:
                        return (_DONE, None)
                    if worker_key in self.dead_keys:
                        return (_DONE, None)

                    # Pause flipped on while we held the lock (sibling path) —
                    # drop the lock and re-enter the outer pause gate.
                    if self.should_pause():
                        break

                    # Honour this key's cooldown without blocking other keys: a short
                    # timed wait on the shared condition lets sibling keys proceed.
                    cd = self.key_cooldown.get(worker_key, 0.0)
                    wait_for = cd - time.time()
                    if wait_for > 0:
                        self.cond.wait(timeout=min(wait_for, 1.0))
                        continue

                    rem = self._remaining_locked()
                    if rem == 0:
                        if self._run_done_locked():
                            self.cond.notify_all()
                            return (_DONE, None)
                        # Pool's empty but a peer still holds a batch that might be
                        # reclaimed — wait to be woken rather than exit prematurely.
                        self.cond.wait(timeout=0.5)
                        continue

                    # Priority ramp-down: with B_est batches left, only the B_est
                    # highest-priority LIVE workers should engage. avg_batch_items is a
                    # shared running estimate (exact count is unknowable cheaply with
                    # adaptive sizing). Rank is computed among alive workers so a dead
                    # key's slots don't pin live workers in permanent rest.
                    b_est = max(1, math.ceil(rem / max(1.0, self.avg_batch_items)))
                    if self._effective_rank_locked(worker_idx) >= b_est:
                        if self._run_done_locked():
                            return (_DONE, None)
                        return (_REST, None)

                    if self.auto_group_small_files:
                        # Собираем кандидатов из разных файлов
                        candidates = []  # список кортежей (fname, item)
                        budget = cal.input_budget(self.window, req.glossary)
                        used = 0
                        max_b = req.max_batch_size

                        for fn, its in self.pool.items():
                            for it in its:
                                if len(candidates) >= max_b:
                                    break
                                cost = cal.est_tokens(it.text) + cal.est_tokens(it.context) + 12
                                if len(candidates) > 0 and used + cost > budget:
                                    break
                                used += cost
                                candidates.append((fn, it))
                            if len(candidates) >= max_b:
                                break

                        if not candidates:
                            self.cond.wait(timeout=0.5)
                            continue

                        # Проверяем overflows_exact и уменьшаем кандидатов, если нужно
                        while len(candidates) > 1:
                            items_only = [x[1] for x in candidates]
                            if self._overflows_exact(cal, items_only, worker_key):
                                candidates = candidates[:len(candidates) // 2]
                            else:
                                break

                        # Сгруппируем удаление из pool по файлам
                        remove_counts = {}
                        for fn, it in candidates:
                            remove_counts[fn] = remove_counts.get(fn, 0) + 1

                        for fn, count in remove_counts.items():
                            del self.pool[fn][:count]
                            if not self.pool[fn]:
                                del self.pool[fn]

                        batch = [x[1] for x in candidates]
                        fname = candidates[0][0]
                    else:
                        fname, items = self._next_file_locked()
                        if items is None:
                            self.cond.wait(timeout=0.5)
                            continue

                        # Token-pack one batch from the front of this file; never mix files.
                        end = cal.next_batch(items, self.window, req.glossary, 0,
                                             req.max_batch_size)
                        while (end) > 1 and self._overflows_exact(cal, items[:end], worker_key):
                            end = end // 2
                        batch = items[:end]
                        del items[:end]
                        if not items:
                            # Keep the key in the map but emptied; cleaned lazily.
                            pass
                    self.in_flight += 1
                    self.in_flight_workers.add(worker_idx)
                    # Per-worker Nth claim (1, 2, 3…) for the card. Global
                    # batch_seq jumps 4→81 on one thread and looks like a bug.
                    self.batch_seq += 1
                    n = self.worker_claim_n.get(worker_idx, 0) + 1
                    self.worker_claim_n[worker_idx] = n
                    self.worker_batch_no[worker_idx] = n
                    return (_CLAIM, (fname, batch))
            # Inner loop broke only for pause re-check → outer gate.

    def _overflows_exact(self, cal: Calibrator, batch: list, worker_key: str) -> bool:
        """Optional pre-send guard for providers with a cheap exact tokenizer."""
        if len(batch) <= 1:
            return False
        cfg = ProviderConfig(
            base_url=self.req.base_url,
            api_key=worker_key,
            model=self.req.model,
            num_ctx=self.req.max_context_tokens,
            timeout_seconds=float(getattr(self.req, "timeout_seconds", 0) or 0),
        )
        exact = self.provider.count_tokens(
            build_prompt(batch, self.req.target_lang, self.req.glossary,
                         self.req.engine, self.req.extra_instruction), cfg
        )
        if exact is None:
            return False
        return exact > cal.input_budget(self.window, self.req.glossary)

    def finish_batch(self, worker_idx: int, batch: list, tr_map: dict[str, str], key_died: bool) -> None:
        """Fold a finished batch back into shared state. ALWAYS call from finally:
        if key_died, un-translated strings return to the pool so a surviving key
        picks them up — a crash here must never strand them."""
        with self.cond:
            self.in_flight_workers.discard(worker_idx)
            if tr_map:
                self.result.update(tr_map)
                a = 0.4
                self.avg_batch_items = (
                    (1 - a) * self.avg_batch_items + a * len(batch)
                )
            if key_died:
                leftover = [it for it in batch if it.id not in tr_map]
                for it in leftover:
                    n = self.reclaim_count.get(it.id, 0) + 1
                    self.reclaim_count[it.id] = n
                    if n <= len(self.keys_to_use):
                        fname = self.item_file.get(it.id, "")
                        self.pool.setdefault(fname, [])
                        self.pool[fname].insert(0, it)
                        self.pool.move_to_end(fname, last=False)
            self.in_flight -= 1
            self.cond.notify_all()

    # -- the blocking send, with retries + elapsed ticks ------------------------

    def _send_once(self, batch, cfg, worker_idx, try_i, try_suffix, start_time):
        """Run provider.translate in a sub-thread so the worker keeps emitting an
        `elapsed` tick while the blocking HTTP call is in flight (its grid card
        would otherwise freeze for the whole request)."""
        # Stamp the fire time at SEND, not at post-batch pace — RPM spacing is
        # "since the last request left", and a long Pause must still count that
        # wall-clock gap (see _pace_delay / _retry_sleep).
        self.worker_last_request[worker_idx] = time.time()
        res_queue: "queue.Queue[tuple[bool, object]]" = queue.Queue()

        def sub_worker():
            try:
                prompt = build_prompt(
                    batch, self.req.target_lang, self.req.glossary,
                    self.req.engine, self.req.extra_instruction,
                )
                tr_res = self.provider.complete_prompt(prompt, batch, cfg)
                res_queue.put((True, tr_res))
            except Exception as e_thread:  # noqa: BLE001 — surfaced to retry loop
                res_queue.put((False, e_thread))

        thread = threading.Thread(target=sub_worker, daemon=True)
        thread.start()

        tr = None
        last_yield = time.time()
        # First pause tick during this request forces an immediate finishing
        # event (don't wait the full 1s cadence) so the card flips as soon as
        # the user hits pause.
        saw_pause = False
        while thread.is_alive() and not self._is_aborted():
            try:
                ok, val = res_queue.get(timeout=0.5)
                if ok:
                    tr = val
                else:
                    raise val
                break
            except queue.Empty:
                now = time.time()
                paused_now = self.should_pause()
                due = (now - last_yield) >= 1.0
                pause_edge = paused_now and not saw_pause
                if not (due or pause_edge):
                    continue
                last_yield = now
                saw_pause = saw_pause or paused_now
                elapsed = int(now - start_time)
                # Pause never aborts an in-flight HTTP call — but the card must
                # not keep saying "translating" as if pause did nothing, nor
                # flip to "paused" while the request is still burning tokens.
                if paused_now:
                    self._emit(
                        worker_idx,
                        "finishing_batch",
                        status=f"Finishing batch before pause "
                        f"({len(batch)} strings)... [{elapsed}s]",
                        batch_num=self.worker_batch_no.get(
                            worker_idx, self.batch_seq + 1
                        ),
                        batch_size=len(batch),
                        try_i=try_i,
                        elapsed=elapsed,
                    )
                else:
                    self._emit(
                        worker_idx,
                        "translating_batch",
                        status=f"Translating batch ({len(batch)} strings)"
                        f"{try_suffix}... [{elapsed}s]",
                        batch_num=self.worker_batch_no.get(
                            worker_idx, self.batch_seq + 1
                        ),
                        batch_size=len(batch),
                        try_i=try_i,
                        elapsed=elapsed,
                    )
        if tr is None and not self._is_aborted():
            ok, val = res_queue.get(timeout=1.0)
            if ok:
                tr = val
            else:
                raise val
        if tr is None:
            raise RuntimeError("Translation thread terminated without result.")
        return tr

    def _translate_with_retries(self, batch, cal, worker_idx, worker_key):
        """Drive one owned batch to completion. Returns (tr_map, key_died,
        safety_skip). key_died=True means the key exhausted its retry budget on
        this batch (circuit breaker) — its leftover strings will be reclaimed."""
        req = self.req
        cfg = ProviderConfig(
            base_url=req.base_url,
            api_key=worker_key,
            model=req.model,
            num_ctx=req.max_context_tokens,
            timeout_seconds=float(getattr(req, "timeout_seconds", 0) or 0),
        )
        prompt_chars = len(build_prompt(batch, req.target_lang, req.glossary,
                                        req.engine, req.extra_instruction))
        batch_tr: dict[str, str] = {}
        last_err = None
        auth_fails = 0  # consecutive auth-class failures → kill the key fast

        for try_i in range(BATCH_TRIES):
            if self._is_aborted() or worker_key in self.dead_keys:
                return ({}, False, False)
            # After a failed attempt the user may have hit Pause — do not fire
            # the next try until they Continue. (In-flight HTTP still always
            # runs to completion; only the gap BETWEEN tries honours pause.)
            if try_i > 0 and not self._wait_if_paused_before_retry(
                worker_idx, batch, try_i
            ):
                return ({}, False, False)

            start_time = time.time()
            try_suffix = f" (retry {try_i + 1}/{BATCH_TRIES})" if try_i > 0 else ""
            # TPM gate BEFORE the request leaves: estimate from our prompt text,
            # wait until this key's 60s window has room, reserve so siblings on
            # the same key cannot over-book. Other keys are unaffected.
            est_tokens = self._est_batch_tokens(batch, cal)
            if not self._wait_tpm(worker_idx, worker_key, est_tokens):
                return ({}, False, False)
            tpm_held = self.tpm_limit > 0
            self._emit(
                worker_idx,
                "translating_batch",
                status=f"Translating batch ({len(batch)} strings){try_suffix}...",
                batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq + 1),
                batch_size=len(batch),
                try_i=try_i,
                elapsed=0,
            )
            try:
                tr = self._send_once(
                    batch, cfg, worker_idx, try_i, try_suffix, start_time
                )
                if self._is_aborted():
                    if tpm_held:
                        self._tpm_commit(worker_idx, worker_key, 0)
                    return ({}, False, False)
                if not tr.translations:
                    raise RuntimeError(
                        "Model response parsed successfully, but returned 0 "
                        "translated strings matching input keys."
                    )
                batch_tr = tr.translations

                # Oversized-translation re-ask (Ren'Py char limits) — identical to
                # the legacy path so the per-line cap is still enforced. Re-asks
                # are real API calls and MUST count toward TPM (spent), not just
                # the main batch.
                reask_tok = self._enforce_char_limits(batch, batch_tr, cfg)

                actual_tok = int(tr.usage.billed_tokens() or 0) + int(reask_tok or 0)
                if tpm_held:
                    # Prefer exact usage; fall back to the pre-send estimate if
                    # the provider returned zeros (unknown).
                    self._tpm_commit(
                        worker_idx, worker_key,
                        actual_tok if actual_tok > 0 else est_tokens,
                    )
                    tpm_held = False
                with self.cond:
                    # Observe under the lock: this is the SHARED calibrator.
                    cal.observe(prompt_chars, tr.usage)
                    self.tok_in += tr.usage.prompt_tokens
                    self.tok_out += tr.usage.completion_tokens
                    self.batches += 1
                    self.requests_sent += 1  # a successful request reached the server
                    self.chars_per_token = cal.chars_per_token
                    self.output_ratio = cal.out_ratio
                return (batch_tr, False, False)

            except Exception as e:  # noqa: BLE001
                err_str = str(e)
                is_safety = "GEMINI_SAFETY_BLOCK" in err_str
                batch_contents = [{"id": it.id, "text": it.text} for it in batch]
                logger.error(
                    "[Worker %d] Batch attempt %d failed: %s. Batch items: %s",
                    worker_idx,
                    try_i + 1,
                    e,
                    json.dumps(batch_contents, ensure_ascii=False)[:2000],
                )
                last_err = e
                # Live error for the UI session log — every failed attempt, not
                # only terminal ones (so 429 spam / auth / parse fail is visible
                # mid-run without opening interprex.log).
                bnum = self.worker_batch_no.get(worker_idx, self.batch_seq + 1)
                reached = _reached_server(err_str)
                kind = _classify_error(err_str)
                err_class = "network" if not reached else kind
                net_status = (
                    f"Network drop (try {try_i + 1}/{BATCH_TRIES}) — "
                    f"same batch will retry, strings are kept"
                    if err_class == "network"
                    else f"Batch error (try {try_i + 1}): {err_str[:220]}"
                )
                self._emit(
                    worker_idx,
                    "batch_error",
                    status=net_status,
                    last_error=err_str[:800],
                    error_class=err_class,
                    batch_num=bnum,
                    batch_size=len(batch),
                    try_i=try_i,
                )
                # A failed request that still reached the server spent the quota.
                # TPM: commit the estimate (no usage on error); pure connection
                # failures release the reserve without counting spend.
                if tpm_held:
                    if _reached_server(err_str):
                        self._tpm_commit(worker_idx, worker_key, est_tokens)
                    else:
                        self._tpm_commit(worker_idx, worker_key, 0)
                    tpm_held = False
                if _reached_server(err_str):
                    with self.cond:
                        self.requests_sent += 1
                if is_safety:
                    logger.warning(
                        "[Worker %d] Safety block detected. Skipping this batch.",
                        worker_idx,
                    )
                    with self.cond:
                        self.errors.append(f"[Worker {worker_idx + 1}] {e}")
                        self.batches += 1
                    return ({}, False, True)

                if kind == "auth":
                    # An invalid/expired key won't fix itself this run. Give it a
                    # couple of grace tries (a 401 can be a transient edge blip),
                    # then declare the key dead so its work fails over fast rather
                    # than burning ~26 minutes of pointless retries.
                    auth_fails += 1
                    if auth_fails >= 2:
                        logger.error(
                            "[Worker %d] Key looks invalid (%s). Failing it over.",
                            worker_idx, err_str[:200],
                        )
                        with self.cond:
                            self.errors.append(f"[Worker {worker_idx + 1}] {e}")
                        return ({}, True, False)
                else:
                    auth_fails = 0

                if try_i < BATCH_TRIES - 1:
                    # User paused after/during this failure: hold the owned batch
                    # and do NOT auto-retry until Continue. (They hit Pause
                    # because something is wrong — hammering the provider is
                    # the opposite of what they asked for.)
                    if self.should_pause():
                        if not self._wait_if_paused_before_retry(
                            worker_idx, batch, try_i + 1
                        ):
                            return ({}, False, False)
                        # Unpaused: fall through to normal back-off then retry.
                    # rate/overload replies STILL spend the per-minute quota on
                    # every cloud API, so wait at least the pacing delay before
                    # re-sending — and record a per-key cooldown so siblings on
                    # this key wait too while other keys keep working.
                    #
                    # CRITICAL: delay_seconds==0 (user set only TPM, left RPM
                    # empty) must NOT skip the multi-thread cooldown. Without
                    # it, only this worker backs off 8s while siblings keep
                    # firing → 429 storm, "TPM ignored" symptom. Floor to a
                    # fixed no-RPM cooldown (or the RPM-derived delay).
                    rate_floor = 0.0
                    if kind == "rate":
                        rate_floor = (
                            self.delay_seconds
                            if self.delay_seconds > 0
                            else _RATE_COOLDOWN_NO_RPM_S
                        )
                    floor = rate_floor
                    if kind == "rate" and rate_floor > 0:
                        # The failed request still consumed a quota slot, so
                        # the cooldown starts from NOW — siblings honour it.
                        with self.cond:
                            self.key_cooldown[worker_key] = max(
                                self.key_cooldown.get(worker_key, 0.0),
                                time.time() + rate_floor,
                            )
                    # Stagger sibling retries so threads that all hit an error at
                    # once don't re-fire in lockstep (which would just re-trigger
                    # the same rate limit). Spread evenly across the full delay
                    # window so N threads on one key each fire at a different
                    # second, keeping the peak RPM at N/delay ≤ 1 request per
                    # thread-period.
                    stagger = 0.0
                    if rate_floor > 0 and self.threads > 1:
                        rank = worker_idx % self.threads
                        stagger = rate_floor * rank / self.threads
                    self._retry_sleep(worker_idx, batch, try_i, start_time,
                                      min_wait=floor, stagger=stagger)

        # Exhausted all retries → this key is dead (circuit breaker FAIL_STREAK=1).
        if last_err is not None:
            with self.cond:
                self.errors.append(f"[Worker {worker_idx + 1}] {last_err}")
        return ({}, True, False)

    # Width slack before re-asking: only kerning/shaping (advance widths sum
    # slightly looser than real layout). The old 1.1 absorbed the len()-vs-px
    # approximation; with a true pixel measurement that error is gone, so the
    # tolerance shrinks to a small, physically-meaningful margin.
    _PX_TOLERANCE = 1.03
    # HEURISTIC FALLBACK (used when we can't read the real box from the engine).
    # Slack on top of the original caption's measured width when computing a UI
    # button/menu budget. The source word is rarely the box: buttons have padding
    # and our menu choices wrap (choice_button ysize=None + 'subtitle' layout in
    # _interprex_font.rpy), so the translation has more room than the bare original
    # width. Without this, "Save" (4 chars) crushes "Сохранение" (10) to "Сох".
    # The authoritative fit is the runtime auto-fit, which inherits the true box
    # from Text.render — this number only guards the no-engine path.
    _UI_WIDTH_SLACK = 1.6
    # Floor (in target-script chars) every UI caption budget is raised to, so a
    # standard menu word — Сохранение / Настройки / Продолжить / Загрузить — always
    # fits regardless of how short the English source is.
    _MIN_CAPTION_CHARS = 12
    # How many times we re-ask the model to shorten a still-overflowing caption
    # before giving up and recording a font-shrink factor for inject. Each round
    # costs a request, so keep it small; 2 catches almost everything.
    _MAX_REASKS = 2

    def _overflow_ratio(self, item_id: str, translation: str) -> float:
        """tr_px / orig_px for `item_id` (1.0 == exactly the original's width).
        Returns 0.0 when no pixel budget is known (caller treats as 'fits')."""
        orig_px = self.item_orig_px.get(item_id)
        if not orig_px:
            return 0.0
        try:
            from parsers.renpy import measure_translation_px

            tr_px = measure_translation_px(
                translation, self.req.target_lang, self.font_size,
                self._font_style,
            )
            return tr_px / orig_px
        except Exception:
            return 0.0

    def _overflows(self, item_id: str, translation: str) -> bool:
        """True if `translation` overruns its budget.

        - Ren'Py fixed-width captions: rendered pixel WIDTH vs original budget.
        - RimWorld research-tree buttons: wrapped LINE COUNT vs max_lines.
        - Fallback: char hint * 1.1 when no pixel/line ground truth exists."""
        # Line-count path (research tree) — ground truth is wrap lines, not width.
        max_lines = self.item_max_lines.get(item_id)
        if max_lines:
            try:
                from parsers.i18n import research_label_overflows
                lang = self.item_line_lang.get(item_id) or self.req.target_lang
                return research_label_overflows(translation, lang, max_lines)
            except Exception:
                limit = self.item_limits.get(item_id)
                return limit is not None and len(translation) > limit * 1.1
        orig_px = self.item_orig_px.get(item_id)
        if orig_px:
            ratio = self._overflow_ratio(item_id, translation)
            if ratio:
                return ratio > self._PX_TOLERANCE
        limit = self.item_limits.get(item_id)
        return limit is not None and len(translation) > limit * 1.1

    # A translation with this many words OR FEWER is never re-asked "shorter":
    # asking the model to shrink "Сохранение" (1 word) only yields a butchered
    # abbreviation ("Сох"). Instead we keep the FULL word and shrink the font
    # (size_overrides). Only multi-word captions (3+), where a synonym/rephrase can
    # genuinely shorten without mangling, go through the re-ask. User rule:
    # "≤2 words → full word + font shrink; 3+ words → may re-ask shorter".
    # NOTE: research-tree line limits do NOT use font-shrink (engine has none) —
    # those always re-ask, even for 1–2 words.
    _MAX_WORDS_FONT_FIT = 2

    def _enforce_char_limits(self, batch, batch_tr, cfg) -> int:
        """Make any translation that overran its UI budget fit.

        Ren'Py fixed-width captions — WITHOUT ever butchering a word into an
        abbreviation. Two paths by word count:
        - 1–2 words (e.g. "Сохранение"): keep the word WHOLE, record a font-shrink
          factor (size_overrides) so inject renders it smaller but intact. No re-ask.
        - 3+ words: re-ask the model for a shorter rephrase up to _MAX_REASKS times
          (a synonym can shorten honestly here), then font-shrink whatever still
          overflows.

        RimWorld research-tree buttons (item_max_lines): always re-ask shorter
        (line-count ground truth). No font-shrink — the research UI has none. If
        still over after re-asks, keep the last model text + log (fail-soft).

        Either way the final text is the model's own — never cut — and a short
        Ren'Py caption never degrades to "Сох".

        Returns billed tokens spent on re-ask API calls (for TPM ledger).
        """
        all_oversized = [
            it for it in batch
            if it.id in batch_tr and self._overflows(it.id, batch_tr[it.id])
        ]
        if not all_oversized:
            return 0

        # Partition: research line-limits always re-ask; Ren'Py short captions
        # skip the re-ask (font-shrink only).
        def _word_count(s: str) -> int:
            return len(s.split())

        line_limited = [it for it in all_oversized if it.id in self.item_max_lines]
        renpy_oversized = [it for it in all_oversized if it.id not in self.item_max_lines]

        short_fit = [it for it in renpy_oversized
                     if _word_count(batch_tr[it.id]) <= self._MAX_WORDS_FONT_FIT]
        oversized = [it for it in renpy_oversized
                     if _word_count(batch_tr[it.id]) > self._MAX_WORDS_FONT_FIT]
        # Research labels always go through re-ask (even 1-word giants that wrap).
        oversized = line_limited + oversized

        # Record font-shrink for the Ren'Py short ones right away — full word preserved.
        for it in short_fit:
            self._record_font_shrink(it.id, batch_tr[it.id])

        if not oversized:
            return 0
        logger.info(
            "Detected %d translations exceeding UI budget "
            "(%d research line-limit, %d multi-word width). "
            "Re-asking shorter (%d short Ren'Py captions font-shrunk)...",
            len(oversized), len(line_limited),
            len(oversized) - len(line_limited), len(short_fit),
        )

        reask_billed = 0
        for round_no in range(self._MAX_REASKS):
            retry_batch = []
            for it in oversized:
                prev = batch_tr[it.id]
                base = self.item_limits.get(it.id) or len(prev)
                max_lines = self.item_max_lines.get(it.id)
                if max_lines:
                    # Line-limit: tighten char hint ~15% per re-ask so the model
                    # gets a concrete smaller number, not just "shorter".
                    tighter = max(4, int(min(base, len(prev)) * (0.85 ** (round_no + 1))))
                    retry_batch.append(
                        TranslateItem(
                            id=it.id, text=it.text, context=it.context,
                            max_chars=tighter, max_lines=max_lines,
                        )
                    )
                else:
                    ratio = self._overflow_ratio(it.id, prev) or 1.15
                    # Aim a bit under the budget: tighten the char target by the
                    # measured overflow ratio so the model has a concrete smaller
                    # number to hit, not a vague "shorter".
                    tighter = max(3, int(min(base, len(prev)) / max(ratio, 1.01)))
                    retry_batch.append(
                        TranslateItem(id=it.id, text=it.text, context=it.context,
                                      max_chars=tighter)
                    )
            try:
                retry_prompt = build_prompt(
                    retry_batch, self.req.target_lang, self.req.glossary,
                    self.req.engine, self.req.extra_instruction,
                )
                retry_tr = self.provider.complete_prompt(retry_prompt, retry_batch, cfg)
            except Exception as e_retry:  # noqa: BLE001
                logger.error("Retry translation failed: %s", e_retry)
                break
            reask_billed += int(retry_tr.usage.billed_tokens() or 0)
            with self.cond:
                self.tok_in += retry_tr.usage.prompt_tokens
                self.tok_out += retry_tr.usage.completion_tokens
                self.requests_sent += 1  # re-ask also hit the provider
            # Keep a re-ask result only if it actually fits better than what we had.
            for rit in retry_batch:
                if rit.id not in retry_tr.translations:
                    continue
                r = retry_tr.translations[rit.id]
                if rit.id in self.item_max_lines:
                    # Prefer fewer lines; if equal lines, prefer shorter char count.
                    try:
                        from parsers.i18n import research_label_line_count
                        lang = self.item_line_lang.get(rit.id) or self.req.target_lang
                        old_n = research_label_line_count(batch_tr[rit.id], lang)
                        new_n = research_label_line_count(r, lang)
                        if new_n < old_n or (new_n == old_n and len(r) <= len(batch_tr[rit.id])):
                            batch_tr[rit.id] = r
                    except Exception:
                        batch_tr[rit.id] = r
                elif self._overflow_ratio(rit.id, r) <= self._overflow_ratio(rit.id, batch_tr[rit.id]) or not self.item_orig_px.get(rit.id):
                    batch_tr[rit.id] = r
            oversized = [it for it in oversized if self._overflows(it.id, batch_tr[it.id])]
            if not oversized:
                return reask_billed

        # Still overflowing: Ren'Py → font-shrink; research → keep text + warn.
        for it in oversized:
            if it.id in self.item_max_lines:
                logger.warning(
                    "Research button id '%s' still exceeds %d lines after re-asks; "
                    "keeping model text (no truncate, no font-shrink available).",
                    it.id, self.item_max_lines[it.id],
                )
            else:
                self._record_font_shrink(it.id, batch_tr[it.id])
        return reask_billed

    # Font-shrink floor: never make a caption smaller than this fraction of the
    # game's own size — below it text is unreadable, better to let it ride a hair
    # wide than render 8px ants. Mirrors the inject-side floor.
    _FONT_SHRINK_FLOOR = 0.6

    def _record_font_shrink(self, item_id: str, translation: str) -> None:
        """Record a measured font-shrink factor (<1.0) for `item_id` so inject
        renders that caption's style smaller — keeping the FULL word intact. Clamped
        to _FONT_SHRINK_FLOOR for readability."""
        ratio = self._overflow_ratio(item_id, translation)
        if ratio <= self._PX_TOLERANCE:
            return
        factor = max(self._FONT_SHRINK_FLOOR, (1.0 / ratio) * self._PX_TOLERANCE)
        with self.cond:
            prev = self.size_overrides.get(item_id, 1.0)
            self.size_overrides[item_id] = min(prev, factor)
        logger.info(
            "Caption id '%s' too wide; font shrink factor %.3f recorded "
            "(word kept whole, not abbreviated).", item_id, factor,
        )

    # -- pause / pacing helpers -------------------------------------------------

    def _park_while_paused(self, worker_idx: int, **extra) -> bool:
        """Emit `paused` ONCE, then sleep until Continue/abort.

        Spamming the event every 0.5s made every card (and the header) flip
        пауза ↔ дописываю ↔ resting as React reapplied the same phase. Sleep
        still ticks so Continue is noticed immediately.
        Returns False if aborted.
        """
        if not self.should_pause() or self._is_aborted():
            return not self._is_aborted()
        extra.setdefault("status", "Paused")
        extra.setdefault("batch_num", self.worker_batch_no.get(worker_idx, 0))
        extra.setdefault("batch_size", extra.get("batch_size", 0))
        self._emit(worker_idx, "paused", **extra)
        while self.should_pause() and not self._is_aborted():
            time.sleep(0.5)
        return not self._is_aborted()

    def _wait_if_paused_before_retry(self, worker_idx, batch, next_try_i: int) -> bool:
        """If pause is on after a batch failure, hold until Continue (or abort).

        Returns False if aborted (caller should drop out without killing the key
        for a pause — the batch stays owned only until we exit; reclaim path
        still runs via the worker finally if the key dies later). Returns True
        when the worker may proceed with the next try.
        """
        if not self.should_pause() or self._is_aborted():
            return not self._is_aborted()
        return self._park_while_paused(
            worker_idx,
            status=(
                f"Paused after error — press Continue to retry "
                f"(try {next_try_i + 1}/{BATCH_TRIES})"
            ),
            batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq + 1),
            batch_size=len(batch),
            try_i=max(0, next_try_i - 1),
            last_error=None,
        )

    def _retry_sleep(self, worker_idx, batch, try_i, start_time, min_wait=0.0,
                     stagger=0.0) -> None:
        """Back-off between retries on an OWNED batch.

        Uses an absolute wall-clock deadline (`ready_at`). Pause freezes ACTION
        only (no auto-retry until Continue) — it does NOT re-arm a full backoff
        when the user resumes after a long Pause. If the deadline already passed
        while they were parked, Continue fires immediately. Abort interrupts.
        min_wait raises the floor for rate/overload; stagger spreads siblings.
        """
        sleep_time = max(min_wait,
                         _RETRY_BACKOFF_FIRST if try_i == 0 else _RETRY_BACKOFF_REST)
        sleep_time += stagger
        ready_at = time.time() + sleep_time
        while not self._is_aborted():
            # Pause mid-backoff: park until Continue, then re-check the SAME
            # ready_at (do not restart the full wait — RPM/backoff already
            # advanced on the wall clock while the user was away).
            if self.should_pause():
                if not self._wait_if_paused_before_retry(
                    worker_idx, batch, try_i + 1
                ):
                    return
                continue
            now = time.time()
            remaining = ready_at - now
            if remaining <= 0:
                break
            wait_left = int(max(0, math.ceil(remaining)))
            elapsed = int(now - start_time) if start_time else 0
            self._emit(
                worker_idx,
                "waiting_retry",
                status=f"Waiting before retry ({wait_left}s left)...",
                batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq + 1),
                batch_size=len(batch),
                try_i=try_i,
                elapsed=elapsed,
                wait_left=wait_left,
            )
            # Sleep the EXACT remainder (capped at 1s for abort/pause responsiveness).
            # Flat 1.0s would round sub-second stagger up and re-lockstep siblings.
            time.sleep(min(1.0, remaining))

    def _pace_delay(self, worker_idx, t0, stagger_offset=0.0) -> None:
        """Sleep only until last_fire + delay_seconds (wall-clock RPM spacing).

        `worker_last_request` is stamped in `_send_once` when the request leaves.
        This runs AFTER a batch finishes: if the HTTP call already ate most of
        the delay window, remaining is small/zero — we never re-arm a full delay
        from "now". Pause parks the worker without rewriting the deadline; after
        a long Pause, Continue proceeds immediately if the window already passed.
        stagger_offset is unused for post-batch pacing (initial spread is
        `_initial_stagger`); kept for call-site compatibility.
        """
        if self.delay_seconds <= 0:
            return
        last = self.worker_last_request.get(worker_idx)
        if last is None:
            # No request stamped yet (shouldn't happen after a successful batch);
            # fall back to batch-start + stagger so we never wait forever.
            last = t0 + stagger_offset - self.delay_seconds
        target = last + self.delay_seconds
        while not self._is_aborted():
            # Pause: park only. Do NOT move `target` forward — wall-clock is the
            # ground truth for provider RPM.
            if not self._park_while_paused(worker_idx):
                return
            remaining = target - time.time()
            if remaining <= 0:
                return
            wait_left = int(math.ceil(remaining))
            # batch_num = the batch just finished — UI shows
            # "Batch N done — waiting Xs" so the RPM wait is obvious.
            bnum = self.worker_batch_no.get(worker_idx, 0)
            self._emit(
                worker_idx,
                "waiting_delay",
                status=(
                    f"Batch {bnum} done — waiting {wait_left}s..."
                    if bnum
                    else f"Pacing ({wait_left}s left)..."
                ),
                wait_left=wait_left,
                batch_num=bnum or None,
            )
            time.sleep(min(1.0, remaining))

    # -- the worker loop --------------------------------------------------------

    def _initial_stagger(self, worker_idx: int) -> None:
        """Sleep before the very first claim so threads on the same key don't
        all fire at t=0.

        Spacing = delay_seconds / threads — the same interval the UI's RPM cap
        implies (5 threads, 40 RPM, delay=7.5s → 0 / 1.5 / 3 / 4.5 / 6s).
        A 2s cap used to bunch threads 2..N at t=2, which is a 3-request burst
        and the usual "I set 40 RPM and still 429" first-second.
        """
        if self.delay_seconds <= 0 or self.threads <= 1:
            return
        rank = worker_idx % self.threads
        offset = self.delay_seconds * rank / self.threads
        if offset <= 0:
            return
        start = time.time()
        while not self._is_aborted():
            remaining = offset - (time.time() - start)
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def worker_loop(self, worker_idx: int, worker_key: str) -> None:
        # Shared run calibrator — see self.cal. Do NOT construct a per-thread
        # one: the first burst then all use language defaults and under-reserve.
        cal = self.cal
        # Per-thread stagger offset within the key group. Threads on the same
        # key fire at stagger, stagger+delay, stagger+2*delay, … so they're
        # always spaced by delay/threads seconds — not all at once.
        thread_rank = worker_idx % self.threads if self.threads > 1 else 0
        stagger_offset = (self.delay_seconds * thread_rank / self.threads
                          if self.delay_seconds > 0 and self.threads > 1 else 0.0)
        self._initial_stagger(worker_idx)
        while not self._is_aborted():
            kind, payload = self.claim_batch(worker_idx, cal, worker_key)
            if kind == _DONE:
                self._emit(worker_idx, "done", status="Done")
                return
            if kind == _REST:
                self._emit(worker_idx, "resting", status="Resting")
                # Block until state changes (a peer finished or reclaimed), so a
                # resting worker neither busy-spins nor misses returned work.
                with self.cond:
                    if not self._run_done_locked():
                        self.cond.wait(timeout=0.5)
                continue

            _fname, batch = payload
            # Use free TPM now: shrink a fat batch if partial budget remains so
            # free tokens aren't left idle while we wait for a full-size send.
            batch = self._tpm_shrink_to_free(batch, cal, worker_key, _fname)
            t0 = time.time()
            tr_map: dict[str, str] = {}
            key_died = False
            try:
                tr_map, key_died, _safety = self._translate_with_retries(
                    batch, cal, worker_idx, worker_key
                )
            finally:
                # Fold into result FIRST, then emit completed_batch. Emitting
                # before finish_batch made `done=len(result)` lag this batch AND
                # race sibling workers: a late completed event could carry a
                # STALE lower done than a peer already reported → main progress
                # bar jumped 89→15→40 as NDJSON reordered concurrent emits.
                self.finish_batch(worker_idx, batch, tr_map, key_died)

            # Emit translations after result is updated so `done` includes this
            # batch. Frontend still monotonic-maxes done as a belt-and-suspenders.
            if tr_map:
                self._emit(
                    worker_idx,
                    "completed_batch",
                    status="Completed batch!",
                    translations=self._fan_out(tr_map),
                    batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq),
                )

            if key_died:
                with self.cond:
                    self.dead_keys.add(worker_key)
                    self.cond.notify_all()
                self._emit(worker_idx, "error", status="Error: key failed")
                return

            self._pace_delay(worker_idx, t0, stagger_offset)

    # -- the public stream ------------------------------------------------------

    def _spawn_and_drain(self, assignment):
        """Spawn the given (worker_idx, key) workers and yield their NDJSON events
        until they all finish. `assignment` is a list of (idx, key)."""
        threads = []
        for idx, key in assignment:
            t = threading.Thread(target=self.worker_loop, args=(idx, key), daemon=True)
            t.start()
            threads.append(t)
        while any(t.is_alive() for t in threads) or not self.event_queue.empty():
            try:
                yield self.event_queue.get(timeout=0.1) + "\n"
            except queue.Empty:
                pass

    def stream(self):
        """Generator of NDJSON lines: progress events, then one final `done`."""
        try:
            # First paint MUST carry the TPM ledger when a cap is set — the UI
            # seeds bars from this event. Omitting tpm_* here left the panel
            # blank until a worker emitted (and never showed if an old sidecar
            # path never paced), which read as "TPM ignored".
            init_evt: dict = {
                "type": "progress",
                "done": self._done_count(),
                "total": self.total,
                "batches": 0,
                "status": "Initializing translator...",
                "phase": "initializing",
                "translations": {},
                "worker_idx": 0,
                "key_idx": 0,
            }
            if self.tpm_limit > 0:
                with self.cond:
                    tpm_snap = self._tpm_snapshot_locked(now=time.time())
                init_evt["tpm_limit"] = tpm_snap["limit"]
                init_evt["tpm_keys"] = tpm_snap["keys"]
                if tpm_snap["keys"]:
                    row0 = tpm_snap["keys"][0]
                    init_evt["tpm_used"] = row0["used"]
                    init_evt["tpm_spent"] = row0["spent"]
                    init_evt["tpm_reserved"] = row0["reserved"]
                    init_evt["tpm_free"] = row0["free"]
            yield json.dumps(init_evt, ensure_ascii=False) + "\n"

            # Fill the pool (fresh mutable copy of the prepared, file-ordered reps).
            with self.cond:
                self.pool = OrderedDict(
                    (f, list(items)) for f, items in self._prepared_by_file.items()
                )

            assignment = [
                (i, self.keys_to_use[i // self.threads])
                for i in range(self.worker_count)
            ]
            logger.info(
                "Spawning %d workers across %d key(s) (threads=%d, delay=%.1fs, tpm=%d)",
                self.worker_count, len(self.keys_to_use), self.threads,
                self.delay_seconds, self.tpm_limit,
            )
            yield from self._spawn_and_drain(assignment)

            # Auto-finish sweep: any rep still missing (e.g. its key died mid-run)
            # gets one more pass on the keys that are still alive.
            if not self._is_aborted():
                missing = [it for it in self.reps if it.id not in self.result]
                alive_keys = [k for k in self.keys_to_use if k not in self.dead_keys]
                if missing and alive_keys:
                    logger.info("Sweep: re-attempting %d missing strings.", len(missing))
                    with self.cond:
                        self.dead_keys = set(self.dead_keys)  # keep, just rebuild pool
                        self.pool = OrderedDict()
                        for it in missing:
                            f = self.item_file.get(it.id, "")
                            # rebuild the prepared TranslateItem for this rep
                            prep = next(
                                (p for p in self._prepared_by_file.get(f, [])
                                 if p.id == it.id),
                                TranslateItem(id=it.id, text=it.text, context=it.context),
                            )
                            self.pool.setdefault(f, []).append(prep)
                        self.in_flight = 0
                        self.in_flight_workers.clear()
                        self.worker_last_request.clear()
                    sweep_assignment = []
                    sweep_threads = max(1, self.threads)
                    for i, key in enumerate(alive_keys):
                        for j in range(sweep_threads):
                            sweep_assignment.append((i * sweep_threads + j, key))
                    yield from self._spawn_and_drain(sweep_assignment)

            with self.cond:
                done = len(self.result)
                errors = list(self.errors)
                aborted = self.aborted
                tok_in, tok_out = self.tok_in, self.tok_out
                cpt, out_ratio = self.chars_per_token, self.output_ratio
                fanned = self._fan_out(dict(self.result))
                # Fan out the measured font-shrink factors to every sharing id,
                # same as translations, so inject can map id -> style -> size.
                size_fixes: dict[str, float] = {}
                for rid, factor in self.size_overrides.items():
                    for sid in self.groups[self.rep_key[rid]]:
                        size_fixes[sid] = factor
            logger.info(
                "Translation stream complete. Translated %d/%d strings. "
                "Errors: %d. Aborted: %s",
                done, self.total, len(errors), aborted,
            )
            yield json.dumps({
                "type": "done",
                "translations": fanned,
                "size_fixes": size_fixes,
                "errors": errors,
                "aborted": aborted,
                "usage": {"prompt_tokens": tok_in, "completion_tokens": tok_out},
                "calibration": {
                    "chars_per_token": round(cpt, 3),
                    "output_ratio": round(out_ratio, 3),
                },
            }, ensure_ascii=False) + "\n"
        finally:
            self.set_aborted()
            logger.info("Translation stream finished/cleaned up.")
