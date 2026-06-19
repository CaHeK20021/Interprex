import re
import logging
from pathlib import Path
from .base import BaseParser, TranslationString

logger = logging.getLogger(__name__)

# Language map for UE3 3-letter codes
UE3_LANG_MAP = {
    "English": "INT",
    "French": "FRA",
    "German": "DEU",
    "Italian": "ITA",
    "Spanish": "ESP",           # European
    "Spanish-LA": "ESN",        # Latin American
    "Portuguese": "POR",        # European
    "Portuguese-BR": "PTB",     # Brazilian
    "Russian": "RUS",
    "Polish": "POL",
    "Czech": "CZE",
    "Hungarian": "HUN",
    "Turkish": "TUR",
    "Japanese": "JPN",
    "Korean": "KOR",
    "Chinese": "CHN",           # Simplified
    "Chinese-TW": "CHT",        # Traditional
    "Arabic": "ARA",
    "Dutch": "NLD",
    "Danish": "DAN",
    "Finnish": "FIN",
    "Norwegian": "NOR",
    "Swedish": "SWE",
}

# Valid UE3 INT filename: starts with a letter, then letters/digits/underscores.
# Real games name dialogue files CU_E1_1A.INT, Episode02__CU_E2_1A.INT, etc.,
# so underscores MUST be allowed — without them ~62% of Life Is Strange is dropped.
INT_NAME_RE = re.compile(r'^[A-Za-z]\w*\.INT$', re.I)
# Parent dir is a 3-letter UE3 language code (INT, RUS, FRA, ...).
LANG_DIR_RE = re.compile(r'^[A-Z]{3}$', re.I)

# Keys carrying engine/locale metadata, not translatable text.
NON_TRANSLATABLE_KEYS = {"langid", "sublangid", "language"}
# Bare config values, not display text (Borderlands 2 leaks ~25k of these).
NON_TRANSLATABLE_VALUES = {"true", "false", "none"}
# UE object-literal value, e.g. (Name=Core.HelloWorldCommandlet,Class=Class,...)
OBJECT_LITERAL_RE = re.compile(r'^\([^)]*\b(?:Name|Class)=')

# UE3 struct values pack real text inside fields. Two delimiter dialects exist:
#   Borderlands 2:   Subtitles[0]=(Text="line",Time=0)          <- real " quotes
#   BioShock Inf.:   Subtitles[0]=(Subtitle=\"line\",Speaker=\"X\")  <- escaped \"
# The (\\"|") group captures whichever opens; \2 backrefs it as the closer, so a
# single pattern handles both. Only these field names carry human-facing text;
# everything else (Time, flags, internal Name=) is config.
STRUCT_FIELD_RE = re.compile(r'(\w+)=(\\"|")((?:\\.|(?!\2).)*?)\2')
TRANSLATABLE_STRUCT_FIELDS = {
    # Borderlands 2
    "text", "displayname", "transformedname", "caption", "header", "body",
    # BioShock Infinite
    "subtitle", "speaker", "itemname", "itemdescription", "commandname",
    "checkpointdisplaystring", "checkpointdisplaydescription",
    "keyboardbuttonname", "mousebuttonname", "friendlyname",
}

def is_struct_value(value: str) -> bool:
    """A UE3 struct literal, e.g. (Text="...",Time=0)."""
    return value.lstrip().startswith('(')

def iter_struct_fields(value: str):
    """Yield (field_name, occurrence_idx, raw_content, start, end, escaped_delim)
    for each translatable quoted field inside a struct value. raw_content is
    still escaped; start/end span the content (between the quotes) for surgical
    inject; escaped_delim is True when the field used the \\" dialect (which also
    escapes apostrophes). Shared by extract and inject so the path — and thus the
    stable id — is computed identically on both sides."""
    counts: dict[str, int] = {}
    for m in STRUCT_FIELD_RE.finditer(value):
        name = m.group(1)
        if name.lower() not in TRANSLATABLE_STRUCT_FIELDS:
            continue
        idx = counts.get(name, 0)
        counts[name] = idx + 1
        yield name, idx, m.group(3), m.start(3), m.end(3), (m.group(2) == '\\"')

