"""Provider contract + shared helpers for translation backends.

A provider takes a batch of source strings and a target language and returns
{id: translation}. Everything provider-agnostic lives here:
  - the prompt that asks for strict JSON keyed by id (so we never guess which
    output line maps to which input),
  - a tolerant parser for the model's reply.

Each concrete provider only implements _complete(): prompt in, raw text out.
Providers are STATELESS — all config (base url, api key, model) arrives per
request from the frontend, so nothing is stored server-side.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("interprex")
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TranslateItem:
    """One string to translate, trimmed to what the model needs."""
    id: str
    text: str
    context: str = ""
    # >0 marks a FIXED-WIDTH UI caption (button/menu choice) whose translation
    # must not exceed this many characters. 0 = ordinary string, no width limit.
    # Surfaced to the model as a first-class field by build_prompt (NOT buried in
    # context, which the system prompt tells the model to treat as ignorable
    # metadata) so the constraint is actually obeyed.
    max_chars: int = 0
    # Optional pixel-width budget (rendered width constraint in pixels).
    # Used by the model as a strict visual guideline (especially for Cyrillic scripts).
    max_pixels: int = 0


@dataclass
class ProviderConfig:
    """Per-request config sent by the frontend. Fields used depend on provider."""
    base_url: str = ""   # local servers (Ollama / LM Studio)
    api_key: str = ""    # cloud (Gemini)
    api_key_2: str = ""  # second api key for Gemini rotation
    model: str = ""
    num_ctx: int = 0     # Ollama: context window to allocate (0 = server default)
    free_only: bool = False


@dataclass
class Usage:
    """EXACT token counts reported by the model for one completion. This is the
    model's own tokenizer counting its own input/output — the ground truth we
    calibrate against, available on every provider (OpenAI `usage`, Gemini
    `usageMetadata`). prompt = input we sent, completion = translation produced."""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class CompletionResult:
    """What a provider's _complete() returns: the raw reply plus exact usage (if
    the server reported it; zeros mean unknown)."""
    text: str
    usage: Usage


@dataclass
class TranslateResult:
    """What translate() returns: id->translation map plus the batch's exact usage
    so the caller can calibrate future batch sizes."""
    translations: dict[str, str]
    usage: Usage


# Core system prompt — sent for EVERY engine. Contains only universal rules:
# JSON contract, casing, gender-neutral wording, escape sequences.
# Engine-specific rules (fixed-width captions, control codes, tone, format
# specifiers) are added per-engine via BaseParser.engine_prompt_addon().
_SYSTEM_CORE = (
    "You are a professional game-localization translator. Translate each source string into {lang}.\n"
    "Input is a JSON object mapping ids to objects with a 'text' field and an optional 'context' field.\n"
    "The 'context' field provides metadata (e.g. speaker name, UI location) to help determine the correct "
    "gender, grammatical forms, and tone. Do NOT translate the context itself.\n"
    "CRITICAL: Strictly match the letter casing and capitalization of the source text (capitalized words, UPPERCASE terms). Never change capitalization.\n"
    "Do not resolve escape characters; if the source contains a literal '\\n', keep exactly '\\n' in the translation "
    "instead of inserting a real newline.\n"
    "UNKNOWN GENDER: when the target language marks gender on past-tense verbs or adjectives (Russian, Spanish, "
    "French, German, Portuguese, \u2026) and the source text does NOT make the subject's gender clear (e.g. a reusable "
    "line like 'HAS BEEN EXECUTED' shown for different characters, or referring to the player), choose a "
    "GENDER-NEUTRAL wording instead of writing clumsy dual forms like '\u043a\u0430\u0437\u043d\u0451\u043d(\u0430)' or 'executed/a'. Rephrase with a "
    "noun or impersonal construction that carries no gender. Example: 'HAS BEEN EXECUTED' -> '\u041a\u0410\u0417\u041d\u042c \u0421\u0412\u0415\u0420\u0428\u0415\u041d\u0410' or "
    "'\u041f\u0420\u0418\u0413\u041e\u0412\u041e\u0420 \u041f\u0420\u0418\u0412\u0415\u0414\u0401\u041d \u0412 \u0418\u0421\u041f\u041e\u041b\u041d\u0415\u041d\u0418\u0415' (never '\u041a\u0410\u0417\u041d\u0401\u041d(\u0410)'). Only use a specific gendered form when the context "
    "field or the text itself makes the gender certain.\n"
    "Return ONLY a JSON object containing an 'items' array, where each item has 'id' (from the input) "
    "and 'translated' (the translated string). Example:\n"
    "{{\n"
    "  \"items\": [\n"
    "    {{ \"id\": \"a1b2\", \"translated\": \"translation\" }}\n"
    "  ]\n"
    "}}\n"
    "Do NOT wrap the JSON in markdown code blocks (e.g., ```json) and do not write any explanations."
)

# Backward-compat alias — selftest.py imports SYSTEM_INSTRUCTION by name and
# checks that "GENDER-NEUTRAL" is present in it. That rule lives in _SYSTEM_CORE.
# fixed_width / max_chars rules were moved to renpy.py engine_prompt_addon().
SYSTEM_INSTRUCTION = _SYSTEM_CORE


def build_prompt(items: list[TranslateItem], lang: str,
                 glossary: dict[str, str], engine: str = "") -> str:
    """One prompt for a whole batch. Keyed by id both ways so the mapping back
    is unambiguous regardless of order or reworded output.

    `engine` — when non-empty, the matching parser's engine_prompt_addon() is
    appended after the core instructions. Unknown or empty engine degrades
    gracefully to the core-only prompt (no crash)."""
    lines = [_SYSTEM_CORE.format(lang=lang), ""]

    # Engine-specific instructions: load the parser's addon and insert it after
    # the core. The import is local to avoid a circular dependency at module load
    # (providers imports nothing from parsers at the top level).
    if engine:
        try:
            from parsers import get_parser
            addon = get_parser(engine).engine_prompt_addon()
            if addon:
                lines.append(addon)
                lines.append("")
        except Exception:
            pass  # unknown engine or import error — degrade to core-only

    if glossary:
        lines.append("Glossary (use these translations consistently):")
        for src, dst in glossary.items():
            lines.append(f"  {src} => {dst}")
        lines.append("")
    lines.append("Translate these strings. Reply with JSON in the format { \"items\": [ { \"id\": \"id\", \"translated\": \"translation\" } ] }:")
    lines.append("Any item flagged as fixed width MUST fit within its limits — rephrase shorter if needed, never exceed them.")
    payload = {}
    for it in items:
        val = {"text": it.text}
        if it.context:
            val["context"] = it.context
        # Width constraint as a first-class sibling of text, not inside context.
        if it.max_chars:
            val["max_chars"] = it.max_chars
            val["fixed_width"] = True
        if it.max_pixels:
            val["max_pixels"] = it.max_pixels
            val["fixed_width"] = True
        payload[it.id] = val
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def parse_reply(raw: str, ids: list[str]) -> dict[str, str]:
    """Pull {id: translation} out of a model reply that may be wrapped in prose
    or ```json fences. Only keys we asked for are kept.

    Empty or whitespace-only translations are silently dropped: an LLM that
    returned "" for a key either skipped it or hallucinated a blank. Keeping ""
    would overwrite the source text with nothing — it is safer to leave the
    string untranslated and let the retry / sweep take another pass at it.
    """
    text = raw.strip()
    # strip code fences if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    
    # Strip non-JSON prefix/suffix
    start_bracket = text.find("[")
    start_brace = text.find("{")
    
    # Determine the starting position of JSON
    if start_bracket != -1 and (start_brace == -1 or start_bracket < start_brace):
        start = start_bracket
        end = text.rfind("]")
    else:
        start = start_brace
        end = text.rfind("}")
        
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
        
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("JSON decode failed for model reply. Raw text:\n%s", raw)
        return {}
        
    wanted = set(ids)
    out: dict[str, str] = {}
    
    # Format A: List of items (e.g. [{"id": "...", "translated": "..."}] or {"items": [{"id": ..., "translated": ...}]})
    items_list = None
    if isinstance(data, list):
        items_list = data
    elif isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        items_list = data["items"]
        
    if items_list is not None:
        for item in items_list:
            if isinstance(item, dict) and "id" in item and "translated" in item:
                k = str(item["id"]).strip().lower()
                v = item["translated"]
                if k in wanted and str(v).strip():
                    out[k] = str(v)
        if not out:
            logger.warning("No matching translation IDs found in model list response. Raw text:\n%s", raw)
        return out
        
    # Format B: Flat dictionary (e.g. {"id1": "translation1"}) or dict with objects {"id1": {"text": "translation1"}}
    if isinstance(data, dict):
        for k, v in data.items():
            k_lower = str(k).strip().lower()
            if k_lower in wanted:
                if isinstance(v, dict):
                    v = v.get("translated", v.get("text", ""))
                translation = str(v)
                if translation.strip():
                    out[k_lower] = translation
        if not out:
            logger.warning("No matching translation IDs found in model dict response. Raw text:\n%s", raw)
        return out


# --- token budgeting --------------------------------------------------------
#
# Local models on small VRAM (e.g. 8 GB) run with a tight context window — 2k or
# 4k tokens. A batch that overflows it is SILENTLY TRUNCATED by llama.cpp/Ollama:
# the model never sees the tail strings and returns junk or gaps, with no error.
# So we must pack each prompt to fit the window instead of using a fixed count.
#
# We have no tokenizer here for the cheap path, so estimate. ~4 chars/token is
# the usual English rule; non-Latin scripts run denser, so we use a conservative
# 3 chars/token to overshoot rather than overflow.
_CHARS_PER_TOKEN = 3
_PROMPT_OVERHEAD_TOKENS = 220  # system instruction + JSON scaffolding

# How many output tokens a translation costs RELATIVE to its input. The
# translation doesn't exist yet at packing time, so this can never be measured —
# only reserved for. Dense target scripts (Cyrillic, CJK) tokenize to MORE
# tokens than the same English input, so they need a bigger reserve or the
# OUTPUT gets truncated mid-translation. Keyed by target language; default 1.0.
_OUTPUT_RATIO = {
    "russian": 1.5,
    "ukrainian": 1.5,
    "japanese": 1.6,
    "chinese (simplified)": 1.6,
    "chinese (traditional)": 1.6,
    "korean": 1.6,
}
_DEFAULT_OUTPUT_RATIO = 1.2  # most languages run a bit longer than English


def output_ratio(target_lang: str) -> float:
    return _OUTPUT_RATIO.get(target_lang.strip().lower(), _DEFAULT_OUTPUT_RATIO)


def est_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN + 1)


def input_budget_for(max_context_tokens: int, glossary: dict[str, str],
                     target_lang: str) -> int:
    """Tokens available for SOURCE strings in one prompt, after reserving prompt
    scaffolding, the glossary, and an output share sized to the target language.

      window = overhead + glossary + input + input*ratio
      => input = (window - overhead - glossary) / (1 + ratio)
    """
    glossary_tokens = sum(est_tokens(f"{k} => {v}") for k, v in glossary.items())
    fixed = _PROMPT_OVERHEAD_TOKENS + glossary_tokens
    ratio = output_ratio(target_lang)
    return max(1, int((max_context_tokens - fixed) / (1 + ratio)))



# --- adaptive calibration ---------------------------------------------------
#
# Instead of mapping model names to HF tokenizers (a lookup table you'd have to
# maintain forever, that breaks on every new/renamed model), we LEARN each
# model's real tokenization from the exact `usage` it reports after each batch:
#
#   chars_per_token  = chars we sent / prompt_tokens it counted
#   output_ratio     = completion_tokens / prompt_tokens it produced
#
# The first batch uses conservative language defaults; from the second on, the
# numbers are this exact model's own, for any provider. Zero maintenance.

class Calibrator:
    """Learns chars/token and output ratio for the model in use from observed
    usage. Seed with language defaults; refine after every batch."""

    def __init__(self, target_lang: str):
        self.chars_per_token: float = float(_CHARS_PER_TOKEN)
        self.out_ratio: float = output_ratio(target_lang)
        self._samples = 0

    def est_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token) + 1)

    def input_budget(self, window: int, glossary: dict[str, str]) -> int:
        glossary_tokens = sum(self.est_tokens(f"{k} => {v}")
                              for k, v in glossary.items())
        fixed = _PROMPT_OVERHEAD_TOKENS + glossary_tokens
        # Reserve output by the learned ratio, with a safety margin so a noisy
        # estimate errs toward a smaller (safe) batch rather than overflow.
        return max(1, int((window - fixed) / (1 + self.out_ratio) * 0.92))

    def observe(self, prompt_chars: int, usage: "Usage") -> None:
        """Fold one batch's exact usage into the running estimate. Ignores empty
        usage (provider didn't report it -> we keep the heuristic)."""
        if usage.prompt_tokens <= 0:
            return
        cpt = prompt_chars / usage.prompt_tokens
        if usage.completion_tokens > 0:
            ratio = usage.completion_tokens / usage.prompt_tokens
        else:
            ratio = self.out_ratio
        # Exponential moving average so one odd batch can't whipsaw the size.
        if self._samples == 0:
            self.chars_per_token = cpt
            self.out_ratio = ratio
        else:
            a = 0.4
            self.chars_per_token = (1 - a) * self.chars_per_token + a * cpt
            self.out_ratio = (1 - a) * self.out_ratio + a * ratio
        self._samples += 1

    def next_batch(self, items: list["TranslateItem"], window: int,
                   glossary: dict[str, str], start: int, max_batch_size: int = 30) -> int:
        """Index one past the last item that fits a batch starting at `start`,
        using the current calibration. Always advances by at least one."""
        budget = self.input_budget(window, glossary)
        i, used = start, 0
        while i < len(items):
            if i - start >= max_batch_size:
                break
            it = items[i]
            cost = self.est_tokens(it.text) + self.est_tokens(it.context) + 12
            if i > start and used + cost > budget:
                break
            used += cost
            i += 1
        return max(i, start + 1)


class BaseProvider(ABC):
    """name is the stable provider id used by the frontend dropdown."""
    name: str = ""

    @abstractmethod
    def _complete(self, prompt: str, cfg: ProviderConfig) -> CompletionResult:
        """Send one prompt; return its raw reply plus exact token usage (zeros if
        the server didn't report any)."""
        raise NotImplementedError

    def count_tokens(self, text: str, cfg: ProviderConfig) -> int | None:
        """Exact token count for `text` using THIS model's tokenizer, or None if
        the provider can't do it cheaply. None => caller falls back to the
        char-based estimate. Must never raise: a counting failure should degrade
        to the estimate, not break translation."""
        return None

    def list_models(self, cfg: ProviderConfig) -> list[str]:
        """Model ids this backend can serve, for the UI dropdown. Empty list if
        the backend can't be queried (server down, no key, no such route) — the
        UI then falls back to a free-text field. Must never raise."""
        return []

    def active_model(self, cfg: ProviderConfig) -> str:
        """The model the backend would use right now if asked — i.e. the one
        loaded in VRAM for a local server. "" when there's no notion of an active
        model (cloud) or it can't be determined. The UI preselects this so a
        local user usually doesn't have to pick. Must never raise."""
        return ""

    def complete_prompt(self, prompt: str, items: list[TranslateItem],
                        cfg: ProviderConfig) -> TranslateResult:
        """Send a pre-built prompt string and parse the reply. Used by the
        scheduler (Variant A): the scheduler builds the prompt with the engine
        addon, then hands us only the raw string. The provider stays stateless
        and engine-agnostic."""
        res = self._complete(prompt, cfg)
        translations = parse_reply(res.text, [it.id for it in items])
        return TranslateResult(translations, res.usage)

    def translate(self, items: list[TranslateItem], lang: str,
                  glossary: dict[str, str],
                  cfg: ProviderConfig, engine: str = "") -> TranslateResult:
        """Convenience wrapper that builds the prompt internally. Kept for
        callers outside the scheduler (e.g. renpy_python_translator). Pass
        engine to get engine-specific prompt instructions."""
        if not items:
            return TranslateResult({}, Usage())
        prompt = build_prompt(items, lang, glossary, engine)
        return self.complete_prompt(prompt, items, cfg)
