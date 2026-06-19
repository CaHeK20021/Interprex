"""QSP (Quest Soft Player) parser — compiled `.qsp` game files.

QSP is an open-source engine for text interactive fiction, very common in the
Russian IF scene. The shipped game is a single `.qsp` file: UTF-16LE, no BOM,
`\\r\\n` between fields, and lightly obfuscated by a fixed per-character shift
(QGen writes `plain = stored + 5`; classic QSP writes `plain = stored - 5` —
we auto-detect the sign from the location-count field, see `_detect_offset`).

FILE LAYOUT (each "line" produced by split("\\r\\n") is ONE field; newlines that
belong *inside* a field are themselves ciphered, so they never split):

    line 0   QSPGAME                      signature   (plain)
    line 1   2025-… (QGen 4.3.0-b1)       version     (plain)
    line 2   <password>                   (ciphered, ignored — not the key)
    line 3   <location count>             (ciphered integer)
    line 4…  per location, in order:
               name                       jump target / structural key  (NOT text)
               description                base text shown on the location (TEXT)
               on-visit code              QSPS code block
               action count
               then per action:
                 image path               (NOT text)
                 action name              menu choice shown to player  (TEXT)
                 action code              QSPS code block

WHAT WE TRANSLATE. Location *descriptions* and *action names* are pure
player-facing text — taken whole. Inside the code blocks text is mixed with
logic, exactly like Ren'Py, so we POSITIVELY match only strings that are clearly
output (BEDROCK: missing a string is safe, mangling code is not):

  - operand of an output command:      *pl 'x'  *p 'x'  *nl 'x'  pl/p/nl 'x'
  - prompt argument:                   input('x')   msg('x')
  - a bare string statement:           'x'                (printed by QSP)
  - prose assigned to a display var:   $sys = 'System: '  (value has a space,
                                       a <<token>> or ends with ':')

Everything else is skipped: jump targets (gt/gs/jump 'loc'), variable values,
comparisons (if $x='y'), asset paths ('img\\a.gif'), command keywords ('*list').

PATH (write-back address + part of the id, BEDROCK #2). Structural, keyed by the
location name (a stable, unique address — never a line number):

    ["loc", <name>, "desc"]                         location description
    ["loc", <name>, "act", <a>, "name"]             Nth action's name
    ["loc", <name>, "code", <i>]                     Nth text string in on-visit code
    ["loc", <name>, "act", <a>, "code", <i>]         Nth text string in action code

WRITE-BACK is surgical per field: extract() and inject() walk the SAME `_scan`,
so the address computed for a string is identical by construction. inject()
deciphers only the touched field, splices the translation into the recorded span
(code strings get single-quotes doubled, QSP's escape), re-enciphers that field,
and re-joins — every other byte (signature, version, password, untouched
fields, the trailing CRLF) is preserved exactly.
"""

from __future__ import annotations

import os
import re

from .base import BaseParser, TranslationString, make_id

# Commands whose first quoted operand is shown to the player:
#   *pl/*p/*nl, pl/p/nl  output text
#   act 'Label':         a dynamic action — its name is the button the player
#                        clicks (the second arg, if any, is a location to jump to,
#                        but only the first string is the label)
#   menu 'name'          opens a menu defined elsewhere (its NAME is a key, not
#                        text) — intentionally NOT here
_OUT_CMDS = frozenset({"pl", "p", "nl", "*pl", "*p", "*nl", "act", "*act"})
# Function calls whose quoted argument is a player-facing prompt.
_PROMPT_FNS = frozenset({"input", "msg"})

