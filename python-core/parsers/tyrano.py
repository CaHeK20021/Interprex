"""Parser for TyranoBuilder and TyranoScript games (.ks scenario scripts).

TyranoBuilder games are HTML5/JavaScript visual novels packaged with Electron,
NW.js, or plain web. Scenarios are written in KAG-derived TyranoScript (.ks).

Structure:
  <app_dir>/tyrano/tyrano.js
  <app_dir>/data/scenario/*.ks
  <app_dir>/data/others/       (fonts and assets)
  <app_dir>/tyrano/css/font.css
"""

from __future__ import annotations

import os
import re
import sys
import hashlib
from pathlib import Path

from .base import BaseParser, TranslationString, make_id, update_metadata


_TRAILING_TAGS_RE = re.compile(r'((?:\s*\[(?:p|l|r|cm|er)\s*\])+\s*)$')
_UI_TAG_RE = re.compile(r'(\[(?:glink|tb_ptext_show|ptext|button|link)\b[^\]]*?\])', re.IGNORECASE)
_ATTR_TEXT_RE = re.compile(r'(\btext\s*=\s*")([^"]*)(")|(\btext\s*=\s*\')([^\']*)(\')')
_EMB_RE = re.compile(r'\[emb\b[^\]]*\]', re.IGNORECASE)


def _find_tyrano_app_dir(root: str | Path) -> Path | None:
    """Locate the TyranoBuilder application root directory containing tyrano/ and data/scenario/."""
    r = Path(root)
    if not r.is_dir():
        return None
    candidates = [
        r,
        r / "resources" / "app",
        r / "www",
        r / "package.nw",
    ]
    for c in candidates:
        if not c.is_dir():
            continue
        tyrano_dir = c / "tyrano"
        scen_dir = c / "data" / "scenario"
        if tyrano_dir.is_dir() and scen_dir.is_dir():
            has_tyrano_js = (tyrano_dir / "tyrano.js").is_file() or any(tyrano_dir.glob("*.js"))
            has_ks = any(scen_dir.glob("*.ks"))
            if has_tyrano_js and has_ks:
                return c
    return None


def _find_font_source() -> Path | None:
    """Locate the bundled NotoSans-Regular.ttf font."""
    candidate = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSans-Regular.ttf"
    if candidate.is_file():
        return candidate
    candidate = Path.cwd() / "python-core" / "assets" / "fonts" / "NotoSans-Regular.ttf"
    if candidate.is_file():
        return candidate
    return None


def _is_non_latin(lang: str | None) -> bool:
    """True if target language needs Unicode font fallback (Cyrillic, CJK, etc.)."""
    if not lang:
        return True
    l = lang.lower()
    return any(c in l for c in ("ru", "rus", "bel", "ukr", "bul", "serb", "cyr", "ja", "jp", "ko", "kor", "zh", "chi"))


def _replace_ui_tag_text(tag_str: str, new_text: str) -> str:
    """Replace only the text="..." attribute inside a UI tag, preserving all other attributes."""
    def repl(m: re.Match) -> str:
        if m.group(1) is not None:
            safe_text = new_text.replace('"', '&quot;') if '"' in new_text else new_text
            return f"{m.group(1)}{safe_text}{m.group(3)}"
        else:
            safe_text = new_text.replace("'", "&#39;") if "'" in new_text else new_text
            return f"{m.group(4)}{safe_text}{m.group(6)}"
    return _ATTR_TEXT_RE.sub(repl, tag_str, count=1)


