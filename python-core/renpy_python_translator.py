#!/usr/bin/env python3
"""Ren'Py Inline Python String Translator.

Extracts, classifies, translates, and replaces raw Python string literals inside
LOOSE Ren'Py visual novel scripts (.rpy) on disk. Scripts that exist only inside
a .rpa archive are deliberately NOT touched here: inline-Python edits require
rewriting the .rpy in place, but the archive also ships a compiled .rpyc, so a
loose edited copy would double-load and crash the game. Dialogue in archived
scripts is still translated via the archive-safe tl/ path (parsers/renpy.py).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import textwrap
import time
import threading
import queue
from pathlib import Path
import httpx

# Configure console streams to handle Unicode (e.g. emojis) on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("renpy_python_translator")

_should_pause = False

def set_paused(value: bool):
    global _should_pause
    _should_pause = value

def _wait_if_paused():
    while _should_pause:
        time.sleep(0.3)

# Ensure we can import parsers and providers relative to this script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parsers import rpa as rpamod
from parsers.base import INTERPREX_DIR
from parsers.renpy import RenPyParser

# Reuse the SAME error bucketing the main scheduler uses, so this path fails over
# identically (no duplicated marker lists to drift out of sync).
from scheduler import _classify_error, _reached_server

# ---------------------------------------------------------------------------
# Classification cache — persists between runs so identical candidates are not
# re-classified.  Key = SHA-256(value + raw_line + context_function +
# context_variable + context_param); versioned by (model + provider +
# CLASSIFICATION_BATCH_PROMPT hash) so changing any of those invalidates the
# whole cache.
# ---------------------------------------------------------------------------

_CACHE_FILENAME = "classify_cache.json"  # lives inside Interprex/ (was a dotfile)
_CACHE_VERSION = 1  # bump to force invalidation on format changes


def _candidate_cache_key(entry: dict) -> str:
    """Deterministic hash for one candidate — must not include mutable fields
    like file_path or line number (the same string in a different file should
    hit the cache)."""
    blob = "|".join([
        entry.get("value", ""),
        entry.get("raw_line", ""),
        entry.get("context_function") or "",
        entry.get("context_variable") or "",
        entry.get("context_param") or "",
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _cache_version_key(model: str, provider: str | None) -> str:
    """Hash of the knobs that change classification output.
    Model and provider are excluded so switching them doesn't invalidate the cache.
    """
    blob = "|".join([
        str(_CACHE_VERSION),
        CLASSIFICATION_BATCH_PROMPT,
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


class _ClassificationCache:
    """Disk-backed cache for classification decisions.

    Layout in JSON:
    {
      "version_key": "<hash>",
      "entries": { "<candidate_key>": {"decision": "TRANSLATE"|"SKIP", "reason": "..."} }
    }
    """

    def __init__(self, game_path: Path, model: str, provider: str | None):
        self._path = game_path / INTERPREX_DIR / _CACHE_FILENAME
        self._version_key = _cache_version_key(model, provider)
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = data.get("entries", {})
            logger.info("Classification cache loaded: %d entries", len(self._entries))
            if data.get("version_key") != self._version_key:
                # Key mismatch (e.g. upgraded from model-dependent cache), preserve entries but mark dirty to save new key
                self._dirty = True
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            self._entries = {}

    def save(self):
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({
                    "version_key": self._version_key,
                    "entries": self._entries,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Classification cache saved: %d entries", len(self._entries))
        except OSError as exc:
            logger.warning("Failed to save classification cache: %s", exc)

    # -- lookup / store -------------------------------------------------------

    def get(self, entry: dict) -> tuple[str, str] | None:
        """Return (decision, reason) or None on miss."""
        key = _candidate_cache_key(entry)
        hit = self._entries.get(key)
        if hit is None:
            return None
        return hit["decision"], hit["reason"]

    def put(self, entry: dict, decision: str, reason: str):
        key = _candidate_cache_key(entry)
        self._entries[key] = {"decision": decision, "reason": reason}
        self._dirty = True


# ---------------------------------------------------------------------------
# Translation cache — persists the actual inline-Python TRANSLATIONS (not just
# the classify decision) so a second run re-translates ONLY new strings, and the
# no-API "apply cached" path (writeBack) can lay the translation back down
# without spending any API quota. Keyed identically to the classification cache
# (value+context), but versioned by the TARGET LANGUAGE too — a Russian cache
# must never feed a French run.
# ---------------------------------------------------------------------------

_TRANSLATION_CACHE_FILENAME = "python_translations.json"  # inside Interprex/
_TRANSLATION_CACHE_VERSION = 1


def _translation_version_key(target_lang: str) -> str:
    """Hash of the knobs that change translation output. Target language is
    load-bearing here (unlike the classify cache): the cached value IS in that
    language, so a language switch must invalidate it."""
    blob = "|".join([
        str(_TRANSLATION_CACHE_VERSION),
        (target_lang or "").strip().lower(),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


class _TranslationCache:
    """Disk-backed cache for inline-Python translations.

    Layout in JSON:
    {
      "version_key": "<hash of version+target_lang>",
      "entries": { "<candidate_key>": {"translated": "..."} }
    }
    """

    def __init__(self, game_path: Path, target_lang: str):
        self._path = game_path / INTERPREX_DIR / _TRANSLATION_CACHE_FILENAME
        self._version_key = _translation_version_key(target_lang)
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("version_key") == self._version_key:
                self._entries = data.get("entries", {})
            else:
                # Different language/version — do NOT reuse stale translations.
                self._entries = {}
                self._dirty = True
            logger.info("Translation cache loaded: %d entries", len(self._entries))
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            self._entries = {}

    def save(self):
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({
                    "version_key": self._version_key,
                    "entries": self._entries,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Translation cache saved: %d entries", len(self._entries))
        except OSError as exc:
            logger.warning("Failed to save translation cache: %s", exc)

    def get(self, entry: dict) -> str | None:
        """Return the cached translation for this candidate, or None on miss."""
        hit = self._entries.get(_candidate_cache_key(entry))
        return hit["translated"] if hit else None

    def put(self, entry: dict, translated: str):
        self._entries[_candidate_cache_key(entry)] = {"translated": translated}
        self._dirty = True


CLASSIFICATION_PROMPT = """
Ты классификатор строк в исходном коде Ren'Py визуальных новелл.
Твоя задача: определить является ли строка ПОЛЬЗОВАТЕЛЬСКИМ ТЕКСТОМ который видит игрок, или ТЕХНИЧЕСКИМ КОДОМ.

ВАЖНО: при сомнении возвращай SKIP. Лучше не перевести чем сломать игру.

Строка: {value}
Контекст (код вокруг): {raw_line}
Функция/метод: {context_function}
Переменная/объект: {context_variable}
Имя параметра: {context_param}

