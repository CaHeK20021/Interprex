"""Ren'Py (.rpy) parser — writes translations to the engine's NATIVE `tl/` format.

Ren'Py games keep their scripts as plain-text `.rpy` files under `game/`. The
player-facing text we want is:

  - say statements  ->  e "Hello."   /   "Narration."   /   e happy "Hi."
  - menu choices    ->  "Yes please":
  - screen widgets  ->  textbutton "Save"  /  text "Heading"  /  label "Title"
  - translatable fn ->  _("Back")  (UI strings wrapped in the translation marker)
  - character names ->  define e = Character("Eileen")

WRITE-BACK STRATEGY — this is the important design choice. We do NOT rewrite the
original `.rpy`. Instead we emit Ren'Py's OWN translation format into
`game/tl/<lang>/`, exactly as the engine's `translate` command does. Benefits:
the original survives game patches, the player can switch language, and a bad
escape can never corrupt the source. The engine loads these `tl/` files
automatically when `config.language` is set.

TWO BLOCK TYPES (mirrors renpy/translation/generation.py):
  - say lines       -> `translate <lang> <identifier>:` blocks. The identifier is
                       `md5(get_code()+"\\r\\n")[:8]`, prefixed by the label — it
                       MUST match the engine's byte-for-byte or the translation
                       won't bind. Verified 56242/56242 against an engine-generated
                       oracle. See CLAUDE.md "Ren'Py native tl/ format".
  - everything else -> one `translate <lang> strings:` block per file, as
                       `old "..."` / `new "..."` pairs (menu choices, `_()` calls,
                       screen text, AND character display names — the engine runs
                       names through translate_string at display time, so old/new
                       covers them too; character.py::DynamicCharacter.__str__ ->
                       substitutions.substitute(translate=True)).

TWO IDENTITIES, do not conflate:
  - OUR id = make_id(engine, file, path, original), FNV-1a, parity with
    src/lib/types.ts. Used for translation memory + matching LLM results.
    `translations` passed to inject() is keyed by THIS id.
  - ENGINE identifier = the md5 thing above. Computed ONLY when writing tl/ say
    blocks. Never stored on a TranslationString.

extract() and inject() walk the file through the SAME `_scan()` generator, so the
record they compute for a given string is identical by construction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil

from .base import BaseParser, TranslationString

logger = logging.getLogger(__name__)


# A say line: optional speaker + sprite-attribute words, then a "double-quoted"
# string, then whatever trailing modifiers (`with vpunch`, a comment, a menu's
# `:`). Single-quoted strings are intentionally NOT matched: apostrophes in code
# and contractions make them ambiguous, and dialogue is double-quoted by
# convention. `text` captures the raw inner content (escapes preserved) so the
# round-trip is lossless.
#
# IMPORTANT: the prefix group uses a simple CHARACTER CLASS `[\w\s@.]*` — O(N),
# zero backtracking. Dynamic @[...] attribute expressions (e.g. `r @[r.username]`)
# are pre-normalised by `_normalise_line` below before this regex is applied, so
# the character class never needs to match `[` or `]` directly.
_LINE_RE = re.compile(
    r'^(?P<indent>\s*)'
    r'(?P<prefix>[\w\s@.]*)'
    r'\s*"(?P<text>(?:[^"\\]|\\.)*)"'
    r'(?P<suffix>.*)$'
)
# NOTE the trailing `\s*`: the speaker (or last attribute) may abut the quote
# with NO space — `Koji"Hi."` is valid Ren'Py and the engine lexer accepts it,
# so we must too or its say-block hash silently diverges.

# Matches @[...] dynamic attribute expressions in say-line prefixes.
# Examples: `r @[r.username]`, `e @[mood]`, `c @[c.state] @[c.sub]`.
# The bracket content can be any expression except a nested `]`.
_AT_BRACKET_RE = re.compile(r'@\[[^\]]*\]')


def _normalise_line(line: str) -> str:
    """Replace @[...] dynamic attribute tokens in a say-line prefix with `@_`
    so that the fast (no-backtracking) _LINE_RE character-class can match them.
    Only substitutes tokens that appear BEFORE the first unescaped double-quote,
    so strings inside dialogue are never touched."""
    # Find the position of the first unescaped `"` — prefix ends there.
    i = 0
    while i < len(line):
        c = line[i]
        if c == '\\':
            i += 2
            continue
        if c == '"':
            break
        i += 1
    if i == 0:
        return line  # starts with a quote, nothing to normalise
    prefix_part = line[:i]
    rest = line[i:]
    return _AT_BRACKET_RE.sub('@_', prefix_part) + rest

# Say ARGUMENTS: a say line may carry a parenthesised argument list right after
# the string — `m "hi"(channel=m.dm)` / `r "x" (reacts=[ChatReact("?",a,2)])`.
# The engine folds `arguments.get_code()` into the say identifier (ast.Say.
# get_code → ArgumentInfo.get_code) and re-emits each argument's expression
# VERBATIM from source (verified against the engine oracle on Killer Chat: inner
# comma/bracket spacing is preserved byte-for-byte). So we reuse the raw `(...)`
# verbatim — no expression parsing. We detect it structurally rather than by a
# fixed-depth regex (args nest arbitrarily, e.g. `(reacts=([ChatReact(...)]))`):
# a say arg list is a suffix that, stripped, is wholly wrapped in `(...)`.

def _extract_say_args(suffix: str) -> str | None:
    """Return the verbatim `(...)` say-argument string if `suffix` is exactly a
    parenthesised arg list (the line ends after it), else None. Balanced-paren
    check so a `(...)` that closes early — `(x) with y` — is NOT mistaken for it."""
    s = suffix.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return None
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return None  # outer paren closed before end -> trailing code
    return s if depth == 0 else None

# A menu choice is a bare quoted string whose only trailing content is an
# optional `if <condition>` guard and the block-opening colon.
_MENU_SUFFIX_RE = re.compile(r'\s*(?:if\s.+?)?:\s*$')


def _is_menu_choice_with_args(suffix: str) -> bool:
    """True if `suffix` is a parenthesised argument list followed by a menu-choice
    tail (optional `if` guard + the block-opening `:`), e.g.
    `(reacts=[ChatReact("😆",m,1.0)]):`. These are chat-style menu choices that
    carry per-choice arguments — without this they slip past `_MENU_SUFFIX_RE`
    (which sees the leading `(`) and `_extract_say_args` (which needs the line to
    end at `)`, not `:`), then get dropped by the `'"' in suffix` skip because the
    inner emoji arg contains a quote. Same structural balanced-paren walk as
    `_extract_say_args` (args nest arbitrarily) — not a regex."""
    s = suffix.strip()
    if not s.startswith("("):
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # Outer arg list closed; the remainder must be a menu-choice tail.
                return bool(_MENU_SUFFIX_RE.match(s[i + 1:]))
    return False

# Translatable PREFIX statements that the engine groups with the FOLLOWING say
# into one translate block (so the block's md5 covers them too — see
# `_block_digest`). `Restructurer.callback` appends any node with
# `.translatable == True` to the current group WITHOUT closing it; the next Say
# closes the group and `create_translate` hashes every node's get_code. In
# practice (verified empirically on Killer Chat via _blockscan) the only such
# prefixes are `voice "<file>"` and `nvl clear` — both registered
# `translatable=True` in renpy/common (00voice.rpy / 00nvl_mode.rpy). Their
# get_code is `UserStatement.get_code()` == the raw LOGICAL line (indent +
# trailing `# comment` stripped, inner spacing kept), so we reproduce it with
# `_logical_code`.
_VOICE_RE = re.compile(r'^\s*voice\s+"(?:[^"\\]|\\.)*"')
_NVL_CLEAR_RE = re.compile(r'^\s*nvl\s+clear\s*(?:#.*)?$')
_GENERIC_LABELS = {"start", "end", "init", "main_menu", "navigation", "setup", "options", "splashscreen", "after_load"}

# Detect a file that is an EXISTING translation (a shipped `tl/<lang>/` file the
# game's own developer generated), vs real source. Both can live under `tl/` —
# notably games that keep code under `tl/None/` (Ren'Py's "no language" tree) —
# so a path-based "skip tl/" rule is wrong: it would drop real dialogue. We
# classify by CONTENT: a translation file is full of `translate <lang> …` blocks
# and carries NO top-level source statements (label/define/screen/…). Skipping
# these avoids re-translating another language's text into ours (e.g. Watch the
# Road ships a complete `tl/chinese/`, ~3k strings we must NOT ingest as source).
_TRANSLATE_BLOCK_RE = re.compile(r'^[ \t]*translate[ \t]+\w', re.M)
_TOPLEVEL_SOURCE_RE = re.compile(
    r'^(?:label|define|default|image|screen|init|transform|style|python)\b', re.M)


def _is_existing_translation_file(text: str) -> bool:
    """True if `text` is a shipped tl/ translation file (translate blocks, no
    top-level source), which extract/inject must skip. False for real source —
    including code that merely lives under `tl/None/`."""
    if not _TRANSLATE_BLOCK_RE.search(text):
        return False
    return not _TOPLEVEL_SOURCE_RE.search(text)

_LABEL_RE = re.compile(r'^\s*label\s+(?P<name>\.?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)')
_RESET_FLOW_RE = re.compile(r'^\s*(?:jump|call|return|scene)\b')

# A `translate <lang> …:` block opener — the start of an EXISTING translation
# (a say block `translate ru id:`, a `translate ru strings:`, or a
# `translate ru python:`). Its body is already-translated text we must NOT
# ingest as source. Files can be all-translation (caught earlier by
# `_is_existing_translation_file`) OR mixed (real code + inline translate blocks
# in one file, e.g. a dev keeping script + its translation together) — the mixed
# case is only catchable mid-scan, by skipping the block's indented body.
_TRANSLATE_OPEN_RE = re.compile(r'^(?P<indent>\s*)translate\s+\w+\b.*:\s*$')

# `menu:` opener (optionally `menu name:`). Marks the start of a choice block; the
# first say line inside it is the menu caption (engine emits it `nointeract`).
_MENU_RE = re.compile(r'^(?P<indent>\s*)menu\b.*:\s*$')

# `menu NAME:` / `menu NAME(args):` — a NAMED menu. The engine compiles this to a
# real `Label NAME` (parser.py::menu_statement → `ast.Label(loc, label, ...)`),
# so every say inside such a menu takes NAME as its translate-id label prefix,
# NOT the enclosing `label`. We must mirror that (see set_label in extract()).
# Anonymous `menu:` has no name and leaves the label state untouched.
_MENU_NAME_RE = re.compile(
    r'^(?P<indent>\s*)menu\s+(?P<name>\.?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)'
    r'\s*(?:\(.*\))?\s*:\s*$')

# Character display-name definition:
#   define e = Character("Eileen", ...)
#   default mc = ChatCharacter(name="Maya", status_text="...", ...)
# Matches both positional and keyword `name=` forms.  Captures the variable
# name for the speaker lookup table.
_CHAR_DEF_RE = re.compile(
    r'^\s*(?:define|default)\s+(?P<var>\w+)\s*=\s*\w*Character\w*\s*\('
)

# Translatable keyword arguments inside Character() calls:
#   status_text="...", profile="...", dominant_role="..."
# Extracted from the FULL logical line (multi-line Character() calls are joined
# by iter_logical_lines).  These are player-visible strings that the game
# displays in chat profiles and status bars.
_CHAR_KWARG_RE = re.compile(
    r'(?P<key>status_text|profile|dominant_role)\s*=\s*"(?P<val>(?:[^"\\]|\\.)*)"'
)

# Screen declaration:  screen foo():  /  screen foo(x, y):
# An optional `init [priority]` prefix is allowed because the decompiler (unrpyc)
# emits `init -501 screen main_menu():` for any screen with a non-default init
# priority.  Without this, such screens are never recognised as screen blocks and
# their bare-string `textbutton "..."` widgets go un-extracted (real bug: the
# main_menu "start"/"load"/"prefs"/"help" buttons in OnlineObsessionDemo).
_SCREEN_RE = re.compile(
    r'^(?P<indent>\s*)(?:init(?:\s+[+-]?\d+)?\s+)?screen\s+(?P<name>\w+)'
)

# Player-visible text widgets inside a screen block.
# Captures the first double-quoted string on lines starting with:
#   textbutton "Label"   - clickable button
#   text "..."           - static display text
#   label "Heading"      - section-heading widget (NOT the narrative 'label')
#   tooltip "..."        - hover tooltip (property of a widget, also player-visible)
_SCREEN_WIDGET_RE = re.compile(
    r'^\s*(?P<kind>textbutton|text|label|tooltip)\s+"(?P<text>(?:[^"\\]|\\.)*)"'
)

# textbutton/text/label with a VARIABLE (not a string literal) — e.g.
# `textbutton page:` inside a screen for-loop.  Resolved from for-loop
# bindings tracked in `screen_for_vars`.
_SCREEN_WIDGET_VAR_RE = re.compile(
    r'^\s*(?P<kind>textbutton|text|label)\s+(?P<var>[A-Za-z_]\w*)\s*[:\(]'
)

# `for var, width in [("STR", num), ...]:` collapsed into one logical line
# by iter_logical_lines (tracks bracket depth).  Extracts the first loop
# variable and all string literals in the tuple list.
_SCREEN_FOR_RE = re.compile(
    r'^\s*for\s+(?P<var>[A-Za-z_]\w*)\s*,?\s*\w*\s+in\s+\[(?P<body>.+)\]\s*:',
    re.DOTALL,
)

# Translatable-function call:  _("Back")  — Ren'Py's gettext-style marker. Used in
# screens.rpy menu buttons (Back/Save/History/...). We catch every occurrence on a
# line; positions let us assign a stable per-line index for the id.
_USCORE_RE = re.compile(r'_\(\s*"(?P<text>(?:[^"\\]|\\.)*)"\s*\)')
_USCORE_F_RE = re.compile(r'_\(\s*f(?P<quote>["\'])(?P<text>.*?)(?P=quote)\s*\)', re.IGNORECASE)

# `show text "..."` statement — a quick on-screen Text displayable (intro cards,
# chapter titles). Ren'Py runs the literal through translate_string() at render
# time (substitute=True by default), so it's translatable exactly like _() /
# screen text: emit it as a `string` (old/new) entry. Optional trailing modifiers
# (`with dissolve`, `as tag`, `at transform`, `behind ...`) are code and live in
# the suffix — we only take the quoted text. A bare `text "..."` INSIDE a screen
# is a different statement, already handled by the screen-widget branch; this
# matches only the top-level `show text` form.
_SHOW_TEXT_RE = re.compile(
    r'^(?P<indent>\s*)show\s+text\s+"(?P<text>(?:[^"\\]|\\.)*)"'
)


# If the first prefix word is one of these, the line is a statement, not a say
# with that word as the speaker. (`return "x"` must not become a "return"
# character speaking "x".) Assignments and `$`/`(` lines are already excluded by
# the prefix shape; this covers the keyword statements that look say-like.
# text/textbutton added so they are never mistaken for character names outside
# screen blocks (they are handled by _SCREEN_WIDGET_RE inside screens).
_KEYWORDS = frozenset({
    "return", "jump", "call", "scene", "show", "hide", "play", "stop",
    "queue", "pause", "with", "if", "elif", "else", "while", "for", "pass",
    "define", "default", "image", "transform", "label", "menu", "python",
    "init", "screen", "style", "translate", "window", "voice", "from",
    "import", "nvl", "del", "raise", "assert", "global",
    "text", "textbutton",
    # Style and layout keywords to prevent extracting style properties as dialogue
    "background", "font", "color", "style_prefix", "hover", "idle", "selected",
    "insensitive", "active", "selected_hover", "selected_idle", "selected_insensitive",
    "outlines", "thumb", "scrollbar", "borders", "tile", "focus", "margin", "padding",
    "align", "anchor", "pos", "xpos", "ypos", "xanchor", "yanchor", "xalign", "yalign",
    "spacing", "properties", "size", "xsize", "ysize", "minimum", "maximum", "xminimum",
    "yminimum", "xmaximum", "ymaximum", "area", "alt", "key", "action", "clicked"
})

_CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')
# Matches any quoted font file path: "fonts/DejaVuSans.ttf", "custom.otf", etc.
# .ttf / .ttc / .otf are font-only extensions — safe to replace wholesale.
_FONT_REF_RE = re.compile(r'"[^"]+\.(?:ttf|ttc|otf)"', re.IGNORECASE)

_ASSETS_FONTS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
)
# NOTE: when bundled with PyInstaller, __file__ resolves to the temp extraction
# directory (sys._MEIPASS) so this path remains correct — no special casing
# needed as long as "python-core/assets/fonts" is listed as a data bundle in
# the .spec file (add_data entries). Verify this if fonts stop copying in a
# packaged build.

# Порядок важен: более специфичные скрипты — выше.
# Первый матч определяет шрифт для всего перевода.
_SCRIPT_DETECTORS = [
    # CJK: Chinese / Japanese / Korean
    (re.compile(r'[一-鿿぀-ゟ゠-ヿ가-힯]'),
     "NotoSansCJK-Regular.ttc"),
    # Arabic / Persian / Urdu
    (re.compile(r'[؀-ۿ]'),
     "NotoSansArabic-Regular.ttf"),
    # Hebrew
    (re.compile(r'[֐-׿]'),
     "NotoSansHebrew-Regular.ttf"),
    # Thai
    (re.compile(r'[฀-๿]'),
     "NotoSansThai-Regular.ttf"),
    # Devanagari: Hindi / Marathi / Sanskrit
    (re.compile(r'[ऀ-ॿ]'),
     "NotoSansDevanagari-Regular.ttf"),
    # Cyrillic: Russian / Ukrainian / Bulgarian / Serbian etc.
    (re.compile(r'[Ѐ-ӿ]'),
     "NotoSans-Regular.ttf"),
]

# Pixel-font variant of the detectors above. Used when the user chooses the
# "pixel" font style so a translated string lands in a bitmap font that matches a
# pixel-art game's UI instead of the smooth Noto. Two bundled pixel fonts cover
# the scripts we actually have one for:
#   - PixelOperator (CC0): Latin ONLY (incl. accented é/ü/ñ/ç…). It ships NO
#     Cyrillic — every PixelOperator variant has 0 Cyrillic glyphs, so Russian on
#     it renders as empty boxes (tofu). Cyrillic therefore uses Zpix instead.
#   - Zpix / 最像素 (OFL): Chinese (simpl+trad) + Japanese (kana+kanji) AND a full
#     proportional Cyrillic block (verified: И=0.75em, и=0.67em — prose, not
#     full-width boxes). It has NO Hangul, so Korean MUST stay on the smooth Noto
#     CJK — hence the Hangul detector is listed BEFORE the Han/Kana one.
# Scripts with no quality pixel font (Arabic/Hebrew/Thai/Devanagari) fall back to
# their smooth Noto. Coverage verified by hand against the actual cmaps.
_PIXEL_SCRIPT_DETECTORS = [
    # Hangul FIRST: Zpix lacks it, so Korean keeps the smooth Noto CJK.
    (re.compile(r'[가-힯]'),
     "NotoSansCJK-Regular.ttc"),
    # Chinese + Japanese (Han + Kana, NO Hangul) -> pixel Zpix.
    (re.compile(r'[一-鿿぀-ゟ゠-ヿ]'),
     "Zpix.ttf"),
    # Arabic / Persian / Urdu — no pixel font, smooth Noto.
    (re.compile(r'[؀-ۿ]'),
     "NotoSansArabic-Regular.ttf"),
    # Hebrew — no pixel font, smooth Noto.
    (re.compile(r'[֐-׿]'),
     "NotoSansHebrew-Regular.ttf"),
    # Thai — no pixel font, smooth Noto.
    (re.compile(r'[฀-๿]'),
     "NotoSansThai-Regular.ttf"),
    # Devanagari — no pixel font, smooth Noto.
    (re.compile(r'[ऀ-ॿ]'),
     "NotoSansDevanagari-Regular.ttf"),
    # Cyrillic -> pixel Zpix (PixelOperator has no Cyrillic glyphs).
    (re.compile(r'[Ѐ-ӿ]'),
     "Zpix.ttf"),
]

# Target-language code -> Ren'Py tl/ directory name. The engine names its
# translation dirs by full language word, not ISO code. Unknown codes pass
# through lowercased (a creator's custom language name still works).
_RENPY_LANGS = {
    "ru": "russian", "russian": "russian",
    "uk": "ukrainian", "ukrainian": "ukrainian",
    "en": "english", "english": "english",
    "es": "spanish", "spanish": "spanish",
    "fr": "french", "french": "french",
    "de": "german", "german": "german",
    "it": "italian", "italian": "italian",
    "pt": "portuguese", "portuguese": "portuguese",
    "pl": "polish", "polish": "polish",
    "tr": "turkish", "turkish": "turkish",
    "ja": "japanese", "japanese": "japanese",
    "zh": "chinese", "chinese": "chinese",
    "ko": "korean", "korean": "korean",
}

# Month names per target language, keyed by the normalized tl/ dir name
# (_lang_dir output). Used to localize dates the game builds via
# datetime.strftime("%B …") / "%b …": on Windows `locale.setlocale(LC_TIME,
# "<lang>.UTF-8")` is UNRELIABLE — strftime returns bytes in the OS codepage
# that Python then mis-decodes, rendering "Декабрь" as cp1251 mojibake (real
# Killer Chat bug). So instead of touching locale at all we substitute the
# English month name (strftime's stable C-locale output) for the target one
# inside our translate_string wrapper. Index 0 = January. Full names first,
# abbreviated (matching %b) second.
_MONTHS_FULL = {
    "russian": ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
                "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"],
    "ukrainian": ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
                  "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"],
    "spanish": ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
                "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"],
    "german": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
               "August", "September", "Oktober", "November", "Dezember"],
    "french": ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet",
               "Août", "Septembre", "Octobre", "Novembre", "Décembre"],
    "italian": ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"],
    "portuguese": ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                   "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"],
    "polish": ["Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
               "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień"],
    "turkish": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
                "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"],
    "japanese": ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月",
                 "9月", "10月", "11月", "12月"],
    "chinese": ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月",
                "九月", "十月", "十一月", "十二月"],
    "korean": ["1월", "2월", "3월", "4월", "5월", "6월", "7월", "8월",
               "9월", "10월", "11월", "12월"],
}
# Abbreviated month names (matching strftime "%b"). Where a script has no common
# 3-letter abbreviation we reuse the full name (still correct, just not shortened).
_MONTHS_ABBR = {
    "russian": ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен",
                "окт", "ноя", "дек"],
    "ukrainian": ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер",
                  "жов", "лис", "гру"],
    "spanish": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep",
                "oct", "nov", "dic"],
    "german": ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep",
               "Okt", "Nov", "Dez"],
    "french": ["janv", "févr", "mars", "avr", "mai", "juin", "juil", "août",
               "sept", "oct", "nov", "déc"],
    "italian": ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set",
                "ott", "nov", "dic"],
    "portuguese": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set",
                   "out", "nov", "dez"],
    "polish": ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz",
               "paź", "lis", "gru"],
    "turkish": ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl",
                "Eki", "Kas", "Ara"],
}
# English source names strftime emits in the C locale (our stable lookup keys).
_MONTHS_EN_FULL = ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"]
_MONTHS_EN_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                   "Oct", "Nov", "Dec"]


# Ren'Py inline text markup ({i}, {/color}, {w=0.5}, {size=+4}…) and runtime
# interpolation ([player_name], [[, …]). Stripped before the "is this prose?"
# test so closing tags like {/color} and escapes like \n don't masquerade as
# file paths — that over-broad check used to silently drop ~950 real lines on
# Takei's Journey (every line with a colour tag or a newline).
_RENPY_TAG_RE = re.compile(r'\{[^}]*\}')
_RENPY_SUB_RE = re.compile(r'\[[^\]]*\]')
_HEX_COLOR_RE = re.compile(r'#[0-9a-fA-F]{3,8}\Z')
_ASSET_EXT = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    ".ttf", ".otf", ".ttc",
    ".ogg", ".mp3", ".wav", ".opus", ".flac",
    ".webm", ".avi", ".mkv", ".mov",
    ".rpy", ".rpyc",
)

# Технические/общие лейблы Ren'Py — не несут художественного контекста,
# не передаём их в поле context при переводе (экономим токены).
_GENERIC_LABELS: frozenset[str] = frozenset({
    "start", "end", "init", "main_menu", "navigation", "setup", "options",
    "after_load", "quit", "before_main_menu", "after_game_menu", "game_menu",
    "splashscreen", "pause", "config", "preferences", "save", "load",
    "confirm", "help", "about", "history", "skip", "notify", "replay",
})


def _is_generic_label(label: str) -> bool:
    """True if the label carries no artistic context worth passing to the LLM."""
    return (
        not label
        or label.lower() in _GENERIC_LABELS
        or len(label) < 3
        or label.startswith("_")
    )


def is_technical_string(s: str) -> bool:
    """True if the string is an asset path, style config, or code constant rather
    than player-facing text. Conservative on the path checks: Ren'Py dialogue is
    riddled with `/` (closing tags `{/i}`) and `\\` (the `\\n` escape), so a bare
    "contains a slash" test is wrong — it nukes real lines. We strip markup first,
    then require some actual letters in what remains."""
    cleaned = s.strip()
    if not cleaned:
        return True
    # Asset reference by known extension (gui/nvl.png, fonts/x.ttf, bgm.ogg).
    if cleaned.lower().endswith(_ASSET_EXT):
        return True
    # Strip inline markup + interpolation EARLY. The path/slash heuristic below
    # must run on the de-tagged content, not the raw string: a closing text tag
    # `{/i}` contains a `/`, so `{i}Exhausting.{/i}` (one word, no space, has a
    # `.`) would otherwise be misread as a file path and the line silently
    # dropped — real bug on Beyond the Turquoise Stars ("{i}Exhausting.{/i}").
    content = _RENPY_SUB_RE.sub("", _RENPY_TAG_RE.sub("", cleaned))
    # A spaceless slug containing a path separator and a dot is a path even with
    # an unknown extension (audio/se/click.foo). Prose has spaces; paths don't.
    if " " not in content and ("/" in content or "\\" in content) and "." in content:
        return True
    # Hex colour literal (#fff, #68aee3, #11223344).
    if _HEX_COLOR_RE.match(cleaned):
        return True
    # Specific style/config keys that are quoted but never shown to the player.
    if cleaned in {
        "window_background", "window_bottom_padding", "window_top_padding",
        "navigation", "subtitle", "bottom_left", "bottom_right",
        "top_left", "top_right", "thought", "medium", "small", "large",
    }:
        return True
    # After stripping markup, if no letters survive (e.g. "...", "{w=0.5}",
    # "[count]") there is nothing to translate.
    if not any(c.isalpha() for c in content):
        return True
    return False


# ---------------------------------------------------------------------------
# Engine translate-identifier algorithm.
#
# Reproduced byte-for-byte from the Ren'Py source so our tl/ say blocks bind to
# the same dialogue the engine does. Verified 56242/56242 against an oracle
# generated by `<Game>.exe <gamedir> translate russian`. References:
#   renpy/lexer.py::Lexer.string           (_lexer_decode)
#   renpy/translation/__init__.py
#       ::encode_say_string                (_encode_say_string)
#       ::Restructurer.create_translate    (_md5_identifier)
#       ::Restructurer.unique_identifier   (_compute_identifier)
#   renpy/ast.py::Say.get_code             (_say_get_code)
# ---------------------------------------------------------------------------

def _dequote(m: "re.Match") -> str:
    c = m.group(1)
    if c == "{":
        return "{{"
    elif c == "[":
        return "[["
    elif c == "%":
        return "%%"
    elif c == "n":
        return "\n"
    elif c[0] == "u":
        g2 = m.group(2)
        if g2:
            return chr(int(g2, 16))
        return ""
    else:
        return c


def _lexer_decode(raw_inner: str) -> str:
    """What the engine's lexer stores after reading a quoted string: collapse runs
    of whitespace to a single space, then expand escapes. The identifier is hashed
    from this decoded form, NOT the raw bytes between the quotes — so a source
    `"foo  bar"` (two spaces) hashes the same as `"foo bar"`.

    Correct for say-lines and menu choices — the engine reads those through its
    string LEXER (`lexer.py::Lexer.string`), which collapses whitespace. NOT
    correct for `old`/`new` strings-block keys whose runtime value is a Python
    expression (screen `text`, `_()`, Character names) — use `_py_decode` there."""
    s = re.sub(r'[ \n]+', ' ', raw_inner)
    s = re.sub(r'\\(u([0-9a-fA-F]{1,4})|.)', _dequote, s)
    return s


def _py_decode(raw_inner: str) -> str:
    """Expand escapes the way Python's `eval` does (the SAME escapes as the lexer,
    minus the whitespace collapse). Use for `old` keys whose runtime value comes
    from a Python string literal, NOT the Ren'Py lexer:

      - screen `text "..."`  (a screen-language simple_expression → PyExpr)
      - `_("...")`           (a Python call)
      - Character names      (a Python literal)

    The engine parses an `old "..."` strings entry via `parse_string` →
    `compile(..., "eval")` → `eval` (`renpy/parser.py::translate_strings`), which
    does NOT collapse whitespace. So a source `text "a!   \\n"` (three spaces) must
    keep all three spaces in the key, or `translate_string`'s exact-match dict
    lookup misses and the string renders untranslated. (Real bug: the StarBlitz
    quiz caption in OnlineObsessionDemo — `_lexer_decode` collapsed `!   \\n` to
    `! \\n`, so the runtime three-space value never matched.)"""
    return re.sub(r'\\(u([0-9a-fA-F]{1,4})|.)', _dequote, raw_inner)


def _encode_say_string(s: str) -> str:
    """Inverse of the lexer: re-escape a decoded string into Ren'Py say-string
    source form. The `(?<= ) ` rule escapes a second consecutive space as `\\ ` so
    intentional double spaces survive the lexer's whitespace collapse."""
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    s = re.sub(r'(?<= ) ', r'\ ', s)
    return '"' + s + '"'