# Stripped before the letter test so markup/interpolation isn't mistaken for
# translatable content:
#   <<expr>>  QSP value interpolation (<<$name>>, <<hp>>)
#   <tag ...> HTML markup — QSP games with `usehtml=1` wrap text in <b>, <center>,
#             <font color="red">, <img src="…">, etc. The slashes in closing tags
#             (</b>) and the attributes are NOT asset paths.
_TOKEN_RE = re.compile(r"<<[^>]*>>|<[^>]*>")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# An asset reference: a slash- or backslash-separated path ending in a known media
# extension (e.g. 'lain\\lain01.gif', 'data/web/img.png'). Must end in the
# extension so prose that merely contains a slash or a date isn't rejected; the
# markup (which also holds slashes) is already gone by the time this runs.
_ASSET_RE = re.compile(
    r"^[^'\"<>]*[\\/][^'\"<>]*\.(?:gif|png|jpe?g|bmp|wav|mp3|ogg|midi|avi|mpe?g)$",
    re.IGNORECASE,
)


def _shift(s: str, n: int) -> str:
    """Shift every code unit by n (mod 2**16). _shift(stored, off) -> plain;
    _shift(plain, -off) -> stored."""
    return "".join(chr((ord(c) + n) & 0xFFFF) for c in s)


def _norm_nl(s: str) -> str:
    r"""Normalize any newline style to QSP's in-field convention, `\r\n`. In-field
    newlines are ciphered (they never collide with the literal `\r\n` field
    separator) and the engine writes them as CRLF throughout, so a translation
    must too — otherwise identity inject isn't byte-exact and an LLM that returns
    bare `\n` would leave mixed endings inside a field."""
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _detect_offset(lines: list[str]) -> int | None:
    """The cipher is a fixed shift; QGen uses +5, classic QSP -5. Decide by which
    one turns the location-count field (line 3) into a plain integer. Returns the
    decipher offset, or None if this doesn't look like a QSP file."""
    if len(lines) <= 3:
        return None
    for off in (5, -5):
        if _shift(lines[3], off).strip().isdigit():
            return off
    return None


def _has_text(raw: str) -> bool:
    """True if a code string carries real translatable letters.

    Markup and interpolation are stripped FIRST, then the remainder is judged:
    a string that is only image/HTML tags (`<img src=…>`), only <<…>> values, or
    only an asset path — even behind a `<<$sys>>` display prefix — has nothing to
    translate. `<center>Отлично!</center>` survives (the prose remains); a bare
    `<<$sys>>data\\web\\x.png` does not (nothing but a path remains)."""
    cleaned = _TOKEN_RE.sub("", raw).strip()
    if _ASSET_RE.match(cleaned):
        return False
    return bool(_LETTER_RE.search(cleaned))


def _is_prose(inner: str) -> bool:
    return (" " in inner.strip()) or ("<<" in inner) or inner.rstrip().endswith(":")


def _classify(code: str, open_idx: int, after_close: int) -> bool:
    """Decide whether the quoted string at code[open_idx] is player-facing text,
    from the token immediately before it. open_idx points at the opening quote;
    after_close is the index just past the closing quote."""
    k = open_idx - 1
    while k >= 0 and code[k] in " \t":
        k -= 1

    # Statement boundary -> a bare string statement, which QSP prints. Unless the
    # next non-space char is '=', which would make this an assignment LHS.
    if k < 0 or code[k] in "\n\r&:":
        m = after_close
        while m < len(code) and code[m] in " \t":
            m += 1
        return not (m < len(code) and code[m] == "=")

    # Function argument: input('…') / msg('…') are prompts; other calls aren't.
    if code[k] == "(":
        e = k
        k -= 1
        while k >= 0 and (code[k].isalnum() or code[k] in "_$"):
            k -= 1
        return code[k + 1 : e].lower() in _PROMPT_FNS

    # Assignment / comparison ($x = '…', if $x = '…'): only prose, never ids.
    if code[k] in "=!<>":
        return _is_prose(code[open_idx + 1 : after_close - 1])

    # Otherwise a command word precedes it: text only for output commands.
    e = k + 1
    while k >= 0 and (code[k].isalnum() or code[k] in "_*$"):
        k -= 1
    return code[k + 1 : e].lower() in _OUT_CMDS