Верни ТОЛЬКО JSON без какого-либо текста вокруг:
{{"decision": "TRANSLATE" | "SKIP", "reason": "одна строка объяснения"}}
"""

SYSTEM_INSTRUCTION = (
    "You are a professional game-localization translator. Translate each source string into {lang}.\n"
    "Input is a JSON object mapping ids to objects with a 'text' field and an optional 'context' field.\n"
    "The 'context' field provides metadata (e.g. speaker name, UI location) to help determine the correct "
    "gender, grammatical forms, and tone. Do NOT translate the context itself.\n"
    "Preserve all in-game control codes, escape sequences, and placeholders EXACTLY as they appear "
    "(e.g. \\n, \\., \\C[1], %1, {{name}}, <i>). Do not translate text inside such codes. Keep the tone "
    "natural for a player.\n"
    "CRITICAL: Ren'Py text interpolation tokens like [variable_name], [mc.status], [VALUE], [v] "
    "MUST appear in the translation EXACTLY as in the source — same spelling, same case, same brackets. "
    "These are code variables, not translatable text. Changing [player_name] to [имя_игрока] WILL crash "
    "the game. If a string contains [var], copy it verbatim into the translation.\n"
    "CRITICAL: Strictly match the letter casing and capitalization of the source text (capitalized words, UPPERCASE terms). Never change capitalization.\n"
    "Ren'Py code tokens MUST be copied verbatim from the source — they are NOT translatable text:\n"
    "  • [variable_name], [mc.status] — text interpolation variables (keep exact spelling + case)\n"
    "  • {b}, {/b}, {i}, {/i}, {color=#RRGGBB}, {/color}, {size=N}, {/size}, {a=url}, {/a} — text tags\n"
    "  • %(name)s, %s, %d — Python format strings (keep verbatim)\n"
    "  • \\n, \\t — escape sequences (keep as literal backslash+n, NOT as real newline)\n"
    "Changing any of these WILL crash the game.\n"
    "Do not resolve escape characters; if the source contains a literal '\\n', keep exactly '\\n' in the translation "
    "instead of inserting a real newline.\n"
    "Return ONLY a JSON object containing an 'items' array, where each item has 'id' (from the input) "
    "and 'translated' (the translated string). Example:\n"
    "{{\n"
    "  \"items\": [\n"
    "    {{ \"id\": \"a1b2\", \"translated\": \"translation\" }}\n"
    "  ]\n"
    "}}\n"
    "Do NOT wrap the JSON in markdown code blocks (e.g., ```json) and do not write any explanations."
)

_STRING_LITERAL_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""|'
    r"'''(?:[^'\\]|\\.|'(?!''))*'''|"
    r'"(?:[^"\\]|\\.)*"|'
    r"'(?:[^'\\]|\\.)*'"
)

# ---------------------------------------------------------------------------
# AST Traversal Helpers
# ---------------------------------------------------------------------------

def populate_parents(node):
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent


class StringExtractor(ast.NodeVisitor):
    def __init__(self, raw_lines: list[str], start_line: int, block_type: str):
        self.raw_lines = raw_lines
        self.start_line = start_line
        self.block_type = block_type
        self.candidates = []

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self._handle_string(node)
        self.generic_visit(node)

    def visit_Str(self, node):
        self._handle_string(node)
        self.generic_visit(node)

    def _handle_string(self, node):
        val = node.value if isinstance(node, ast.Constant) else node.s
        
        # Calculate start/end lines relative to file
        start_offset = node.lineno - 1
        abs_start_line = self.start_line + start_offset
        if self.block_type == "multiline":
            abs_start_line = self.start_line + node.lineno
            
        end_lineno = getattr(node, "end_lineno", node.lineno)
        end_offset = end_lineno - 1
        abs_end_line = self.start_line + end_offset
        if self.block_type == "multiline":
            abs_end_line = self.start_line + end_lineno

        # Bound check
        abs_start_line = max(1, min(abs_start_line, len(self.raw_lines)))
        abs_end_line = max(1, min(abs_end_line, len(self.raw_lines)))

        # Find the raw literal in the file lines
        raw_literal = find_raw_literal(self.raw_lines, abs_start_line, abs_end_line, val)
        if not raw_literal:
            return  # skip if we can't find its exact representation in source
            
        # Get raw context line
        raw_line = self.raw_lines[abs_start_line - 1]

        # Context analysis
        context_function = None
        context_variable = None
        context_param = None
        
        curr = node
        while hasattr(curr, 'parent'):
            p = curr.parent
            
            # Keyword arg
            if isinstance(p, ast.keyword) and p.value == curr:
                context_param = p.arg
                
            # Function/Method call
            if isinstance(p, ast.Call):
                if isinstance(p.func, ast.Name):
                    context_function = p.func.id
                elif isinstance(p.func, ast.Attribute):
                    context_function = p.func.attr
                    context_variable = self._unparse_simple(p.func.value)
                break
                
            # Assignment
            if isinstance(p, ast.Assign):
                if p.targets:
                    context_variable = self._unparse_simple(p.targets[0])
                break
                
            curr = p
            
        self.candidates.append({
            "value": val,
            "line": abs_start_line,
            "end_line": abs_end_line,
            "context_function": context_function,
            "context_variable": context_variable,
            "context_param": context_param,
            "raw_literal": raw_literal,
            "raw_line": raw_line
        })
        
    def _unparse_simple(self, node) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            val = self._unparse_simple(node.value)
            return f"{val}.{node.attr}" if val else node.attr
        return None


def find_raw_literal(file_lines: list[str], abs_start_line: int, abs_end_line: int, expected_value: str) -> str | None:
    sliced_lines = file_lines[abs_start_line - 1 : abs_end_line]
    joined_text = "\n".join(sliced_lines)
    
    for match in _STRING_LITERAL_RE.finditer(joined_text):
        literal = match.group(0)
        try:
            if ast.literal_eval(literal) == expected_value:
                return literal
        except Exception:
            pass
    return None

# ---------------------------------------------------------------------------
# Classification Rules
# ---------------------------------------------------------------------------

SKIP_CONTEXTS = {
    "renpy.music", "renpy.sound", "renpy.image",
    "renpy.video", "renpy.display",
    "config", "define", "AudioURL", "im.Scale"
}

def hard_skip(entry: dict) -> bool:
    v = entry["value"]
    
    v_stripped = v.strip()
    
    if not v_stripped:
        return True
    
    if "/" in v or "\\" in v:
        return True
    
    if re.match(r'^#[0-9a-fA-F]{3,8}$', v_stripped):
        return True

    if v_stripped.startswith((',', ']', '[', ')', '(', '=', '+', '-', '*', '%', '/', ';', '{', '}')) or \
       v_stripped.endswith((',', ']', '[', ')', '(', '=', '+', '-', '*', '%', '/', ';', '{', '}')):
        return True
    
    if any(f in entry["raw_line"] for f in SKIP_CONTEXTS):
        return True
    
    return False


# Keyword args / list names that signal player-visible display text. Module-level
# so hard_translate AND the comparison-key "is this visible?" predicate share one
# source of truth.
_TRANSLATE_PARAMS = {
    "status_text", "label", "text", "message",
    "description", "title", "caption", "tooltip",
    "display_name", "bio", "name", "dialogue",
    "hint", "confirm", "question", "prompt",
    "button_text", "menu_item", "option"
}
_TRANSLATE_LISTS = {
    "messages", "chat", "blog", "posts", "notifications", "log",
    "options", "choices", "buttons", "items", "menu", "replies",
    "tips", "hints", "descriptions", "names", "labels"
}


def hard_translate(entry: dict) -> bool:
    if entry["context_param"] in _TRANSLATE_PARAMS:
        return True

    if (entry["context_function"] in {"append", "add", "insert", "extend"} and
        entry["context_variable"] and
        any(w in entry["context_variable"] for w in _TRANSLATE_LISTS)):
        return True

    if entry["context_function"] in {"set", "setattr"} and entry["context_param"] in _TRANSLATE_PARAMS:
        return True

    return False


# ---------------------------------------------------------------------------
# Comparison-key safety
# ---------------------------------------------------------------------------
# A string literal the game COMPARES in code (`if x == "home"`, `msg != "voice"`,
# `w in ["a","b"]`, a dict key, `.get("k")`) is a KEY, not display text. Most are
# invisible internal state codes ('default', 'voice', 'text_chat', 'home', font
# names, character-name routes saved to disk). Translating ANY of them — even in
# one place — breaks the comparison, so a click/branch/mode silently dies (this is
# the "translated and now I can't click it" bug). We cannot tell a visible
# "murder weapon" from an internal "text_chat" by looks, so we FORCE-SKIP every
# string that appears in a comparison ANYWHERE in the scripts. Better to leave a
# few visible key-strings English than to break game logic. Display names still
# translate via the tl/ path; only the in-code literal is protected.

# Equality/inequality against a quoted literal, either side.
_CMP_EQ_RE = re.compile(r'(?:==|!=)\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
                        r'|("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')\s*(?:==|!=)')
# `.get("key"` — dict lookup by literal.
_CMP_GET_RE = re.compile(r'\.get\(\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')')
# `in [ ... ]` membership — capture the bracket body, then pull literals from it.
_CMP_IN_RE = re.compile(r'\bin\s*\[([^\]]*)\]')
# A dict-key literal: `"key":` (quoted string immediately followed by a colon).
_CMP_DICTKEY_RE = re.compile(r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')\s*:')
# Any quoted literal (used to scan inside an `in [...]` body).
_ANY_LIT_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


def _decode_lit(lit: str) -> str | None:
    """ast.literal_eval a quoted literal to its string value; None if not a str."""
    try:
        v = ast.literal_eval(lit)
    except Exception:
        return None
    return v if isinstance(v, str) and v else None


def find_comparison_keys(sources: dict) -> set[str]:
    """Scan ALL script sources and return the set of decoded string values that
    are used as a comparison key somewhere (==, !=, in [...], dict key, .get()).

    Scans raw text line-by-line across every file, so it catches keys compared in
    a DIFFERENT file than where they're assigned, and inside screen conditions —
    places the AST candidate extractor never sees. Pure text matching: a key is a
    key regardless of which block it lives in."""
    keys: set[str] = set()
    for _path, content in sources.items():
        for line in content.split("\n"):
            for m in _CMP_EQ_RE.finditer(line):
                lit = m.group(1) or m.group(2)
                v = _decode_lit(lit) if lit else None
                if v:
                    keys.add(v)
            for m in _CMP_GET_RE.finditer(line):
                v = _decode_lit(m.group(1))
                if v:
                    keys.add(v)
            for m in _CMP_DICTKEY_RE.finditer(line):
                v = _decode_lit(m.group(1))
                if v:
                    keys.add(v)
            for m in _CMP_IN_RE.finditer(line):
                for lit in _ANY_LIT_RE.findall(m.group(1)):
                    v = _decode_lit(lit)
                    if v:
                        keys.add(v)
    return keys


# A comparison key is FORCE-SKIPPED by default (translating it breaks logic). But
# a few keys are ALSO player-visible prose (e.g. "murder weapon", "Death of the
# Author") and should be translated — consistently across every occurrence so the
# `==` still matches. The predicates below decide which keys to promote. Bias HARD
# toward NOT promoting: a missed prose key just stays English (safe); a wrongly
# promoted internal code ("home", "voice") BREAKS the game. So a key is promoted
# only when ALL hold: it is player-visible via inline display code, it is not a
# code-token, and it is not already shown translated via the tl/ path.

_MIN_VISIBLE_LEN = 3


def _is_display_candidate(entry: dict) -> bool:
    """True if this candidate sits in a player-VISIBLE inline context: a
    renpy.input prompt, a display keyword arg, or an append/extend to a
    display-ish list. Mirrors hard_translate's visibility signal."""
    cf = entry.get("context_function")
    cv = entry.get("context_variable")
    if cf == "input" and cv in (None, "renpy"):
        return True
    if entry.get("context_param") in _TRANSLATE_PARAMS:
        return True
    if (cf in {"append", "add", "insert", "extend"} and cv and
            any(w in cv for w in _TRANSLATE_LISTS)):
        return True
    return False