def is_engine_localization(int_file: Path) -> bool:
    """True if the file lives under Engine/Localization (shared editor/core
    boilerplate: editor errors, object refs, config — never game text)."""
    parts = [p.lower() for p in int_file.parts]
    for i, part in enumerate(parts):
        if part == 'localization' and i > 0 and parts[i - 1] == 'engine':
            return True
    return False

def iter_int_files(root: str):
    """Yield game-text INT files: valid filename, 3-letter lang parent dir,
    excluding the engine's own Localization tree."""
    for int_file in Path(root).rglob('Localization/**/*.INT'):
        if not INT_NAME_RE.match(int_file.name):
            continue
        if not LANG_DIR_RE.match(int_file.parent.name):
            continue
        if is_engine_localization(int_file):
            continue
        yield int_file

def is_translatable(key: str, value: str) -> bool:
    """Filter structural noise that would waste tokens or break the game if
    'translated': metadata keys, object literals, and values with no letters
    (pure numbers/config/punctuation)."""
    if key.strip().lower() in NON_TRANSLATABLE_KEYS:
        return False
    if value.strip().lower() in NON_TRANSLATABLE_VALUES:
        return False
    if OBJECT_LITERAL_RE.match(value):
        return False
    if not any(c.isalpha() for c in value):
        return False
    return True

# Single-pass de-escaping pattern
ESCAPE_RE = re.compile(r'\\(u[0-9a-fA-F]{4}|.)')

def de_escape(val: str) -> str:
    """Converts escape sequences back to literal characters."""
    def replace(match):
        seq = match.group(1)
        if seq.startswith('u') and len(seq) == 5:
            try:
                return chr(int(seq[1:], 16))
            except ValueError:
                return match.group(0)
        elif seq == '\\':
            return '\\'
        elif seq == '"':
            return '"'
        elif seq == "'":
            return "'"
        elif seq == 'n':
            return '\n'
        elif seq == 't':
            return '\t'
        elif seq == 'r':
            return '\r'
        else:
            return seq
    return ESCAPE_RE.sub(replace, val)

def escape(val: str, quote_char: str) -> str:
    """Escapes special characters for INI file output."""
    res = []
    for char in val:
        if char == '\\':
            res.append('\\\\')
        elif char == quote_char:
            res.append('\\' + quote_char)
        elif char == '\n':
            res.append('\\n')
        elif char == '\t':
            res.append('\\t')
        elif char == '\r':
            res.append('\\r')
        else:
            res.append(char)
    return "".join(res)

def escape_struct_field(val: str, escaped_delim: bool) -> str:
    r"""Escape a struct field's content for write-back, per dialect:
      real-quote (BL2)      delimiter " : a literal quote becomes \"
      escaped-quote (BSI)   delimiter \" : a literal quote stays BARE " (writing
                            \" would collide with the delimiter), and the
                            apostrophe is escaped \'
    de_escape is the exact inverse of both for the full char set (verified a
    perfect round-trip on both games' corpora, and on translations that
    introduce quotes/apostrophes)."""
    if not escaped_delim:
        return escape(val, '"')
    res = []
    for char in val:
        if char == '\\':
            res.append('\\\\')
        elif char == "'":
            res.append("\\'")
        elif char == '\n':
            res.append('\\n')
        elif char == '\t':
            res.append('\\t')
        elif char == '\r':
            res.append('\\r')
        else:  # literal " passes through bare — delimiter is the 2-char \"
            res.append(char)
    return "".join(res)

def is_escaped_wrapped(value: str) -> bool:
    r"""True if a plain (unquoted-at-INI-level) value is wrapped in escaped
    quotes, e.g. \"text\" (BioShock Infinite). Such a value is display text whose
    quotes are escapes, not INI delimiters."""
    v = value.strip()
    return len(v) >= 4 and v.startswith('\\"') and v.endswith('\\"')

def unwrap_escaped(value: str) -> str:
    r"""If a value is \"...\"-wrapped, strip the wrapper and de-escape the inner
    text (BSI apostrophe dialect). Otherwise return it unchanged. This is what
    the LLM should see and is the stable-id input."""
    if is_escaped_wrapped(value):
        return de_escape(value.strip()[2:-2])
    return value