def _say_get_code(who_var: str, attrs: list[str], raw_what: str, *,
                  nointeract: bool, say_args: str | None = None) -> str:
    """Reproduce ast.Say.get_code(): space-joined speaker + attrs + encoded text,
    then ` nointeract` (menu captions) and the verbatim argument list, in the
    engine's order. `raw_what` is the source inner text; it's run through the lexer
    decode then re-encoded so the hash matches the engine regardless of how the
    source happened to escape it. `say_args` is the raw `(...)` from source, which
    the engine appends verbatim (ArgumentInfo.get_code re-emits expressions as
    written) — order in get_code is: text, nointeract, [id], arguments."""
    parts: list[str] = []
    if who_var:
        parts.append(who_var)
    parts.extend(attrs)
    parts.append(_encode_say_string(_lexer_decode(raw_what)))
    code = " ".join(parts)
    if nointeract:
        code += " nointeract"
    if say_args:
        code += " " + say_args
    return code


def _strip_line_comment(s: str) -> str:
    """Drop a trailing `# comment` that is OUTSIDE any string, the way the lexer's
    logical-line reader does (it never includes the comment in `l.text`). A `#`
    inside double/single quotes is literal and kept. Used to reproduce a prefix
    node's get_code (== logical line) from raw source — one voice line in Killer
    Chat carries a trailing `#Maybe switch versions` the engine strips."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(s):
                out.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        else:
            if c in "\"'":
                quote = c
                out.append(c)
            elif c == "#":
                break
            else:
                out.append(c)
        i += 1
    return "".join(out)


def _logical_code(raw_line: str) -> str:
    """A prefix UserStatement's get_code(): the raw LOGICAL line — leading indent
    and a trailing out-of-string `# comment` removed, inner spacing untouched."""
    return _strip_line_comment(raw_line).strip()


def _md5_identifier(code: str) -> str:
    return _block_digest([code])


def _block_digest(codes: list[str]) -> str:
    """create_translate()'s digest over a translate BLOCK: md5 of each node's
    get_code + "\\r\\n", in order. A plain say block is a single-element list, so
    this is byte-identical to the old per-say hash; a voice/nvl-prefixed block
    feeds [prefix_code, …, say_code] — exactly what the engine groups."""
    md5 = hashlib.md5()
    for code in codes:
        md5.update((code + "\r\n").encode("utf-8"))
    return md5.hexdigest()[:8]


def _compute_identifier(label: str | None, digest: str, seen: dict[str, int]) -> str:
    """unique_identifier(): label-prefixed digest, with `_1`, `_2`… appended on
    collision. `seen` MUST be per-file and advanced in document order — that's how
    the engine numbers duplicates, so we match its ordering exactly."""
    base = digest if label is None else label.replace(".", "_") + "_" + digest
    cnt = seen.get(base, 0)
    seen[base] = cnt + 1
    return base if cnt == 0 else f"{base}_{cnt}"


def _string_quote(s: str) -> str:
    """Quote a value for an `old`/`new` strings entry. Same escaping as a say
    string minus the double-space rule (strings keep their text verbatim)."""
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    return '"' + s + '"'


_ESCAPE_RE = re.compile(r'\\(u[0-9a-fA-F]{4}|.)')

def _unescape_translation(s: str) -> str:
    """Decode escaping in the translated string from the database before writing it to rpy."""
    def replace(m):
        c = m.group(1)
        if c == "n":
            return "\n"
        elif c == "t":
            return "\t"
        elif c == "r":
            return "\r"
        elif c == "\\":
            return "\\"
        elif c == '"':
            return '"'
        elif c == "'":
            return "'"
        elif c[0] == "u":
            try:
                return chr(int(c[1:], 16))
            except ValueError:
                return m.group(0)
        else:
            return c
    return _ESCAPE_RE.sub(replace, s)


def _escape_bad_percent(s: str) -> str:
    """Escape a `%` that Ren'Py would treat as a BROKEN string-format code -> `%%`.

    With `config.old_substitutions` (ON by default), the engine runs displayed
    text through `%`-substitution: `%s`/`%d`/`%(name)s` are format specs and a lone
    `%` must be written `%%`. The LLM routinely drops the escaping when translating
    (e.g. source `100%% ASSIGNMENT` -> translation `НА 100%`), which the engine then
    flags as "Unterminated/Unknown string format code" and can crash at render. We
    fix it deterministically at write-time — NO API call, can't be fragile.

    Mirrors EXACTLY the state machine in the engine's lint.py (text_checks): only a
    `%` that begins an invalid or unterminated spec is escaped; valid specs and an
    already-doubled `%%` are left byte-verbatim, so this is idempotent and never
    touches legitimate format strings. Verified by check_renpy_percent (selftest)."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "%":
            out.append(c)
            i += 1
            continue
        j = i + 1
        # Mapping key: %(name)...
        if j < n and s[j] == "(":
            k = s.find(")", j)
            if k == -1:                       # unterminated mapping -> bare %
                out.append("%%")
                i += 1
                continue
            j = k + 1
        # Conversion flags / width / length modifiers.
        while j < n and s[j] in "#0123456780- +hlL":
            j += 1
        if j < n and s[j] in "diouxXeEfFgGcrs%":
            out.append(s[i:j + 1])            # valid spec (incl. %%) -> verbatim
            i = j + 1
            continue
        out.append("%%")                      # invalid/unterminated -> escape
        i += 1
    return "".join(out)


# A single Ren'Py text tag: capture the leading "/" (closing) and the body.
_TEXT_TAG_RE = re.compile(r'\{(/?)([^{}]*)\}')


def _repair_text_tags(s: str) -> str:
    """Fix a closing text tag whose name the LLM corrupted by appending junk —
    e.g. `{i}…{/iR}` -> `{i}…{/i}`. A real shipped bug: the engine rejects
    "Close text tag '{/iR}' does not match open text tag '{i}'" and the line breaks.

    Ren'Py matches a closing tag `{/name}` against the most-recently-opened tag of
    the same name (LIFO). We walk the string keeping a stack of open tag names; when
    a closing tag's name does NOT match the stack top but DOES start with it (the
    append-junk corruption: open `i` / close `iR`), we snap the close back to the
    open name. Conservative: anything else (valid match, the bare `{/}` close-last
    form, an unrelated mismatch we can't safely guess) is left byte-verbatim, so
    valid markup is never touched. Idempotent. The tag NAME is everything before an
    optional `=arg` (`{size=+4}` opens "size", closes `{/size}`)."""
    if "{" not in s:
        return s

    def _name(body: str) -> str:
        # Tag name = text before '=' (args) and without surrounding spaces.
        return body.split("=", 1)[0].strip()

    open_stack: list[str] = []
    out: list[str] = []
    pos = 0
    changed = False
    for m in _TEXT_TAG_RE.finditer(s):
        out.append(s[pos:m.start()])
        pos = m.end()
        closing, body = m.group(1), m.group(2)
        name = _name(body)
        if not closing:
            # Opening tag (or a standalone like {w=0.5}/{p}/{nw}/{clear}); we only
            # need real paired tags on the stack. Push the name so a later close
            # can match it; standalones simply never get a matching close, which is
            # harmless (they stay on the stack but we never force-close them).
            open_stack.append(name)
            out.append(m.group(0))
        else:
            if name == "":
                # `{/}` = close the most recent tag; valid, leave as-is.
                if open_stack:
                    open_stack.pop()
                out.append(m.group(0))
            elif open_stack and open_stack[-1] == name:
                open_stack.pop()
                out.append(m.group(0))
            elif open_stack and name.startswith(open_stack[-1]) and len(open_stack[-1]) > 0:
                # Corrupted close: name has junk appended to the real tag name.
                fixed = open_stack.pop()
                out.append("{/%s}" % fixed)
                changed = True
            else:
                # Can't safely guess — leave verbatim (don't risk valid markup).
                out.append(m.group(0))
                if name in open_stack:
                    # Pop down to it so the stack stays sane for later tags.
                    while open_stack and open_stack.pop() != name:
                        pass
    out.append(s[pos:])
    return "".join(out) if changed else s


def _match_newlines(translation: str, original: str) -> str:
    """Drop ARTEFACT empty lines the LLM added that the original doesn't have —
    most often a trailing `\\n` left over when a 2-line source was translated as
    one line (`"I DIDN'T SIGN UP\\nFOR THIS"` -> `"Я НА ЭТО НЕ ПОДПИСЫВАЛСЯ\\n"`).
    That stray newline makes a fixed-height button reserve a SECOND, empty line, so
    the engine centres the visible text against two lines and it sits jammed at the
    top (real Killer Chat bug). It also fools the box-fit into shrinking a caption
    that actually fits on one line.

    Conservative — only LEADING/TRAILING fully-empty lines are removed, and only
    when the original doesn't itself start/end with a blank line. Interior blank
    lines (a deliberate paragraph gap the translator kept) and any non-empty line
    are left byte-verbatim. `translation`/`original` are the UNESCAPED strings (real
    newline chars). Idempotent."""
    if "\n" not in translation:
        return translation
    o_lines = original.split("\n")
    t_lines = translation.split("\n")
    o_lead = len(o_lines) > 0 and o_lines[0].strip() == ""
    o_trail = len(o_lines) > 1 and o_lines[-1].strip() == ""
    # Trim trailing empties the original lacks.
    while len(t_lines) > 1 and t_lines[-1].strip() == "" and not o_trail:
        t_lines.pop()
    # Trim leading empties the original lacks.
    while len(t_lines) > 1 and t_lines[0].strip() == "" and not o_lead:
        t_lines.pop(0)
    return "\n".join(t_lines)


def iter_logical_lines(text: str):
    lines = text.split("\n")
    current_logical = []
    start_line = None
    
    in_quote = None  # '"' or "'" or '`' or '"""' or "'''"
    paren_depth = 0  # track (), [], {}
    
    i = 0
    while i < len(lines):
        line = lines[i]
        if start_line is None:
            start_line = i + 1
        
        j = 0
        escaped = False
        comment_start = -1
        
        while j < len(line):
            c = line[j]
            if escaped:
                escaped = False
                j += 1
                continue
            if c == "\\":
                escaped = True
                j += 1
                continue
            
            if not in_quote:
                if line[j:j+3] == '"""':
                    in_quote = '"""'
                    j += 3
                    continue
                if line[j:j+3] == "'''":
                    in_quote = "'''"
                    j += 3
                    continue
                if c in '"\'`':
                    in_quote = c
                    j += 1
                    continue
                
                if c == '#':
                    comment_start = j
                    break
                
                if c in '([{':
                    paren_depth += 1
                elif c in ')]}':
                    paren_depth = max(0, paren_depth - 1)
            else:
                if in_quote == '"""':
                    if line[j:j+3] == '"""':
                        in_quote = None
                        j += 3
                        continue
                elif in_quote == "'''":
                    if line[j:j+3] == "'''":
                        in_quote = None
                        j += 3
                        continue
                else:
                    if c == in_quote:
                        in_quote = None
                        j += 1
                        continue
            j += 1
            
        if comment_start != -1:
            line_content = line[:comment_start]
        else:
            line_content = line
            
        current_logical.append(line_content)
        
        continues = False
        if in_quote:
            continues = True
        elif paren_depth > 0:
            continues = True
        elif line_content.rstrip().endswith("\\"):
            current_logical[-1] = line_content.rstrip()[:-1]
            continues = True
            
        if continues:
            current_logical.append("\n")
        else:
            yield start_line, "".join(current_logical)
            current_logical = []
            start_line = None
            
        i += 1
        
    if current_logical:
        yield start_line, "".join(current_logical)