def _looks_like_code_token(value: str) -> bool:
    """True if `value` looks like an internal code, NOT player prose — must never
    be globally replaced. Conservative: anything ambiguous returns True (skip)."""
    v = value.strip()
    if len(v) < _MIN_VISIBLE_LEN:
        return True  # "V", "he", "id" — too short to risk
    if not any(c.isalpha() for c in v):
        return True  # no letters → numbers/symbols
    # Single lowercase identifier-shaped token: 'home', 'voice', 'text_chat', 'him'
    if " " not in v and v == v.lower() and (("_" in v) or v.isalnum()):
        return True
    return False


def _visible_translatable_key(value: str, display_entries: list) -> bool:
    """Promote `value` (a comparison key) to translation only if it is real prose
    AND appears in at least one player-visible inline context."""
    if _looks_like_code_token(value):
        return False
    return any(_is_display_candidate(e) for e in display_entries)


def _likely_save_stored(value: str, sources: dict) -> bool:
    """Heuristic: value is assigned to a variable somewhere AND compared somewhere
    → it may live in a save file, so translating it can break OLD saves. Used only
    to WARN the user (translation still proceeds — fresh playthroughs are fine)."""
    assigned = compared = False
    needle_d = '"' + value.replace('"', '\\"') + '"'
    needle_s = "'" + value.replace("'", "\\'") + "'"
    for content in sources.values():
        for line in content.split("\n"):
            if needle_d not in line and needle_s not in line:
                continue
            if "==" in line or "!=" in line or re.search(r'\bin\s*\[', line):
                compared = True
            if re.search(r'=\s*("|\')', line) and "==" not in line:
                assigned = True
            if assigned and compared:
                return True
    return False


# ---------------------------------------------------------------------------
# Gemini API Interaction
# ---------------------------------------------------------------------------

import time

# ---------------------------------------------------------------------------
# Multi-key worker pool (classification + translation share this)
# ---------------------------------------------------------------------------
# The two phases (classify candidates, translate the keepers) each have a fixed,
# known-up-front set of batches. We run them across ALL api keys with the same
# failover semantics as scheduler.py: workers = threads x keys, a worker bound to
# keys[i // threads]; an auth-dead key retires (its batches requeue to a survivor),
# a rate error cools the key down (not killed), other errors retry to a cap. The
# only hard invariant is that no batch is silently dropped — every batch ends
# merged OR accounted-as-exhausted so the caller's safe default (SKIP / leave
# untranslated) fills it.

# Grace before declaring an auth-class key dead (a single 403 blip shouldn't kill
# a key the way a real invalid key should — but fail fast, don't burn minutes).
_POOL_AUTH_GRACE = 2
# Per-batch retry cap for "other" errors, multiplied by key count so a batch gets
# a fair shot on every key before we give up on it.
_POOL_BATCH_TRIES = 6
_POOL_RETRY_BACKOFF = 8.0


def _build_key_list(api_keys, legacy_key: str) -> list[str]:
    """Dedupe-preserve api_keys + the legacy single key, mirroring scheduler.py.
    Collapses to [""] for local / empty-key providers (failover is then a no-op)."""
    candidates = list(api_keys or []) + [legacy_key or ""]
    seen: set[str] = set()
    keys: list[str] = []
    for k in candidates:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys or [legacy_key or ""]


def _run_batches_over_keypool(batches, keys, threads, delay_seconds, label, process_fn):
    """Run every batch across the key-pool with failover; return the merged result.

    process_fn(batch, key, worker_idx) -> dict; it MUST raise on an API error so
    the pool can classify it. label is the log verb ("Classified"/"Translated").
    """
    threads = max(1, int(threads or 1))
    keys = list(keys) or [""]
    total = len(batches)

    work: "queue.Queue" = queue.Queue()
    for i, b in enumerate(batches):
        work.put((i, b, 0))

    merged: dict = {}
    merged_lock = threading.Lock()
    dead_keys: set[str] = set()
    dead_lock = threading.Lock()
    key_cooldown: dict[str, float] = {}
    cd_lock = threading.Lock()
    pacing_last: dict[str, float] = {}
    pacing_lock = threading.Lock()
    # How many batches have reached a terminal state (done OR exhausted). When this
    # hits `total`, every worker may exit even if the queue is momentarily empty
    # mid-requeue — this is what prevents a busy-spin hang.
    accounted = [0]
    accounted_lock = threading.Lock()

    def account_one():
        with accounted_lock:
            accounted[0] += 1

    def worker(worker_idx: int, key: str):
        auth_fails = 0
        while True:
            _wait_if_paused()  # pause only at the claim boundary, never mid-request
            with dead_lock:
                if key in dead_keys:
                    return
            # Per-key rate cooldown (sibling workers on this key honour it; other
            # keys are unaffected).
            with cd_lock:
                cd = key_cooldown.get(key, 0.0)
            wait = cd - time.time()
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            # Per-key pacing: a request occupies >= delay_seconds of wall-clock.
            if delay_seconds > 0:
                with pacing_lock:
                    last = pacing_last.get(key, 0.0)
                gap = delay_seconds - (time.time() - last)
                if gap > 0:
                    time.sleep(min(gap, 1.0))
                    continue
            try:
                batch_idx, batch, attempts = work.get_nowait()
            except queue.Empty:
                with accounted_lock:
                    if accounted[0] >= total:
                        return
                time.sleep(0.2)
                continue
            if delay_seconds > 0:
                with pacing_lock:
                    pacing_last[key] = time.time()
            try:
                result = process_fn(batch, key, worker_idx)
                auth_fails = 0
                with merged_lock:
                    merged.update(result)
                account_one()
                logger.info("%s batch %d/%d [thread %d]", label, batch_idx + 1, total, worker_idx)
            except Exception as e:
                kind = _classify_error(str(e))
                if kind == "auth":
                    auth_fails += 1
                    if auth_fails >= _POOL_AUTH_GRACE:
                        with dead_lock:
                            dead_keys.add(key)
                            last_key_dead = len(dead_keys) >= len(keys)
                        logger.warning(
                            "Thread %d key failed; requeueing batch %d/%d",
                            worker_idx, batch_idx + 1, total,
                        )
                        if last_key_dead:
                            # No survivor left to take it — account so the pool ends;
                            # caller's default (SKIP/untranslated) covers the batch.
                            # Emit the parseable terminal-failure line so the grid
                            # card stops showing a frozen "translating batch N".
                            logger.error("Failed batch %d/%d [thread %d]: %s",
                                         batch_idx + 1, total, worker_idx, e)
                            account_one()
                        else:
                            work.put((batch_idx, batch, attempts))
                        return  # retire this worker
                    work.put((batch_idx, batch, attempts))  # grace: let a sibling try
                    continue
                elif kind == "rate":
                    if delay_seconds > 0:
                        with cd_lock:
                            key_cooldown[key] = max(
                                key_cooldown.get(key, 0.0), time.time() + delay_seconds
                            )
                    work.put((batch_idx, batch, attempts))  # retry, key stays alive
                    continue
                else:  # "other": cumulative cap across keys, then fall to default
                    if attempts + 1 < _POOL_BATCH_TRIES * len(keys):
                        time.sleep(min(_POOL_RETRY_BACKOFF, 1.0))
                        work.put((batch_idx, batch, attempts + 1))
                        continue
                    logger.error(
                        "Failed batch %d/%d [thread %d]: %s",
                        batch_idx + 1, total, worker_idx, e,
                    )
                    account_one()
                    continue

    workers = []
    for i in range(threads * len(keys)):
        th = threading.Thread(
            target=worker, args=(i, keys[i // threads]),
            daemon=True, name=f"renpy_pool_{i}",
        )
        th.start()
        workers.append(th)
    for th in workers:
        th.join()
    return merged


def gemini_post(prompt: str, api_key: str, model: str, response_schema: dict = None, base_url: str = None, provider: str = None) -> dict:
    # Decide the wire format. When the caller tells us the provider (the normal
    # path now does), trust it: only "gemini" speaks the native generateContent
    # protocol; everything else is OpenAI-compatible (Ollama / LM Studio /
    # OpenRouter). Without a provider hint, fall back to the legacy URL-substring
    # heuristic so old callers / self-tests behave exactly as before.
    if provider:
        is_gemini = provider == "gemini"
    else:
        is_gemini = not (base_url and "generativelanguage.googleapis.com" not in base_url)
    is_openai_compat = not is_gemini

    if is_openai_compat:
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # Tell a Vercel proxy which upstream to hit (real APIs ignore it).
        if provider:
            headers["x-provider"] = provider
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
    else:
        # Gemini. If a proxy base_url is set, route through it the SAME way the
        # main path does (providers/gemini.py::_get_proxy_url): keep the native
        # /v1beta/...:generateContent path and the x-goog-api-key header, swap
        # ONLY the host. This is what makes a Vercel proxy work — the old code
        # mistook the proxy URL for an OpenAI endpoint and sent a Gemini key as a
        # Bearer token to chat/completions.
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        if base_url:
            url = url.replace("https://generativelanguage.googleapis.com", base_url.rstrip("/"))
        headers = {
            "x-goog-api-key": api_key,
            "x-provider": "gemini",  # lets a Vercel proxy route to Gemini upstream
            "Content-Type": "application/json",
        }
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
        }
        if response_schema and "gemma" not in model.lower():
            body["generationConfig"]["responseSchema"] = response_schema
        
    max_retries = 5
    initial_delay = 2.0
    backoff_factor = 2.0
    
    for attempt in range(max_retries):
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=200)
            if resp.status_code == 429:
                delay = initial_delay * (backoff_factor ** attempt)
                logger.warning("Rate limit hit (429). Retrying in %.1f seconds...", delay)
                time.sleep(delay)
                continue
            if resp.status_code in (500, 503, 504):
                delay = initial_delay * (backoff_factor ** attempt)
                logger.warning("Server error (%d). Retrying in %.1f seconds...", resp.status_code, delay)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            try:
                if is_openai_compat:
                    text = data["choices"][0]["message"]["content"]
                else:
                    parts = data["candidates"][0]["content"]["parts"]
                    text = "".join(part["text"] for part in parts if not part.get("thought"))
                return json.loads(text)
            except Exception as e:
                raise RuntimeError(f"Unexpected response from model: {data}") from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 503, 504):
                delay = initial_delay * (backoff_factor ** attempt)
                logger.warning("HTTP status error (%d). Retrying in %.1f seconds...", e.response.status_code, delay)
                time.sleep(delay)
                continue
            raise e
        except (httpx.RequestError, httpx.TimeoutException) as e:
            if attempt == max_retries - 1:
                raise e
            delay = initial_delay * (backoff_factor ** attempt)
            logger.warning("Network error (%s). Retrying in %.1f seconds...", e, delay)
            time.sleep(delay)
            
    raise RuntimeError("Max retries exceeded for API call.")