def rewrap_escaped(translated: str) -> str:
    r"""Inverse of unwrap_escaped: re-wrap a translation in escaped quotes with
    BSI escaping (apostrophes escaped)."""
    return '\\"' + escape_struct_field(translated, True) + '\\"'

def detect_encoding(raw_bytes: bytes) -> tuple[str, bool]:
    """Detects encoding and if a BOM is present, returning (encoding, has_bom)."""
    # 1. UTF-16 BOMs
    if raw_bytes.startswith(b'\xff\xfe'):
        return 'utf-16le', True
    if raw_bytes.startswith(b'\xfe\xff'):
        return 'utf-16be', True
        
    has_utf8_bom = raw_bytes.startswith(b'\xef\xbb\xbf')
    # Strip UTF-8 BOM if present for detection
    content_bytes = raw_bytes[3:] if has_utf8_bom else raw_bytes
    
    # 2. Try strict UTF-8
    try:
        decoded_utf8 = content_bytes.decode('utf-8')
        # Check if it contains cp1251 content instead (mojibake scenario)
        try:
            decoded_cp1251 = content_bytes.decode('cp1251')
            cyrillic_in_cp1251 = sum(1 for c in decoded_cp1251 if '\u0400' <= c <= '\u04FF')
            cyrillic_in_utf8 = sum(1 for c in decoded_utf8 if '\u0400' <= c <= '\u04FF')
            
            utf8_non_ascii = sum(1 for c in decoded_utf8 if ord(c) > 127)
            if cyrillic_in_cp1251 > 0 and cyrillic_in_utf8 == 0 and utf8_non_ascii > 0:
                return 'cp1251', False
        except Exception:
            pass
            
        return 'utf-8-sig' if has_utf8_bom else 'utf-8', has_utf8_bom
    except UnicodeDecodeError:
        pass
        
    # 3. Try cp1251 (Russian)
    try:
        decoded_cp1251 = content_bytes.decode('cp1251')
        cyrillic_count = sum(1 for c in decoded_cp1251 if '\u0400' <= c <= '\u04FF')
        if cyrillic_count > 0:
            return 'cp1251', False
    except Exception:
        pass
        
    # 5. Try chardet / cchardet fallback
    for lib_name in ('cchardet', 'chardet'):
        try:
            lib = __import__(lib_name)
            detected = lib.detect(content_bytes)
            if detected and detected.get('encoding'):
                return detected['encoding'], False
        except ImportError:
            pass
            
    # 6. Fallback to cp1252
    return 'cp1252', False

def write_with_bom(filepath: Path, content: str, encoding: str, has_bom: bool) -> None:
    """Writes content to filepath with proper encoding and BOM, forcing CRLF endings."""
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Drop a leading BOM char if present: the utf-8-sig / utf-16 codecs below
    # add their own BOM, so a U+FEFF left in `content` (utf-16le decode keeps it)
    # would otherwise produce a doubled BOM.
    if content.startswith('﻿'):
        content = content[1:]

    # Force CRLF line endings by first normalizing all to LF, then replacing with CRLF
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    content = content.replace('\n', '\r\n')
    
    if encoding == 'utf-8-sig' or (encoding == 'utf-8' and has_bom):
        raw_bytes = content.encode('utf-8-sig')
    elif encoding in ('utf-16', 'utf-16le', 'utf-16be') or (encoding.startswith('utf-16') and has_bom):
        raw_bytes = content.encode('utf-16')
    else:
        raw_bytes = content.encode(encoding)
        
    filepath.write_bytes(raw_bytes)

def split_lines_keep_endings(content: str) -> list[str]:
    """Splits a string into lines preserving line endings."""
    lines = []
    current = []
    for char in content:
        current.append(char)
        if char == '\n':
            lines.append("".join(current))
            current = []
    if current:
        lines.append("".join(current))
    return lines

class UnrealElement:
    pass

class CommentOrEmptyElement(UnrealElement):
    def __init__(self, line: str):
        self.line = line

class SectionHeaderElement(UnrealElement):
    def __init__(self, section_name: str, line: str):
        self.section_name = section_name
        self.line = line

