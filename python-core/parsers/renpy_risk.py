"""Ren'Py text-overflow RISK ANALYZER — measure, don't guess.

The width/height fitting heuristics (scheduler.py pixel budget, runtime auto-fit)
are about MAKING text fit. This module answers the prior question the user asked:
*does this specific game even have an overflow risk, and where?* — by reading the
game's own layout declarations instead of guessing.

The ground truth for "can a dialogue overflow" is the box the game gives its text:

  - A FIXED-height textbox (stock `gui.textbox_height = N`, no scroll) clips a long
    translation -> RISK. (Verified real case: Watch the Road, textbox_height=278,
    longest EN say line 327 chars; RU ~1.5x can clip.)
  - An AUTO-growing / scrolled dialogue box never clips -> NO risk. (Verified real
    case: Killer Chat, `calculate_dialogue_height` + `ysize None` + viewports.)

So we scan the scripts (loose AND archived, mirroring renpy.py::_iter_sources),
extract the layout signals, and emit a per-game verdict with a human-readable
reason. This is READ-ONLY: nothing is extracted to disk, nothing is modified.

Verified offline by `check_renpy_risk` (selftest.py) with synthetic fixtures, and
on the two real games above (Killer Chat -> none, Watch the Road -> high).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict


# A line that is a say (dialogue) statement: optional speaker token, then a single
# double-quoted string, nothing trailing. Mirrors the spirit of renpy.py's
# _LINE_RE but intentionally loose — for a STATISTIC, not for hashing.
_SAY_LINE_RE = re.compile(r'^\s*(?:[A-Za-z_]\w*\s+)?"((?:[^"\\]|\\.)*)"\s*$')

# `define gui.textbox_height = 278`  /  `= None`
_GUI_DEF_RE = re.compile(
    r'^\s*(?:define|default)\s+gui\.(?P<name>\w+)\s*=\s*(?P<val>.+?)\s*$'
)

# How long a say string (style tags stripped) has to be before we treat it as an
# overflow candidate against a typical fixed textbox. The two-line stock textbox
# (~278px tall) comfortably holds ~180-220 Latin chars; a RU translation is ~1.5x
# longer, so an EN source past ~150 chars is where RU starts to risk clipping.
_LONG_SAY_CHARS = 150

_TAG_RE = re.compile(r"\{[^}]*\}")


@dataclass
class RiskReport:
    """Per-game overflow risk summary. JSON-serializable via asdict()."""

    # Verdict for the dialogue (say) textbox.
    dialogue_overflow_risk: str = "unknown"  # none | low | high | unknown
    dialogue_reason: str = ""

    # Raw signals (so the UI / caller can show specifics, not just a label).
    has_custom_say_screen: bool = False
    textbox_height: str = ""        # the declared value as text ("278", "None", "")
    textbox_height_fixed: bool = False
    auto_height_dialogue: bool = False   # game computes its own dialogue height
    has_dialogue_scroll: bool = False    # viewport/vpgrid present near dialogue

    say_lines: int = 0
    long_say_lines: int = 0          # say strings over _LONG_SAY_CHARS
    longest_say_chars: int = 0
    longest_say_sample: str = ""

    p_tags: int = 0                  # {p} pause-page tags in say lines
    w_tags: int = 0
    nw_tags: int = 0

    # Choice/menu button signals.
    choice_button_width: str = ""
    choice_button_height: str = ""
    choice_button_fixed_width: bool = False

    files_scanned: int = 0
    sources_from_archive: int = 0


def _iter_rpy_sources(root: str):
    """Yield (file_rel, text) for every .rpy from loose files AND .rpa archives.

    Read-only mirror of renpy.py::_iter_sources (loose wins over archived on a
    path collision). Kept self-contained so the analyzer never triggers the
    parser's decompile/backup machinery — it only ever reads.
    """
    from . import rpa as rpamod

    sources: dict[str, str] = {}

    # 1) Loose .rpy (highest priority).
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip our own output and the engine cache; DO read tl/None (real source).
        dirnames[:] = [d for d in dirnames if d not in ("cache", ".interprex_backups")]
        for name in filenames:
            if not name.endswith(".rpy"):
                continue
            fpath = os.path.join(dirpath, name)
            file_rel = os.path.relpath(fpath, root).replace("\\", "/")
            if file_rel in sources:
                continue
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    sources[file_rel] = f.read()
            except OSError:
                continue

    # 2) Archived .rpy (loose wins on collision).
    archive_count = 0
    game_dir = os.path.join(root, "game")
    for arc in rpamod.iter_rpa_files(game_dir):
        if "__MACOSX" in arc:
            continue
        try:
            inner_files = rpamod.read_rpa(arc)
        except Exception:
            continue
        for rf in inner_files:
            file_rel = "game/" + rf.path
            if file_rel in sources:
                continue
            sources[file_rel] = rf.data
            archive_count += 1

    return sources, archive_count


def analyze(root: str) -> dict:
    """Scan a Ren'Py game folder and return a RiskReport as a plain dict.

    Degrades gracefully: an unreadable game returns an 'unknown' verdict rather
    than raising, so the caller (sidecar endpoint) can always answer."""
    rep = RiskReport()

    try:
        sources, archive_count = _iter_rpy_sources(root)
    except Exception as e:  # never raise to the endpoint
        rep.dialogue_reason = f"could not read scripts: {e}"
        return asdict(rep)

    rep.files_scanned = len(sources)
    rep.sources_from_archive = archive_count

    longest = 0
    longest_sample = ""

    for name, data in sources.items():
        # --- gui.* layout constants ---
        for line in data.splitlines():
            m = _GUI_DEF_RE.match(line)
            if m:
                gname, gval = m.group("name"), m.group("val").strip()
                if gname == "textbox_height":
                    rep.textbox_height = gval
                    # Fixed iff it's a literal number (None / expr => not fixed).
                    rep.textbox_height_fixed = bool(re.match(r"^\d+$", gval))
                elif gname == "choice_button_width":
                    rep.choice_button_width = gval
                    rep.choice_button_fixed_width = bool(re.match(r"^\d+$", gval))
                elif gname == "choice_button_height":
                    rep.choice_button_height = gval

        # --- custom say screen / auto-height / scroll signals ---
        say_m = re.search(r"^\s*screen\s+say\b", data, re.M)
        if say_m:
            rep.has_custom_say_screen = True
            # Scroll detection must be scoped to the SAY SCREEN BODY, not the whole
            # file. A viewport elsewhere in the same file (a chat log, a gallery)
            # says nothing about whether the dialogue line itself can scroll. We
            # slice from the screen header to the next top-level `screen `/`label `
            # (or EOF) and look for viewport/vpgrid only there.
            body_start = say_m.start()
            nxt = re.search(r"^\s*(?:screen|label|init)\b", data[say_m.end():], re.M)
            body_end = say_m.end() + nxt.start() if nxt else len(data)
            say_body = data[body_start:body_end]
            if re.search(r"\bviewport\b|\bvpgrid\b", say_body):
                rep.has_dialogue_scroll = True
        if re.search(r"calculate_dialogue_height|reduce_messages_window_height", data):
            rep.auto_height_dialogue = True

        # --- say-line statistics ---
        for line in data.splitlines():
            m = _SAY_LINE_RE.match(line)
            if not m:
                continue
            s = m.group(1)
            if len(s) < 2:
                continue
            rep.say_lines += 1
            if "{p}" in s or "{p=" in s:
                rep.p_tags += 1
            if "{w}" in s or "{w=" in s:
                rep.w_tags += 1
            if "{nw}" in s:
                rep.nw_tags += 1
            plain = _TAG_RE.sub("", s)
            if len(plain) >= _LONG_SAY_CHARS:
                rep.long_say_lines += 1
            if len(plain) > longest:
                longest = len(plain)
                longest_sample = plain[:120]

    rep.longest_say_chars = longest
    rep.longest_say_sample = longest_sample

    _verdict(rep)
    return asdict(rep)


def _verdict(rep: RiskReport) -> None:
    """Fill dialogue_overflow_risk + dialogue_reason from the collected signals."""
    # No dialogue at all -> nothing to overflow.
    if rep.say_lines == 0:
        rep.dialogue_overflow_risk = "none"
        rep.dialogue_reason = "no say dialogue found"
        return

    # An auto-growing or scrolled dialogue box cannot clip text.
    if rep.auto_height_dialogue or rep.has_dialogue_scroll:
        rep.dialogue_overflow_risk = "none"
        why = "auto-computed dialogue height" if rep.auto_height_dialogue \
            else "scrolling dialogue area"
        rep.dialogue_reason = f"{why}; translations cannot clip"
        return

    # A non-fixed textbox height (None / unset / expression) grows with content.
    if rep.textbox_height and not rep.textbox_height_fixed:
        rep.dialogue_overflow_risk = "none"
        rep.dialogue_reason = f"textbox_height={rep.textbox_height} (not fixed)"
        return

    # Fixed-height stock textbox: risk scales with how many long lines exist.
    if rep.textbox_height_fixed:
        if rep.long_say_lines > 0:
            rep.dialogue_overflow_risk = "high"
            rep.dialogue_reason = (
                f"fixed textbox_height={rep.textbox_height}px and "
                f"{rep.long_say_lines} say line(s) over {_LONG_SAY_CHARS} chars "
                f"(longest {rep.longest_say_chars}); a longer translation can clip"
            )
        else:
            rep.dialogue_overflow_risk = "low"
            rep.dialogue_reason = (
                f"fixed textbox_height={rep.textbox_height}px but no very long "
                f"source lines (longest {rep.longest_say_chars})"
            )
        return

    # textbox_height never declared and no auto/scroll signal: stock default box
    # is fixed-ish, so flag low/high by long-line presence but say we're unsure.
    if rep.long_say_lines > 0:
        rep.dialogue_overflow_risk = "low"
        rep.dialogue_reason = (
            f"no explicit textbox_height; {rep.long_say_lines} long say line(s) "
            f"(longest {rep.longest_say_chars}) — verify in-game"
        )
    else:
        rep.dialogue_overflow_risk = "none"
        rep.dialogue_reason = "no explicit fixed textbox and no long source lines"