CLASSIFICATION_BATCH_PROMPT = """
Ты — классификатор строк в исходном коде Ren'Py визуальных новелл.
Твоя задача: определить для каждой строки из списка, является ли она ПОЛЬЗОВАТЕЛЬСКИМ ТЕКСТОМ, который видит игрок (диалоги, описания предметов, меню, тексты кнопок), или ТЕХНИЧЕСКИМ КОДОМ (пути, ID событий, служебные переменные, логи, имена файлов, форматирование).

Правила классификации:
1. TRANSLATE: Игрок видит эту строку на экране. Она написана на понятном языке (например, английском) и несёт смысловую нагрузку для игрока.
2. SKIP: Это технические имена, внутренние идентификаторы, служебные сообщения об ошибках, пути к файлам, ключи словарей или документация/комментарии к коду.

ВАЖНО: При сомнении выбирай SKIP. Лучше не перевести, чем сломать игру.
"""

CLASSIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "decisions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": { "type": "STRING" },
                    "decision": { "type": "STRING", "enum": ["TRANSLATE", "SKIP"] },
                    "reason": { "type": "STRING" }
                },
                "required": ["id", "decision", "reason"]
            }
        }
    },
    "required": ["decisions"]
}

def _thread_idx() -> int:
    name = threading.current_thread().name
    parts = name.rsplit("_", 1)
    try:
        return int(parts[-1])
    except (IndexError, ValueError):
        return 0


def _classify_batch_raw(entries: list[dict], api_key: str, model: str, base_url: str = None, provider: str = None) -> dict[str, tuple[str, str]]:
    """Classify one batch; PROPAGATE API errors so the key-pool can fail over.
    Returns {value: (decision, reason)} only for entries the model answered."""
    prompt_items = []
    id_map = {}
    for idx, entry in enumerate(entries):
        item_id = f"c_{idx}"
        id_map[item_id] = entry

        prompt_items.append({
            "id": item_id,
            "value": entry["value"],
            "raw_line": entry["raw_line"],
            "context_function": entry["context_function"] or "None",
            "context_variable": entry["context_variable"] or "None",
            "context_param": entry["context_param"] or "None"
        })

    prompt = CLASSIFICATION_BATCH_PROMPT + "\n\n" + json.dumps({"candidates": prompt_items}, ensure_ascii=False, indent=2)

    res = gemini_post(prompt, api_key, model, CLASSIFY_SCHEMA, base_url, provider)
    decisions_list = []
    if isinstance(res, list):
        decisions_list = res
    elif isinstance(res, dict):
        decisions_list = res.get("decisions", []) or res.get("items", []) or []

    results: dict[str, tuple[str, str]] = {}
    for item in decisions_list:
        if isinstance(item, dict):
            item_id = item.get("id")
            decision = item.get("decision") or item.get("label") or "SKIP"
            reason = item.get("reason") or item.get("explanation") or "No reason provided"
            if item_id in id_map:
                entry = id_map[item_id]
                results[entry["value"]] = (decision, reason)
    return results


def classify_batch(entries: list[dict], api_key: str, model: str, base_url: str = None) -> tuple[dict[str, tuple[str, str]], int]:
    """Back-compat wrapper (used by self-tests): swallows errors, SKIP-fills gaps."""
    results: dict[str, tuple[str, str]] = {}
    try:
        results = _classify_batch_raw(entries, api_key, model, base_url)
    except Exception as e:
        logger.error("Batch classification failed: %s", e)

    # Default to SKIP for anything that failed to be classified
    for entry in entries:
        if entry["value"] not in results:
            results[entry["value"]] = ("SKIP", "Failed to classify/error")

    return results, _thread_idx()


def _translate_batch_raw(entries: list[dict], target_lang: str, api_key: str, model: str, base_url: str = None, provider: str = None, extra_instruction: str = None) -> dict[str, str]:
    """Translate one batch; PROPAGATE API errors so the key-pool can fail over.
    Returns {source_value: translated_value} for entries the model answered."""
    prompt_items = []
    id_map = {}
    for idx, entry in enumerate(entries):
        item_id = f"id_{idx}"
        id_map[item_id] = entry["value"]

        ctx_parts = []
        if entry["context_function"]:
            ctx_parts.append(f"Function: {entry['context_function']}")
        if entry["context_variable"]:
            ctx_parts.append(f"Variable: {entry['context_variable']}")
        if entry["context_param"]:
            ctx_parts.append(f"Param: {entry['context_param']}")

        prompt_items.append({
            "id": item_id,
            "text": entry["value"],
            "context": " | ".join(ctx_parts)
        })

    instruction = SYSTEM_INSTRUCTION.format(lang=target_lang)
    if extra_instruction:
        instruction = extra_instruction + "\n\n" + instruction
    lines = [instruction, "", "Translate these strings:"]
    payload = {item["id"]: {"text": item["text"], "context": item["context"]} for item in prompt_items}
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    prompt = "\n".join(lines)

    schema = {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": { "type": "STRING" },
                        "translated": { "type": "STRING" }
                    },
                    "required": ["id", "translated"]
                }
            }
        },
        "required": ["items"]
    }

    res = gemini_post(prompt, api_key, model, schema, base_url, provider)
    items_list = []
    if isinstance(res, list):
        items_list = res
    elif isinstance(res, dict):
        items_list = res.get("items", []) or res.get("translations", []) or []

    translations: dict[str, str] = {}
    for item in items_list:
        if isinstance(item, dict):
            item_id = item.get("id")
            translated_val = item.get("translated") or item.get("translation")
            if item_id in id_map and translated_val:
                translations[id_map[item_id]] = translated_val
    return translations


def translate_batch(entries: list[dict], target_lang: str, api_key: str, model: str, base_url: str = None) -> tuple[dict[str, str], int]:
    """Back-compat wrapper (used by self-tests): swallows errors."""
    translations: dict[str, str] = {}
    try:
        translations = _translate_batch_raw(entries, target_lang, api_key, model, base_url)
    except Exception as e:
        logger.error("Batch translation failed: %s", e)

    return translations, _thread_idx()

# ---------------------------------------------------------------------------
# Pipeline Steps
# ---------------------------------------------------------------------------