class KeyValueElement(UnrealElement):
    def __init__(self, section: str, key: str, before_eq: str, after_eq: str, 
                 value: str, quote_char: str | None, inline_comment: str, 
                 raw_lines: list[str]):
        self.section = section
        self.key = key
        self.before_eq = before_eq
        self.after_eq = after_eq
        self.value = value
        self.quote_char = quote_char
        self.inline_comment = inline_comment
        self.raw_lines = raw_lines
        self.key_idx = 0

def parse_file(content: str) -> list[UnrealElement]:
    """Parses INI content using a custom state-machine."""
    raw_lines = split_lines_keep_endings(content)
    elements = []
    
    current_section = ""
    in_multiline = False
    multiline_key = ""
    multiline_before_eq = ""
    multiline_after_eq = ""
    multiline_quote_char = None
    multiline_value_lines = []
    multiline_raw_lines = []
    
    section_re = re.compile(r'^\s*\[([^\]]+)\]')
    
    for line in raw_lines:
        stripped_line = line.strip()
        
        if in_multiline:
            multiline_raw_lines.append(line)
            # Scan character by character for the closing quote matching multiline_quote_char
            escaped = False
            closing_quote_idx = -1
            for i, char in enumerate(line):
                if char == '\\':
                    escaped = not escaped
                elif char == multiline_quote_char:
                    if not escaped:
                        closing_quote_idx = i
                        break
                    escaped = False
                else:
                    escaped = False
            
            if closing_quote_idx != -1:
                val_part = line[:closing_quote_idx]
                rest_part = line[closing_quote_idx+1:]
                
                multiline_value_lines.append(val_part)
                
                # Suffix part after the closing quote is the inline comment
                # Strip line endings from rest_part
                if rest_part.endswith('\r\n'):
                    rest_part = rest_part[:-2]
                elif rest_part.endswith('\n') or rest_part.endswith('\r'):
                    rest_part = rest_part[:-1]
                
                inline_comment = rest_part
                full_val_str = "".join(multiline_value_lines)
                de_escaped_val = de_escape(full_val_str)
                
                elements.append(KeyValueElement(
                    section=current_section,
                    key=multiline_key,
                    before_eq=multiline_before_eq,
                    after_eq=multiline_after_eq,
                    value=de_escaped_val,
                    quote_char=multiline_quote_char,
                    inline_comment=inline_comment,
                    raw_lines=list(multiline_raw_lines)
                ))
                
                in_multiline = False
                multiline_value_lines = []
                multiline_raw_lines = []
            else:
                multiline_value_lines.append(line)
            continue
            
        # Standard line processing
        sec_match = section_re.match(stripped_line)
        if sec_match:
            current_section = sec_match.group(1)
            elements.append(SectionHeaderElement(current_section, line))
            continue
            
        if stripped_line.startswith(';') or stripped_line.startswith('//') or not stripped_line:
            elements.append(CommentOrEmptyElement(line))
            continue
            
        if '=' not in line:
            elements.append(CommentOrEmptyElement(line))
            continue
            
        before_eq, _, after_eq_and_val = line.partition('=')
        key_name = before_eq.strip()
        if not key_name or key_name.startswith(';') or key_name.startswith('//'):
            elements.append(CommentOrEmptyElement(line))
            continue
            
        lstripped_val = after_eq_and_val.lstrip()
        after_eq_len = len(after_eq_and_val) - len(lstripped_val)
        after_eq_str = after_eq_and_val[:after_eq_len]
        
        quote_char = None
        if lstripped_val.startswith('"'):
            quote_char = '"'
        elif lstripped_val.startswith("'"):
            quote_char = "'"
            
        if quote_char:
            quoted_content = lstripped_val[1:]
            escaped = False
            closing_quote_idx = -1
            for i, char in enumerate(quoted_content):
                if char == '\\':
                    escaped = not escaped
                elif char == quote_char:
                    if not escaped:
                        closing_quote_idx = i
                        break
                    escaped = False
                else:
                    escaped = False
                    
            if closing_quote_idx != -1:
                val_part = quoted_content[:closing_quote_idx]
                rest_part = quoted_content[closing_quote_idx+1:]
                
                if rest_part.endswith('\r\n'):
                    rest_part = rest_part[:-2]
                elif rest_part.endswith('\n') or rest_part.endswith('\r'):
                    rest_part = rest_part[:-1]
                    
                inline_comment = rest_part
                de_escaped_val = de_escape(val_part)
                
                elements.append(KeyValueElement(
                    section=current_section,
                    key=key_name,
                    before_eq=before_eq,
                    after_eq=after_eq_str,
                    value=de_escaped_val,
                    quote_char=quote_char,
                    inline_comment=inline_comment,
                    raw_lines=[line]
                ))
            else:
                in_multiline = True
                multiline_key = key_name
                multiline_before_eq = before_eq
                multiline_after_eq = after_eq_str
                multiline_quote_char = quote_char
                multiline_value_lines = [quoted_content]
                multiline_raw_lines = [line]
        else:
            # Unquoted value: inline comment starts at first ';' or '//' that is
            # NOT inside a quoted span. Struct values like
            #   Subtitles[0]=(Text="I must go now; my people need me.",Time=0)
            # carry ';' inside quotes — splitting there truncates the dialogue.
            comment_start_idx = -1
            i = 0
            val_len = len(lstripped_val)
            in_quote = False
            esc = False
            while i < val_len:
                ch = lstripped_val[i]
                if in_quote:
                    if esc:
                        esc = False
                    elif ch == '\\':
                        esc = True
                    elif ch == '"':
                        in_quote = False
                    i += 1
                    continue
                if ch == '"':
                    in_quote = True
                elif ch == ';':
                    comment_start_idx = i
                    break
                elif lstripped_val[i:i+2] == '//':
                    comment_start_idx = i
                    break
                i += 1
                
            if comment_start_idx != -1:
                val_part = lstripped_val[:comment_start_idx]
                rest_part = lstripped_val[comment_start_idx:]
            else:
                val_part = lstripped_val
                rest_part = ""
                
            if rest_part:
                if rest_part.endswith('\r\n'):
                    rest_part = rest_part[:-2]
                elif rest_part.endswith('\n') or rest_part.endswith('\r'):
                    rest_part = rest_part[:-1]
            else:
                if val_part.endswith('\r\n'):
                    val_part = val_part[:-2]
                elif val_part.endswith('\n') or val_part.endswith('\r'):
                    val_part = val_part[:-1]
            
            # Preserve space between the unquoted value and the inline comment
            trailing_spaces = val_part[len(val_part.rstrip()):]
            value_str = val_part.strip()
            rest_part = trailing_spaces + rest_part
            
            elements.append(KeyValueElement(
                section=current_section,
                key=key_name,
                before_eq=before_eq,
                after_eq=after_eq_str,
                value=value_str,
                quote_char=None,
                inline_comment=rest_part,
                raw_lines=[line]
            ))
            
    # Assign duplicate keys indexing
    section_key_counts = {}
    for el in elements:
        if isinstance(el, KeyValueElement):
            sec_key = (el.section, el.key)
            idx = section_key_counts.get(sec_key, 0)
            el.key_idx = idx
            section_key_counts[sec_key] = idx + 1
            
    return elements