def _is_exec_code(s: str) -> bool:
    """True if the string contains executable QSP code, usually via EXEC: link attribute."""
    cleaned = s.strip().lower()
    return (
        cleaned.startswith("exec:") or
        cleaned.startswith("goto:") or
        cleaned.startswith("gs:") or
        cleaned.startswith("xgt:")
    )


def _scan_code(code: str):
    """Yield (inner_start, inner_end, original, quote_char) for each translatable string in a
    QSPS code block."""
    i, n = 0, len(code)
    while i < n:
        char = code[i]
        if char == "'":
            # Single-quoted string
            start = i
            i += 1
            while i < n:
                if code[i] == "'":
                    if i + 1 < n and code[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            if i < n:
                inner_start, inner_end = start + 1, i
                raw = code[inner_start:inner_end]
                orig = raw.replace("''", "'")
                if _classify(code, start, i + 1) and _has_text(orig) and not _is_exec_code(orig):
                    yield inner_start, inner_end, orig, "'"
                i += 1
            else:
                break
        elif char == '"':
            # Double-quoted string
            start = i
            i += 1
            while i < n:
                if code[i] == '"':
                    if i + 1 < n and code[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
            if i < n:
                inner_start, inner_end = start + 1, i
                raw = code[inner_start:inner_end]
                orig = raw.replace('""', '"')
                if _classify(code, start, i + 1) and _has_text(orig) and not _is_exec_code(orig):
                    yield inner_start, inner_end, orig, '"'
                i += 1
            else:
                break
        elif char == '[':
            # Bracketed string (nests)
            start = i
            i += 1
            depth = 1
            while i < n and depth > 0:
                if code[i] == '[':
                    depth += 1
                elif code[i] == ']':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth == 0:
                inner_start, inner_end = start + 1, i
                orig = code[inner_start:inner_end]
                if _classify(code, start, i + 1) and _has_text(orig) and not _is_exec_code(orig):
                    yield inner_start, inner_end, orig, '[]'
                i += 1
            else:
                break
        else:
            i += 1


def _scan(text: str):
    """Walk a whole deciphered-on-demand .qsp and yield one record per
    translatable string, in file order:

        {path, original, context, field, start, end, escape}

    `field` is the index into text.split("\\r\\n") (one ciphered field per entry);
    `start`/`end` bound the replacement inside that field's DECIPHERED form.
    `escape` is the quote type for code strings and False for whole-field values.
    """
    lines = text.split("\r\n")
    off = _detect_offset(lines)
    if off is None:
        return

    def D(i: int) -> str:
        return _shift(lines[i], off)

    try:
        count = int(D(3).strip())
    except (ValueError, IndexError):
        return

    idx = 4
    for _ in range(count):
        if idx + 3 >= len(lines):
            return
        name = D(idx)
        desc_i, code_i = idx + 1, idx + 2
        try:
            acnt = int(D(idx + 3).strip())
        except ValueError:
            return

        desc = D(desc_i)
        if _has_text(desc):
            yield {
                "path": ["loc", name, "desc"],
                "original": desc,
                "context": f"{name} (location description)",
                "field": desc_i,
                "start": 0,
                "end": len(desc),
                "escape": False,
            }

        on_visit = D(code_i)
        for ci, (s, e, orig, qchar) in enumerate(_scan_code(on_visit)):
            yield {
                "path": ["loc", name, "code", str(ci)],
                "original": orig,
                "context": name,
                "field": code_i,
                "start": s,
                "end": e,
                "escape": qchar,
            }

        idx += 4
        for a in range(acnt):
            if idx + 2 >= len(lines):
                return
            an_i, ac_i = idx + 1, idx + 2
            an = D(an_i)
            if _has_text(an):
                yield {
                    "path": ["loc", name, "act", str(a), "name"],
                    "original": an,
                    "context": f"{name} (menu choice)",
                    "field": an_i,
                    "start": 0,
                    "end": len(an),
                    "escape": False,
                }
            act_code = D(ac_i)
            for ci, (s, e, orig, qchar) in enumerate(_scan_code(act_code)):
                yield {
                    "path": ["loc", name, "act", str(a), "code", str(ci)],
                    "original": orig,
                    "context": name,
                    "field": ac_i,
                    "start": s,
                    "end": e,
                    "escape": qchar,
                }
            idx += 3


class QspParser(BaseParser):
    engine = "qsp"

    # --- detection --------------------------------------------------------
    @staticmethod
    def detect(root: str) -> bool:
        for _ in QspParser._qsp_files(root):
            return True
        return False

    @staticmethod
    def _qsp_files(root: str, sub_paths: list[str] | None = None) -> list[str]:
        """Every .qsp under sub_paths (or the whole tree) that starts with the
        QSPGAME signature. Sorted for deterministic ids."""
        if sub_paths:
            starts = [os.path.join(root, p) for p in sub_paths]
        else:
            starts = [root]
        out: list[str] = []
        for start in starts:
            if os.path.isfile(start):
                if QspParser._is_qsp(start):
                    out.append(start)
                continue
            for dirpath, dirnames, filenames in os.walk(start):
                dirnames[:] = [d for d in dirnames if d != ".interprex_backups"]
                for name in filenames:
                    if name.lower().endswith(".qsp"):
                        fp = os.path.join(dirpath, name)
                        if QspParser._is_qsp(fp):
                            out.append(fp)
        return sorted(out)

    @staticmethod
    def _is_qsp(fpath: str) -> bool:
        try:
            with open(fpath, "rb") as f:
                head = f.read(32)
        except OSError:
            return False
        # UTF-16LE "QSPGAME" with no BOM.
        return head.startswith("QSPGAME".encode("utf-16le"))

    # --- extract ----------------------------------------------------------
    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        results: list[TranslationString] = []
        for fpath in self._qsp_files(root, sub_paths):
            file_rel = os.path.relpath(fpath, root).replace("\\", "/")
            with open(fpath, "rb") as f:
                text = f.read().decode("utf-16le")
            for rec in _scan(text):
                if rec["original"].strip():
                    results.append(self._mk(file_rel, rec["path"], rec["original"],
                                            rec.get("context", "")))
        return results

    # --- inject -----------------------------------------------------------
    def inject(self, root: str, translations: dict[str, str],
               target_lang: str | None = None,
               sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0
        for fpath in self._qsp_files(root, sub_paths):
            file_rel = os.path.relpath(fpath, root).replace("\\", "/")
            with open(fpath, "rb") as f:
                text = f.read().decode("utf-16le")
            lines = text.split("\r\n")
            off = _detect_offset(lines)
            if off is None:
                continue

            # Gather every edit, grouped by ciphered field, in DECIPHERED span
            # coordinates. A field (a code block) may receive several.
            edits: dict[int, list[tuple[int, int, str]]] = {}
            for rec in _scan(text):
                sid = make_id(self.engine, file_rel, rec["path"], rec["original"])
                if sid not in translations:
                    continue
                value = translations[sid]
                quote = rec["escape"]
                if quote:
                    # QSP quoted string inside code: normalize and escape based on quote type
                    value = _norm_nl(value)
                    if quote == "'":
                        value = value.replace("'", "''")
                    elif quote == '"':
                        value = value.replace('"', '""')
                edits.setdefault(rec["field"], []).append((rec["start"], rec["end"], value))
                written += 1

            if not edits:
                continue

            for fi, spans in edits.items():
                plain = _shift(lines[fi], off)
                for start, end, value in sorted(spans, reverse=True):
                    plain = plain[:start] + value + plain[end:]
                lines[fi] = _shift(plain, -off)

            self.backup_file(root, fpath)
            with open(fpath, "wb") as f:
                f.write("\r\n".join(lines).encode("utf-16le"))

        return written