def load_all_sources(game_path: Path) -> dict[Path, str]:
    """Return inline-Python translatable sources from both loose and archived .rpy,
    READ-ONLY: archived scripts are read straight out of the .rpa into memory and
    NEVER written to disk.

    This is the heart of the non-fragile design. The old path extracted archived
    .rpy to disk, edited them, and recompiled .rpyc via the game runtime — which
    risked the double-load crash and needed the runtime. We now translate purely
    through a native `translate <lang> strings:` dictionary (see
    _write_inline_strings_file), so we only need to READ the source text to find
    translatable candidates. Nothing touches the archive or the disk; works on any
    game, runtime or not.

    Archived sources get a synthetic key path `<game_dir>/<inner-rel>` (not written
    to disk) so candidate extraction and logging have a stable, readable path.
    Loose files win on path collision (matches the engine: disk before archive).
    """
    game_dir = game_path / "game"
    if not game_dir.is_dir():
        game_dir = game_path

    sources: dict[Path, str] = {}

    # Loose .rpy files on disk (excluding our own backups and the tl/ output).
    rpy_files = list(game_dir.rglob("*.rpy"))
    rpy_files = [f for f in rpy_files if ".interprex" not in f.parts and "tl" not in f.parts]
    loose_rel: set[str] = set()
    for fpath in rpy_files:
        abs_path = fpath.resolve()
        loose_rel.add(fpath.relative_to(game_dir).as_posix())
        try:
            sources[abs_path] = fpath.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed to read loose file %s: %s", fpath, e)

    # Read archived .rpy straight from the .rpa into memory (no disk writes).
    try:
        from parsers.rpa import iter_rpa_files, read_rpa
        archived_read = 0
        for arc_path in iter_rpa_files(str(game_dir)):
            try:
                for rf in read_rpa(arc_path, ".rpy"):
                    rel = rf.path.replace("\\", "/")
                    if rel in loose_rel:
                        continue  # loose file takes priority
                    key = (game_dir / rel).resolve()  # synthetic path, not written
                    if key in sources:
                        continue
                    sources[key] = rf.data.replace("\r\n", "\n")
                    archived_read += 1
            except Exception as e:
                logger.error("Failed to read archive %s: %s", arc_path, e)
        if archived_read:
            logger.info("Read %d archived script(s) from .rpa (in-memory, no disk writes).", archived_read)
    except Exception as e:
        logger.warning("Could not read archived scripts: %s", e)

    return sources


def _backup_created(game_path: Path, file_path: Path):
    """Backup a file that was CREATED (not originally on disk) as type='created'.

    On restore, these files are simply deleted. Used for .rpy/.rpyc extracted
    from archives — they didn't exist on disk before, so restore just removes them."""
    backup_dir = game_path / ".interprex_backups"
    rel_path = file_path.relative_to(game_path).as_posix()

    metadata_path = backup_dir / "metadata.json"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Write gitignore if not exists
    gitignore_path = backup_dir / ".gitignore"
    if not gitignore_path.exists():
        try:
            gitignore_path.write_text("*\n", encoding="utf-8")
        except Exception:
            pass

    # Load existing metadata
    metadata = {}
    if metadata_path.exists():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception:
            pass

    if rel_path in metadata:
        return  # Already backed up

    metadata[rel_path] = {"type": "created", "orig_sha256": ""}
    try:
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        logger.info("Backed up (created): %s", rel_path)
    except Exception as e:
        logger.error("Failed to write created-backup metadata for %s: %s", rel_path, e)


def _remove_created_backup(game_path: Path, file_path: Path):
    """Drop a previously recorded type='created' entry from the backup metadata.

    Used when an extracted file we'd staged as 'created' is deleted again (e.g. an
    orphan loose .rpy removed to avoid the double-load crash): without this the
    metadata would point at a file that no longer exists."""
    metadata_path = game_path / ".interprex_backups" / "metadata.json"
    if not metadata_path.exists():
        return
    rel_path = file_path.relative_to(game_path).as_posix()
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        if metadata.pop(rel_path, None) is not None:
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            logger.info("Removed stale created-backup entry: %s", rel_path)
    except Exception as e:
        logger.warning("Failed to remove created-backup entry for %s: %s", rel_path, e)


def backup_file(game_path: Path, file_path: Path):
    backup_dir = game_path / ".interprex_backups"
    rel_path = file_path.relative_to(game_path).as_posix()
    
    # Check if metadata.json already has this file
    metadata_path = backup_dir / "metadata.json"
    if metadata_path.exists():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            if rel_path in metadata:
                return  # Already backed up
        except Exception:
            pass

    # Read original bytes
    try:
        orig_bytes = file_path.read_bytes()
    except Exception as e:
        logger.error("Failed to read original file %s: %s", file_path, e)
        return

    orig_sha = hashlib.sha256(orig_bytes).hexdigest()
    orig_temp_path = backup_dir / (rel_path + ".orig_temp")
    orig_temp_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write gitignore if not exists
    gitignore_path = backup_dir / ".gitignore"
    if not gitignore_path.exists():
        try:
            gitignore_path.write_text("*\n", encoding="utf-8")
        except Exception:
            pass

    # Write orig_temp
    try:
        orig_temp_path.write_bytes(orig_bytes)
        from parsers.base import update_metadata
        update_metadata(str(game_path), rel_path, orig_sha, "", "patch")
        logger.info("Backed up (staged): %s", rel_path)
    except Exception as e:
        logger.error("Failed to write backup temp for %s: %s", rel_path, e)


def finalize_backups(game_path: Path):
    backup_dir = game_path / ".interprex_backups"
    if not backup_dir.is_dir():
        return

    from utils.binary_diff import make_patch
    from parsers.base import update_metadata

    metadata_path = backup_dir / "metadata.json"
    if not metadata_path.exists():
        return

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception as e:
        logger.error("Failed to load metadata for finalization: %s", e)
        return

    # List to track entries we need to remove because they were not modified
    to_remove = []

    for rel_path, info in metadata.items():
        orig_temp_path = backup_dir / (rel_path + ".orig_temp")
        if not orig_temp_path.exists():
            continue

        target_file = game_path / rel_path
        if not target_file.exists():
            continue

        try:
            orig_bytes = orig_temp_path.read_bytes()
            mod_bytes = target_file.read_bytes()
            
            if orig_bytes == mod_bytes:
                orig_temp_path.unlink()
                to_remove.append(rel_path)
                logger.info("File was not modified, removed temp backup: %s", rel_path)
                continue

            # Generate binary patch
            patch = make_patch(orig_bytes, mod_bytes)
            patch_path = backup_dir / (rel_path + ".patch")
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_bytes(patch)

            # Delete the temp file
            orig_temp_path.unlink()

            # Update metadata atomically
            mod_sha = hashlib.sha256(mod_bytes).hexdigest()
            update_metadata(str(game_path), rel_path, info["orig_sha256"], mod_sha, "patch")
            logger.info("Finalized patch for: %s", rel_path)
        except Exception as e:
            logger.error("Failed to finalize backup patch for %s: %s", rel_path, e)

    if to_remove:
        try:
            # Re-load metadata to avoid race conditions
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            for rel_path in to_remove:
                metadata.pop(rel_path, None)
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.error("Failed to remove unmodified entries from metadata: %s", e)


def extract_python_blocks(rpy_content: str) -> list[dict]:
    lines = rpy_content.split("\n")
    blocks = []
    
    in_block = False
    block_indent = 0
    block_lines = []
    block_start_line = 0
    
    python_block_re = re.compile(r'^\s*(?:init\s+[-0-9]+\s+)?python(?:\s+\w+)?\s*:\s*(?:#.*)?$')
    
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            if in_block:
                block_lines.append("")  # keep line numbers aligned
            continue
            
        cur_indent = len(line) - len(line.lstrip())
        
        if in_block:
            if cur_indent > block_indent or stripped.startswith("#"):
                block_lines.append(line)
            else:
                # Block ended
                blocks.append({
                    "start_line": block_start_line,
                    "code": "\n".join(block_lines),
                    "type": "multiline"
                })
                in_block = False
                block_lines = []
                # Fall through to process current line in normal state
                
        if not in_block:
            if stripped.startswith("$"):
                blocks.append({
                    "start_line": idx,
                    "code": line.lstrip()[1:].strip(),
                    "type": "single"
                })
            elif python_block_re.match(stripped):
                in_block = True
                block_indent = cur_indent
                block_start_line = idx
                block_lines = []
                
    if in_block and block_lines:
        blocks.append({
            "start_line": block_start_line,
            "code": "\n".join(block_lines),
            "type": "multiline"
        })
        
    return blocks


# `show screen NAME(...)` / `call screen NAME(...)` — Ren'Py statements (NOT
# python: blocks), so the AST extractor never sees their argument literals. Yet
# those args carry player-visible text (e.g. search history:
# `show screen search_bar(history_entries=["how to murder someone..."])`). We
# scan these lines and pull string literals from the parenthesised args.
_SCREEN_CALL_RE = re.compile(
    r'^\s*(?:show|call)\s+screen\s+(\w+)\s*\((.*)\)\s*:?\s*(?:#.*)?$'
)
_SCREEN_STR_LIT_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


def _extract_screen_call_candidates(content: str) -> list[dict]:
    """Candidates from `show/call screen NAME(...)` argument literals. Same dict
    shape as StringExtractor's output so they flow through the SAME classification
    (comparison-key protection + hard_skip + Gemini) and apply machinery."""
    candidates: list[dict] = []
    for i, line in enumerate(content.split("\n")):
        m = _SCREEN_CALL_RE.match(line)
        if not m:
            continue
        screen_name, arglist = m.group(1), m.group(2)
        for lm in _SCREEN_STR_LIT_RE.finditer(arglist):
            raw_literal = lm.group(0)
            try:
                val = ast.literal_eval(raw_literal)
            except Exception:
                continue
            if not isinstance(val, str) or not val.strip():
                continue
            candidates.append({
                "value": val,
                "line": i + 1,
                "end_line": i + 1,
                "context_function": "screen",
                "context_variable": screen_name,
                "context_param": None,
                "raw_literal": raw_literal,
                "raw_line": line.strip(),
            })
    return candidates