class UnrealParser(BaseParser):
    engine = "unreal"

    def engine_prompt_addon(self) -> str:
        return (
            "TECHNICAL STRINGS (UI / SUBTITLES): these strings come from a game's "
            "localization files and may contain format specifiers or markup.\n"
            "FORMAT SPECIFIERS: preserve %s, %d, %f, %i, {0}, {1}, {UserName}, "
            "{value} and similar patterns EXACTLY — they are filled in at runtime.\n"
            "ESCAPE SEQUENCES: keep literal \\n and \\t as-is; do NOT convert them "
            "into real newlines or tabs inside the JSON string.\n"
            "TONE: use a neutral, professional register suitable for UI labels, "
            "subtitles, and system messages. Avoid overly literary or conversational style."
        )

    @staticmethod
    def detect(root: str) -> bool:
        """True if the directory contains Unreal Engine 3 localization files."""
        for int_file in iter_int_files(root):
            # Scan first 2KB for valid INI sections and key-value pairs
            try:
                raw = int_file.read_bytes()[:2048]
                encoding, _ = detect_encoding(raw)
                sample = raw.decode(encoding, errors='ignore')
                has_section = bool(re.search(r'^\s*\[[^\]]+\]', sample, re.M))
                has_kv = bool(re.search(r'^\s*[^\s=]+=', sample, re.M))
                if has_section and has_kv:
                    return True
            except Exception:
                continue
        return False

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        """Extract all translatable strings from INI files."""
        strings = []
        
        # Find all INT files recursively under root inside any Localization folder
        int_files = list(iter_int_files(root))

        if sub_paths:
            # Filter if sub_paths are specified
            rel_sub_paths = [Path(p).as_posix() for p in sub_paths]
            int_files = [f for f in int_files if f.relative_to(root).as_posix() in rel_sub_paths]

        for file_path in int_files:
            rel_file = file_path.relative_to(root).as_posix()
            try:
                raw = file_path.read_bytes()
                encoding, _ = detect_encoding(raw)
                content = raw.decode(encoding)
                
                elements = parse_file(content)
                for el in elements:
                    if not isinstance(el, KeyValueElement) or not el.value:
                        continue
                    base_path = [el.section, el.key, str(el.key_idx)]
                    ctx = f"Section: [{el.section}] | Key: {el.key}"
                    if is_struct_value(el.value):
                        # Pull each translatable field out of the struct so the
                        # LLM only ever sees clean text — never (Text="...",Time=0).
                        for name, fidx, raw_content, _s, _e, esc in iter_struct_fields(el.value):
                            field_val = de_escape(raw_content)
                            if not is_translatable(name, field_val):
                                continue
                            strings.append(self._mk(
                                file=rel_file,
                                path=base_path + [name, str(fidx)],
                                original=field_val,
                                context=ctx,
                            ))
                    else:
                        # Plain value. Some games (BioShock Infinite) wrap the
                        # whole value in escaped quotes: Subtitle=\"text\". Unwrap
                        # + de-escape so the LLM sees clean text; re-wrapped on
                        # inject. The de-escaped form is the stable-id input.
                        clean = unwrap_escaped(el.value)
                        if is_translatable(el.key, clean):
                            strings.append(self._mk(
                                file=rel_file,
                                path=base_path,
                                original=clean,
                                context=ctx,
                            ))
            except Exception as e:
                logger.error(f"Failed to extract from {rel_file}: {e}")

        return strings

    def _inject_struct(self, rel_source: str, base_path: list[str],
                       value: str, translations: dict[str, str]) -> tuple[str, int]:
        """Replace translated fields inside a struct value in place. Returns the
        rebuilt value and the count of fields actually replaced. Rebuilds
        right-to-left so earlier spans keep their offsets; the same id math as
        extract (iter_struct_fields) guarantees lookups line up."""
        replacements = []  # (start, end, escaped_new)
        for name, fidx, raw_content, start, end, esc in iter_struct_fields(value):
            field_val = de_escape(raw_content)
            if not is_translatable(name, field_val):
                continue
            str_id = self._mk(
                file=rel_source, path=base_path + [name, str(fidx)],
                original=field_val,
            ).id
            if str_id not in translations:
                continue
            # Re-escape with the SAME dialect this field used (escaped-quote
            # fields also escape apostrophes), so the struct stays consistent.
            replacements.append((start, end, escape_struct_field(translations[str_id], esc)))

        if not replacements:
            return value, 0

        out = value
        for start, end, new in reversed(replacements):
            out = out[:start] + new + out[end:]
        return out, len(replacements)

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        """Inject translations into target language files."""
        if not target_lang:
            return 0

        # Map target language to UE3 folder code
        ue3_lang = UE3_LANG_MAP.get(target_lang)
        if not ue3_lang:
            ue3_lang = target_lang[:3].upper()
            logger.warning(f"Language {target_lang!r} not found in UE3_LANG_MAP. Falling back to {ue3_lang!r}")

        # Find INT files as source recursively
        int_files = list(iter_int_files(root))

        if sub_paths:
            rel_sub_paths = [Path(p).as_posix() for p in sub_paths]
            int_files = [f for f in int_files if f.relative_to(root).as_posix() in rel_sub_paths]

        injected_count = 0

        for source_path in int_files:
            rel_source = source_path.relative_to(root).as_posix()
            try:
                # Read English file
                raw = source_path.read_bytes()
                encoding, has_bom = detect_encoding(raw)
                content = raw.decode(encoding)
                
                elements = parse_file(content)
                
                # Determine target file path: locate 'Localization' in parts,
                # change the language directory after it, and update filename suffix.
                parts = list(source_path.parts)
                loc_idx = -1
                for idx, part in enumerate(parts):
                    if part.lower() == 'localization':
                        loc_idx = idx
                        break
                        
                if loc_idx != -1 and loc_idx + 1 < len(parts):
                    parts[loc_idx + 1] = ue3_lang
                    
                filename = parts[-1]
                if '.' in filename:
                    base, _, ext = filename.rpartition('.')
                    parts[-1] = f"{base}.{ue3_lang}"
                else:
                    parts[-1] = f"{filename}.{ue3_lang}"
                    
                target_path = Path(*parts)
                
                # If target file exists, back it up
                if target_path.exists():
                    self.backup_file(root, str(target_path))

                # Reconstruct content
                output_lines = []
                for el in elements:
                    if isinstance(el, CommentOrEmptyElement):
                        output_lines.append(el.line)
                    elif isinstance(el, SectionHeaderElement):
                        output_lines.append(el.line)
                    elif isinstance(el, KeyValueElement):
                        base_path = [el.section, el.key, str(el.key_idx)]

                        if is_struct_value(el.value):
                            # Surgically replace only translated fields inside the
                            # struct, leaving Time/flags/structure byte-untouched.
                            new_value, n = self._inject_struct(
                                rel_source, base_path, el.value, translations
                            )
                            if n == 0:
                                output_lines.extend(el.raw_lines)
                                continue
                            injected_count += n
                            output_lines.append(
                                f"{el.before_eq}={el.after_eq}{new_value}{el.inline_comment}\n"
                            )
                            continue

                        # Escape-wrapped plain values (BSI \"text\") use the
                        # unwrapped, de-escaped form as the id input — must match
                        # extract exactly.
                        wrapped = is_escaped_wrapped(el.value)
                        id_input = unwrap_escaped(el.value) if wrapped else el.value
                        str_id = self._mk(
                            file=rel_source, path=base_path, original=id_input
                        ).id

                        if str_id not in translations:
                            # Untranslated: emit the original line(s) verbatim.
                            # Re-escaping is lossy for stray backslashes in real
                            # game text (e.g. "/!\ WARNING /!\"), so never round-
                            # trip a value we aren't actually changing.
                            output_lines.extend(el.raw_lines)
                            continue

                        val_to_write = translations[str_id]
                        injected_count += 1

                        # Format output line keeping whitespace/inline comment structure
                        if el.quote_char:
                            escaped_val = escape(val_to_write, el.quote_char)
                            output_lines.append(f"{el.before_eq}={el.after_eq}{el.quote_char}{escaped_val}{el.quote_char}{el.inline_comment}\n")
                        elif wrapped:
                            # Re-wrap in escaped quotes (BSI), preserving any
                            # surrounding whitespace the original value had.
                            stripped = el.value.strip()
                            lead = el.value[:len(el.value) - len(el.value.lstrip())]
                            trail = el.value[len(el.value.rstrip()):]
                            output_lines.append(f"{el.before_eq}={el.after_eq}{lead}{rewrap_escaped(val_to_write)}{trail}{el.inline_comment}\n")
                        else:
                            # Note: unquoted values shouldn't have quotes added
                            output_lines.append(f"{el.before_eq}={el.after_eq}{val_to_write}{el.inline_comment}\n")
                            
                # Join content and write using helper
                output_content = "".join(output_lines)
                write_with_bom(target_path, output_content, encoding, has_bom)
                
            except Exception as e:
                logger.error(f"Failed to inject into {rel_source}: {e}")

        return injected_count
