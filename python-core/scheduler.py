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
  * Pace: each request occupies at least `delay_seconds` of wall-clock; a worker
    that finished faster sleeps the remainder before its next claim (honours a
    provider's per-minute request limit).
  * Pause: a worker always FINISHES and EMITS its in-flight batch (so the
    frontend auto-saves the project) before blocking at the next claim boundary.

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

# Back-off (seconds) between retries: short on the first miss, longer after. For
# RATE/OVERLOAD errors the effective wait is raised to at least `delay_seconds`
# (see _classify_error) — on every cloud API a 429/503/"overloaded" reply STILL
# counts against the per-minute request quota, so hammering retries would only
# dig the hole deeper.
_RETRY_BACKOFF_FIRST = 8
_RETRY_BACKOFF_REST = 16

# Default window when the UI doesn't constrain it (cloud models, big local ones).
DEFAULT_CONTEXT_TOKENS = 8192


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
        # rep id -> font-shrink factor (<1.0) for captions that STILL overflow
        # after all re-asks; consumed by inject to reduce that style's font. Empty
        # for the common case (shortening was enough). Guarded by self.cond.
        self.size_overrides: dict[str, float] = {}
        self.item_file: dict[str, str] = {}
        prepared_by_file: "OrderedDict[str, list]" = OrderedDict()
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
            prepared_by_file.setdefault(c.file, []).append(
                TranslateItem(id=c.id, text=c.text, context=context, max_chars=max_chars, max_pixels=max_pixels)
            )
            self.item_file[c.id] = c.file
        self._prepared_by_file = prepared_by_file

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
        self.threads = max(1, int(getattr(req, "threads", 1) or 1))
        self.worker_count = self.threads * len(keys)
        self.delay_seconds = max(0.0, float(getattr(req, "delay_seconds", 0.0) or 0.0))

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
        self.reclaim_count: dict[str, int] = {}
        self.result: dict[str, str] = {}  # rep id -> translation (authoritative)
        self.errors: list[str] = []
        self.aborted = False
        self.tok_in = 0
        self.tok_out = 0
        self.batches = 0
        # Per-worker batch numbering for the UI grid. `batch_seq` is a monotonic
        # ticket handed out at each claim; `worker_batch_no[worker_idx]` is the
        # number the worker currently owns, so each thread's card shows ITS batch
        # (thread 1 → batch 1, thread 2 → batch 2, …) instead of all cards sharing
        # one global count. A reclaimed/failed-over batch gets a fresh higher
        # number when another worker re-claims it.
        self.batch_seq = 0
        self.worker_batch_no: dict[int, int] = {}
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
        evt.update(extra)
        self.event_queue.put(json.dumps(evt, ensure_ascii=False))

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
        race the pool draining underneath it."""
        req = self.req
        with self.cond:
            while True:
                if self.aborted:
                    return (_DONE, None)
                if worker_key in self.dead_keys:
                    return (_DONE, None)

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
                # Hand this worker its own batch ticket so its grid card shows a
                # distinct number (not the shared global `self.batches`).
                self.batch_seq += 1
                self.worker_batch_no[worker_idx] = self.batch_seq
                return (_CLAIM, (fname, batch))

    def _overflows_exact(self, cal: Calibrator, batch: list, worker_key: str) -> bool:
        """Optional pre-send guard for providers with a cheap exact tokenizer."""
        if len(batch) <= 1:
            return False
        cfg = ProviderConfig(
            base_url=self.req.base_url,
            api_key=worker_key,
            model=self.req.model,
            num_ctx=self.req.max_context_tokens,
        )
        exact = self.provider.count_tokens(
            build_prompt(batch, self.req.target_lang, self.req.glossary), cfg
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
        res_queue: "queue.Queue[tuple[bool, object]]" = queue.Queue()

        def sub_worker():
            try:
                tr_res = self.provider.translate(
                    batch, self.req.target_lang, self.req.glossary, cfg
                )
                res_queue.put((True, tr_res))
            except Exception as e_thread:  # noqa: BLE001 — surfaced to retry loop
                res_queue.put((False, e_thread))

        thread = threading.Thread(target=sub_worker, daemon=True)
        thread.start()

        tr = None
        last_yield = time.time()
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
                if now - last_yield >= 1.0:
                    last_yield = now
                    elapsed = int(now - start_time)
                    self._emit(
                        worker_idx,
                        "translating_batch",
                        status=f"Translating batch ({len(batch)} strings)"
                        f"{try_suffix}... [{elapsed}s]",
                        batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq + 1),
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
        )
        prompt_chars = len(build_prompt(batch, req.target_lang, req.glossary))
        batch_tr: dict[str, str] = {}
        last_err = None
        auth_fails = 0  # consecutive auth-class failures → kill the key fast

        for try_i in range(BATCH_TRIES):
            if self._is_aborted() or worker_key in self.dead_keys:
                return ({}, False, False)
            self._wait_while_paused(worker_idx, len(batch), try_i)
            if self._is_aborted():
                return ({}, False, False)

            start_time = time.time()
            try_suffix = f" (retry {try_i + 1}/{BATCH_TRIES})" if try_i > 0 else ""
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
                    return ({}, False, False)
                if not tr.translations:
                    raise RuntimeError(
                        "Model response parsed successfully, but returned 0 "
                        "translated strings matching input keys."
                    )
                batch_tr = tr.translations

                # Oversized-translation re-ask (Ren'Py char limits) — identical to
                # the legacy path so the per-line cap is still enforced.
                self._enforce_char_limits(batch, batch_tr, cfg)

                cal.observe(prompt_chars, tr.usage)
                with self.cond:
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
                # A failed request that still reached the server spent the quota.
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

                kind = _classify_error(err_str)
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
                    # rate/overload replies STILL spend the per-minute quota on
                    # every cloud API, so wait at least the pacing delay before
                    # re-sending — and record a per-key cooldown so siblings on
                    # this key wait too while other keys keep working.
                    floor = self.delay_seconds if kind == "rate" else 0.0
                    if kind == "rate" and self.delay_seconds > 0:
                        # The failed request still consumed an RPM slot, so
                        # the cooldown starts from NOW — the next request must
                        # wait a full delay_seconds from this point.
                        with self.cond:
                            self.key_cooldown[worker_key] = max(
                                self.key_cooldown.get(worker_key, 0.0),
                                time.time() + self.delay_seconds,
                            )
                    # Stagger sibling retries so threads that all hit an error at
                    # once don't re-fire in lockstep (which would just re-trigger
                    # the same rate limit). Spread evenly across the full delay
                    # window so N threads on one key each fire at a different
                    # second, keeping the peak RPM at N/delay ≤ 1 request per
                    # thread-period.
                    stagger = 0.0
                    if self.delay_seconds > 0 and self.threads > 1:
                        rank = worker_idx % self.threads
                        stagger = self.delay_seconds * rank / self.threads
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
        """True if `translation` renders WIDER than the original's pixel budget.
        Measures real rendered width in the target-script font — no len()/avg
        approximation. Falls back to the char hint if pixels are unavailable."""
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
    _MAX_WORDS_FONT_FIT = 2

    def _enforce_char_limits(self, batch, batch_tr, cfg) -> None:
        """Make any translation that overran its Ren'Py per-line width budget fit —
        WITHOUT ever butchering a word into an abbreviation. Two paths by word count:

        - 1–2 words (e.g. "Сохранение"): keep the word WHOLE, record a font-shrink
          factor (size_overrides) so inject renders it smaller but intact. No re-ask.
        - 3+ words: re-ask the model for a shorter rephrase up to _MAX_REASKS times
          (a synonym can shorten honestly here), then font-shrink whatever still
          overflows.

        Either way the final text is the model's own — never cut — and a short
        caption never degrades to "Сох"."""
        all_oversized = [
            it for it in batch
            if it.id in batch_tr and self._overflows(it.id, batch_tr[it.id])
        ]
        if not all_oversized:
            return

        # Partition: short captions skip the re-ask entirely (font-shrink only).
        def _word_count(s: str) -> int:
            return len(s.split())

        short_fit = [it for it in all_oversized
                     if _word_count(batch_tr[it.id]) <= self._MAX_WORDS_FONT_FIT]
        oversized = [it for it in all_oversized
                     if _word_count(batch_tr[it.id]) > self._MAX_WORDS_FONT_FIT]

        # Record font-shrink for the short ones right away — full word preserved.
        for it in short_fit:
            self._record_font_shrink(it.id, batch_tr[it.id])

        if not oversized:
            return
        logger.info(
            "Detected %d multi-word translations exceeding width budget. "
            "Re-asking shorter (%d short captions font-shrunk, word kept whole)...",
            len(oversized), len(short_fit),
        )

        for round_no in range(self._MAX_REASKS):
            retry_batch = []
            for it in oversized:
                prev = batch_tr[it.id]
                ratio = self._overflow_ratio(it.id, prev) or 1.15
                # Aim a bit under the budget: tighten the char target by the
                # measured overflow ratio so the model has a concrete smaller
                # number to hit, not a vague "shorter".
                base = self.item_limits.get(it.id) or len(prev)
                tighter = max(3, int(min(base, len(prev)) / max(ratio, 1.01)))
                retry_batch.append(
                    TranslateItem(id=it.id, text=it.text, context=it.context,
                                  max_chars=tighter)
                )
            try:
                retry_tr = self.provider.translate(
                    retry_batch, self.req.target_lang, self.req.glossary, cfg
                )
            except Exception as e_retry:  # noqa: BLE001
                logger.error("Retry translation failed: %s", e_retry)
                break
            with self.cond:
                self.tok_in += retry_tr.usage.prompt_tokens
                self.tok_out += retry_tr.usage.completion_tokens
            # Keep a re-ask result only if it actually fits better than what we had.
            for rit in retry_batch:
                if rit.id not in retry_tr.translations:
                    continue
                r = retry_tr.translations[rit.id]
                if self._overflow_ratio(rit.id, r) <= self._overflow_ratio(rit.id, batch_tr[rit.id]) or not self.item_orig_px.get(rit.id):
                    batch_tr[rit.id] = r
            oversized = [it for it in oversized if self._overflows(it.id, batch_tr[it.id])]
            if not oversized:
                return

        # Still overflowing after the last re-ask: font-shrink the remainder.
        for it in oversized:
            self._record_font_shrink(it.id, batch_tr[it.id])

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

    def _wait_while_paused(self, worker_idx, batch_size, try_i) -> None:
        while self.should_pause() and not self._is_aborted():
            self._emit(
                worker_idx,
                "paused",
                status="Paused",
                batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq + 1),
                batch_size=batch_size,
                try_i=try_i,
                elapsed=0,
            )
            time.sleep(1.0)

    def _retry_sleep(self, worker_idx, batch, try_i, start_time, min_wait=0.0,
                     stagger=0.0) -> None:
        """Back-off between retries, interruptible by pause/resume/abort. min_wait
        raises the floor (used for rate/overload errors so the re-send respects
        the per-minute quota). stagger adds a fixed per-thread offset so siblings
        that errored together don't retry in lockstep."""
        sleep_time = max(min_wait,
                         _RETRY_BACKOFF_FIRST if try_i == 0 else _RETRY_BACKOFF_REST)
        sleep_time += stagger
        start_sleep = time.time()
        was_paused = False
        while not self._is_aborted():
            if self.should_pause():
                was_paused = True
                paused_start = time.time()
                while self.should_pause() and not self._is_aborted():
                    elapsed = int(time.time() - start_time) if start_time else 0
                    self._emit(
                        worker_idx,
                        "paused",
                        status="Paused",
                        batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq + 1),
                        batch_size=len(batch),
                        try_i=try_i,
                        elapsed=elapsed,
                    )
                    time.sleep(1.0)
                p_dur = time.time() - paused_start
                start_time += p_dur
                start_sleep += p_dur
            if was_paused and not self.should_pause():
                break
            now = time.time()
            remaining = sleep_time - (now - start_sleep)
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
            # Sleep the EXACT remainder (capped at 1s for pause/abort
            # responsiveness). Sleeping a flat 1.0s would round sub-second stagger
            # offsets up to the next whole second and re-synchronize sibling
            # retries — the very lockstep the stagger exists to prevent.
            time.sleep(min(1.0, remaining))

    def _pace_delay(self, worker_idx, t0, stagger_offset=0.0) -> None:
        """Hold the request to at least delay_seconds of wall-clock. Interruptible
        by pause (which doesn't count against the delay) and abort.
        stagger_offset shifts this thread's pacing window so threads on the
        same key fire at stagger, stagger+delay, stagger+2*delay, … —
        keeping the peak RPM at threads/delay instead of threads at once."""
        if self.delay_seconds <= 0:
            return
        # target = the earliest wall-clock time this thread may fire again.
        # On the first call target = t0 + stagger (initial spread).
        # On subsequent calls target = last_fire + delay (constant spacing).
        last = self.worker_last_request.get(worker_idx)
        if last is not None:
            target = last + self.delay_seconds
        else:
            target = t0 + stagger_offset
        self.worker_last_request[worker_idx] = time.time()
        while not self._is_aborted():
            # Don't burn the delay while the user has us paused.
            while self.should_pause() and not self._is_aborted():
                self._emit(worker_idx, "paused", status="Paused")
                time.sleep(1.0)
                # Restart pacing from the last request, preserving natural
                # spacing instead of re-synchronizing all threads.
                last = self.worker_last_request.get(worker_idx)
                target = (last + self.delay_seconds) if last is not None else time.time() + self.delay_seconds
            remaining = target - time.time()
            if remaining <= 0:
                return
            wait_left = int(math.ceil(remaining))
            self._emit(
                worker_idx,
                "waiting_delay",
                status=f"Pacing ({wait_left}s left)...",
                wait_left=wait_left,
            )
            time.sleep(min(1.0, remaining))

    # -- the worker loop --------------------------------------------------------

    def _initial_stagger(self, worker_idx: int) -> None:
        """Sleep before the very first claim so threads on the same key don't
        all fire at t=0. Each thread gets a small offset (≤1s per thread) to
        spread the initial burst, but never more than 2s total."""
        if self.delay_seconds <= 0 or self.threads <= 1:
            return
        rank = worker_idx % self.threads
        # Cap stagger at 2s total so the last thread doesn't wait ages.
        offset = min(2.0, rank * min(1.0, self.delay_seconds / self.threads))
        if offset <= 0:
            return
        start = time.time()
        while not self._is_aborted():
            remaining = offset - (time.time() - start)
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def worker_loop(self, worker_idx: int, worker_key: str) -> None:
        cal = Calibrator(self.req.target_lang)
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
            t0 = time.time()
            tr_map: dict[str, str] = {}
            key_died = False
            try:
                tr_map, key_died, _safety = self._translate_with_retries(
                    batch, cal, worker_idx, worker_key
                )
                # Emit the batch's translations BEFORE any pause/next-claim so the
                # frontend merges + auto-saves the project file for this batch.
                if tr_map:
                    self._emit(
                        worker_idx,
                        "completed_batch",
                        status="Completed batch!",
                        translations=self._fan_out(tr_map),
                        batch_num=self.worker_batch_no.get(worker_idx, self.batch_seq),
                    )
            finally:
                self.finish_batch(worker_idx, batch, tr_map, key_died)

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
            yield json.dumps({
                "type": "progress",
                "done": self._done_count(),
                "total": self.total,
                "batches": 0,
                "status": "Initializing translator...",
                "phase": "initializing",
                "translations": {},
            }, ensure_ascii=False) + "\n"

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
                "Spawning %d workers across %d key(s) (threads=%d, delay=%.1fs)",
                self.worker_count, len(self.keys_to_use), self.threads,
                self.delay_seconds,
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