def parse_and_extract_candidates(file_path: Path, content: str) -> list[dict]:
    raw_lines = content.split("\n")
    blocks = extract_python_blocks(content)

    candidates = []
    for block in blocks:
        dedented_code = textwrap.dedent(block["code"])
        try:
            tree = ast.parse(dedented_code)
            populate_parents(tree)
            visitor = StringExtractor(raw_lines, block["start_line"], block["type"])
            visitor.visit(tree)
            candidates.extend(visitor.candidates)
        except Exception:
            pass

    # Pull literals from show/call screen statements (not python: blocks).
    candidates.extend(_extract_screen_call_candidates(content))

    # Add file path to each candidate
    for cand in candidates:
        cand["file_path"] = file_path

    return candidates


_INLINE_STRINGS_REL = "game/tl/{lang}/_interprex_inline.rpy"

# Ren'Py-specific tokens that must be preserved verbatim in translations.
# Changing these crashes the game at runtime.
_RENPY_VAR_RE = re.compile(r'\[([^\]]+)\]')
_RENPY_TAG_RE = re.compile(r'\{/?[a-zA-Z][^}]*\}')
_RENPY_PERCENT_RE = re.compile(r'%(?:%|\(\w+\)[diouxXeEfFgGcrs]|[-#0+]*\d*\.?\d*[hlL]?[diouxXeEfFgGcrs])')


def _validate_renpy_tokens(old: str, new: str) -> list[str]:
    """Check all Ren'Py-specific tokens are preserved in translation.

    Returns list of violation descriptions (empty = OK). Checks:
    1. [var] interpolation tokens — same set, same case
    2. {tag} text tags — same set (opening + closing)
    3. %s / %(name)s / %d format specs — same set
    """
    violations = []
    old_vars = sorted(m.group(0) for m in _RENPY_VAR_RE.finditer(old))
    new_vars = sorted(m.group(0) for m in _RENPY_VAR_RE.finditer(new))
    if old_vars != new_vars:
        violations.append(f"[var] tokens differ: {old_vars} -> {new_vars}")
    old_tags = sorted(m.group(0) for m in _RENPY_TAG_RE.finditer(old))
    new_tags = sorted(m.group(0) for m in _RENPY_TAG_RE.finditer(new))
    if old_tags != new_tags:
        violations.append(f"{{tag}} tokens differ: {old_tags} -> {new_tags}")
    old_fmt = sorted(m.group(0) for m in _RENPY_PERCENT_RE.finditer(old))
    new_fmt = sorted(m.group(0) for m in _RENPY_PERCENT_RE.finditer(new))
    if old_fmt != new_fmt:
        violations.append(f"%-format tokens differ: {old_fmt} -> {new_fmt}")
    return violations

# Matches an `old "..."` / `old '...'` line in a tl/ strings block.
_TL_OLD_RE = re.compile(r'^\s*old\s+("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')\s*$')


def _existing_tl_string_keys(game_path: Path, lang: str) -> set[str]:
    """Collect every `old "..."` key already present in the tl/<lang>/ tree
    (the dialogue path writes `translate <lang> strings:` blocks too). Ren'Py
    forbids a duplicate `old` key GLOBALLY per language — emitting one again
    crashes the game at init. So inline strings must EXCLUDE anything the
    dialogue tl/ already covers. Returns decoded key values. Skips our own
    _interprex_inline.rpy so a re-run doesn't see itself."""
    keys: set[str] = set()
    tl_dir = game_path / "game" / "tl" / lang
    if not tl_dir.is_dir():
        return keys
    inline_name = "_interprex_inline.rpy"
    for f in tl_dir.rglob("*.rpy"):
        if f.name == inline_name:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.split("\n"):
            m = _TL_OLD_RE.match(line)
            if not m:
                continue
            try:
                keys.add(ast.literal_eval(m.group(1)))
            except Exception:
                pass
    return keys


def _write_inline_strings_file(game_path: Path, lang: str,
                               pairs: list[tuple[str, str]]) -> int:
    """Write inline-Python translations as a NATIVE Ren'Py `translate <lang>
    strings:` block — NOT by editing archived .rpy. The engine runs every
    displayed string through translate_string() (a runtime old→new dict lookup)
    BEFORE [var] interpolation, so this catches blog/status/search-history text
    no matter where it lives, never touches archives, never compiles .rpyc, and
    cannot break a code-level comparison (those read the raw value, not the dict).

    `pairs` = list of (original_value, translated). `old` is the original string
    EXACTLY as the engine sees it pre-interpolation (the template, with any
    `[var]`). Deduped by `old` (the dict is global per language; duplicate keys
    warn and only the last wins). Written as ONE file, registered as a `created`
    backup so restore just deletes it. Returns the number of pairs written."""
    from parsers.renpy import _string_quote, _escape_bad_percent

    # Seed with keys ALREADY in the dialogue tl/ tree: Ren'Py crashes on a
    # duplicate `old` key per language. Pre-loading them here makes the dedup
    # below skip anything the dialogue path already translates.
    seen: set[str] = _existing_tl_string_keys(game_path, lang)
    skipped_dupe = 0
    lines = [
        "# Added by Interprex — inline-Python translations via Ren'Py's native",
        "# runtime string dictionary (translate_string). No archive edits, no",
        "# .rpyc recompile, cannot break code comparisons. Restore deletes this.",
        f"translate {lang} strings:",
    ]
    written = 0
    skipped_vars = 0
    for original, translated in pairs:
        if not original or not translated:
            continue
        if original in seen:
            skipped_dupe += 1
            continue
        violations = _validate_renpy_tokens(original, translated)
        if violations:
            skipped_vars += 1
            logger.info("Skipped inline pair (tokens corrupted): %r -> %r [%s]",
                        original, translated, "; ".join(violations))
            continue
        seen.add(original)
        # `new` is DISPLAYED text → runs through the engine's %-substitution, so a
        # bare % the LLM left unescaped is a crash-class lint error. Fix it
        # deterministically (same engine-accurate rule as the dialogue path). The
        # `old` KEY is the exact runtime lookup string and must stay byte-verbatim.
        new_val = _escape_bad_percent(translated)
        # Also cover a .lower()/.upper() display transform (e.g. status text shown
        # as `text mc.status.lower()`): add the cased key too so the dict still
        # hits after the in-code transform. Harmless if never used.
        lines.append("")
        lines.append(f"    old {_string_quote(original)}")
        lines.append(f"    new {_string_quote(new_val)}")
        written += 1
        for variant in (original.lower(), original.upper()):
            if variant != original and variant not in seen:
                seen.add(variant)
                lines.append("")
                lines.append(f"    old {_string_quote(variant)}")
                lines.append(f"    new {_string_quote(new_val)}")

    if written == 0:
        return 0

    # Auto-detect format patterns like "MONTH {}" in game source and generate
    # translated variants (e.g. "MONTH 1" → "МЕСЯЦ 1"). These are dynamic
    # strings our LLM pipeline never sees (no _() call), but the engine's
    # translate_string() can match them at runtime.
    try:
        from parsers.renpy import RenPyParser
        _fmt_pairs = _detect_format_patterns(game_path, lang)
        for orig, trans in _fmt_pairs:
            if orig not in seen:
                seen.add(orig)
                new_val = _escape_bad_percent(trans)
                lines.append("")
                lines.append(f"    old {_string_quote(orig)}")
                lines.append(f"    new {_string_quote(new_val)}")
                written += 1
    except Exception:
        pass

    rel = _INLINE_STRINGS_REL.format(lang=lang)
    abs_path = game_path / rel.replace("/", os.sep)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    # Register as created BEFORE writing (mirrors _atomic_write semantics) so
    # restore removes it even if the write is later interrupted.
    if not abs_path.exists():
        _backup_created(game_path, abs_path)
    with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    logger.info("Wrote inline strings file: %s (%d entries, %d skipped as already in tl/, %d skipped due to corrupted [var] tokens)",
                rel, written, skipped_dupe, skipped_vars)
    return written


# Common format patterns across games: "MONTH {}" → "МЕСЯЦ N", etc.
# Maps English format prefix → list of (number, translated) pairs.
_FORMAT_PATTERN_TRANSLATIONS: dict[str, dict[str, str]] = {
    "MONTH": {
        "russian": {str(i): f"МЕСЯЦ {i}" for i in range(1, 13)},
    },
}


def _detect_format_patterns(game_path: Path, lang: str) -> list[tuple[str, str]]:
    """Scan game source for common format patterns (e.g. "MONTH {}") and
    generate translate_string()-ready old/new pairs. Returns pairs that
    should be added to the inline strings dict."""
    pairs: list[tuple[str, str]] = []
    lang_lower = lang.lower()

    # Find all format patterns in source: "PREFIX {}".format(...) or f"PREFIX {var}"
    import re
    _FMT_RE = re.compile(r'''(?:"|')([A-Z]{2,15})\s*\{\}(?:"|')''')

    try:
        from parsers.renpy import RenPyParser
        p = RenPyParser()
        for _fp, text in p._iter_sources(str(game_path)):
            for m in _FMT_RE.finditer(text):
                prefix = m.group(1)
                translations = _FORMAT_PATTERN_TRANSLATIONS.get(prefix, {})
                # Try exact lang match, then script name (e.g. "russian" from "Russian")
                trans_map = translations.get(lang_lower)
                if not trans_map:
                    for key, val in translations.items():
                        if key in lang_lower or lang_lower in key:
                            trans_map = val
                            break
                if not trans_map:
                    continue
                for num, translated in trans_map.items():
                    orig = f"{prefix} {num}"
                    pairs.append((orig, translated))
    except Exception:
        pass
    return pairs