class RenPyParser(BaseParser):
    engine = "renpy"

    def __init__(self) -> None:
        super().__init__()
        self._decompile_temp_dirs: list[str] = []

    def engine_prompt_addon(self) -> str:
        return (
            "REN'PY SYNTAX RULES:\n"
            "VARIABLES & INTERPOLATION: text may contain [variable_name] or [expr] "
            "sequences \u2014 these are runtime interpolations. Copy them VERBATIM into the "
            "translation; never translate the name inside the brackets.\n"
            "STYLE TAGS: sequences like {b}, {i}, {u}, {s}, {color=#fff}, {size=*0.8}, "
            "{alpha=*0.5}, {p}, {w}, {nw}, {fast}, {cps=N} are Ren'Py display tags. "
            "Preserve all opening and closing tags exactly as they appear.\n"
            "SCENE / TONE: the 'context' field often contains the label name (e.g. "
            "'label: romance', 'label: dm', 'label: office') and may include the "
            "immediately preceding dialogue line: Prev line: '...' (speaker). Use it "
            "to match tone and register, maintain consistent T-V distinction (\u0442\u044b/\u0432\u044b), "
            "and resolve gender or pronoun ambiguities. Visual novels are highly personal; "
            "when translating 'you' into languages with T-V distinction (like Russian, "
            "Spanish, French, German), default to the informal address (e.g., '\u0442\u044b', "
            "'t\u00fa', 'tu', 'du') unless the context clearly dictates a formal address (e.g., "
            "a teacher, boss, or stranger).\n"
            "DIALOGUE NATURALNESS: these are spoken lines of a visual novel. The "
            "translation should sound like a real person talking, not a subtitle or a "
            "technical manual.\n"
            "FIXED-WIDTH CAPTIONS: some items carry a fixed_width flag and constraints "
            "like max_chars and/or max_pixels. These render inside a fixed-size UI box "
            "(a Ren'Py menu choice or screen button). Prefer a translation that fits the "
            "limits. If max_pixels is specified, estimate the visual width (Cyrillic "
            "glyphs are ~10-20% wider than Latin) and try to stay within it.\n"
            "CRITICAL \u2014 NEVER ABBREVIATE OR TRUNCATE A WORD: do NOT write "
            "'\u0421\u043e\u0445' for '\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435', "
            "'\u041d\u0430\u0441\u0442\u0440.' for '\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438', "
            "'\u041f\u0440\u043e\u0434.' for '\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c'. "
            "A single word MUST always be spelled out in full, even if it would exceed "
            "the limit \u2014 the app shrinks the font to make a whole word fit, so a full "
            "word that is slightly too long is CORRECT and a clipped/abbreviated word is "
            "WRONG. Only when the caption is THREE OR MORE words may you shorten it, and "
            "only by rephrasing with a shorter synonym or dropping filler \u2014 never by "
            "abbreviating. Example: 'Start a New Game' (3+ words) -> "
            "'\u041d\u043e\u0432\u0430\u044f \u0438\u0433\u0440\u0430' (rephrased, fine). "
            "Example: 'Save' (1 word) -> '\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435' in full "
            "(NOT '\u0421\u043e\u0445\u0440.' or '\u0421\u043e\u0445'), even if longer than the box. "
            "These limits are constraints to RESPECT, never text to translate."
        )


    # --- detection --------------------------------------------------------
    @staticmethod
    def detect(root: str) -> bool:
        # If there's a 'renpy' directory at the root, it's almost certainly a Ren'Py game.
        if os.path.isdir(os.path.join(root, "renpy")) and os.path.isdir(os.path.join(root, "game")):
            return True

        game = os.path.join(root, "game")
        # Check for loose .rpy or .rpyc files
        if os.path.isdir(game):
            for dirpath, _, filenames in os.walk(game):
                if "renpy" in dirpath.split(os.sep):
                    continue
                for f in filenames:
                    if f.endswith((".rpy", ".rpyc")):
                        return True
        else:
            for dirpath, _, filenames in os.walk(root):
                if "renpy" in dirpath.split(os.sep):
                    continue
                for f in filenames:
                    if f.endswith((".rpy", ".rpyc")):
                        return True

        # Check for .rpa archives containing .rpy or .rpyc files
        if os.path.isdir(game):
            from . import rpa as rpamod
            for arc in rpamod.iter_rpa_files(game):
                if rpamod.archive_has_suffix(arc, ".rpy") or rpamod.archive_has_suffix(arc, ".rpyc"):
                    return True

        return False

    @staticmethod
    def _rpy_files(root: str, sub_paths: list[str] | None = None) -> list[str]:
        """All .rpy under sub_paths (relative to root) or game/, except the tl/ tree (existing translations)
        and the renpy/common runtime. Sorted for deterministic ids."""
        if sub_paths:
            paths_to_walk = [os.path.join(root, p) for p in sub_paths]
        else:
            paths_to_walk = [os.path.join(root, "game")]

        out: list[str] = []
        for start_path in paths_to_walk:
            if not os.path.exists(start_path):
                continue
            if os.path.isfile(start_path):
                if start_path.endswith(".rpy"):
                    out.append(start_path)
                continue
            for dirpath, dirnames, filenames in os.walk(start_path):
                # Don't descend into already-translated or runtime trees.
                dirnames[:] = [d for d in dirnames if d not in ("tl", "cache")]
                for name in filenames:
                    if name.endswith(".rpy"):
                        out.append(os.path.join(dirpath, name))
        return sorted(out)

    def _iter_sources(self, root: str, sub_paths: list[str] | None = None):
        """Yield (file_rel, text) for every .rpy source, from BOTH loose files and
        any `.rpa` archive — so games that ship their scripts only inside an
        archive (e.g. Killer Chat!) are translatable too.

        `file_rel` is the root-relative, forward-slash path. For an archived file
        it is `"game/" + inner_path`, byte-identical to what the same file would
        get if it were loose on disk — so the stable id is unchanged whether a
        game packs its scripts or not (and stays portable between game copies).

        Loose files WIN over archived ones of the same path: the engine loads
        on-disk files in preference to the archive, so we mirror that. extract()
        and inject() share this generator, guaranteeing identical addressing."""
        sources: dict[str, str] = {}  # file_rel -> text, insertion = priority

        # 1) Loose .rpy first (highest priority).
        for fpath in self._rpy_files(root, sub_paths):
            file_rel = os.path.relpath(fpath, root).replace("\\", "/")
            if file_rel in sources:
                continue
            with open(fpath, encoding="utf-8") as f:
                sources[file_rel] = f.read()

        # 1b) Decompiled .rpy from temp dirs (same priority as loose).
        for temp_dir in self._decompile_temp_dirs:
            for dirpath, _, filenames in os.walk(temp_dir):
                for f in filenames:
                    if not f.endswith(".rpy"):
                        continue
                    fpath = os.path.join(dirpath, f)
                    # Map temp path back to game/ relative path
                    rel_in_temp = os.path.relpath(fpath, temp_dir).replace("\\", "/")
                    file_rel = "game/" + rel_in_temp
                    if file_rel in sources:
                        continue
                    with open(fpath, encoding="utf-8") as fh:
                        sources[file_rel] = fh.read()

        # 2) Archived .rpy. Only in whole-game mode (sub_paths empty); in
        #    sub-path mode an archive is read only if its own root-relative path
        #    was selected (mirrors unreal4 filtering paks by pak path).
        from . import rpa as rpamod
        game_dir = os.path.join(root, "game")
        for arc in rpamod.iter_rpa_files(game_dir):
            if sub_paths:
                arc_rel = os.path.relpath(arc, root).replace("\\", "/")
                if arc_rel not in sub_paths:
                    continue
            try:
                inner_files = rpamod.read_rpa(arc)
            except Exception as e:  # foreign/corrupt archive -> skip
                logger.warning("skipping unreadable .rpa %s: %s", arc, e)
                continue
            for rf in inner_files:
                file_rel = "game/" + rf.path
                if file_rel in sources:
                    continue  # loose (or earlier archive) override wins
                sources[file_rel] = rf.data.replace("\r\n", "\n")

        for file_rel in sorted(sources):
            text = sources[file_rel]
            # Skip shipped tl/<lang>/ translation files (another language's text);
            # content-based so code living under tl/None/ is still read as source.
            if _is_existing_translation_file(text):
                logger.info("Skipping existing translation file: %s", file_rel)
                continue
            yield file_rel, text

    def _cleanup_decompile_temp(self) -> None:
        """Remove all temporary directories created by _decompile_rpyc_files."""
        for d in self._decompile_temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        self._decompile_temp_dirs.clear()

    # --- shared traversal -------------------------------------------------
    @staticmethod
    def _scan(text: str):
        """Yield one record per translatable string, in file order. Each record:

            {
              path, original, context,          # our contract (id is made from these)
              native_kind,                      # "say" | "menu_choice" | "string"
              who_var, attrs, raw_what,         # engine identifier inputs (say only)
              label, is_menu_caption, src_line, # ditto + tl/ comment line
            }

        `native_kind` selects the tl/ block type on inject:
          say          -> `translate <lang> <identifier>:` block
          menu_choice  -> `translate <lang> strings:` entry (old/new)
          string       -> `translate <lang> strings:` entry (screen text, _(),
                          character display names)

        PATH is structural (label/screen + index), never a line number, so an edit
        in one label leaves every other id untouched (BEDROCK #2). It is NOT
        changed by this rewrite — existing translations keep matching.

        Two parsing contexts via indent tracking: NARRATIVE (say/menu/define) and
        SCREEN (textbutton/text/label inside a `screen` block)."""
        label = ""
        global_label = ""
        # (label, kind) -> running count for narrative strings.
        counts: dict[tuple[str, str], int] = {}
        last_dialogue: dict[str, str] | None = None
        # variable name -> display name, built from `define` lines so that
        # `e "Hello."` can be annotated with "Eileen" instead of just "e".
        char_names: dict[str, str] = {}

        # Screen state.
        # screen_indent: indent column of the 'screen' keyword line; -1 = not
        # inside a screen block.
        screen_indent: int = -1
        screen_name: str = ""
        # kind -> running count for text widgets in the current screen.
        screen_counts: dict[str, int] = {}
        # Variable -> list of string values, resolved from `for var in [("STR", ...)]`
        # inside the current screen.  Lets `textbutton var` extract translatable strings.
        screen_for_vars: dict[str, list[str]] = {}
        screen_for_indent: int = -1

        # Menu-caption state (drives nointeract). When we pass a `menu:` opener we
        # arm a flag; the first say line indented inside it is the caption.
        pending_caption: bool = False
        menu_indent: int = -1

        # Inline-translate-block state. When inside a `translate <lang> …:` block,
        # every indented line is ALREADY-translated text (not source) and must be
        # skipped. -1 = not in a translate block; otherwise the indent column of
        # the `translate` opener (we leave the block on the first line de-dented
        # to/past it). Guards mixed files (source + inline translations together).
        translate_indent: int = -1

        # Per-narrative-context running index for _() calls, so their ids are
        # stable. Keyed by the container (screen name or label).
        uscore_counts: dict[str, int] = {}

        # Accumulated translatable PREFIX nodes (voice / nvl clear) waiting to be
        # grouped with the next say. The engine's Restructurer appends each to the
        # current group; the next Say closes the group and `create_translate`
        # hashes all of them. Any OTHER real statement flushes the group (as its
        # own block), so we clear this on every non-prefix, non-comment line.
        # Each entry is (get_code, src_line); the line of the FIRST node becomes
        # the block's `# file:line` header (block[0].linenumber in the engine).
        pending_prefix: list[tuple[str, int]] = []

        def uscore_records(line: str, li: int, container_path_prefix: list[str], container_key: str, ctx: str):
            """Yield a record for each _() call on the line, expanding f-strings if needed."""
            matches = []
            for um in _USCORE_RE.finditer(line):
                matches.append((um.start(), um.end(), um.group("text"), False))
            for um in _USCORE_F_RE.finditer(line):
                matches.append((um.start(), um.end(), um.group("text"), True))
            matches.sort(key=lambda x: x[0])
            
            for start, end, t, is_f in matches:
                if is_f:
                    variants = expand_f_string(t)
                else:
                    variants = [t]
                
                tail = line[end:]
                for var_t in variants:
                    if not var_t.strip() or is_technical_string(var_t):
                        continue
                    
                    t_processed = parse_string_methods(var_t, tail)
                    
                    idx = uscore_counts.get(container_key, 0)
                    uscore_counts[container_key] = idx + 1
                    yield {
                        "path": container_path_prefix + ["uscore", str(idx)],
                        "original": t_processed,
                        "context": ctx,
                        "native_kind": "string",
                        "who_var": None,
                        "attrs": [],
                        "raw_what": t_processed,
                        "label": label,
                        "is_menu_caption": False,
                        "src_line": li,
                    }

        def set_label(raw_label: str):
            """Apply a `label`/named-`menu` name to the current label state, exactly
            as the engine's lexer does (lexer.py::set_global_label + label_name):
            a `.local` name resolves against the current global; a global name
            becomes the new global. Used by BOTH the `label` and `menu NAME`
            branches so they can never drift."""
            nonlocal label, global_label
            if raw_label.startswith("."):
                label = (global_label + raw_label) if global_label else raw_label
            else:
                label = raw_label
                global_label = raw_label.split(".")[0]

        for li, line in iter_logical_lines(text):
            if not line.strip():
                continue  # blank lines carry no indent information

            cur_indent = len(line) - len(line.lstrip())

            # Comments are not AST nodes: the engine never extracts them and they
            # never flush a pending translate group. Skip before anything else so
            # a `# ...` between a voice line and its say can't break the grouping.
            if line.lstrip().startswith("#"):
                continue

            if _RESET_FLOW_RE.match(line):
                last_dialogue = None
                continue

            # Inside an inline `translate <lang> …:` block? Its body is already
            # translated — skip it. Leave the block on the first line de-dented to
            # or past the opener. (Checked before the opener match so a sibling
            # translate block right after another is entered cleanly.)
            if translate_indent >= 0:
                if cur_indent > translate_indent:
                    continue
                translate_indent = -1  # left the block; fall through to re-process

            # A `translate <lang> …:` opener — skip the whole block that follows.
            to = _TRANSLATE_OPEN_RE.match(line)
            if to:
                translate_indent = len(to.group("indent"))
                pending_prefix = []  # don't let a stale prefix bind across it
                last_dialogue = None
                continue

            # Translatable PREFIX nodes accumulate, waiting for the next say to
            # close the group (mirrors Restructurer.callback). They carry no
            # player text themselves, so we only stash their get_code.
            if _VOICE_RE.match(line) or _NVL_CLEAR_RE.match(line):
                pending_prefix.append((_logical_code(line), li))
                continue

            # Every other real statement either CONSUMES the pending prefix (a
            # say, below) or FLUSHES it (the engine emits the orphaned voice/nvl
            # as its own text-free block — nothing for us to translate). Snapshot
            # it here and clear the shared state; only the say branch re-reads it.
            block_prefix = pending_prefix
            pending_prefix = []

            # -- Screen entry ---------------------------------------------
            # Checked before the interior block so back-to-back 'screen'
            # declarations at the same indent level transition cleanly.
            sm = _SCREEN_RE.match(line)
            if sm:
                screen_indent = cur_indent
                screen_name = sm.group("name")
                screen_counts = {}
                screen_for_vars = {}
                screen_for_indent = -1
                pending_caption = False
                menu_indent = -1
                last_dialogue = None
                continue
 
            # -- Screen interior ------------------------------------------
            if screen_indent >= 0:
                if cur_indent <= screen_indent:
                    # De-dented back to (or past) the screen's own level:
                    # we've left the block. Reset and fall through so this
                    # line still gets processed as narrative (label, etc.).
                    screen_indent = -1
                    screen_name = ""
                    screen_counts = {}
                    screen_for_vars = {}
                    screen_for_indent = -1
                    last_dialogue = None
                    # fall through to narrative processing below
                else:
                    # Track for-loop variable bindings: `for x, w in [("STR", 137), ...]:`
                    fm = _SCREEN_FOR_RE.match(line)
                    if fm:
                        var_name = fm.group("var")
                        body = fm.group("body")
                        vals = re.findall(r'"([^"]*)"', body)
                        if vals:
                            screen_for_vars[var_name] = vals
                            screen_for_indent = cur_indent
                        # fall through — the for-line itself isn't a widget

                    # Clear for-loop bindings when we leave the for-loop's indent.
                    if screen_for_indent >= 0 and cur_indent <= screen_for_indent and not fm:
                        screen_for_vars = {}
                        screen_for_indent = -1

                    wm = _SCREEN_WIDGET_RE.match(line)
                    if wm:
                        widget_text = wm.group("text")
                        if widget_text.strip() and not is_technical_string(widget_text):
                            kind = wm.group("kind")
                            idx = screen_counts.get(kind, 0)
                            screen_counts[kind] = idx + 1
                            yield {
                                "path": ["screen", screen_name, kind, str(idx)],
                                "original": widget_text,
                                "context": f"{screen_name} {kind}",
                                "native_kind": "string",
                                "who_var": None,
                                "attrs": [],
                                "raw_what": widget_text,
                                "label": None,
                                "is_menu_caption": False,
                                "src_line": li,
                            }
                    else:
                        # textbutton/text/label with a variable from a for-loop.
                        wvm = _SCREEN_WIDGET_VAR_RE.match(line)
                        if wvm and wvm.group("var") in screen_for_vars:
                            kind = wvm.group("kind")
                            for val in screen_for_vars[wvm.group("var")]:
                                if val.strip() and not is_technical_string(val):
                                    idx = screen_counts.get(kind, 0)
                                    screen_counts[kind] = idx + 1
                                    yield {
                                        "path": ["screen", screen_name, kind, str(idx)],
                                        "original": val,
                                        "context": f"{screen_name} {kind}",
                                        "native_kind": "string",
                                        "who_var": None,
                                        "attrs": [],
                                        "raw_what": val,
                                        "label": None,
                                        "is_menu_caption": False,
                                        "src_line": li,
                                    }
                    # _() calls can appear anywhere inside a screen (button
                    # labels, tooltips), including on widget lines.
                    yield from uscore_records(
                        line, li, ["screen", screen_name], f"screen:{screen_name}",
                        f"{screen_name} _()")
                    continue  # don't parse screen internals as narrative

            # -- Narrative context ----------------------------------------

            m = _LABEL_RE.match(line)
            if m:
                set_label(m.group("name"))
                pending_caption = False
                menu_indent = -1
                last_dialogue = None
                continue

            # menu: opener — arm the caption flag for the first say inside.
            mm = _MENU_RE.match(line)
            if mm:
                # A NAMED menu compiles to a real Label in the engine, so its name
                # becomes the label prefix for every say inside (parser.py::
                # menu_statement). An anonymous `menu:` leaves the label untouched.
                nm = _MENU_NAME_RE.match(line)
                if nm:
                    set_label(nm.group("name"))
                pending_caption = True
                menu_indent = cur_indent
                last_dialogue = None
                continue

            # Leaving the menu block (dedent to/past the menu keyword) disarms.
            if menu_indent >= 0 and cur_indent <= menu_indent:
                pending_caption = False
                menu_indent = -1
                last_dialogue = None

            # Character display names: define e = Character("Eileen", ...)
            # _LINE_RE never matches these (= breaks the prefix pattern), so
            # we check them explicitly before the main regex.
            cm = _CHAR_DEF_RE.match(line)
            if cm:
                var = cm.group("var")
                # Extract character name from positional or keyword argument
                name_m = re.search(
                    r'\(\s*"([^"\\]*(?:\\.[^"\\]*)*)"'
                    r'|\bname\s*=\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
                    line,
                )
                name = (name_m.group(1) or name_m.group(2)) if name_m else None
                if name:
                    char_names[var] = name
                    if name.strip() and not is_technical_string(name):
                        yield {
                            "path": ["define", var, "name"],
                            "original": name,
                            "context": "character name",
                            "native_kind": "string",
                            "who_var": None,
                            "attrs": [],
                            "raw_what": name,
                            "label": None,
                            "is_menu_caption": False,
                            "src_line": li,
                        }
                for km in _CHAR_KWARG_RE.finditer(line):
                    val = km.group("val")
                    if val.strip() and not is_technical_string(val):
                        yield {
                            "path": ["define", var, km.group("key")],
                            "original": val,
                            "context": f"character {km.group('key')}",
                            "native_kind": "string",
                            "who_var": None,
                            "attrs": [],
                            "raw_what": val,
                            "label": None,
                            "is_menu_caption": False,
                            "src_line": li,
                        }
                continue

            # `show text "..."` — a top-level on-screen Text displayable (intro
            # cards, chapter titles). The engine translates it via translate_string
            # at render time, so it routes to the strings (old/new) block like _().
            stm = _SHOW_TEXT_RE.match(line)
            if stm:
                show_text = stm.group("text")
                if show_text.strip() and not is_technical_string(show_text):
                    key = (label, "showtext")
                    idx = counts.get(key, 0)
                    counts[key] = idx + 1
                    yield {
                        "path": ["label", label, "showtext", str(idx)],
                        "original": show_text,
                        "context": f"Label: {label} | on-screen text"
                                   if not _is_generic_label(label) else "on-screen text",
                        "native_kind": "string",
                        "who_var": None,
                        "attrs": [],
                        "raw_what": show_text,
                        "label": label,
                        "is_menu_caption": False,
                        "src_line": li,
                    }
                continue

            m = _LINE_RE.match(_normalise_line(line))
            if not m:
                # Even non-say lines may carry _() calls (e.g. `$ x = _("Hi")`).
                _lctx = f"Label: {label} | narrator _()" if not _is_generic_label(label) else "narrator _()"
                yield from uscore_records(
                    line, li, ["label", label], f"label:{label}", _lctx)
                continue

            # Recover the ORIGINAL prefix from the source line — not from the
            # normalised copy — so that @[...] dynamic attribute tokens are
            # preserved verbatim for _say_get_code / engine MD5 computation.
            # Strategy: the first unescaped `"` in the original line marks the
            # end of the prefix; everything between the indent and that quote is
            # the real prefix (split on whitespace for who_var / attrs).
            indent_len = len(m.group("indent"))
            try:
                first_quote = line.index('"', indent_len)
                prefix = line[indent_len:first_quote]
            except ValueError:
                prefix = m.group("prefix")  # fallback (should never happen)
            prefix_words = prefix.split()
            first = prefix_words[0] if prefix_words else ""
            if first in _KEYWORDS:
                yield from uscore_records(
                    line, li, ["label", label], f"label:{label}", "narrator _()")
                continue

            suffix = m.group("suffix")
            say_args = _extract_say_args(suffix)
            if _MENU_SUFFIX_RE.match(suffix):
                kind = "menu"
                native_kind = "menu_choice"
            elif _is_menu_choice_with_args(suffix):
                # Chat-style choice carrying args: `"Choice"(reacts=[...]):`.
                # Only the choice TEXT is translatable; the arg list (which may
                # contain quoted emoji) is code and stays verbatim.
                kind = "menu"
                native_kind = "menu_choice"
            elif say_args is not None:
                # `who "text" (channel=m.dm)` — say with an argument list. Checked
                # BEFORE the `"` test below because args like `(type="voice")`
                # contain a quote and would otherwise be misread as a 2nd string.
                kind = "say"
                native_kind = "say"
            elif '"' in suffix:
                # A second string on the line -> ambiguous (likely code). Skip,
                # but still harvest any _() it may contain.
                _lctx = f"Label: {label} | narrator _()" if not _is_generic_label(label) else "narrator _()"
                yield from uscore_records(
                    line, li, ["label", label], f"label:{label}", _lctx)
                continue
            else:
                kind = "say"
                native_kind = "say"

            # A menu choice is NOT a say caption; once we hit the first choice the
            # caption window is closed.
            if native_kind == "menu_choice":
                pending_caption = False
                menu_indent = -1

            is_generic = _is_generic_label(label)
            speaker = char_names.get(first, first) if first else "narrator"

            # Check if this is a technical string (like style backgrounds, fonts, etc.)
            if is_technical_string(m.group("text")):
                continue

            key = (label, kind)
            idx = counts.get(key, 0)
            counts[key] = idx + 1

            # Engine identifier inputs (used by inject for say blocks).
            if native_kind == "say":
                who_var = first if (prefix_words and first) else None
                attrs = prefix_words[1:] if prefix_words else []
                is_caption = pending_caption
                pending_caption = False  # only the FIRST say in the menu is caption
                # A menu caption WITHOUT a speaker is a bare string: the engine
                # routes it to the strings (old/new) block, not a nointeract say
                # block. A caption WITH a speaker stays a say (nointeract). The
                # path stays a "say" path either way, so our id is unaffected.
                if is_caption and who_var is None:
                    native_kind = "string"
                    is_caption = False
            else:
                who_var = None
                attrs = []
                is_caption = False
                say_args = None  # only say lines carry an argument list

            # Finalize context and track dialogue history
            if native_kind == "say":
                ctx = f"Label: {label} | Speaker: {speaker}" if not is_generic else f"Speaker: {speaker}"
                if last_dialogue is not None:
                    prev_text = last_dialogue["text"]
                    if len(prev_text) > 250:
                        prev_text = prev_text[:150] + "..."
                    ctx += f" | Prev line: '{prev_text}' ({last_dialogue['speaker']})"
                
                # Update dialogue state
                if speaker == "extend":
                    if last_dialogue is not None and m.group("text").strip():
                        sep = " " if not (last_dialogue["text"].endswith(" ") or m.group("text").startswith(" ")) else ""
                        last_dialogue["text"] += sep + m.group("text")
                elif m.group("text").strip():
                    last_dialogue = {"text": m.group("text"), "speaker": speaker}
            else:
                if kind == "say":
                    ctx = f"Label: {label} | Speaker: {speaker}" if not is_generic else f"Speaker: {speaker}"
                else:
                    ctx = f"Label: {label}" if not is_generic else ""

            yield {
                "path": ["label", label, kind, str(idx)],
                "original": m.group("text"),
                "context": ctx,
                "native_kind": native_kind,
                "who_var": who_var,
                "attrs": attrs,
                "raw_what": m.group("text"),
                "say_args": say_args,
                "label": label,
                "is_menu_caption": is_caption,
                "src_line": li,
                # Translatable prefix nodes (voice/nvl) grouped INTO this say's
                # block by the engine — folded into the identifier digest, and
                # re-emitted verbatim in the tl/ block. Only meaningful for say.
                "block_prefix": block_prefix if native_kind == "say" else [],
            }

    def _ensure_unrpyc(self) -> str:
        """Ensure unrpyc is importable (adds it to sys.path) and returns the directory path."""
        import sys

        if "unrpyc" in sys.modules:
            return ""

        # 1. Check if we are running from a PyInstaller bundle
        is_frozen = getattr(sys, "frozen", False)
        if is_frozen:
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                unrpyc_dir = os.path.normpath(os.path.join(meipass, "tools", "unrpyc"))
                if os.path.exists(os.path.join(unrpyc_dir, "unrpyc.py")):
                    if unrpyc_dir not in sys.path:
                        sys.path.insert(0, unrpyc_dir)
                    return unrpyc_dir

        # 2. Check in standard dev/local location
        parsers_dir = os.path.dirname(__file__)
        core_dir = os.path.dirname(parsers_dir)
        unrpyc_dir = os.path.normpath(os.path.join(core_dir, "tools", "unrpyc"))

        if os.path.exists(os.path.join(unrpyc_dir, "unrpyc.py")):
            if unrpyc_dir not in sys.path:
                sys.path.insert(0, unrpyc_dir)
            return unrpyc_dir

        raise RuntimeError(
            "Не удалось найти встроенный декомпилятор unrpyc. "
            "Убедитесь, что папка python-core/tools/unrpyc присутствует."
        )

    def _decompile_rpyc_files(self, root: str) -> None:
        """Decompile .rpyc files (including those packed inside .rpa) into a
        temporary directory. The temp paths are stored in
        ``self._decompile_temp_dirs`` so that ``_iter_sources`` can read them.
        The caller must call ``_cleanup_decompile_temp()`` when done.

        This keeps ALL decompiled files out of the game/ directory, avoiding
        Ren'Py double-load (disk + .rpa archive) and dialogue ID collisions."""
        import tempfile
        from . import rpa as rpamod

        game_dir = os.path.join(root, "game")
        if not os.path.isdir(game_dir):
            return

        # 1. Find all loose .rpyc files
        loose_rpyc = []
        for dirpath, dirnames, filenames in os.walk(game_dir):
            dirnames[:] = [d for d in dirnames if d not in ("tl", "cache")]
            for f in filenames:
                if f.endswith(".rpyc"):
                    loose_rpyc.append(os.path.join(dirpath, f))

        # 2. Find all archived .rpyc files
        archived_rpyc = []  # list of (rpa_path, inner_path)
        for arc in rpamod.iter_rpa_files(game_dir):
            inner_files = rpamod.list_rpa_contents(arc, ".rpyc")
            for inner in inner_files:
                archived_rpyc.append((arc, inner))

        files_to_decompile = []
        temp_dir = None

        # Check loose files — decompile into temp (not game/)
        for rpyc_path in loose_rpyc:
            rpy_path = rpyc_path[:-1]  # strip 'c'
            if not os.path.exists(rpy_path):
                if temp_dir is None:
                    temp_dir = tempfile.mkdtemp(prefix="interprex_rpyc_")
                rel = os.path.relpath(rpyc_path, game_dir)
                temp_rpyc = os.path.normpath(os.path.join(temp_dir, rel))
                os.makedirs(os.path.dirname(temp_rpyc), exist_ok=True)
                shutil.copy2(rpyc_path, temp_rpyc)
                files_to_decompile.append(temp_rpyc)

        # Check archived files — extract to temp
        archived_by_target = {}
        for arc, inner in archived_rpyc:
            rpyc_disk_path = os.path.normpath(os.path.join(game_dir, inner))
            archived_by_target[rpyc_disk_path] = (arc, inner)

        for rpyc_disk_path, (arc, inner) in archived_by_target.items():
            rpy_disk_path = rpyc_disk_path[:-1]
            if os.path.exists(rpy_disk_path):
                continue
            if os.path.exists(rpyc_disk_path):
                # loose .rpyc without .rpy — decompile into temp
                if temp_dir is None:
                    temp_dir = tempfile.mkdtemp(prefix="interprex_rpyc_")
                rel = os.path.relpath(rpyc_disk_path, game_dir)
                temp_rpyc = os.path.normpath(os.path.join(temp_dir, rel))
                os.makedirs(os.path.dirname(temp_rpyc), exist_ok=True)
                shutil.copy2(rpyc_disk_path, temp_rpyc)
                files_to_decompile.append(temp_rpyc)
                continue

            # Extract from .rpa to temp
            if temp_dir is None:
                temp_dir = tempfile.mkdtemp(prefix="interprex_rpyc_")
            temp_rpyc = os.path.normpath(os.path.join(temp_dir, inner))
            try:
                logger.info("Extracting temporary .rpyc: %s from %s", inner, os.path.basename(arc))
                rpamod.extract_rpa_file(arc, inner, temp_rpyc)
                files_to_decompile.append(temp_rpyc)
            except Exception as e:
                logger.error("Failed to extract %s from %s: %s", inner, arc, e)

        if not files_to_decompile:
            return

        try:
            self._ensure_unrpyc()
            import unrpyc
            from pathlib import Path as PathLib
        except Exception as e:
            logger.error("Decompilation aborted: %s", e)
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(
                f"Не удалось инициализировать встроенный декомпилятор. Ошибка: {e}"
            )

        logger.info("Decompiling %d .rpyc files in-process...", len(files_to_decompile))
        for f in files_to_decompile:
            try:
                # unrpyc v2.x requires a per-file Context (collects log + result
                # state). On a partial parse it sets ctx.state to something other
                # than "ok" instead of raising, so surface that for a clear log
                # rather than silently producing an empty .rpy.
                ctx = unrpyc.Context()
                unrpyc.decompile_rpyc(PathLib(f), ctx, overwrite=True)
                if getattr(ctx, "state", "ok") != "ok":
                    logger.error("Decompile of %s did not complete (state=%s): %s",
                                 f, ctx.state, getattr(ctx, "error", None))
            except Exception as fe:
                logger.error("Failed to decompile %s: %s", f, fe)

        # Store temp dir for _iter_sources to read from
        if temp_dir:
            self._decompile_temp_dirs.append(temp_dir)

    # --- extract ----------------------------------------------------------
    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._decompile_rpyc_files(root)
        try:
            results: list[TranslationString] = []
            for file_rel, text in self._iter_sources(root, sub_paths):
                for rec in self._scan(text):
                    if rec["original"].strip():
                        results.append(self._mk(file_rel, rec["path"], rec["original"],
                                                rec.get("context", "")))
            return results
        finally:
            self._cleanup_decompile_temp()


    # --- inject -----------------------------------------------------------
    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None, font_style: str = "smooth", size_fixes: dict[str, float] | None = None) -> int:
        """Write translations into game/tl/<lang>/ in Ren'Py's native format. The
        original .rpy files are NEVER modified. font_style ("smooth"|"pixel")
        selects which bundled font we swap in for non-Latin scripts.

        size_fixes maps a string id -> font shrink factor (<1.0) for captions that
        STILL overflowed their fixed width after the scheduler re-asked the model
        to shorten (the hybrid fit's last resort). These ids are menu/choice
        captions (only menu choices get a pixel budget in the scheduler), which the
        engine renders with `choice_button_text` — so the worst factor across them
        becomes a measured shrink of that style, merged with the static path."""
        self._decompile_rpyc_files(root)
        try:
            self._current_root = root
            lang = self._lang_dir(target_lang)
            written = 0
            detected_font: str | None = None

            # --- dialogue auto-fit budget (safety net) --------------------------
            # If the game declares a FIXED dialogue box in gui.rpy, an over-long
            # translation can clip. We DON'T abbreviate it — we wrap it in a native
            # {size=*scale} so the engine renders the FULL text smaller. We only do
            # this when we KNOW the real box (dialogue_width + textbox_height present
            # as numbers); otherwise we leave the text untouched (user contract:
            # "where the box is unknown, don't touch it"). parse_gui_rpy is
            # last-wins, matching the engine (Watch the Road redefines textbox_height
            # 278 -> 360; the engine and we both take 360).
            fit_box_w = fit_box_h = 0
            fit_font_size = 32
            fit_src_lang = target_lang
            # Menu-CHOICE one-line budget: a choice button with a fixed width and a
            # fixed-height background Frame grows DOWNWARD when a long translation
            # wraps to 2 lines (we set choice_button.ysize=None), so it spills below
            # the UI frame (real Killer Chat bug). Fit the text to ONE LINE by
            # shrinking the font to the button's inner width, so the button never
            # grows. 0 = unknown -> don't touch (same contract as dialogue).
            choice_box_w = 0
            choice_font_size = 30
            try:
                _gui_strs, _gui_ints = parse_gui_rpy(root)
                _w = _gui_ints.get("dialogue_width")
                _h = _gui_ints.get("textbox_height")
                if isinstance(_w, int) and isinstance(_h, int) and _w > 0 and _h > 0:
                    fit_box_w, fit_box_h = _w, _h
                    fit_font_size = _gui_ints.get("text_size") or 32
                # Choice button width: gui.choice_button_width, else the explicit
                # `style choice_button: xsize N` baked into a custom UI (Killer Chat).
                _cw = _gui_ints.get("choice_button_width")
                if not (isinstance(_cw, int) and _cw > 0):
                    _cw = self._style_box_width(root, "choice_button")
                if isinstance(_cw, int) and _cw > 0:
                    choice_box_w = _cw
                    choice_font_size = (
                        _gui_ints.get("choice_button_text_size")
                        or self._parse_style_text_sizes(self._iter_sources(root)).get("choice_button")
                        or _gui_ints.get("text_size") or 30
                    )
            except Exception as e_fit:
                logger.warning("auto-fit budgets unavailable: %s", e_fit)

            # Screen-caption box-fit map: (file_rel, line) -> (box_w, box_h, size)
            # for every textbutton/text/label that lives in a KNOWN fixed box. A
            # `string`-kind caption (screen text / _() / label) gets NO fitting
            # otherwise — only say lines and menu choices did — so a 2-line button
            # like THE RIGHT BUTTON?\nOR THE RIGHT BUTTON? wraps to 3 lines in the
            # wider target script and clips at the top (real Killer Chat bug). We
            # wrap such a caption in {size=*scale} so the engine renders the WHOLE
            # text smaller, exactly like _fit_dialogue. Unknown box -> absent from
            # the map -> untouched (auto-grow contract). Built once here.
            screen_box_map: dict[tuple[str, int], tuple[int, int, int, int, int]] = {}
            try:
                _box_sources = list(self._iter_sources(root, sub_paths))
                _style_boxes = self._parse_style_boxes(_box_sources)
                _style_sizes = self._parse_style_text_sizes(_box_sources)
                _style_spacing = self._parse_style_line_spacing(_box_sources)
                _body_size = _gui_ints.get("text_size") if isinstance(_gui_ints, dict) else None
                if not _body_size and _style_sizes:
                    from collections import Counter
                    _body_size = Counter(_style_sizes.values()).most_common(1)[0][0]
                _body_size = _body_size or 32
                screen_box_map = self._screen_widget_boxes(
                    _box_sources, _style_boxes, _style_sizes, _body_size,
                    _style_spacing,
                )
            except Exception as e_box:
                logger.warning("screen caption box-fit unavailable: %s", e_box)

            # Every font FILE the game references by name (e.g. font "TinyUnicode.ttf",
            # gui.bsod_text_font = "AnalogueOS-Regular.ttf"). The engine resolves ANY
            # font name through config.font_name_map.get(name, name) (renpy/text/text.py),
            # so aliasing each of these → our font catches the fonts a game wires up
            # DIRECTLY by filename, which the font_name_map-key path alone misses (those
            # direct-file fonts are exactly what renders chat/UI as tofu boxes).
            game_font_files: set[str] = set()

            # Load fallback translations from the project file to handle spacing/newline mismatches
            fallback_translations: dict[str, str] = {}
            try:
                from .base import project_file_path
                db_path = project_file_path(root)
                if os.path.exists(db_path):
                    import json
                    with open(db_path, "r", encoding="utf-8") as f:
                        db = json.load(f)
                    def norm(s: str) -> str:
                        import re
                        return re.sub(r'\s+', ' ', s).strip()
                    for entry in db.values():
                        if isinstance(entry, dict) and entry.get("translated") and entry.get("original"):
                            fallback_translations[norm(entry["original"])] = entry["translated"]
            except Exception as e:
                logger.warning("failed to load fallback translations from project file: %s", e)

            # tl_rel -> list of formatted say-block strings
            say_blocks: dict[str, list[str]] = {}
            # tl_rel -> list of (src_comment, old_raw, new_translation)
            string_entries: dict[str, list[tuple[str, str, str]]] = {}
            seen_strings: set[str] = set()

            # Standard Ren'Py common interface translations
            # 1) Try to load from assets/common_translations/renpy/<lang>/common.rpy
            assets_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets", "common_translations"))
            common_src = os.path.join(assets_dir, "renpy", lang, "common.rpy")
            common_copied = False
            if os.path.exists(common_src):
                try:
                    with open(common_src, "r", encoding="utf-8") as f:
                        common_content = f.read()
                    common_dst_rel = self._tl_rel("game/common.rpy", lang)
                    common_dst_abs = os.path.join(root, common_dst_rel.replace("/", os.sep))
                    self._atomic_write(root, common_dst_abs, common_content)
                    common_copied = True
                    old_re = re.compile(r'^\s*old\s+"(?P<text>.*?)"\s*$', re.MULTILINE)
                    for om in old_re.finditer(common_content):
                        seen_strings.add(_unescape_translation(om.group("text")))
                except Exception as e:
                    logger.warning("failed to write common.rpy translation from assets: %s", e)

            # 2) Fallback to hardcoded list for Russian if asset doesn't exist
            if not common_copied and lang == "russian":
                common_rel = "game/common.rpy"
                common_tl_rel = self._tl_rel(common_rel, lang)
                common_strings = [
                    ("Are you sure you want to quit?", "Вы уверены, что хотите выйти?"),
                    ("Are you sure you want to delete this save?", "Вы уверены, что хотите удалить это сохранение?"),
                    ("Are you sure you want to overwrite your save?", "Вы уверены, что хотите перезаписать сохранение?"),
                    ("Loading will lose unsaved progress.\nAre you sure you want to do this?", "При загрузке все несохранённые данные будут потеряны.\nВы уверены, что хотите сделать это?"),
                    ("Are you sure you want to return to the main menu?\nThis will lose unsaved progress.", "Вы уверены, что хотите вернуться в главное меню?\nВсе несохранённые данные будут потеряны."),
                    ("Are you sure you want to end the replay?", "Вы уверены, что хотите завершить повтор?"),
                    ("Skipping", "Пропуск"),
                    ("Fast Skipping", "Быстрый пропуск"),
                    ("Please click to continue.", "Пожалуйста, нажмите, чтобы продолжить."),
                ]
                for old_s, new_s in common_strings:
                    if old_s not in seen_strings:
                        seen_strings.add(old_s)
                        string_entries.setdefault(common_tl_rel, []).append(
                            ("RenPy Common", old_s, new_s))

            for file_rel, text in self._iter_sources(root, sub_paths):
                tl_rel = self._tl_rel(file_rel, lang)
                for m in _FONT_REF_RE.finditer(text):
                    game_font_files.add(m.group(0).strip('"'))
                seen_ids: dict[str, int] = {}

                for rec in self._scan(text):
                    sid = self._id(file_rel, rec["path"], rec["original"])
                    kind = rec["native_kind"]

                    tr_val = None
                    if sid in translations:
                        tr_val = translations[sid]
                    else:
                        norm_orig = re.sub(r'\s+', ' ', rec["original"]).strip()
                        if norm_orig in fallback_translations:
                            tr_val = fallback_translations[norm_orig]

                    if tr_val is None:
                        continue
                    tr = _unescape_translation(tr_val)
                    # Restore %-escaping the LLM tends to drop (100%% -> 100%): the
                    # engine treats displayed text as a format string by default, so
                    # a bare % is a crash-class lint error. Deterministic, no API.
                    tr = _escape_bad_percent(tr)
                    # Repair a closing text tag the LLM corrupted (e.g. {i}…{/iR}):
                    # the engine rejects mismatched tags and the line breaks. Fixed
                    # deterministically before write, so engine-lint never even sees
                    # it. No API.
                    tr = _repair_text_tags(tr)
                    # Drop a stray leading/trailing empty line the LLM left when it
                    # collapsed a multi-line source to fewer lines (a trailing \n →
                    # a phantom 2nd line → button text jams to the top + false
                    # box-fit shrink). Compared against the UNESCAPED original. No API.
                    tr = _match_newlines(tr, _unescape_translation(rec["original"]))

                    if kind == "say":
                        code = _say_get_code(rec["who_var"], rec["attrs"], rec["raw_what"],
                                             nointeract=rec["is_menu_caption"],
                                             say_args=rec.get("say_args"))
                        prefix = rec.get("block_prefix") or []
                        block_codes = [c for c, _ in prefix] + [code]
                        ident = _compute_identifier(rec["label"], _block_digest(block_codes), seen_ids)
                        # Dialogue safety-net: if a FIXED textbox is known and the
                        # full translation would overflow it, wrap in {size=*scale}
                        # so the engine renders the WHOLE text smaller (never cut).
                        say_tr = self._fit_dialogue(
                            tr, fit_box_w, fit_box_h, fit_font_size, fit_src_lang,
                            font_style,
                        )
                        say_blocks.setdefault(tl_rel, []).append(
                            self._format_say_block(file_rel, rec, lang, ident, code, say_tr))
                    else:
                        # `menu_choice` text is read by the engine's string LEXER
                        # (whitespace collapsed) → match with `_lexer_decode`.
                        # `string` (screen text / `_()` / Character name) is a
                        # Python literal eval'd at runtime (whitespace preserved)
                        # → match with `_py_decode`, or a multi-space source caption
                        # never matches its collapsed `old` key (StarBlitz quiz bug).
                        decode = _lexer_decode if kind == "menu_choice" else _py_decode
                        old_decoded = decode(rec["raw_what"])
                        if old_decoded in seen_strings:
                            continue
                        seen_strings.add(old_decoded)
                        # Menu choices: shrink the font so the FULL text fits ONE
                        # line of the fixed-width button — stops the button growing
                        # to 2 lines and spilling below the UI frame. No-op when the
                        # button width is unknown or the text already fits.
                        entry_tr = tr
                        if kind == "menu_choice":
                            entry_tr = self._fit_one_line(
                                tr, choice_box_w, choice_font_size, target_lang,
                                font_style,
                            )
                        else:
                            # Screen caption (text / _() / label / textbutton): if it
                            # lives in a KNOWN fixed box and the translation would
                            # clip, wrap in {size=*scale} so the engine renders the
                            # WHOLE text smaller — never cut. Absent from the map ->
                            # unknown box -> left untouched (auto-grow contract).
                            box = screen_box_map.get((file_rel, rec["src_line"]))
                            if box:
                                bw, bh, bsz, blsp, blld = box
                                entry_tr = self._fit_dialogue(
                                    entry_tr, bw, bh, bsz, target_lang, font_style,
                                    line_spacing=blsp, line_leading=blld,
                                )
                        string_entries.setdefault(tl_rel, []).append(
                            (f"{file_rel}:{rec['src_line']}", old_decoded, entry_tr))

                    if detected_font is None:
                        detected_font = self._detect_font(tr_val, font_style)
                    written += 1

            self._write_tl_files(root, lang, say_blocks, string_entries)
            self._write_language_file(root, lang)
            if detected_font:
                self._write_native_font(root, lang, detected_font, game_font_files)
            measured: dict[str, float] = {}
            if size_fixes:
                worst = min(size_fixes.values())
                if worst < 1.0:
                    measured["choice_button"] = worst
            self._generate_style_overrides(root, lang, measured)

            return written
        finally:
            self._cleanup_decompile_temp()

    @staticmethod
    def _fit_dialogue(tr: str, box_w: int, box_h: int, font_size: int,
                      target_lang: str, font_style: str,
                      line_spacing: int = 0, line_leading: int = 0) -> str:
        """Wrap an overflowing say-line in {size=*scale} so the engine renders the
        FULL translation small enough to fit a known fixed textbox. Returns `tr`
        unchanged when the box is unknown (box_w/h == 0), the text already fits, or
        it already carries a leading {size=...} (don't double-wrap). The word/
        sentence is NEVER cut — only the font scales, exactly per the user's rule.
        line_spacing/line_leading (from the style, default 0, may be negative) are
        added to each line's height so the fit matches the engine exactly."""
        if box_w <= 0 or box_h <= 0:
            return tr
        # Respect an existing author/our size tag at the very start.
        if tr.lstrip().startswith("{size="):
            return tr
        scale = fit_scale_for_box(tr, target_lang, font_size, box_w, box_h,
                                  font_style, line_spacing, line_leading)
        if scale >= 0.999:
            return tr
        return "{size=*%s}%s{/size}" % (scale, tr)

    @staticmethod
    def _fit_one_line(tr: str, box_w: int, font_size: int,
                      target_lang: str, font_style: str) -> str:
        """Shrink a menu-choice caption with {size=*scale} so the FULL text fits a
        SINGLE line of a fixed-width button (box_w px inner width). This keeps the
        button one line tall, so it can't grow and spill below the UI frame. Returns
        `tr` unchanged when the width is unknown (0), it already fits, or it carries
        a leading {size=...}. Never cuts the text; floor is _FIT_FONT_FLOOR."""
        if box_w <= 0:
            return tr
        if tr.lstrip().startswith("{size="):
            return tr
        scale = fit_scale_one_line(tr, target_lang, font_size, box_w, font_style)
        if scale >= 0.999:
            return tr
        return "{size=*%s}%s{/size}" % (scale, tr)

    @staticmethod
    def _style_box_width(root: str, style_name: str) -> int:
        """Largest literal `xsize N` declared in `style <style_name>:` blocks across
        the game's sources (loose + archived). 0 if none — custom UIs (Killer Chat)
        bake `style choice_button: xsize 940` instead of a gui.* constant."""
        import re as _re
        head = _re.compile(r'^([ \t]*)style\s+' + _re.escape(style_name) + r'\b.*:\s*(?:#.*)?$')
        xsize = _re.compile(r'^\s*(?:xsize|xmaximum)\s+(\d+)\b')
        best = 0
        try:
            from .renpy import RenPyParser as _RP  # self, but keep static
        except Exception:
            pass
        parser = RenPyParser()
        for _file_rel, text in parser._iter_sources(root):
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                m = head.match(lines[i])
                if not m:
                    i += 1
                    continue
                indent = len(m.group(1))
                i += 1
                while i < len(lines):
                    l = lines[i]
                    if l.strip() and (len(l) - len(l.lstrip())) <= indent:
                        break
                    xm = xsize.match(l)
                    if xm:
                        best = max(best, int(xm.group(1)))
                    i += 1
        return best

    # Screen widgets whose caption lives in a FIXED box and so can clip a longer
    # translation. `textbutton`/`label` are clickable captions; `text` is a
    # heading/paragraph. All three get box-fit when (and only when) we can read a
    # real fixed width AND height for them — otherwise we leave the text untouched
    # (auto-grow contract). A bare inline `xsize/ysize/xysize` on the widget wins
    # over the style; the style supplies whatever the widget doesn't state.
    _WIDGET_BOX_RE = re.compile(
        r'^(?P<indent>[ \t]*)(?P<kind>textbutton|text|label)\b'
    )
    _STYLE_REF_RE = re.compile(r'\bstyle\s+["\'](?P<style>\w[\w.]*)["\']')
    _PREFIX_REF_RE = re.compile(r'^[ \t]*style_prefix\s+["\']?(?P<prefix>\w[\w.]*)["\']?')
    _INLINE_XYSIZE_RE = re.compile(r'\bxysize\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)')
    _INLINE_XSIZE_RE = re.compile(r'\b(?:xsize|xmaximum)\s+(\d+)\b')
    _INLINE_YSIZE_RE = re.compile(r'\b(?:ysize|ymaximum)\s+(\d+)\b')

    def _parse_style_boxes(self, sources) -> dict[str, tuple[int, int]]:
        """Map style name -> (width, height) declared by `xysize (W,H)` / `xsize N`
        / `ysize N` in its `style <name>:` block, resolving a one-level `is <parent>`
        so a child that only adds geometry still inherits the parent's box (Killer
        Chat: `name_customization_confirm_cancel_button is name_customization_select_button`
        then `xysize (364, 81)`). 0 on an axis means "not fixed on that axis". Same
        scan shape as `_parse_style_text_sizes`, so the two never drift."""
        head = re.compile(
            r'^([ \t]*)style\s+(\w[\w.]*)\b(?:\s+is\s+(\w[\w.]*))?\s*:\s*(?:#.*)?$'
        )
        is_line = re.compile(r'^\s+is\s+(\w[\w.]*)\b')
        xysize = re.compile(r'^\s*xysize\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)')
        xsize = re.compile(r'^\s*(?:xsize|xmaximum)\s+(\d+)\b')
        ysize = re.compile(r'^\s*(?:ysize|ymaximum)\s+(\d+)\b')
        # Padding reduces the inner TEXT area the engine wraps within: the wrap
        # width is `box_w - xpadding(left+right)`, height budget `box_h -
        # ypadding(top+bottom)`. `padding (a, b)` sets both axes; `padding (a, b,
        # c, d)` sets l/t/r/b; `xpadding N`/`ypadding N` set one axis (each side).
        # We do NOT subtract a Frame background's border (e.g. Frame("x", 8, 8)) —
        # that's 9-slice image scaling, not a text inset (verified in imagelike.py).
        pad_tuple = re.compile(r'^\s*padding\s*\(\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(\d+)\s*,\s*(\d+)\s*)?\)')
        xpad_re = re.compile(r'^\s*xpadding\s+(\d+)\b')
        ypad_re = re.compile(r'^\s*ypadding\s+(\d+)\b')
        boxes: dict[str, list[int]] = {}   # name -> [w, h, xpad, ypad]
        parents: dict[str, str] = {}
        for _file_rel, text in sources:
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                m = head.match(lines[i])
                if not m:
                    i += 1
                    continue
                name = m.group(2)
                if m.group(3):
                    parents.setdefault(name, m.group(3))
                indent = len(m.group(1))
                wh = boxes.setdefault(name, [0, 0, 0, 0])
                i += 1
                while i < len(lines):
                    l = lines[i]
                    if l.strip() and (len(l) - len(l.lstrip())) <= indent:
                        break
                    mt = xysize.match(l)
                    if mt:
                        wh[0] = wh[0] or int(mt.group(1))
                        wh[1] = wh[1] or int(mt.group(2))
                    else:
                        mx = xsize.match(l)
                        if mx:
                            wh[0] = wh[0] or int(mx.group(1))
                        my = ysize.match(l)
                        if my:
                            wh[1] = wh[1] or int(my.group(1))
                    pt = pad_tuple.match(l)
                    if pt:
                        # padding (a,b) -> xpad=a, ypad=b; padding(l,t,r,b) -> l+r, t+b.
                        if pt.group(3) is not None:
                            wh[2] = wh[2] or (int(pt.group(1)) + int(pt.group(3)))
                            wh[3] = wh[3] or (int(pt.group(2)) + int(pt.group(4)))
                        else:
                            wh[2] = wh[2] or (2 * int(pt.group(1)))
                            wh[3] = wh[3] or (2 * int(pt.group(2)))
                    xp = xpad_re.match(l)
                    if xp:
                        wh[2] = wh[2] or (2 * int(xp.group(1)))
                    yp = ypad_re.match(l)
                    if yp:
                        wh[3] = wh[3] or (2 * int(yp.group(1)))
                    im = is_line.match(l)
                    if im:
                        parents.setdefault(name, im.group(1))
                    i += 1
        # Inherit a missing axis/padding from the parent (one level — covers the
        # common `child is parent` + child-adds-geometry case).
        for name, parent in parents.items():
            pwh = boxes.get(parent)
            if not pwh:
                continue
            cwh = boxes.setdefault(name, [0, 0, 0, 0])
            for k in range(4):
                cwh[k] = cwh[k] or pwh[k]
        # Return (w, h) for back-compat callers; padding is exposed via the
        # parallel _style_box_padding map below (same keys), so the box tuple
        # stays a 2-tuple everywhere it's already consumed.
        self._style_box_padding = {n: (wh[2], wh[3]) for n, wh in boxes.items()}
        return {n: (wh[0], wh[1]) for n, wh in boxes.items()}

    def _screen_widget_boxes(
        self, sources, style_boxes: dict[str, tuple[int, int]],
        style_sizes: dict[str, int], body_size: int,
        style_spacing: dict[str, tuple[int, int]] | None = None,
    ) -> dict[tuple[str, int], tuple[int, int, int, int, int]]:
        """Map (file_rel, src_line) -> (box_w, box_h, text_size, line_spacing,
        line_leading) for every screen `textbutton`/`text`/`label` widget whose
        caption lives in a KNOWN fixed box (both width AND height resolvable). Only
        these can clip; everything else is omitted so inject leaves it untouched
        (auto-grow contract).

        A widget's box = its own inline `xysize/xsize/ysize` (wins) merged with the
        style it resolves to: an explicit `style "X"` on the widget, else the
        nearest enclosing `style_prefix` → `<prefix>_button`/`<prefix>` /`<prefix>_text`.
        The text size comes from `<style>_text` (via style_sizes) → body_size;
        line_spacing/line_leading from the resolved style (default 0) so the height
        prediction matches the engine even when a style tightens its line box."""
        style_spacing = style_spacing or {}
        out: dict[tuple[str, int], tuple[int, int, int, int, int]] = {}
        for file_rel, text in sources:
            lines = text.split("\n")
            # Stack of (indent, prefix) so a nested style_prefix shadows an outer.
            prefix_stack: list[tuple[int, str]] = []
            for idx, raw in enumerate(lines):
                if not raw.strip() or raw.lstrip().startswith("#"):
                    continue
                indent = len(raw) - len(raw.lstrip())
                # A style_prefix applies to its SIBLINGS (same indent) and deeper
                # lines, until we dedent STRICTLY past it. So pop only on a strict
                # dedent — same-indent siblings (the textbutton next to the prefix)
                # must keep it.
                while prefix_stack and indent < prefix_stack[-1][0]:
                    prefix_stack.pop()
                pm = self._PREFIX_REF_RE.match(raw)
                if pm:
                    # A new prefix at the same indent replaces the previous sibling
                    # prefix rather than nesting under it.
                    while prefix_stack and indent == prefix_stack[-1][0]:
                        prefix_stack.pop()
                    prefix_stack.append((indent, pm.group("prefix")))
                    continue
                wm = self._WIDGET_BOX_RE.match(raw)
                if not wm:
                    continue
                # Gather this widget's logical line (it + deeper-indented props,
                # e.g. `xsize 350` on the next line) so inline sizes are seen.
                blob = raw
                j = idx + 1
                while j < len(lines):
                    nxt = lines[j]
                    if not nxt.strip():
                        j += 1
                        continue
                    if (len(nxt) - len(nxt.lstrip())) <= indent:
                        break
                    blob += "\n" + nxt
                    j += 1

                box_w = box_h = 0
                mxy = self._INLINE_XYSIZE_RE.search(blob)
                if mxy:
                    box_w, box_h = int(mxy.group(1)), int(mxy.group(2))
                mx = self._INLINE_XSIZE_RE.search(blob)
                if mx:
                    box_w = box_w or int(mx.group(1))
                my = self._INLINE_YSIZE_RE.search(blob)
                if my:
                    box_h = box_h or int(my.group(1))

                # Resolve the widget's style: explicit `style "X"` else the active
                # style_prefix's button/base style. text/label/textbutton share the
                # prefix's box style in custom UIs.
                styles: list[str] = []
                sref = self._STYLE_REF_RE.search(blob)
                if sref:
                    styles.append(sref.group("style"))
                if prefix_stack:
                    pfx = prefix_stack[-1][1]
                    styles += [f"{pfx}_button", pfx]
                box_style = None
                for st in styles:
                    sw, sh = style_boxes.get(st, (0, 0))
                    if not box_w and sw:
                        box_w = sw
                    if not box_h and sh:
                        box_h = sh
                    if box_style is None and (sw or sh):
                        box_style = st
                    if box_w and box_h:
                        break

                if box_w <= 0 or box_h <= 0:
                    continue  # unknown box -> don't touch (contract)

                # Subtract the style's padding so (box_w, box_h) is the INNER text
                # area the engine actually wraps within (width - xpadding,
                # height - ypadding) — matching text.py `width - first_indent`.
                # Padding default is 0, so this is a no-op unless a style sets it.
                pad = getattr(self, "_style_box_padding", {})
                xpad = ypad = 0
                for st in styles:
                    px, py = pad.get(st, (0, 0))
                    xpad = xpad or px
                    ypad = ypad or py
                box_w = max(1, box_w - xpad)
                box_h = max(1, box_h - ypad)

                # Text size: <style>_text size for any resolved style, else body.
                tsize = 0
                for st in styles:
                    tsize = style_sizes.get(st)
                    if tsize:
                        break
                tsize = tsize or body_size

                # Line spacing/leading from the resolved style (default 0). These
                # can be negative (tightened line box) — the engine adds them to
                # the per-line height, so honoring them keeps the prediction exact.
                lsp = lld = 0
                for st in styles:
                    s_ls, s_ll = style_spacing.get(st, (0, 0))
                    if not lsp and s_ls:
                        lsp = s_ls
                    if not lld and s_ll:
                        lld = s_ll
                    if lsp and lld:
                        break
                out[(file_rel, idx + 1)] = (box_w, box_h, tsize, lsp, lld)
        return out

    def finalize_backups(self, root: str) -> None:
        """Delete files marked as ``type=created`` in backup metadata, except
        translation output (game/tl/) and the language switcher.

        Decompilements from archives are now done into a temp directory and
        cleaned up immediately after inject/extract, so there are no loose
        .rpy/.rpyc files left in game/ for this method to remove."""
        super().finalize_backups(root)

        backup_dir = os.path.join(root, ".interprex_backups")
        if not os.path.isdir(backup_dir):
            return

        metadata_path = os.path.join(backup_dir, "metadata.json")
        if not os.path.exists(metadata_path):
            return

        import json
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception:
            return

        for rel_path, info in metadata.items():
            if info.get("type") == "created":
                if rel_path.startswith("game/tl/") or rel_path == "game/interprex_language.rpy":
                    continue
                target_file = os.path.join(root, rel_path)
                if os.path.exists(target_file):
                    try:
                        os.remove(target_file)
                    except Exception:
                        pass
                if target_file.endswith(".rpy"):
                    rpyc_file = target_file + "c"
                    if os.path.exists(rpyc_file):
                        try:
                            os.remove(rpyc_file)
                        except Exception:
                            pass

    # --- tl/ formatting + writing ----------------------------------------
    @staticmethod
    def _lang_dir(target_lang: str | None) -> str:
        """Map a target-language code to the engine's tl/ directory name."""
        if not target_lang:
            return "russian"
        key = target_lang.strip().lower()
        return _RENPY_LANGS.get(key, key)

    @staticmethod
    def _tl_rel(file_rel: str, lang: str) -> str:
        """game/tl/<lang>/<path-relative-to-game>, forward slashes. Derived from
        the root-relative `file_rel` (works the same for loose and archived
        sources). For the universal `game/...` layout this strips the `game/`
        prefix; for the rare no-`game/` layout `detect` allows, it falls back to
        nesting the whole rel path under tl/."""
        if file_rel.startswith("game/"):
            rel_in_game = file_rel[len("game/"):]
            return f"game/tl/{lang}/{rel_in_game}"
        return f"game/tl/{lang}/{file_rel}"

    @staticmethod
    def _format_say_block(file_rel: str, rec: dict, lang: str, ident: str,
                          code: str, translation: str) -> str:
        """One say translate block. `code` is the engine get_code of the original
        (used verbatim as the reference comment); the translated line re-uses the
        same speaker/attrs with the translated text encoded as a say string.

        If the engine grouped translatable prefix nodes (voice/nvl) into this
        block, they're emitted exactly as the engine does: ALL get_code comments
        first, then ALL bodies. Prefix bodies are written VERBATIM (a voice line
        is not dialogue — nothing to translate); only the say body is translated.
        The block's `# file:line` header points at the FIRST node (the prefix if
        present), matching block[0].linenumber."""
        who = rec["who_var"]
        attrs = rec["attrs"]
        parts: list[str] = []
        if who:
            parts.append(who)
        parts.extend(attrs)
        parts.append(_encode_say_string(translation))
        tail = " nointeract" if rec["is_menu_caption"] else ""
        # Carry the say arguments through to the translated line verbatim, so the
        # block stays a valid runnable say (and matches the engine's own output).
        say_args = rec.get("say_args")
        if say_args:
            tail += " " + say_args
        translated_line = " ".join(parts) + tail

        prefix = rec.get("block_prefix") or []
        header_line = prefix[0][1] if prefix else rec["src_line"]
        prefix_codes = [c for c, _ in prefix]

        comment_lines = "".join(f"    # {c}\n" for c in prefix_codes) + f"    # {code}\n"
        body_lines = "".join(f"    {c}\n" for c in prefix_codes) + f"    {translated_line}\n"
        return (
            f"# {file_rel}:{header_line}\n"
            f"translate {lang} {ident}:\n"
            f"\n"
            f"{comment_lines}"
            f"{body_lines}"
        )

    def _write_tl_files(self, root: str, lang: str,
                        say_blocks: dict[str, list[str]],
                        string_entries: dict[str, list[tuple[str, str, str]]]) -> None:
        """Write each tl/ file: all say blocks first, then one strings block."""
        all_tl = set(say_blocks) | set(string_entries)
        for tl_rel in sorted(all_tl):
            chunks: list[str] = []
            for block in say_blocks.get(tl_rel, []):
                chunks.append(block)
            entries = string_entries.get(tl_rel, [])
            if entries:
                lines = [f"translate {lang} strings:\n"]
                for src_comment, old_raw, new_tr in entries:
                    lines.append("")
                    lines.append(f"    # {src_comment}")
                    lines.append(f"    old {_string_quote(old_raw)}")
                    lines.append(f"    new {_string_quote(new_tr)}")
                chunks.append("\n".join(lines) + "\n")
            content = "\n".join(chunks)
            abs_path = os.path.join(root, tl_rel.replace("/", os.sep))
            self._atomic_write(root, abs_path, content)

    def _write_language_file(self, root: str, lang: str) -> None:
        """Force the game to start in <lang>. init 999 runs after the game's own
        init, so config.language wins over the game's default."""
        rel = "game/interprex_language.rpy"
        abs_path = os.path.join(root, rel.replace("/", os.sep))
        content = (
            "# Added by Interprex — forces the translated language on every start.\n"
            "init 999 python:\n"
            f'    config.language = "{lang}"\n'
            f'\n'
            f'    _interprex_orig_label_cb = config.label_callback\n'
            f'    def _interprex_language_callback(name, abnormal):\n'
            f'        if not abnormal and _preferences.language != "{lang}":\n'
            f'            renpy.change_language("{lang}")\n'
            f'        if _interprex_orig_label_cb:\n'
            f'            _interprex_orig_label_cb(name, abnormal)\n'
            f'    config.label_callback = _interprex_language_callback\n'
            f'\n'
            f'    def _interprex_after_load():\n'
            f'        if _preferences.language != "{lang}":\n'
            f'            renpy.change_language("{lang}")\n'
            f'    config.after_load_callbacks.append(_interprex_after_load)\n'
        )
        self._atomic_write(root, abs_path, content)

    @staticmethod
    def _build_month_map_literal(lang: str) -> str:
        """Return a Python dict-literal mapping each English month name (the C-locale
        output of strftime %B/%b) to the target-language month name, for embedding in
        the generated _interprex_font.rpy. Empty dict for languages we have no table
        for (English target included), making the wrapper a no-op there.

        Full names are emitted before abbreviations so that, when a script's abbrev
        equals a prefix of the full name, dict insertion order keeps the full mapping;
        the regex in the wrapper is word-bounded so 'Mar' won't match inside 'March'.
        """
        key = (lang or "").strip().lower()
        if key not in _MONTHS_FULL:
            # Regional variants ("portuguese (brazil)", "chinese (simplified)")
            # share the base script's month names — fall back to the part before
            # "(", matching _normalize_lang's stance.
            key = key.split("(")[0].strip()
        full = _MONTHS_FULL.get(key)
        abbr = _MONTHS_ABBR.get(key)
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        if full:
            for en, tgt in zip(_MONTHS_EN_FULL, full):
                if en != tgt:
                    pairs.append((en, tgt))
                    seen.add(en)
        if abbr:
            for en, tgt in zip(_MONTHS_EN_ABBR, abbr):
                # English "May" is identical full and abbreviated; the full form
                # ("Май") must win for %B dates, so never let an abbr overwrite a
                # key already mapped from the full table.
                if en != tgt and en not in seen:
                    pairs.append((en, tgt))
                    seen.add(en)
        if not pairs:
            return "{}"
        items = ", ".join(f"{en!r}: {tgt!r}" for en, tgt in pairs)
        return "{" + items + "}"

    def _write_native_font(self, root: str, lang: str, font_name: str,
                           game_font_files: set[str] | None = None) -> None:
        """Point EVERY font the game uses at our font for <lang>, leaving the
        original untouched (a `translate <lang> python` block only runs when that
        language is active).

        The engine resolves any font name through config.font_name_map.get(name,
        name) (renpy/text/text.py), so mapping a name → our font catches it no
        matter how the game references it: by alias key (default_pixel_font…), or
        DIRECTLY by filename (font "X.ttf", gui.*_font = "X.ttf"). The direct-file
        fonts are the ones the alias-key path alone misses — exactly what renders
        chat/UI as tofu boxes when they lack the target script's glyphs. Mapping
        them all also means whichever of several selectable fonts the player picks
        resolves to ours — only our font is ever shown.

        ONLY plain-string mappings are emitted (no FontGroup). This is critical:
        this block re-runs every time the language/font is (re)applied in-game, and
        FontGroup.add() RAISES if its argument is already a font_name_map alias
        (renpy/text/font.py:888) — which ours become after the first run. Plain
        `map[name] = "fonts/X"` is idempotent, so re-running is always safe. We
        never remap our OWN font file (must resolve to itself) and skip emoji fonts
        so coloured glyphs survive."""
        game = os.path.join(root, "game")
        fonts_dir = os.path.join(game, "fonts")
        os.makedirs(fonts_dir, exist_ok=True)
        dst = os.path.join(fonts_dir, font_name)
        if not os.path.exists(dst):
            src = os.path.join(_ASSETS_FONTS, font_name)
            if os.path.exists(src):
                shutil.copy2(src, dst)

        # Every font filename the game references, mapped → our font. Skip our own
        # font (self-reference) and emoji fonts (Twemoji etc.) — those carry glyphs
        # our text font lacks, so remapping them would blank out emoji.
        ours = font_name.lower()
        emoji_markers = ("emoji", "twemoji", "twitter", "noto-emoji", "notoemoji")
        alias_lines: list[str] = []
        for ff in sorted(game_font_files or ()):
            low = ff.lower()
            if "*" in ff or "?" in ff:
                continue  # glob pattern (register_font_directory), not a real name
            if low == ours or low.endswith("/" + ours):
                continue  # never remap our font to itself
            if any(mk in low for mk in emoji_markers):
                continue  # keep emoji fonts intact
            esc = ff.replace("\\", "\\\\").replace('"', '\\"')
            alias_lines.append(
                f'        config.font_name_map["{esc}"] = "fonts/{font_name}"\n'
            )
        alias_block = "".join(alias_lines)

        rel = f"game/tl/{lang}/_interprex_font.rpy"
        abs_path = os.path.join(root, rel.replace("/", os.sep))
        content = (
            "# Added by Interprex — forces our font for this language (idempotent;\n"
            "# plain-string maps only, never FontGroup — see _write_native_font).\n"
            f"translate {lang} python:\n"
            f'    if not hasattr(config, "font_name_map"):\n'
            f'        config.font_name_map = {{}}\n'
            "    # Remap the game's built-in font aliases → our font.\n"
            f'    for _k in ("default_pixel_font", "bigger_pixel_font", "clean_font", "hyperlegible_font", "special_font"):\n'
            f'        if _k in config.font_name_map:\n'
            f'            config.font_name_map[_k] = "fonts/{font_name}"\n'
            "    # Remap every font the game references directly by filename → our font.\n"
            f"{alias_block}"
            "    # If a gui.* font is set DIRECTLY to a filename (not an alias),\n"
            "    # repoint it too. We must NOT overwrite an alias value (e.g.\n"
            "    # gui.text_font == 'default_pixel_font'): game styles branch sizes on\n"
            "    # that exact string, and the font_name_map remap already covers it.\n"
            f'    for _attr in ("text_font", "name_text_font", "interface_text_font"):\n'
            f'        try:\n'
            f'            _v = getattr(gui, _attr, None)\n'
            f'            if isinstance(_v, str) and _v.lower().rsplit(".", 1)[-1] in ("ttf", "ttc", "otf"):\n'
            f'                setattr(gui, _attr, "fonts/{font_name}")\n'
            f'        except Exception:\n'
            f'            pass\n'
            f'    renpy.notify("Interprex: шрифт переведённого текста подставлен автоматически")\n'
            f"\n"
            # Date localization. Games build dates via strftime("%B …")/"%b …";
            # the result flows through `text`/say -> translate_string. We DON'T
            # touch locale (setlocale(LC_TIME, "<lang>.UTF-8") returns OS-codepage
            # bytes Python mis-decodes on Windows -> "Декабрь" mojibake, the real
            # bug). Instead we hand the translate_string wrapper a deterministic
            # English->target month map and let it substitute. Empty for langs we
            # have no table for (English unchanged), so the wrapper is a no-op there.
            f"    _ix_month_map = {self._build_month_map_literal(lang)}\n"
            f"\n"
            f"    # Style overrides for choice buttons (auto height and subtitle layout fallback)\n"
            f"    style.choice_button.ysize = None\n"
            f"    style.choice_button_text.layout = 'subtitle'\n"
            f"\n"
            f"    # Translate dynamic @ping chat messages BEFORE substitution (Killer\n"
            f"    # Chat!). CRITICAL: we patch add_ping_hyperlinks IN PLACE (swap its\n"
            f"    # __code__) instead of REBINDING the store name. Ren'Py pickles store\n"
            f"    # functions BY REFERENCE and verifies identity on save; rebinding the\n"
            f"    # name makes the saved object != store.add_ping_hyperlinks ->\n"
            f"    # PicklingError -> EVERY save crashes (real player bug, 0 progress).\n"
            f"    # In-place keeps the SAME function object, so saves stay valid. The\n"
            f"    # original behaviour is preserved via a clone; renpy is captured as a\n"
            f"    # default arg so the swapped code needs no globals. Defensive: if the\n"
            f"    # function carries free variables the __code__ swap is impossible, so\n"
            f"    # we skip it (ping-translation off) rather than ever crash.\n"
            f"    # Resolve the function: prefer store.add_ping_hyperlinks (Ren'Py's\n"
            f"    # canonical namespace) over globals() (which may be a copy in\n"
            f"    # translate blocks). Pickling verifies against store.* identity.\n"
            f"    _ix_store = globals().get('store', None)\n"
            f"    _ix_aph = getattr(_ix_store, 'add_ping_hyperlinks', None) if _ix_store else None\n"
            f"    if _ix_aph is None and 'add_ping_hyperlinks' in globals():\n"
            f"        _ix_aph = globals()['add_ping_hyperlinks']\n"
            f"    if _ix_aph is not None and callable(_ix_aph):\n"
            f"        import types as _ix_types\n"
            f"        if not getattr(_ix_aph, '_patched_by_interprex', False):\n"
            f"            try:\n"
            f"                # The clone of the ORIGINAL must get a UNIQUE name. If it\n"
            f"                # kept the original's __name__ ('add_ping_hyperlinks') it\n"
            f"                # would, as a store var in this translate-python block,\n"
            f"                # be pickled BY REFERENCE on save -> getattr(store,\n"
            f"                # 'add_ping_hyperlinks') returns the PATCHED original (a\n"
            f"                # different object) -> 'not the same object as store.\n"
            f"                # add_ping_hyperlinks' PicklingError -> saves crash. A\n"
            f"                # unique name (matching the store var) makes pickle-by-\n"
            f"                # reference resolve to the clone itself, so saves work.\n"
            f"                _interprex_orig_aph = _ix_types.FunctionType(\n"
            f"                    _ix_aph.__code__, _ix_aph.__globals__,\n"
            f"                    '_interprex_orig_aph',\n"
            f"                    _ix_aph.__defaults__, _ix_aph.__closure__)\n"
            f"                try:\n"
            f"                    _interprex_orig_aph.__qualname__ = '_interprex_orig_aph'\n"
            f"                except Exception:\n"
            f"                    pass\n"
            f"                def _ix_aph_wrapper(new_text, _orig=_interprex_orig_aph, _renpy=renpy):\n"
            f"                    if not (_renpy.game.lint or _renpy.predicting()):\n"
            f"                        new_text = _renpy.translation.translate_string(new_text)\n"
            f"                    return _orig(new_text)\n"
            f"                _ix_aph.__code__ = _ix_aph_wrapper.__code__\n"
            f"                _ix_aph.__defaults__ = _ix_aph_wrapper.__defaults__\n"
            f"                _ix_aph._patched_by_interprex = True\n"
            f"            except Exception:\n"
            f"                pass\n"
            f"\n"
            f"    # Patch to dynamically translate ServerRole names and ChatCharacter dominant_roles (for profile traits)\n"
            f"    if 'ServerRole' in globals() or 'ChatCharacter' in globals():\n"
            f"        if not globals().get('_translating_string_class_defined', False):\n"
            f"            class TranslatingString(str):\n"
            f"                def __new__(cls, english_val):\n"
            f"                    obj = str.__new__(cls, english_val)\n"
            f"                    obj.english_val = english_val\n"
            f"                    return obj\n"
            f"                @property\n"
            f"                def russian_val(self):\n"
            f"                    try:\n"
            f"                        import renpy\n"
            f"                    except ImportError:\n"
            f"                        renpy = globals().get('renpy')\n"
            f"                    try:\n"
            f"                        val = renpy.translation.translate_string(self.english_val)\n"
            f"                        if val:\n"
            f"                            return val\n"
            f"                    except Exception:\n"
            f"                        pass\n"
            f"                    return self.english_val\n"
            f"                def __str__(self):\n"
            f"                    return self.russian_val\n"
            f"                def __repr__(self):\n"
            f"                    return repr(self.russian_val)\n"
            f"                def __eq__(self, other):\n"
            f"                    # Use type(self), NOT the bare class name: a method's\n"
            f"                    # global lookup of TranslatingString fails inside a\n"
            f"                    # translate-python exec block (NameError) — type(self)\n"
            f"                    # resolves the class with no global dependency.\n"
            f"                    if isinstance(other, type(self)):\n"
            f"                        return self.english_val == other.english_val\n"
            f"                    return self.english_val == other or self.russian_val == other\n"
            f"                def __ne__(self, other):\n"
            f"                    return not self.__eq__(other)\n"
            f"                def __hash__(self):\n"
            f"                    return hash(self.english_val)\n"
            f"                def __add__(self, other):\n"
            f"                    return self.russian_val + str(other)\n"
            f"                def __radd__(self, other):\n"
            f"                    return str(other) + self.russian_val\n"
            f"                def title(self):\n"
            f"                    return self.russian_val.title()\n"
            f"                def lower(self):\n"
            f"                    return self.russian_val.lower()\n"
            f"                def upper(self):\n"
            f"                    return self.russian_val.upper()\n"
            f"            globals()['TranslatingString'] = TranslatingString\n"
            f"            globals()['_translating_string_class_defined'] = True\n"
            f"        else:\n"
            f"            TranslatingString = globals()['TranslatingString']\n"
            f"\n"
            f"        if 'ServerRole' in globals():\n"
            f"            _ServerRole = globals()['ServerRole']\n"
            f"            if not hasattr(_ServerRole, '_patched_by_interprex'):\n"
            f"                def _get_role_name(self, _TS=TranslatingString):\n"
            f"                    raw = self.__dict__.get('name', '')\n"
            f"                    if isinstance(raw, str) and not isinstance(raw, _TS):\n"
            f"                        return _TS(raw)\n"
            f"                    return raw\n"
            f"                def _set_role_name(self, val):\n"
            f"                    self.__dict__['name'] = val\n"
            f"                _ServerRole.name = property(_get_role_name, _set_role_name)\n"
            f"                _ServerRole._patched_by_interprex = True\n"
            f"\n"
            f"        if 'ChatCharacter' in globals():\n"
            f"            _ChatCharacter = globals()['ChatCharacter']\n"
            f"            if not hasattr(_ChatCharacter, '_patched_by_interprex'):\n"
            f"                def _get_dominant_role(self, _TS=TranslatingString):\n"
            f"                    raw = self.__dict__.get('dominant_role', '')\n"
            f"                    if isinstance(raw, str) and not isinstance(raw, _TS):\n"
            f"                        return _TS(raw)\n"
            f"                    return raw\n"
            f"                def _set_dominant_role(self, val):\n"
            f"                    self.__dict__['dominant_role'] = val\n"
            f"                _ChatCharacter.dominant_role = property(_get_dominant_role, _set_dominant_role)\n"
            f"                _ChatCharacter._patched_by_interprex = True\n"
            f"\n"
            f"        if 'ChatChannel' in globals():\n"
            f"            _ChatChannel = globals()['ChatChannel']\n"
            f"            if not hasattr(_ChatChannel, '_patched_by_interprex_typing'):\n"
            f"                def _get_who_typing_translated(self):\n"
            f"                    if not self.people_typing:\n"
            f"                        return ''\n"
            f"                    try:\n"
            f"                        import renpy\n"
            f"                    except ImportError:\n"
            f"                        renpy = globals().get('renpy')\n"
            f"                    lang = renpy.game.preferences.language\n"
            f"                    is_ru = (lang == 'russian')\n"
            f"                    if len(self.people_typing) == 1:\n"
            f"                        suffix = ' пишет...' if is_ru else ' is typing...'\n"
            f"                        return self.people_typing[0].username + suffix\n"
            f"                    elif len(self.people_typing) >= 4:\n"
            f"                        return 'Несколько человек пишут...' if is_ru else 'Several people are typing...'\n"
            f"                    else:\n"
            f"                        and_word = ' и ' if is_ru else ' and '\n"
            f"                        comma = ', '\n"
            f"                        typer_names_string = ''\n"
            f"                        for i, typing_person in enumerate(self.people_typing):\n"
            f"                            if i > 0:\n"
            f"                                if i == len(self.people_typing)-1:\n"
            f"                                    typer_names_string += and_word + typing_person.username\n"
            f"                                else:\n"
            f"                                    typer_names_string += comma + typing_person.username\n"
            f"                            else:\n"
            f"                                typer_names_string = typing_person.username\n"
            f"                        suffix = ' пишут...' if is_ru else ' are typing...'\n"
            f"                        return typer_names_string + suffix\n"
            f"                _ChatChannel.get_who_typing = property(_get_who_typing_translated)\n"
            f"                _ChatChannel._patched_by_interprex_typing = True\n"
            f"\n"
            # NOTE: top-level (4-space) — NOT under the ServerRole/ChatCharacter
            # gate. It must run for EVERY game so date localization + number-prefix
            # choice translation work universally, not only on Killer Chat.
            f"    # Patch renpy.translation.translate_string to ALSO (a) localize dates the\n"
            f"    # game builds via strftime('%B'/'%b') by substituting English month names\n"
            f"    # for the target ones (deterministic, NO locale — setlocale gives\n"
            f"    # OS-codepage mojibake on Windows), and (b) translate choices prefixed with\n"
            f"    # a number ('1. ...'). CRITICAL: patch IN PLACE via __code__ swap, NEVER\n"
            f"    # rebind renpy.translation.translate_string. The engine captures the\n"
            f"    # ORIGINAL translate_string object into the save graph (via\n"
            f"    # show_display_say's show_function); pickle saves functions BY REFERENCE\n"
            f"    # and verifies identity, so a rebind makes the saved object !=\n"
            f"    # renpy.translation.translate_string -> PicklingError on EVERY save (real\n"
            f"    # shipped player crash, Killer Chat 1.4.1). In-place keeps the SAME object,\n"
            f"    # so saves stay valid. The clone of the original gets a UNIQUE name and is\n"
            f"    # registered on renpy.translation so that, when it too is reached by pickle\n"
            f"    # from the save graph, the by-reference lookup resolves to itself instead of\n"
            f"    # raising 'attribute lookup failed'. _orig/_lookup/_mre are keyword-only\n"
            f"    # (after *args) so their defaults live in __kwdefaults__, not __defaults__.\n"
            f"    _ix_ts = getattr(renpy.translation, 'translate_string', None)\n"
            f"    if callable(_ix_ts) and not getattr(renpy.translation, '_patched_by_interprex_choices', False):\n"
            f"        import types as _ix_ts_types, re as _ix_ts_re\n"
            f"        _ix_month_lookup = dict(_ix_month_map)\n"
            f"        _ix_month_re = None\n"
            f"        if _ix_month_lookup:\n"
            f"            try:\n"
            f"                _ix_month_re = _ix_ts_re.compile(r'\\b(' + '|'.join(_ix_ts_re.escape(_k) for _k in _ix_month_lookup) + r')\\b')\n"
            f"            except Exception:\n"
            f"                _ix_month_re = None\n"
            f"        try:\n"
            f"            _interprex_orig_translate_string = _ix_ts_types.FunctionType(\n"
            f"                _ix_ts.__code__, _ix_ts.__globals__,\n"
            f"                '_interprex_orig_translate_string',\n"
            f"                _ix_ts.__defaults__, _ix_ts.__closure__)\n"
            f"            try:\n"
            f"                _interprex_orig_translate_string.__qualname__ = '_interprex_orig_translate_string'\n"
            f"            except Exception:\n"
            f"                pass\n"
            f"            renpy.translation._interprex_orig_translate_string = _interprex_orig_translate_string\n"
            f"            def _ix_ts_wrapper(s, *args, _orig=_interprex_orig_translate_string, _lookup=_ix_month_lookup, _mre=_ix_month_re, **kwargs):\n"
            f"                res = _orig(s, *args, **kwargs)\n"
            f"                # Only post-process strings the engine had NO tl/ translation\n"
            f"                # for (res == s): already-translated text must stay verbatim,\n"
            f"                # and dynamically-built strings (dates, numbered choices) are\n"
            f"                # exactly the ones that come back unchanged.\n"
            f"                if res == s and s:\n"
            f"                    try:\n"
            f"                        import re as _re2\n"
            f"                        m = _re2.match(r'^(\\d+[\\.\\)]\\s*)(.*)$', s)\n"
            f"                        if m:\n"
            f"                            prefix, rest = m.groups()\n"
            f"                            translated_rest = _orig(rest, *args, **kwargs)\n"
            f"                            if translated_rest != rest:\n"
            f"                                return prefix + translated_rest\n"
            f"                    except Exception:\n"
            f"                        pass\n"
            f"                    if _mre is not None:\n"
            f"                        try:\n"
            f"                            return _mre.sub(lambda mm: _lookup.get(mm.group(0), mm.group(0)), res)\n"
            f"                        except Exception:\n"
            f"                            pass\n"
            f"                return res\n"
            f"            _ix_ts.__code__ = _ix_ts_wrapper.__code__\n"
            f"            _ix_ts.__defaults__ = _ix_ts_wrapper.__defaults__\n"
            f"            _ix_ts.__kwdefaults__ = _ix_ts_wrapper.__kwdefaults__\n"
            f"            renpy.translation._patched_by_interprex_choices = True\n"
            f"        except Exception:\n"
            f"            pass\n"
        )
        self._atomic_write(root, abs_path, content)

    @staticmethod
    def _parse_style_text_sizes(sources) -> dict[str, int]:
        """Map style_prefix -> font size declared in its `style <prefix>_text:`
        block, scanned from the .rpy sources themselves.

        Custom-UI games (e.g. Killer Chat!) ship NO gui.rpy and bake per-element
        sizes straight into style blocks (quick_button_text=21, ovk_moments=72,
        …). gui.rpy alone can't see those, so we read the style blocks directly —
        this is what lets the override be game-relative on a custom UI.

        A literal `size N` inside the block wins. When a style declares none but
        inherits via `style X_text is Y_text`, we follow that parent ONE level
        (covers the common `quick_button_text is button_text`); deeper chains fall
        through to the body-size default, which the re-ask path still covers."""
        # Capture the optional `is <parent>` so we can inherit a size one level.
        _STYLE_DEF_RE = re.compile(
            r'^([ \t]*)style\s+(\w[\w.]*)_text\b(?:\s+is\s+(\w[\w.]*?)(?:_text)?\b)?.*:\s*(?:#.*)?$'
        )
        _SIZE_RE = re.compile(r'^\s*size\s+(\d+)\b')
        _SIZE_EXPR_RE = re.compile(r'^\s*size\s+\(')
        _LAST_INT_RE = re.compile(r'(\d+)\s*\)*\s*$')
        _IS_RE = re.compile(r'^\s+is\s+(\w[\w.]*)\b')
        sizes: dict[str, int] = {}
        parents: dict[str, str] = {}  # prefix -> parent prefix (from `is`)
        for _file_rel, text in sources:
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                m = _STYLE_DEF_RE.match(lines[i])
                if not m:
                    i += 1
                    continue
                prefix = m.group(2)
                if m.group(3):
                    parents.setdefault(prefix, m.group(3))
                block_indent = len(m.group(1))
                i += 1
                while i < len(lines):
                    l = lines[i]
                    if l.strip() and (len(l) - len(l.lstrip())) <= block_indent:
                        break  # dedent ends the block
                    zm = _SIZE_RE.match(l)
                    if zm:
                        # First explicit size wins; keep the largest if a prefix's
                        # _text style appears more than once (be conservative).
                        sizes[prefix] = max(sizes.get(prefix, 0), int(zm.group(1)))
                    elif _SIZE_EXPR_RE.match(l):
                        # Ternary like `size (55 if ... else 29)` — take the last
                        # integer before the closing paren, which is the default
                        # (else) branch value.
                        em = _LAST_INT_RE.search(l)
                        if em:
                            sizes[prefix] = max(sizes.get(prefix, 0), int(em.group(1)))
                    elif not parents.get(prefix):
                        # `is Parent` on a separate line inside the block (common
                        # in Killer Chat: `style X:\n    is Y`). Capture it so the
                        # one-level inheritance below can resolve the size.
                        im = _IS_RE.match(l)
                        if im:
                            # Strip trailing `_text` to match the prefix namespace
                            # used by _STYLE_DEF_RE (startup_text -> startup).
                            pname = im.group(1)
                            if pname.endswith('_text'):
                                pname = pname[:-5]
                            parents.setdefault(prefix, pname)
                    i += 1

        # Resolve one level of `is` inheritance for styles without their own size.
        for prefix, parent in parents.items():
            if prefix not in sizes and parent in sizes:
                sizes[prefix] = sizes[parent]
        return sizes

    @staticmethod
    def _parse_style_line_spacing(sources) -> dict[str, tuple[int, int]]:
        """Map style name -> (line_spacing, line_leading) from its `style <name>:`
        block, resolving one-level `is <parent>`. Both default 0. These can be
        NEGATIVE (games tighten line boxes, e.g. `line_spacing -8` on a 2-line
        confirm button) — and the engine adds them to the FreeType ascent+descent
        per line (place_vertical), so honoring them is what makes our height
        prediction EXACT instead of over-shrinking a deliberately-tightened box.
        Keyed by the BASE style name (no `_text` suffix) to match style_boxes."""
        head = re.compile(
            r'^([ \t]*)style\s+(\w[\w.]*?)(?:_text)?\b(?:\s+is\s+(\w[\w.]*?)(?:_text)?)?\s*:\s*(?:#.*)?$'
        )
        ls_re = re.compile(r'^\s*line_spacing\s+(-?\d+)\b')
        ll_re = re.compile(r'^\s*line_leading\s+(-?\d+)\b')
        is_re = re.compile(r'^\s+is\s+(\w[\w.]*?)(?:_text)?\b')
        vals: dict[str, list] = {}     # name -> [ls, ll, ls_set, ll_set]
        parents: dict[str, str] = {}
        for _file_rel, text in sources:
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                m = head.match(lines[i])
                if not m:
                    i += 1
                    continue
                name = m.group(2)
                if m.group(3):
                    parents.setdefault(name, m.group(3))
                indent = len(m.group(1))
                v = vals.setdefault(name, [0, 0, False, False])
                i += 1
                while i < len(lines):
                    l = lines[i]
                    if l.strip() and (len(l) - len(l.lstrip())) <= indent:
                        break
                    ms = ls_re.match(l)
                    if ms and not v[2]:
                        v[0] = int(ms.group(1)); v[2] = True
                    ml = ll_re.match(l)
                    if ml and not v[3]:
                        v[1] = int(ml.group(1)); v[3] = True
                    mi = is_re.match(l)
                    if mi:
                        parents.setdefault(name, mi.group(1))
                    i += 1
        # One-level inheritance for whichever axis the child didn't set itself.
        for name, parent in parents.items():
            pv = vals.get(parent)
            if not pv:
                continue
            cv = vals.setdefault(name, [0, 0, False, False])
            if not cv[2]:
                cv[0] = pv[0]
            if not cv[3]:
                cv[1] = pv[1]
        return {n: (v[0], v[1]) for n, v in vals.items()}

    def _generate_style_overrides(self, root: str, lang: str,
                                  measured_factors: dict[str, float] | None = None) -> None:
        """For screens with fixed-height frames, inject font size reductions
        in tl/<lang>/ so translated text fits without overflowing.

        Sizes are GAME-RELATIVE and per-style: the original size of each prefix
        is resolved as (its own `style <prefix>_text:` size in the .rpy) →
        (gui.<prefix>_text_size) → (gui.text_size) → game body. We NEVER write a
        size larger than that original, so a UI whose text was already small is
        left untouched instead of being inflated (the old hardcoded 22 bug). The
        shrink target is the game's own body size; for custom-UI games with no
        gui.rpy, body is inferred from the most common `_text` style size in the
        game itself — still fully game-derived, no magic constant.

        measured_factors maps a style prefix -> a shrink factor (<1.0) MEASURED
        from real pixel overflow by the scheduler (the hybrid fit's last resort,
        for captions the model couldn't shorten enough). The final size per style
        is min(static body-shrink, original*measured_factor) — whichever is
        smaller — so a genuinely-overflowing caption gets exactly enough shrink
        even when the static path alone wouldn't have touched it."""
        measured_factors = measured_factors or {}
        # Fixed-extent containers. Beyond xysize(W,H): a tuple form via maximum(),
        # and the single-axis forms ysize/ymaximum/xsize/xmaximum. Literal ints
        # only — `ysize gui.foo` (a variable) is skipped on purpose (we can't know
        # the size statically; the re-ask-shorter path still keeps text fitting).
        _BOX_TUPLE_RE = re.compile(
            r'^\s*(?:xysize|maximum)\s*\(\s*\d+\s*,\s*(\d+)\s*\)'
        )
        _BOX_AXIS_RE = re.compile(
            r'^\s*(?:ysize|ymaximum|xsize|xmaximum)\s+(\d+)\b'
        )
        # Height-only: for frame_heights we must NOT count xsize/xmaximum as
        # container height (a narrow width ≠ a short frame).
        _BOX_HEIGHT_RE = re.compile(
            r'^\s*(?:ysize|ymaximum)\s+(\d+)\b'
        )
        _SCREEN_DEF_RE = re.compile(
            r'^(\s*)screen\s+(\w[\w.]*)\s*(?:\(([^)]*)\))?\s*:'
        )
        _STYLE_PREFIX_RE = re.compile(
            r'^\s+style_prefix\s+["\']?(\w[\w.]*)["\']?'
        )
        # A text/textbutton/label widget naming its own style explicitly, e.g.
        # `text "Hi" style "foo_text"` or `textbutton _("X") text_style "bar_text"`.
        # Lets us cover screens that set no style_prefix. We strip a trailing
        # `_text` so it joins the same prefix space as style_prefix.
        _WIDGET_STYLE_RE = re.compile(
            r'^\s*(?:text|textbutton|label)\b.*?\b(?:text_style|style)\s+["\'](\w[\w.]*?)(?:_text)?["\']'
        )

        # Materialize sources once: we scan them for both screens (below) and
        # explicit style sizes (custom-UI games bake sizes into style blocks).
        all_sources = list(self._iter_sources(root))

        # Per-style explicit sizes read straight from the .rpy style blocks.
        style_sizes = self._parse_style_text_sizes(all_sources)

        # Resolve the game's body text size — the target we shrink an oversized
        # fixed-height caption DOWN to. Priority:
        #   1) gui.text_size from gui.rpy (loose or archived), the canonical value;
        #   2) for custom-UI games with no gui.rpy, the MOST COMMON explicit _text
        #      style size in the game (its de-facto body size);
        #   3) a conservative last resort only when neither exists.
        _, _gui_ints = parse_gui_rpy(root)
        body_size = _gui_ints.get("text_size")
        if not body_size and style_sizes:
            from collections import Counter
            body_size = Counter(style_sizes.values()).most_common(1)[0][0]
        if not body_size:
            body_size = 22

        style_overrides: dict[str, set[str]] = {}
        # Per-prefix frame geometry — drives the tight-frame shrink below
        # body_size when a fixed-height container is too small for translated text.
        # frame_heights: max frame height per prefix (for backwards compat).
        # frame_dims: (frame_w, frame_h, pad_x, pad_y) from the OUTERMOST
        # xysize(W,H) + padding(N,M) in the screen — drives PIL-based measurement.
        frame_heights: dict[str, int] = {}
        frame_dims: dict[str, tuple[int, int, int, int]] = {}
        _BOX_TUPLE_SCAN = re.compile(
            r'^\s*(?:xysize|maximum)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)'
        )
        _PADDING_RE = re.compile(
            r'padding\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)'
        )

        for file_rel, text in all_sources:
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                sm = _SCREEN_DEF_RE.match(lines[i])
                if sm:
                    screen_start = i
                    screen_indent = len(lines[i]) - len(lines[i].lstrip())
                    i += 1
                    while i < len(lines):
                        l = lines[i]
                        if l.strip() and not l[0].isspace():
                            break
                        if l.strip() and (len(l) - len(l.lstrip())) <= screen_indent and not l.lstrip().startswith("#"):
                            break
                        i += 1
                    screen_body = lines[screen_start:i]
                    has_fixed_height = False
                    prefixes: set[str] = set()
                    max_frame_h = 0
                    first_frame_w = 0
                    frame_pad_x = 0
                    frame_pad_y = 0
                    for sl in screen_body:
                        mt = _BOX_TUPLE_RE.match(sl)
                        if mt and int(mt.group(1)) > 0:
                            has_fixed_height = True
                            h = int(mt.group(1))
                            if h > max_frame_h:
                                max_frame_h = h
                            if first_frame_w == 0:
                                ms = _BOX_TUPLE_SCAN.match(sl)
                                if ms:
                                    first_frame_w = int(ms.group(1))
                        ma = _BOX_AXIS_RE.match(sl)
                        if ma and int(ma.group(1)) > 0:
                            has_fixed_height = True
                        mh = _BOX_HEIGHT_RE.match(sl)
                        if mh and int(mh.group(1)) > 0:
                            h = int(mh.group(1))
                            if h > max_frame_h:
                                max_frame_h = h
                        pm = _PADDING_RE.search(sl)
                        if pm and frame_pad_x == 0:
                            frame_pad_x = int(pm.group(1))
                            frame_pad_y = int(pm.group(2))
                        sp = _STYLE_PREFIX_RE.match(sl)
                        if sp:
                            prefixes.add(sp.group(1))
                        # A widget naming its own style covers screens with no
                        # style_prefix at all (3b).
                        wm = _WIDGET_STYLE_RE.match(sl)
                        if wm:
                            prefixes.add(wm.group(1))
                    if has_fixed_height and prefixes:
                        for p in prefixes:
                            style_overrides.setdefault(p, set()).add(f"style {p}_text:")
                            if max_frame_h > 0:
                                cur = frame_heights.get(p)
                                if cur is None or max_frame_h > cur:
                                    frame_heights[p] = max_frame_h
                            if first_frame_w > 0 and max_frame_h > 0:
                                existing = frame_dims.get(p)
                                if existing is None or max_frame_h > existing[1]:
                                    frame_dims[p] = (first_frame_w, max_frame_h, frame_pad_x, frame_pad_y)
                else:
                    i += 1

        # Styles with a MEASURED overflow factor must be emitted even if they
        # never appeared in a static fixed-height screen (e.g. choice_button,
        # whose box the engine sizes itself — the static scan can't see it).
        for p in measured_factors:
            style_overrides.setdefault(p, set())

        if not style_overrides:
            return

        parts = [
            "# Auto-generated by Interprex: shrinks oversized text inside\n"
            "# fixed-height screens to THIS game's own body text size so the\n"
            "# translation fits. Never enlarges; styles already at/below the\n"
            "# body size are left untouched (no override emitted).\n"
        ]
        emitted = 0
        for prefix in sorted(style_overrides):
            # The style's own ORIGINAL size, resolved per style:
            #   its explicit `style <prefix>_text:` size in the .rpy
            #   → gui.<prefix>_text_size → gui.text_size → body.
            # If we can't read a real size for this style, we DON'T override it
            # (orig falls to body → reduced == orig → skipped): never guess a size
            # we can't see, so we never inflate an inherited small style.
            orig = (
                style_sizes.get(prefix)
                or _gui_ints.get(f"{prefix}_text_size")
                or _gui_ints.get("text_size")
                or body_size
            )
            # Static path: shrink to the game's body size (never enlarge).
            reduced = min(orig, body_size)
            # Tight-frame path: when the style lives inside a small fixed-height
            # frame, shrink BELOW body_size by measuring how many lines of
            # representative Russian text fit in the frame at body_size via PIL,
            # then computing the exact shrink to fit. Falls back to heuristic if
            # PIL is unavailable. Only frames >= 400 px — smaller values are UI chrome.
            fd = frame_dims.get(prefix)
            if fd and 400 <= fd[1] < 600:
                fw, fh, px, py = fd
                inner_w = max(1, fw - 2 * px)
                # Non-text area: padding + buttons/nulls ≈ 35% of frame height
                # (empirical: screens typically dedicate ~65% to text content)
                text_budget_h = max(1, int((fh - 2 * py) * 0.65))
                tight_size = body_size
                try:
                    font = _target_font("russian", body_size)
                    # Representative Russian screen text: 3 paragraphs of typical
                    # dialog-screen length (~50 words each). Must match the
                    # density of real translated screens to produce accurate shrink.
                    sample = (
                        "Благодарим за терпение.\n\n"
                        "Для обеспечения безопасности вашего аккаунта требуется "
                        "дополнительное подтверждение личности через нашу новую "
                        "систему косметической перекрестной проверки.\n\n"
                        "Чтобы подтвердить, что вы не робот, и перейти к "
                        "следующему этапу, пожалуйста, нажмите правую кнопку."
                    )
                    lines = _wrapped_line_count(sample, font, inner_w)
                    needed_h = lines * _line_height(font, body_size)
                    if needed_h > text_budget_h:
                        factor = text_budget_h / needed_h
                        tight_size = max(12, int(body_size * factor))
                except Exception:
                    # PIL unavailable: fallback to heuristic
                    tight_size = max(12, int(body_size * (fh / 550)))
                reduced = min(reduced, tight_size)
            # Measured path: if the scheduler saw this style's text actually
            # overflow, shrink by the exact measured factor too. Floor at 12px so
            # text stays legible; take whichever path yields the smaller size.
            factor = measured_factors.get(prefix)
            if factor and factor < 1.0:
                measured_size = max(12, int(orig * factor))
                reduced = min(reduced, measured_size)
            if reduced >= orig:
                continue  # already <= original — don't inflate, skip override
            parts.append(
                f"style {prefix}_text:\n"
                f"    size {reduced}\n"
            )
            emitted += 1

        if emitted == 0:
            return  # nothing oversized to fix in this game

        tl_rel = f"game/tl/{lang}/_ui_style_fixes.rpy"
        abs_path = os.path.join(root, tl_rel.replace("/", os.sep))
        self._atomic_write(root, abs_path, "\n".join(parts))
        logger.info("Generated style overrides: %s (%d prefixes)", tl_rel, emitted)

    def _atomic_write(self, root: str, abs_path: str, content: str) -> None:
        """Write content to abs_path atomically (tempfile + os.replace). Backs up
        an existing file first; new files need no backup (the original is never
        touched)."""
        import tempfile
        import time

        if os.path.exists(abs_path):
            self.backup_file(root, abs_path)
        else:
            from .base import update_metadata
            try:
                rel_path = os.path.relpath(abs_path, root).replace("\\", "/")
                update_metadata(root, rel_path, "", "", "created")
            except Exception:
                pass

        dir_name = os.path.dirname(abs_path)
        os.makedirs(dir_name, exist_ok=True)
        data = content.encode("utf-8")
        with tempfile.NamedTemporaryFile("wb", dir=dir_name, prefix="tmp_tl_", delete=False) as tf:
            tf.write(data)
            temp_path = tf.name
        try:
            delays = [0.1, 0.2, 0.4, 0.8]
            for attempt, delay in enumerate(delays):
                try:
                    os.replace(temp_path, abs_path)
                    break
                except PermissionError:
                    if attempt == len(delays) - 1:
                        raise
                    time.sleep(delay)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    # --- font helpers -----------------------------------------------------
    @staticmethod
    def _detect_font(text: str, font_style: str = "smooth") -> str | None:
        """Return the bundled font filename that covers the script in *text*,
        or None if plain Latin (original game font is fine). With
        font_style="pixel" the bitmap variants are used where one exists
        (Cyrillic→PixelOperator, ja/zh→Zpix); Korean and pixel-less scripts
        still resolve to their smooth Noto."""
        detectors = (
            _PIXEL_SCRIPT_DETECTORS if font_style == "pixel" else _SCRIPT_DETECTORS
        )
        for pattern, font_name in detectors:
            if pattern.search(text):
                return font_name
        return None

    def _id(self, file: str, path: list[str], original: str) -> str:
        # Use the same id the string was extracted with.
        from .base import make_id
        return make_id(self.engine, file, path, original)