def _repair_tyrano_inline_tags(orig: str, trans: str) -> str:
    """Ensure inline variables and formatting tags from orig are preserved verbatim in trans."""
    orig_embs = _EMB_RE.findall(orig)
    trans_embs = re.findall(r'\[(?:emb|вставка|exp)\b[^\]]*\]', trans, flags=re.IGNORECASE)
    if orig_embs:
        if len(orig_embs) == len(trans_embs):
            for oe, te in zip(orig_embs, trans_embs):
                if oe != te:
                    trans = trans.replace(te, oe, 1)
        elif len(trans_embs) == 0 and len(orig_embs) == 1:
            if orig.strip().startswith(orig_embs[0]):
                trans = orig_embs[0] + " " + trans.lstrip()

    orig_fonts = re.findall(r'\[font\b[^\]]*\]', orig, flags=re.IGNORECASE)
    trans_fonts = re.findall(r'\[font\b[^\]]*\]', trans, flags=re.IGNORECASE)
    if orig_fonts and len(orig_fonts) == len(trans_fonts):
        for of, tf in zip(orig_fonts, trans_fonts):
            if of != tf:
                trans = trans.replace(tf, of, 1)

    return trans


class TyranoParser(BaseParser):
    engine = "tyrano"

    def engine_prompt_addon(self) -> str:
        return (
            "ENGINE: TyranoBuilder / TyranoScript (visual novel).\n"
            "CONTROL CODES AND INLINE TAGS:\n"
            "- Preserve inline tags like [emb exp=\"...\"], [l], [r], [font ...], [resetfont] EXACTLY.\n"
            "- Do NOT alter, remove, or translate code inside tags (e.g. keep [emb exp= \"f.name\" ] intact).\n"
            "- Do NOT translate speaker prefixes or UI markup unless instructed.\n"
            "TONE: Natural dialogue and narrative for a visual novel. Keep choice buttons concise."
        )

    @staticmethod
    def detect(root: str) -> bool:
        return _find_tyrano_app_dir(root) is not None

    def _iter_ks_files(self, root: str) -> list[Path]:
        app_dir = _find_tyrano_app_dir(root)
        if not app_dir:
            return []
        scen_dir = app_dir / "data" / "scenario"
        if not scen_dir.is_dir():
            return []
        files = []
        for p in scen_dir.rglob("*.ks"):
            # Skip system macros/preloads and editor preview file
            rel_parts = p.relative_to(scen_dir).parts
            if "system" in rel_parts:
                continue
            if p.name.lower() == "_preview.ks":
                continue
            files.append(p)
        return sorted(files)

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        results: list[TranslationString] = []
        r_path = Path(root)

        for ks_path in self._iter_ks_files(root):
            rel_file = ks_path.relative_to(r_path).as_posix()
            if sub_paths is not None:
                if not (rel_file in sub_paths or any(rel_file.startswith(sp) for sp in sub_paths)):
                    continue

            try:
                lines = ks_path.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
            except Exception:
                continue

            in_script = False
            in_html = False
            current_speaker = ""

            for line_idx, line in enumerate(lines):
                s = line.strip()
                if not s:
                    continue
                if s.startswith(";") or s.startswith("//"):
                    continue

                # Script and HTML block tracking
                if "[iscript]" in s or "@iscript" in s:
                    in_script = True
                    continue
                if "[endscript]" in s or "@endscript" in s:
                    in_script = False
                    continue
                if in_script:
                    continue

                if "[html]" in s or "@html" in s:
                    in_html = True
                    continue
                if "[endhtml]" in s or "@endhtml" in s:
                    in_html = False
                    continue
                if in_html:
                    continue

                # Labels
                if s.startswith("*"):
                    continue

                # Speaker lines (#Name or reset #)
                if s.startswith("#"):
                    speaker_name = s[1:].strip()
                    if speaker_name:
                        current_speaker = speaker_name
                        results.append(
                            self._mk(
                                file=rel_file,
                                path=["line", str(line_idx), "speaker"],
                                original=speaker_name,
                                context="Speaker name",
                            )
                        )
                    else:
                        current_speaker = ""
                    continue

                # UI tags with text="..." (glink, tb_ptext_show, ptext, button, link)
                ui_matches = list(_UI_TAG_RE.finditer(line))
                if ui_matches:
                    found_ui = False
                    for tag_idx, m_tag in enumerate(ui_matches):
                        tag_str = m_tag.group(1)
                        m_text = _ATTR_TEXT_RE.search(tag_str)
                        if m_text:
                            text_val = m_text.group(2) if m_text.group(2) is not None else m_text.group(5)
                            if text_val and text_val.strip():
                                tag_lower = tag_str.split()[0].lstrip("[").lower()
                                ctx = "Choice button (keep short)" if tag_lower in ("glink", "button", "link") else f"UI {tag_lower} text"
                                path_tag = ["line", str(line_idx), "attr_text"] if len(ui_matches) == 1 else ["line", str(line_idx), f"attr_text_{tag_idx}"]
                                results.append(
                                    self._mk(
                                        file=rel_file,
                                        path=path_tag,
                                        original=text_val,
                                        context=ctx,
                                    )
                                )
                                found_ui = True
                    if found_ui:
                        continue

                # Dialogue and narrative text
                text_without_tags = re.sub(r'\[[^\]]*\]', '', s).strip()
                if not text_without_tags:
                    continue
                if not any(c.isalpha() for c in text_without_tags):
                    continue

                clean_line = line.rstrip("\r\n")
                m_trail = _TRAILING_TAGS_RE.search(clean_line)
                if m_trail:
                    dialogue_text = clean_line[:m_trail.start()].strip()
                else:
                    dialogue_text = clean_line.strip()

                if dialogue_text:
                    ctx = f"Speaker: {current_speaker}" if current_speaker else "Narrator"
                    results.append(
                        self._mk(
                            file=rel_file,
                            path=["line", str(line_idx), "dialogue"],
                            original=dialogue_text,
                            context=ctx,
                        )
                    )

        return results

    def inject(
        self,
        root: str,
        translations: dict[str, str],
        target_lang: str | None = None,
        sub_paths: list[str] | None = None,
    ) -> int:
        count = 0
        r_path = Path(root)

        for ks_path in self._iter_ks_files(root):
            rel_file = ks_path.relative_to(r_path).as_posix()
            if sub_paths is not None:
                if not (rel_file in sub_paths or any(rel_file.startswith(sp) for sp in sub_paths)):
                    continue

            try:
                lines = ks_path.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
            except Exception:
                continue

            modified = False
            new_lines = []
            in_script = False
            in_html = False

            for line_idx, line in enumerate(lines):
                s = line.strip()
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                leading_ws = line[:len(line) - len(line.lstrip())]

                if not s or s.startswith(";") or s.startswith("//"):
                    new_lines.append(line)
                    continue

                if "[iscript]" in s or "@iscript" in s:
                    in_script = True
                    new_lines.append(line)
                    continue
                if "[endscript]" in s or "@endscript" in s:
                    in_script = False
                    new_lines.append(line)
                    continue
                if in_script:
                    new_lines.append(line)
                    continue

                if "[html]" in s or "@html" in s:
                    in_html = True
                    new_lines.append(line)
                    continue
                if "[endhtml]" in s or "@endhtml" in s:
                    in_html = False
                    new_lines.append(line)
                    continue
                if in_html:
                    new_lines.append(line)
                    continue

                if s.startswith("*"):
                    new_lines.append(line)
                    continue

                # Speaker lines (#Name)
                if s.startswith("#"):
                    speaker_name = s[1:].strip()
                    if speaker_name:
                        str_id = make_id(self.engine, rel_file, ["line", str(line_idx), "speaker"], speaker_name)
                        if str_id in translations:
                            trans = translations[str_id].strip()
                            new_lines.append(f"{leading_ws}#{trans}{newline}")
                            modified = True
                            count += 1
                            continue
                    new_lines.append(line)
                    continue

                # UI tags with text="..."
                ui_matches = list(_UI_TAG_RE.finditer(line))
                if ui_matches:
                    line_mod = False
                    cur_line = line
                    # Process in reverse order so spans don't shift
                    for tag_idx in range(len(ui_matches) - 1, -1, -1):
                        m_tag = ui_matches[tag_idx]
                        tag_str = m_tag.group(1)
                        m_text = _ATTR_TEXT_RE.search(tag_str)
                        if m_text:
                            text_val = m_text.group(2) if m_text.group(2) is not None else m_text.group(5)
                            if text_val and text_val.strip():
                                path_tag = ["line", str(line_idx), "attr_text"] if len(ui_matches) == 1 else ["line", str(line_idx), f"attr_text_{tag_idx}"]
                                str_id = make_id(self.engine, rel_file, path_tag, text_val)
                                if str_id in translations:
                                    trans_text = translations[str_id].strip()
                                    new_tag_str = _replace_ui_tag_text(tag_str, trans_text)
                                    cur_line = cur_line[:m_tag.start(1)] + new_tag_str + cur_line[m_tag.end(1):]
                                    line_mod = True
                                    count += 1
                    if line_mod:
                        new_lines.append(cur_line)
                        modified = True
                        continue
                    else:
                        new_lines.append(line)
                        continue

                # Dialogue and narrative text
                text_without_tags = re.sub(r'\[[^\]]*\]', '', s).strip()
                if not text_without_tags or not any(c.isalpha() for c in text_without_tags):
                    new_lines.append(line)
                    continue

                clean_line = line.rstrip("\r\n")
                m_trail = _TRAILING_TAGS_RE.search(clean_line)
                suffix = m_trail.group(1) if m_trail else ""
                dialogue_text = clean_line[:m_trail.start()].strip() if m_trail else clean_line.strip()

                if dialogue_text:
                    str_id = make_id(self.engine, rel_file, ["line", str(line_idx), "dialogue"], dialogue_text)
                    if str_id in translations:
                        trans = translations[str_id]
                        repaired = _repair_tyrano_inline_tags(dialogue_text, trans)
                        new_lines.append(f"{leading_ws}{repaired}{suffix}{newline}")
                        modified = True
                        count += 1
                        continue

                new_lines.append(line)

            if modified:
                self.backup_file(root, str(ks_path))
                ks_path.write_text("".join(new_lines), encoding="utf-8")

        # Install Cyrillic font and patch tyrano/css/font.css if translating to non-Latin
        app_dir = _find_tyrano_app_dir(root)
        if app_dir and _is_non_latin(target_lang):
            self._install_font_and_css(root, app_dir)

        self.finalize_backups(root)
        return count

    def _install_font_and_css(self, root: str, app_dir: Path) -> None:
        """Copy NotoSans-Regular.ttf to data/others/InterprexNoto.ttf and patch tyrano/css/font.css."""
        font_src = _find_font_source()
        if not font_src:
            return

        dest_font_dir = app_dir / "data" / "others"
        dest_font_dir.mkdir(parents=True, exist_ok=True)
        dest_font = dest_font_dir / "InterprexNoto.ttf"

        if not dest_font.exists():
            font_bytes = font_src.read_bytes()
            dest_font.write_bytes(font_bytes)
            mod_sha = hashlib.sha256(font_bytes).hexdigest()
            rel_font_path = dest_font.relative_to(Path(root)).as_posix()
            update_metadata(root, rel_font_path, "", mod_sha, "created")

        font_css = app_dir / "tyrano" / "css" / "font.css"
        if font_css.is_file():
            css_content = font_css.read_text(encoding="utf-8", errors="replace")
            if "InterprexNoto" not in css_content:
                self.backup_file(root, str(font_css))
                font_rule = (
                    "\n/* Interprex Font Override */\n"
                    "@font-face {\n"
                    "    font-family: 'InterprexNoto';\n"
                    "    src: url('../../data/others/InterprexNoto.ttf') format('truetype');\n"
                    "}\n"
                    "body, #tyrano_base, .tyrano_base, .message_inner, .message_inner p, "
                    ".message_inner span, .glink_button, .layer_event_click, .ptext {\n"
                    "    font-family: 'InterprexNoto', 'Noto Sans', sans-serif !important;\n"
                    "}\n"
                )
                font_css.write_text(css_content + font_rule, encoding="utf-8")