# ---------------------------------------------------------------------------
# Test Suite Scenario
# ---------------------------------------------------------------------------

def run_self_tests():
    logger.info("Running self-tests...")
    
    # 1. Test raw literal finder
    lines = [
        "    $ my_var = \"test value\"",
        "    $ other_var = 'second value'",
        "    python:",
        "        third = \"\"\"multiline",
        "value\"\"\""
    ]
    
    lit1 = find_raw_literal(lines, 1, 1, "test value")
    assert lit1 == '"test value"', f"Expected '\"test value\"', got {repr(lit1)}"
    
    lit2 = find_raw_literal(lines, 2, 2, "second value")
    assert lit2 == "'second value'", f"Expected \"'second value'\", got {repr(lit2)}"
    
    lit3 = find_raw_literal(lines, 4, 5, "multiline\nvalue")
    assert lit3 == '"""multiline\nvalue"""', f"Expected triple-quotes, got {repr(lit3)}"
    
    # 2. Test hard classification rules
    entry_skip = {"value": "no_spaces_here", "raw_line": "x = 'no_spaces_here'"}
    assert hard_skip(entry_skip) is True, "Should skip single-word variables"
    
    entry_trans = {
        "value": "Welcome back, player!",
        "context_param": "status_text",
        "raw_line": "Member(status_text='Welcome back, player!')"
    }
    assert hard_skip(entry_trans) is False, "Prose shouldn't be skipped"
    assert hard_translate(entry_trans) is True, "Should translate status_text keyword arguments"
    
    logger.info("All self-tests passed successfully!")

# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------