# --- Automatic Menu Character Limit Calculation ---

ALPHABET_SAMPLES = {
    "russian":    "абвгдежзийклмнопрстуфхцчшщыьэюя ",
    "english":    "abcdefghijklmnopqrstuvwxyz ",
    "spanish":    "abcdefghijklmnñopqrstuvwxyzáéíóúü ",
    "german":     "abcdefghijklmnopqrstuvwxyzäöüß ",
    "french":     "abcdefghijklmnopqrstuvwxyzàâæçéèêëîïôœùûü ",
    "japanese":   "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん ",
    "chinese":    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种",
    "korean":     "가나다라마바사아자차카타파하갈날달랄말발살알잘찰칼탈팔할 ",
    "portuguese": "abcdefghijklmnopqrstuvwxyzáâãàéêíóôõúüç ",
}

LANG_FONTS = {
    "russian":    os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"),
    "english":    os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"),
    "spanish":    os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"),
    "german":     os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"),
    "french":     os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"),
    "japanese":   os.path.join(_ASSETS_FONTS, "NotoSansCJK-Regular.ttc"),
    "chinese":    os.path.join(_ASSETS_FONTS, "NotoSansCJK-Regular.ttc"),
    "korean":     os.path.join(_ASSETS_FONTS, "NotoSansCJK-Regular.ttc"),
    "portuguese": os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"),
}

# Pixel-style fonts per script, mirroring LANG_FONTS. The width we MEASURE must
# match the font inject WRITES, or the UI-fitting budget drifts — so the same
# pixel/smooth choice flows into both. Korean has no pixel hangul (Zpix lacks it)
# → stays on the smooth Noto CJK, exactly like _PIXEL_SCRIPT_DETECTORS.
_PIXEL_LANG_FONTS = {
    # Russian uses Zpix: PixelOperator has zero Cyrillic glyphs (tofu boxes).
    "russian":    os.path.join(_ASSETS_FONTS, "Zpix.ttf"),
    "english":    os.path.join(_ASSETS_FONTS, "PixelOperator.ttf"),
    "spanish":    os.path.join(_ASSETS_FONTS, "PixelOperator.ttf"),
    "german":     os.path.join(_ASSETS_FONTS, "PixelOperator.ttf"),
    "french":     os.path.join(_ASSETS_FONTS, "PixelOperator.ttf"),
    "japanese":   os.path.join(_ASSETS_FONTS, "Zpix.ttf"),
    "chinese":    os.path.join(_ASSETS_FONTS, "Zpix.ttf"),
    "korean":     os.path.join(_ASSETS_FONTS, "NotoSansCJK-Regular.ttc"),
    "portuguese": os.path.join(_ASSETS_FONTS, "PixelOperator.ttf"),
}