def main(args_list=None):
    parser = argparse.ArgumentParser(description="Ren'Py Inline Python String Translator")
    parser.add_argument("--root", type=str, required=True, help="Path to the game directory")
    parser.add_argument("--api-key", type=str, default="", help="Single Gemini API Key (legacy; superseded by --api-keys)")
    parser.add_argument("--api-keys", type=str, default="", help="All API keys: JSON array, or comma-separated. Spreads work across keys with failover.")
    parser.add_argument("--target-lang", type=str, default="russian", help="Target language name")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Gemini model to use")
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be translated/skipped, without modifying any files")
    parser.add_argument("--base-url", type=str, default=None, help="Base URL for OpenAI-compatible API (e.g. OpenRouter) or a Gemini proxy")
    parser.add_argument("--provider", type=str, default=None, help="Provider id (gemini / openrouter / ollama / ...); decides the wire format, not the URL")
    parser.add_argument("--threads", type=int, default=4, help="Parallel workers PER KEY (total = threads x keys)")
    parser.add_argument("--delay-seconds", type=float, default=0.0, help="Per-key pacing: a request occupies at least this many seconds")
    parser.add_argument("--apply-cached-only", action="store_true", help="No API: apply inline-Python translations from the cache only (used by writeBack)")

    args = parser.parse_args(args_list)

    # Parse the key list: prefer --api-keys (JSON array, else comma-split), falling
    # back to the legacy single --api-key. Deduped/collapsed by _build_key_list.
    parsed_keys: list[str] = []
    if args.api_keys:
        try:
            loaded = json.loads(args.api_keys)
            if isinstance(loaded, list):
                parsed_keys = [str(k) for k in loaded]
            else:
                parsed_keys = [str(loaded)]
        except (json.JSONDecodeError, ValueError):
            parsed_keys = [k.strip() for k in args.api_keys.split(",")]
    keys = _build_key_list(parsed_keys, args.api_key)
    
    if args.test:
        run_self_tests()
        sys.exit(0)
        
    game_path = Path(args.root)
    if not game_path.is_dir():
        logger.error("Game path does not exist: %s", args.root)
        sys.exit(1)

    # Read all sources (loose + archived, in-memory — archives are NOT extracted
    # to disk; translation goes through a native strings dictionary, see
    # _write_inline_strings_file). No runtime needed, no .rpyc compilation.
    sources = load_all_sources(game_path)
    if not sources:
        logger.error("No .rpy script sources found.")
        sys.exit(1)

    logger.info("Found %d script sources to process.", len(sources))
    
    # Extract candidates
    all_candidates = []
    for fpath, content in sources.items():
        try:
            candidates = parse_and_extract_candidates(fpath, content)
            all_candidates.extend(candidates)
        except Exception as e:
            logger.error("Failed to parse candidates in %s: %s", fpath.name, e)
            
    logger.info("Extracted %d string literal candidates from Python blocks.", len(all_candidates))
    
    # Strings the game compares in code (==, !=, in [...], dict key, .get()).
    # Translating any of these breaks logic/clicks, so they are force-skipped
    # below — see find_comparison_keys. Computed once over ALL sources so a key
    # compared in a different file (or a screen condition) is still protected.
    comparison_keys = find_comparison_keys(sources)
    logger.info("Found %d distinct comparison-key strings (will be protected from translation).", len(comparison_keys))

    # Promote visible prose keys: keys that are both compared AND player-visible
    # (e.g. "murder weapon", "Death of the Author"). These get globally replaced
    # so the == still matches after translation.
    display_by_value: dict[str, list[dict]] = {}
    for e in all_candidates:
        display_by_value.setdefault(e["value"], []).append(e)
    _norm = lambda s: re.sub(r"\s+", " ", s).strip()
    try:
        displayed_via_tl = {_norm(s.original) for s in RenPyParser().extract(str(game_path))}
    except Exception as exc:
        logger.warning("Could not extract tl/ strings for promotion filter: %s", exc)
        displayed_via_tl = set()
    visible_translatable_keys = {
        v for v in comparison_keys
        if _visible_translatable_key(v, display_by_value.get(v, []))
        and _norm(v) not in displayed_via_tl
    }
    if visible_translatable_keys:
        logger.info("Promoted %d comparison keys to translation (visible prose): %s",
                     len(visible_translatable_keys), visible_translatable_keys)

    # Classification pipeline
    to_translate = []
    skipped_hard = 0
    skipped_keys = 0
    translated_hard = 0
    classified_gemini_skip = 0
    classified_gemini_trans = 0

    SKIP_FUNCTIONS = {
        "load", "image", "play", "stop", "queue",
        "renpy.music", "renpy.sound", "renpy.image",
        "config", "define", "AudioURL", "im.Scale"
    }

    gemini_candidates = []

    for entry in all_candidates:
        # Force-skip comparison keys FIRST — before hard_translate, so even a
        # "looks translatable" key (e.g. appended to a list AND compared
        # elsewhere) is protected. Keeps game logic/clicks intact.
        # Exception: visible prose keys that have been promoted to translation.
        if entry["value"] in comparison_keys and entry["value"] not in visible_translatable_keys:
            skipped_keys += 1
            if args.dry_run:
                logger.info("[DRY RUN] Would skip (comparison key): %s", repr(entry["value"]))
            continue

        if hard_skip(entry):
            skipped_hard += 1
            if args.dry_run:
                v = entry["value"]
                v_stripped = v.strip()
                reason = "unknown"
                if not v_stripped:
                    reason = "empty"
                elif "/" in v or "\\" in v:
                    reason = "path separators"
                elif re.match(r'^#[0-9a-fA-F]{3,8}$', v_stripped):
                    reason = "hex color"
                elif v_stripped.startswith((',', ']', '[', ')', '(', '=', '+', '-', '*', '%', '/', ';', '{', '}')) or \
                     v_stripped.endswith((',', ']', '[', ')', '(', '=', '+', '-', '*', '%', '/', ';', '{', '}')):
                    reason = "starts/ends with code symbols"
                elif any(f in entry["raw_line"] for f in SKIP_CONTEXTS):
                    reason = "technical context"
                logger.info("[DRY RUN] Would skip: %s (%s)", repr(v), reason)
            continue
            
        if hard_translate(entry):
            translated_hard += 1
            to_translate.append(entry)
            continue
            
        gemini_candidates.append(entry)
        
    total_workers = max(1, args.threads) * len(keys)

    # Translation cache: stores the actual inline-Python translations so a second
    # run re-translates ONLY new strings, and the no-API apply path (writeBack)
    # can lay them back down with zero API quota.
    trans_cache = _TranslationCache(game_path, args.target_lang)

    if args.apply_cached_only:
        # No API at all. hard_translate entries are already in to_translate; add
        # any gemini-candidate that has a stored translation (it was a TRANSLATE
        # in a prior full run). Strings without a cached translation stay English.
        for entry in gemini_candidates:
            if trans_cache.get(entry) is not None:
                to_translate.append(entry)
        logger.info("Apply-cached-only mode: no API; applying from translation cache.")
    elif gemini_candidates:
        classify_cache = _ClassificationCache(game_path, args.model, args.provider)

        cached_entries: list[dict] = []
        uncached_entries: list[dict] = []
        for entry in gemini_candidates:
            hit = classify_cache.get(entry)
            if hit is not None:
                cached_entries.append((entry, hit[0], hit[1]))
            else:
                uncached_entries.append(entry)

        if cached_entries:
            logger.info("Classification cache hit: %d / %d candidates", len(cached_entries), len(gemini_candidates))
            for entry, decision, reason in cached_entries:
                if decision == "TRANSLATE":
                    classified_gemini_trans += 1
                    to_translate.append(entry)
                    if args.dry_run:
                        logger.info("[DRY RUN] Would translate: %s (cached: %s)", repr(entry['value']), reason)
                else:
                    classified_gemini_skip += 1
                    if args.dry_run:
                        logger.info("[DRY RUN] Would skip: %s (cached: %s)", repr(entry['value']), reason)

        if uncached_entries:
            batch_size = 40
            batches = [uncached_entries[i : i + batch_size] for i in range(0, len(uncached_entries), batch_size)]

            logger.info("Classifying %d candidates with Gemini in %d parallel batches (threads=%d)...", len(uncached_entries), len(batches), total_workers)

            def _classify_pf(batch, key, worker_idx):
                return _classify_batch_raw(batch, key, args.model, args.base_url, args.provider)

            decisions_map = _run_batches_over_keypool(
                batches, keys, args.threads, args.delay_seconds, "Classified", _classify_pf
            )

            for entry in uncached_entries:
                decision, reason = decisions_map.get(entry["value"], ("SKIP", "Failed to classify/error"))
                classify_cache.put(entry, decision, reason)
                if decision == "TRANSLATE":
                    classified_gemini_trans += 1
                    to_translate.append(entry)
                    if args.dry_run:
                        logger.info("[DRY RUN] Would translate: %s (Gemini: %s)", repr(entry['value']), reason)
                else:
                    classified_gemini_skip += 1
                    if args.dry_run:
                        logger.info("[DRY RUN] Would skip: %s (Gemini: %s)", repr(entry['value']), reason)

        classify_cache.save()

    if gemini_candidates:
        logger.info("Classify phase done: %d translate / %d skip", classified_gemini_trans, classified_gemini_skip)

    logger.info("Classification summary:")
    logger.info("  Skipped as comparison keys (protected): %d", skipped_keys)
    logger.info("  Skipped by hard rules: %d", skipped_hard)
    logger.info("  Translated by hard rules: %d", translated_hard)
    logger.info("  Skipped by Gemini: %d", classified_gemini_skip)
    logger.info("  Translated by Gemini: %d", classified_gemini_trans)
    # Ensure each promoted visible prose key has a representative in to_translate
    # so it lands in the strings dictionary. NOTE: with the native strings-dict
    # approach, a promoted key needs NO global file rewrite — the dict translates
    # only the DISPLAYED text, while a code comparison (`== "key"`) reads the raw
    # English value and still matches. So we just add a representative candidate;
    # no `_global` flag, no apply_global_replacement.
    existing_vals = {e["value"] for e in to_translate}
    for v in visible_translatable_keys:
        if v in existing_vals:
            continue
        reps = display_by_value.get(v, [])
        if not reps:
            continue
        to_translate.append(reps[0].copy())
        existing_vals.add(v)
        translated_hard += 1  # count as hard-translated for stats
    if visible_translatable_keys:
        logger.info("Promoted visible keys: %d", len(visible_translatable_keys))

    if not to_translate:
        logger.info("No strings identified for translation.")
        sys.exit(0)
        
    # Step 4: Translate. Pull anything already in the translation cache (free), and
    # only send cache-misses to the API. This is what makes a re-run "translate
    # only new strings" and lets the no-API apply path work at all.
    translations: dict[str, str] = {}
    uncached_to_translate: list[dict] = []
    for entry in to_translate:
        cached_tr = trans_cache.get(entry)
        if cached_tr is not None:
            translations[entry["value"]] = cached_tr
        else:
            uncached_to_translate.append(entry)

    if translations:
        logger.info("Translation cache hit: %d / %d strings", len(translations), len(to_translate))

    if uncached_to_translate and not args.apply_cached_only:
        batch_size = 30
        batches = [uncached_to_translate[i : i + batch_size] for i in range(0, len(uncached_to_translate), batch_size)]

        logger.info("Translating %d strings in %d parallel batches (threads=%d)...", len(uncached_to_translate), len(batches), total_workers)

        def _translate_pf(batch, key, worker_idx):
            return _translate_batch_raw(batch, args.target_lang, key, args.model, args.base_url, args.provider)

        fresh = _run_batches_over_keypool(
            batches, keys, args.threads, args.delay_seconds, "Translated", _translate_pf
        )

        # Retry strings the model silently dropped from their batch (a real failure
        # mode: hard=True strings like the blog line came back missing). One small
        # poshtuchno pass — anything still missing is logged explicitly, not lost
        # in silence.
        missing = [e for e in uncached_to_translate if e["value"] not in fresh]
        if missing:
            logger.info("Retrying %d string(s) the model dropped from their batch...", len(missing))
            retry = _run_batches_over_keypool(
                [[e] for e in missing], keys, args.threads, args.delay_seconds, "Retried", _translate_pf
            )
            fresh.update(retry)
            still = [e["value"] for e in missing if e["value"] not in fresh]
            if still:
                logger.warning("%d string(s) left untranslated after retry: %s",
                               len(still), [s[:50] for s in still[:10]])

        translations.update(fresh)
        for entry in uncached_to_translate:
            tr = fresh.get(entry["value"])
            if tr:
                trans_cache.put(entry, tr)
        trans_cache.save()

    # Step 4b: Validate Ren'Py tokens in all translations. If [var], {tag}, or
    # %-format tokens got corrupted by the LLM, retry with an explicit
    # instruction to preserve them verbatim. Only skip after a second failure.
    if translations and not args.apply_cached_only:
        corrupted = []
        for orig, tr in list(translations.items()):
            viols = _validate_renpy_tokens(orig, tr)
            if viols:
                corrupted.append((orig, tr, viols))
        if corrupted:
            logger.info("Token corruption detected in %d translation(s), retrying with explicit instruction...",
                        len(corrupted))
            retry_entries = []
            for orig, old_tr, viols in corrupted:
                logger.info("  Corrupted: %r -> %r [%s]", orig, old_tr, "; ".join(viols))
                retry_entries.append({"value": orig, "context_function": "", "context_variable": "", "context_param": ""})
            if retry_entries:
                retry_batches = [retry_entries[i:i+20] for i in range(0, len(retry_entries), 20)]

                def _retry_pf(batch, key, worker_idx):
                    return _translate_batch_raw(batch, args.target_lang, key, args.model,
                                               args.base_url, args.provider,
                                               extra_instruction=(
                                                   "CRITICAL REMINDER: You MUST copy these Ren'Py "
                                                   "patterns EXACTLY from the source — do NOT change "
                                                   "them: [variable_name], {b}, {/b}, {i}, {/i}, "
                                                   "{color=#...}, {size=...}, %(name)s, %s, %d, %%s, "
                                                   "\\n. These are code, not translatable text. "
                                                   "Changing them WILL crash the game."
                                               ))

                retried = _run_batches_over_keypool(
                    retry_batches, keys, args.threads, args.delay_seconds,
                    "TokenFixed", _retry_pf
                )
                fixed = 0
                still_broken = 0
                for orig, old_tr, viols in corrupted:
                    new_tr = retried.get(orig)
                    if new_tr and not _validate_renpy_tokens(orig, new_tr):
                        translations[orig] = new_tr
                        fixed += 1
                        logger.info("  Fixed: %r -> %r", orig, new_tr)
                    else:
                        still_broken += 1
                        logger.warning("  Still broken after retry, will be skipped: %r", orig)
                        del translations[orig]
                logger.info("Token retry: %d fixed, %d still broken (skipped)", fixed, still_broken)
    elif uncached_to_translate and args.apply_cached_only:
        logger.info("Apply-cached-only: %d string(s) have no cached translation, left as-is.",
                    len(uncached_to_translate))

    # Step 5: Emit ONE native `translate <lang> strings:` file. The engine runs
    # every displayed string through translate_string() (runtime old→new lookup)
    # before [var] interpolation, so this translates blog/status/search-history
    # text WITHOUT touching archives, compiling .rpyc, or risking the double-load
    # crash — and it physically cannot break a code comparison (those read the
    # raw value, never the dict). Replaces the old extract→edit→recompile path.
    lang_dir = RenPyParser._lang_dir(args.target_lang)
    pairs: list[tuple[str, str]] = []
    seen_vals: set[str] = set()
    for entry in to_translate:
        original_val = entry["value"]
        if original_val not in translations or original_val in seen_vals:
            continue
        seen_vals.add(original_val)
        pairs.append((original_val, translations[original_val]))

    if args.dry_run:
        for original_val, translated_val in pairs:
            logger.info("[DRY RUN] Would translate: %s ➔ %s", repr(original_val), repr(translated_val))
        success_count = len(pairs)
        logger.info("[DRY RUN] Finished simulation. Total strings to translate: %d", success_count)
    else:
        success_count = _write_inline_strings_file(game_path, lang_dir, pairs)
        finalize_backups(game_path)
        logger.info("Inline Python translation finished. Wrote %d string translations to tl/%s/_interprex_inline.rpy.",
                    success_count, lang_dir)


if __name__ == "__main__":
    main()