_TERNARY_RE = re.compile(r'\{\s*(?P<quote1>["\'])(?P<v1>.*?)(?P=quote1)\s+if\s+.*?\s+else\s+(?P<quote2>["\'])(?P<v2>.*?)(?P=quote2)\s*\}')

def expand_f_string(text: str) -> list[str]:
    """Parse python f-string text and expand ternary expressions into all possible string variants."""
    matches = list(_TERNARY_RE.finditer(text))
    if not matches:
        return [text]
    
    parts = []
    last_idx = 0
    for m in matches:
        parts.append([text[last_idx:m.start()]])
        parts.append([m.group("v1"), m.group("v2")])
        last_idx = m.end()
    parts.append([text[last_idx:]])
    
    import itertools
    results = []
    for combo in itertools.product(*parts):
        results.append("".join(combo))
    return results


def parse_string_methods(text: str, tail: str) -> str:
    """Parse tail of the line starting after _("...") to detect .upper(), .lower(),
    .title(), and .capitalize() method calls, applying them to text."""
    method_re = re.compile(r'^\s*\.\s*(upper|lower|title|capitalize)\s*\(\s*\)')
    current_text = text
    current_tail = tail
    
    while True:
        mm = method_re.match(current_tail)
        if not mm:
            break
        method_name = mm.group(1)
        if method_name == "upper":
            current_text = current_text.upper()
        elif method_name == "lower":
            current_text = current_text.lower()
        elif method_name == "title":
            current_text = current_text.title()
        elif method_name == "capitalize":
            current_text = current_text.capitalize()
        current_tail = current_tail[mm.end():]
        
    return current_text


def _parse_gui_text(content: str) -> tuple[dict[str, str], dict[str, int]]:
    """Extract gui.* string and int assignments from gui.rpy source text."""
    strings: dict[str, str] = {}
    ints: dict[str, int] = {}
    str_pattern = re.compile(r'\b(?:define\s+)?gui\.([a-zA-Z0-9_]+)\s*=\s*[\'"]([^\'"]+)[\'"]')
    int_pattern = re.compile(r'\b(?:define\s+)?gui\.([a-zA-Z0-9_]+)\s*=\s*(\d+)\b')
    for line in content.split("\n"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        sm = str_pattern.search(line)
        if sm:
            strings[sm.group(1)] = sm.group(2)
            continue
        im = int_pattern.search(line)
        if im:
            ints[im.group(1)] = int(im.group(2))
    return strings, ints


def parse_gui_rpy(root: str) -> tuple[dict[str, str], dict[str, int]]:
    """Parse game/gui.rpy and extract all gui.* variables for fonts and sizes.

    Loose gui.rpy on disk wins; if absent (archive-only game like Killer Chat!)
    we fall back to reading gui.rpy straight out of the game's .rpa, so size
    resolution works the SAME way _iter_sources reads screens — otherwise an
    archive-only game would silently lose its real gui sizes."""
    gui_path = os.path.join(root, "game", "gui.rpy")
    if os.path.exists(gui_path):
        try:
            with open(gui_path, "r", encoding="utf-8") as f:
                return _parse_gui_text(f.read())
        except Exception:
            return {}, {}

    # No loose gui.rpy — try the archives.
    try:
        from .rpa import iter_rpa_files, read_rpa
        game_dir = os.path.join(root, "game")
        for arc in iter_rpa_files(game_dir):
            for rf in read_rpa(arc, ".rpy"):
                if rf.path.replace("\\", "/").endswith("gui.rpy"):
                    return _parse_gui_text(rf.data)
    except Exception:
        pass
    return {}, {}


def get_source_font_and_size(root: str) -> tuple[str, int]:
    """Resolve the font path and font size using cascades from gui.rpy."""
    strings, ints = parse_gui_rpy(root)
    
    font_name = (
        strings.get("choice_button_text_font") or
        strings.get("interface_font") or
        strings.get("text_font")
    )
    
    font_size = (
        ints.get("choice_button_text_size") or
        ints.get("text_size") or
        32
    )
    
    source_font_path = None
    if font_name:
        for candidate in [
            os.path.join(root, "game", font_name),
            os.path.join(root, "game", "fonts", font_name)
        ]:
            if os.path.isfile(candidate):
                source_font_path = candidate
                break
                
    if not source_font_path:
        source_font_path = os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf")
        
    return source_font_path, font_size


# Fraction of a typical line that is whitespace, per script. Real prose runs
# ~15% spaces; the uniform alphabet samples above carry only ~1 space per ~27
# letters (~3.6%), which under-weights the (narrow) space and inflates the
# average glyph width — making the char budget too conservative. We blend the
# space width in at its real frequency instead. CJK text has (almost) no spaces,
# so its fraction is ~0.
_SPACE_FRACTION = {
    "russian": 0.16, "english": 0.17, "spanish": 0.17, "german": 0.15,
    "french": 0.17, "portuguese": 0.17,
    "japanese": 0.0, "chinese": 0.0, "korean": 0.02,
}

_FONT_CACHE: dict[tuple[str, int], object] = {}

# The UI sends the target language as its display name ("Russian",
# "Chinese (Simplified)", "Portuguese (Brazil)"); our per-script tables key on a
# bare lowercase script name ("russian", "chinese", "portuguese"). Without this
# normalization every non-English target silently fell back to the English
# sample + Latin font — catastrophic for CJK (wrong glyphs, wrong widths).
_LANG_ALIASES = {
    "chinese (simplified)": "chinese",
    "chinese (traditional)": "chinese",
    "chinese simplified": "chinese",
    "portuguese (brazil)": "portuguese",
    "portuguese (portugal)": "portuguese",
}


def _normalize_lang(target_lang: str) -> str:
    """Map a UI target-language label to our internal script key. Falls back to
    the part before any '(' so unknown regional variants still resolve."""
    s = (target_lang or "").strip().lower()
    if s in _LANG_ALIASES:
        return _LANG_ALIASES[s]
    if s in ALPHABET_SAMPLES:
        return s
    base = s.split("(")[0].strip()
    return base if base in ALPHABET_SAMPLES else s


def _load_font(fpath: str, size: int):
    """Load a PIL font, cached by (path, size). .ttc collections use face 0."""
    from PIL import ImageFont
    key = (fpath, size)
    f = _FONT_CACHE.get(key)
    if f is None:
        if fpath.lower().endswith(".ttc"):
            f = ImageFont.truetype(fpath, size, index=0)
        else:
            f = ImageFont.truetype(fpath, size)
        _FONT_CACHE[key] = f
    return f


def _strip_tags(text: str) -> str:
    """Drop Ren'Py style tags ({b}, {color=...}, …) so they don't count toward
    rendered width. Used for MEASUREMENT only — the real string is never cut."""
    return re.sub(r'\{[^}]*\}', '', text)


def _max_line_px(text: str, font, font_size: int) -> float:
    """Pixel width of the widest line of `text` (tags stripped) in `font`."""
    lines = _strip_tags(text).split("\n") or [""]
    try:
        return max(font.getlength(l) for l in lines)
    except Exception:
        return max((len(l) for l in lines), default=0) * (font_size * 0.6)


def _target_font(target_lang: str, font_size: int, font_style: str = "smooth"):
    """The font the game will actually render the translation in (inject swaps
    the game font to our bundled font for the target script). Must mirror inject's
    choice — pass font_style="pixel" to measure against the bitmap font so the
    UI-fitting budget matches what the player will actually see."""
    lang = _normalize_lang(target_lang)
    table = _PIXEL_LANG_FONTS if font_style == "pixel" else LANG_FONTS
    path = table.get(lang) or os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf")
    try:
        return _load_font(path, font_size)
    except Exception:
        return _load_font(os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"), font_size)


def measure_original_px(original_text: str, source_font_path: str, font_size: int) -> float:
    """Width (px) the ORIGINAL occupies in the GAME's font — the budget the
    translation must fit into. This is the ground truth we enforce against."""
    try:
        src_font = _load_font(source_font_path, font_size)
    except Exception:
        src_font = _load_font(os.path.join(_ASSETS_FONTS, "NotoSans-Regular.ttf"), font_size)
    return _max_line_px(original_text, src_font, font_size)


def measure_translation_px(translation: str, target_lang: str, font_size: int, font_style: str = "smooth") -> float:
    """Width (px) the TRANSLATION will actually render at, in the target-script
    font. Compare against measure_original_px() to know if it really overflows —
    no `len()` / average-width approximation involved."""
    return _max_line_px(translation, _target_font(target_lang, font_size, font_style), font_size)


# Fallback line-height multiple when real font metrics are unavailable. Ren'Py's
# place_vertical stacks each line by the FONT's own ascent+descent (FreeType),
# which for our Noto faces is ~1.39–1.41 × size — NOT 1.25. A too-small multiple
# under-estimates height and lets text clip (the real bug). _line_height() below
# uses the true metrics; this constant only guards the no-PIL degrade path, where
# we err on the GENEROUS side so we never under-count height.
_LINE_HEIGHT_MULT = 1.4
# Never shrink dialogue/caption font below this fraction of the game's own size —
# below it text is unreadable; better to ride a hair tall than render ants.
_FIT_FONT_FLOOR = 0.6


def _line_height(font, font_size: int, line_spacing: int = 0, line_leading: int = 0) -> float:
    """Pixel height of ONE rendered line — matches the engine's place_vertical,
    which stacks lines by the font's FreeType ascent+descent plus the style's
    line_spacing/line_leading (both default 0). PIL's getmetrics() returns the
    same ascent+descent FreeType gives Ren'Py, so this is the engine's real line
    box, not the old font_size*1.25 approximation that under-counted height and
    let translated text clip. Degrades to the generous _LINE_HEIGHT_MULT if
    metrics are unavailable."""
    # line_spacing/line_leading may be NEGATIVE (a style tightening its line box,
    # e.g. line_spacing -8) — the engine ADDS them to ascent+descent, so we do
    # too, clamping only the FINAL height to >=1 (never the addends individually).
    try:
        ascent, descent = font.getmetrics()
        h = ascent + descent
        if h > 0:
            return max(1.0, h + line_spacing + line_leading)
    except Exception:
        pass
    return max(1.0, font_size * _LINE_HEIGHT_MULT + line_spacing + line_leading)


def _wrapped_line_count(plain_text: str, font, box_width: float) -> int:
    """Visual line count of `plain_text` (tags already stripped) wrapped to
    `box_width` in `font`, honoring explicit \\n. Emulates the ENGINE's default
    `layout = "tex"` (Knuth-Plass): the count is the MINIMUM number of lines whose
    optimal break never overflows `box_width`. For a single paragraph this equals
    the greedy count (greedy already achieves the fewest lines), but TeX can place
    a break the greedy pass wouldn't — so we compute the true optimum here rather
    than assume greedy. A word wider than the box still occupies its own line (the
    engine can't break inside a word either), so we never loop forever."""
    total = 0
    for para in plain_text.split("\n"):
        words = para.split(" ")
        if not words or (len(words) == 1 and words[0] == ""):
            total += 1
            continue
        total += _tex_line_count(words, font, box_width)
    return total


def _tex_line_count(words: list, font, box_width: float) -> int:
    """Minimum line count for `words` under TeX-style optimal breaking: DP that
    minimizes summed squared trailing slack with an infinite penalty for any line
    that overflows (except a single over-wide word, which must stand alone). The
    line count of that optimal break is what the engine's `layout=tex` produces.
    Pure measurement — never mutates anything."""
    try:
        space_w = font.getlength(" ")
        widths = [font.getlength(w) for w in words]
    except Exception:
        # No PIL: fall back to greedy with a crude per-char estimate.
        return _greedy_line_count(words, font, box_width)
    n = len(words)
    INF = float("inf")
    # cost[i], lines[i] = best (squared-slack cost, line count) for words[i:].
    cost = [INF] * (n + 1)
    lines = [0] * (n + 1)
    cost[n] = 0.0
    for i in range(n - 1, -1, -1):
        line_w = 0.0
        best_c = INF
        best_l = 0
        for j in range(i, n):
            line_w += widths[j] + (space_w if j > i else 0.0)
            if line_w > box_width and j > i:
                break  # adding word j overflows; stop extending this line
            slack = box_width - line_w
            # Last line is ragged-right: no slack penalty. Tie-break on fewer lines.
            c = (0.0 if j == n - 1 else slack * slack) + cost[j + 1]
            l = 1 + lines[j + 1]
            if c < best_c - 1e-9 or (abs(c - best_c) <= 1e-9 and l < best_l):
                best_c = c
                best_l = l
        if best_l == 0:  # even one word overflows -> it stands alone
            best_l = 1 + lines[i + 1]
        cost[i] = best_c if best_c < INF else 0.0
        lines[i] = best_l
    return lines[0]


def _greedy_line_count(words: list, font, box_width: float) -> int:
    """Greedy fallback line count (used only when PIL metrics are unavailable)."""
    line = ""
    count = 1
    for w in words:
        trial = w if not line else line + " " + w
        try:
            fits = font.getlength(trial) <= box_width
        except Exception:
            fits = len(trial) * 0.6 * getattr(font, "size", 20) <= box_width
        if fits or not line:
            line = trial
        else:
            count += 1
            line = w
    return count


def fit_scale_for_box(translation: str, target_lang: str, font_size: int,
                      box_width: float, box_height: float,
                      font_style: str = "smooth",
                      line_spacing: int = 0, line_leading: int = 0) -> float:
    """Largest font-scale in [_FIT_FONT_FLOOR, 1.0] at which `translation`, word-
    wrapped to `box_width`, fits within `box_height`. Returns 1.0 if it already
    fits (no shrink needed) or if box dims are unknown/non-positive (we never
    shrink when we don't truly know the box — caller guarantees that contract).

    This drives the dialogue safety-net: inject wraps an overflowing say line in
    a native `{size=*scale}` tag so the ENGINE itself renders the FULL, unabridged
    translation smaller — the word/sentence is never cut. Mirrors the engine's own
    `{size=*}` semantics (text.py). line_spacing/line_leading (style values,
    default 0, possibly negative) scale WITH the font and are added per line so the
    height check equals the engine's place_vertical."""
    if box_width <= 0 or box_height <= 0:
        return 1.0
    plain = _strip_tags(translation)
    if not plain.strip():
        return 1.0
    # Measurement needs PIL (Pillow). If it's unavailable (or any measurement
    # error), DEGRADE GRACEFULLY to no-shrink — auto-fit is a nicety; it must
    # NEVER make inject fail. (Project rule: degrade, never raise.)
    try:
        s = 1.0
        while s >= _FIT_FONT_FLOOR - 1e-9:
            fs = max(1, int(round(font_size * s)))
            font = _target_font(target_lang, fs, font_style)
            lines = _wrapped_line_count(plain, font, box_width)
            # The engine scales line_spacing/line_leading with the {size=*} tag
            # too (they're style ints fed through scale_int), so scale them here.
            ls = int(round(line_spacing * s))
            ll = int(round(line_leading * s))
            # Real engine line box (FreeType ascent+descent + spacing), not fs*1.25.
            if lines * _line_height(font, fs, ls, ll) <= box_height:
                return round(s, 3)
            s -= 0.05
        return round(_FIT_FONT_FLOOR, 3)
    except Exception:
        return 1.0


def fit_scale_one_line(translation: str, target_lang: str, font_size: int,
                       box_width: float, font_style: str = "smooth") -> float:
    """Largest font-scale in [_FIT_FONT_FLOOR, 1.0] at which the WIDEST line of
    `translation` fits `box_width` on a SINGLE line (no wrap). Used to keep a
    fixed-width menu button one line tall so it can't grow and spill past the UI
    frame. Returns 1.0 if it already fits or box_width is unknown/<=0."""
    if box_width <= 0:
        return 1.0
    plain = _strip_tags(translation)
    if not plain.strip():
        return 1.0
    # Degrade to no-shrink if PIL/measurement is unavailable (see fit_scale_for_box).
    try:
        s = 1.0
        while s >= _FIT_FONT_FLOOR - 1e-9:
            fs = max(1, int(round(font_size * s)))
            font = _target_font(target_lang, fs, font_style)
            # Widest existing line (honor explicit \n) must fit on one line.
            if _max_line_px(plain, font, fs) <= box_width:
                return round(s, 3)
            s -= 0.05
        return round(_FIT_FONT_FLOOR, 3)
    except Exception:
        return 1.0


def _avg_char_width(target_lang: str, font_size: int, font_style: str = "smooth") -> float:
    """Frequency-weighted mean glyph width for the target script: letters share
    one weight, the (narrow) space is blended in at its real prose frequency.
    Used only to turn a pixel budget into a character HINT for the model."""
    lang = _normalize_lang(target_lang)
    tgt_font = _target_font(target_lang, font_size, font_style)
    sample = ALPHABET_SAMPLES.get(lang) or ALPHABET_SAMPLES["english"]
    letters = [c for c in sample if c != " "]
    try:
        letter_w = sum(tgt_font.getlength(c) for c in letters) / max(1, len(letters))
        space_w = tgt_font.getlength(" ")
    except Exception:
        return font_size * 0.6
    sf = _SPACE_FRACTION.get(lang, 0.16)
    avg = (1 - sf) * letter_w + sf * space_w
    return avg if avg > 0 else font_size * 0.6


def get_char_limit(original_text: str, source_font_path: str, target_lang: str, font_size: int, font_style: str = "smooth") -> int:
    """Character budget (a HINT for the model) for a translation to fit within the
    original's pixel width. Pixels are the ground truth (see measure_*_px); this
    just converts that width into an approximate char count the model can aim for,
    using a frequency-weighted average glyph width for the target script."""
    if not original_text:
        return 5
    original_px = measure_original_px(original_text, source_font_path, font_size)
    avg_width = _avg_char_width(target_lang, font_size, font_style)
    return max(5, int(original_px / avg_width))


# --- Engine-oracle lint -----------------------------------------------------
# The game ships its own Ren'Py runtime + a launcher exe. Running
# `<exe> <basedir> lint` makes the ENGINE ITSELF validate the WHOLE project,
# INCLUDING our injected tl/<lang>/ files — the same "engine oracle" stance the
# project already uses for translate-id verification. This turns "will our
# injection break the game?" from a guess into a measured fact, and catches
# defects our static validators can't (verified: it flagged a translated "100%"
# as an unterminated string-format code — a real runtime hazard).
#
# Findings reference file paths, so we split OURS (tl/<lang>/) from pre-existing
# game issues. lint exits 0 even WITH findings, so we parse text, never the code.

def _find_engine_exe(root: str) -> str | None:
    """Locate the game's Ren'Py launcher exe next to its basedir.

    A shipped game has `<Name>.exe` (+ `<Name>.py`, `<Name>.sh`) in its basedir
    alongside `lib/` and `renpy/`. We pick the .exe whose stem also has a paired
    .py launcher (the Ren'Py bootstrap), which avoids matching unrelated exes.
    Returns None if nothing suitable is found (player machine without the SDK)."""
    import glob

    try:
        candidates = glob.glob(os.path.join(root, "*.exe"))
    except OSError:
        return None
    for exe in candidates:
        stem = os.path.splitext(exe)[0]
        # The Ren'Py launcher always ships a same-named .py bootstrap.
        if os.path.exists(stem + ".py"):
            return exe
    # Fallback: any .exe if the basedir clearly looks like a Ren'Py game.
    if candidates and os.path.isdir(os.path.join(root, "renpy")):
        return candidates[0]
    return None


def lint_with_engine(root: str, lang: str | None = None,
                     timeout: int = 240) -> dict:
    """Run the game engine's own `lint` over the project and report findings.

    Returns a dict (JSON-serializable):
      {
        available: bool,          # engine exe found + ran
        ours: [ {file, line, message}, ... ],   # findings in OUR tl/<lang>/
        ours_count: int,
        other_count: int,         # pre-existing game findings (not ours)
        reason: str,              # human note when unavailable
      }
    DEGRADES GRACEFULLY: if the engine exe isn't present (a Steam player without
    the SDK) or the run fails/times out, returns available=False and never raises
    — exactly like the translate-id oracle, which is optional too."""
    import subprocess

    result = {"available": False, "ours": [], "ours_count": 0,
              "other_count": 0, "reason": ""}

    exe = _find_engine_exe(root)
    if not exe:
        result["reason"] = "engine exe not found (game has no bundled SDK)"
        return result

    # Normalize the language to the engine's tl/ directory name (e.g. a UI display
    # name "Russian" or code "ru" -> "russian"), so findings under our output tree
    # are classified as OURS, not "other". Same map inject uses (_lang_dir).
    if lang:
        lang = RenPyParser._lang_dir(lang)

    try:
        proc = subprocess.run(
            [exe, root, "lint"],
            capture_output=True, text=True, timeout=timeout,
            cwd=root, encoding="utf-8", errors="replace",
        )
        out = proc.stdout or ""
    except subprocess.TimeoutExpired:
        result["reason"] = f"engine lint timed out after {timeout}s"
        return result
    except Exception as e:  # noqa: BLE001 — never raise to the caller
        result["reason"] = f"engine lint failed: {e}"
        return result

    # If lint also wrote lint.txt, prefer the on-disk copy (full, un-truncated).
    lint_txt = os.path.join(root, "lint.txt")
    if os.path.exists(lint_txt):
        try:
            with open(lint_txt, encoding="utf-8", errors="replace") as f:
                out = f.read()
        except OSError:
            pass

    result["available"] = True

    # Each finding starts with `game/...:<line> <message>`. Split ours vs others
    # by whether the path is under our tl/<lang>/ output tree.
    finding_re = re.compile(r"^(game/[^\s:]+):(\d+)\s+(.*)$")
    tl_marker = f"/tl/{lang}/".lower() if lang else "/tl/"
    ours, other = [], 0
    for line in out.splitlines():
        m = finding_re.match(line.strip())
        if not m:
            continue
        fpath, lineno, msg = m.group(1), int(m.group(2)), m.group(3)
        norm = "/" + fpath.lower()
        if (tl_marker in norm) or norm.startswith("/game/tl/" + (lang.lower() + "/" if lang else "")):
            ours.append({"file": fpath, "line": lineno, "message": msg,
                         "actionable": _lint_is_actionable(msg)})
        else:
            other += 1
    result["ours"] = ours
    result["ours_count"] = len(ours)
    result["actionable_count"] = sum(1 for f in ours if f["actionable"])
    result["other_count"] = other
    return result


# Lint findings inside tl/ files that are BENIGN artifacts of linting translation
# files in isolation (the `who`/attribute is a runtime variable the linter can't
# resolve) — NOT defects we introduced. Everything else (text-tag mismatches,
# string-format-code errors, duplicate translation keys) is a REAL hazard our
# injection may have caused, and is what the UI should surface.
_LINT_BENIGN_RE = re.compile(
    r"Could not evaluate .* in the who part|"
    r"has not been given a parameter list|"
    r"is not used|may not be reachable",
    re.I,
)


def _lint_is_actionable(message: str) -> bool:
    """True if a tl/ lint finding is a genuine hazard worth surfacing (vs benign
    translation-file lint noise)."""
    return not _LINT_BENIGN_RE.search(message)
