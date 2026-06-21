from __future__ import annotations

import os
import re
import html
import logging

from .base import BaseParser, TranslationString, make_id

logger = logging.getLogger(__name__)

PASSAGE_RE = re.compile(r'(<tw-passagedata\s+[^>]*?>)([\s\S]*?)(</tw-passagedata>)', re.IGNORECASE)

def find_twine_html_files(root: str) -> list[str]:
    html_files = []
    try:
        max_depth = 3
        root_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.count(os.sep) - root_depth
            if depth >= max_depth:
                dirnames.clear()
            for f in filenames:
                if f.endswith(".html"):
                    filepath = os.path.join(dirpath, f)
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="ignore") as file:
                            head = file.read(1024 * 1024)
                            if "<tw-storydata" in head or "<tw-passagedata" in head:
                                rel_path = os.path.relpath(filepath, root).replace("\\", "/")
                                html_files.append(rel_path)
                    except Exception:
                        continue
    except Exception:
        pass
    return html_files

def get_attr(attrs_str, attr_name):
    match = re.search(fr'{attr_name}="([^"]*)"', attrs_str)
    return match.group(1) if match else None

def escape_twine(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;')

class TwineParser(BaseParser):
    engine = "twine"

    def engine_prompt_addon(self) -> str:
        return (
            "ENGINE RULES:\n"
            "This is a Twine/SugarCube game. You will translate story text and UI choices.\n"
            "DO NOT translate target link names, variables (starting with $), or macro code.\n"
            "Keep formatting, tags, and special characters exactly intact.\n"
        )

    @staticmethod
    def detect(root: str) -> bool:
        # Check recursively up to depth 3 for any Twine html
        return len(find_twine_html_files(root)) > 0

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results = []
        
        # Locate the html files
        html_files = []
        if sub_paths:
            for p in sub_paths:
                if p.endswith(".html") and os.path.isfile(os.path.join(root, p)):
                    html_files.append(p)
        else:
            html_files = find_twine_html_files(root)

        for rel_file in html_files:
            abs_path = os.path.join(root, rel_file)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    html_content = f.read()
            except Exception as e:
                logger.error(f"Failed to read twine file {rel_file}: {e}")
                continue

            for match in PASSAGE_RE.finditer(html_content):
                header = match.group(1)
                content_escaped = match.group(2)
                
                name = get_attr(header, "name")
                tags = get_attr(header, "tags") or ""
                
                if not name:
                    continue
                    
                # Skip system passages
                if "stylesheet" in tags or "script" in tags or name == "StoryInit":
                    continue

                content = html.unescape(content_escaped)
                token_re = re.compile(r'(<style[^>]*?>[\s\S]*?</style>|<script[^>]*?>[\s\S]*?</script>|<<[\s\S]*?>>|<[^>]*?>|\[\[[^\]]*?\]\])', re.IGNORECASE | re.MULTILINE)
                tokens = token_re.split(content)

                for idx, token in enumerate(tokens):
                    if not token:
                        continue

                    # 1. Twine link: [[Label|Passage]] or [[Passage]]
                    if token.startswith('[[') and token.endswith(']]'):
                        body = token[2:-2].strip()
                        body_unescaped = html.unescape(body)
                        if '|' in body_unescaped:
                            label = body_unescaped.split('|', 1)[0].strip()
                            if label and re.search(r'[a-zA-Zа-яА-Я]', label):
                                results.append(self._mk(rel_file, [f"passage:{name}", f"token:{idx}", "link_label"], label, f"Link in passage: {name}"))
                        elif '->' in body_unescaped:
                            label = body_unescaped.split('->', 1)[0].strip()
                            if label and re.search(r'[a-zA-Zа-яА-Я]', label):
                                results.append(self._mk(rel_file, [f"passage:{name}", f"token:{idx}", "link_label"], label, f"Link in passage: {name}"))
                        elif '<-' in body_unescaped:
                            label = body_unescaped.split('<-', 1)[1].strip()
                            if label and re.search(r'[a-zA-Zа-яА-Я]', label):
                                results.append(self._mk(rel_file, [f"passage:{name}", f"token:{idx}", "link_label"], label, f"Link in passage: {name}"))
                        else:
                            if body_unescaped and re.search(r'[a-zA-Zа-яА-Я]', body_unescaped):
                                results.append(self._mk(rel_file, [f"passage:{name}", f"token:{idx}", "link_target"], body_unescaped, f"Link in passage: {name}"))

                    # 2. SugarCube Macro: <<macro ...>>
                    elif token.startswith('<<') and token.endswith('>>'):
                        body = token[2:-2].strip()
                        body_unescaped = html.unescape(body)
                        words = body_unescaped.split()
                        if words:
                            macro_name = words[0].lower()
                            if macro_name in ("button", "dialog", "link", "print", "notify", "append", "prepend", "replace"):
                                quotes = re.findall(r'"([^"]*)"|\'([^\']*)\'', body_unescaped)
                                for q_idx, (q1, q2) in enumerate(quotes):
                                    val = q1 or q2
                                    val = val.strip()
                                    if val and re.search(r'[a-zA-Zа-яА-Я]', val) and not val.startswith('$'):
                                        results.append(self._mk(rel_file, [f"passage:{name}", f"token:{idx}", f"macro_str:{q_idx}"], val, f"Macro '{macro_name}' in passage: {name}"))

                    # 3. HTML Tag: ignore
                    elif token.startswith('<') and token.endswith('>'):
                        continue

                    # 4. Text
                    else:
                        text = token.strip()
                        if text and re.search(r'[a-zA-Zа-яА-Я]', text):
                            results.append(self._mk(rel_file, [f"passage:{name}", f"token:{idx}", "text"], text, f"Text in passage: {name}"))

        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0

        html_files = []
        if sub_paths:
            for p in sub_paths:
                if p.endswith(".html") and os.path.isfile(os.path.join(root, p)):
                    html_files.append(p)
        else:
            html_files = find_twine_html_files(root)

        for rel_file in html_files:
            abs_path = os.path.join(root, rel_file)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    html_content = f.read()
            except Exception as e:
                logger.error(f"Failed to read twine file {rel_file}: {e}")
                continue

            self.backup_file(root, abs_path)

            parts = []
            last_pos = 0
            file_modified = False

            for match in PASSAGE_RE.finditer(html_content):
                parts.append(html_content[last_pos:match.start(2)])
                header = match.group(1)
                content_escaped = match.group(2)
                
                name = get_attr(header, "name")
                if not name:
                    parts.append(content_escaped)
                    last_pos = match.end(2)
                    continue

                content = html.unescape(content_escaped)
                token_re = re.compile(r'(<style[^>]*?>[\s\S]*?</style>|<script[^>]*?>[\s\S]*?</script>|<<[\s\S]*?>>|<[^>]*?>|\[\[[^\]]*?\]\])', re.IGNORECASE | re.MULTILINE)
                tokens = token_re.split(content)
                
                new_tokens_escaped = []
                passage_modified = False

                for idx, token in enumerate(tokens):
                    if not token:
                        new_tokens_escaped.append("")
                        continue

                    # 1. Twine link
                    if token.startswith('[[') and token.endswith(']]'):
                        body = token[2:-2].strip()
                        body_unescaped = html.unescape(body)
                        
                        if '|' in body_unescaped:
                            label_unescaped, target = body_unescaped.split('|', 1)
                            str_id = make_id(self.engine, rel_file, [f"passage:{name}", f"token:{idx}", "link_label"], label_unescaped.strip())
                            trans = translations.get(str_id)
                            if trans and trans != label_unescaped.strip():
                                l_space = len(label_unescaped) - len(label_unescaped.lstrip())
                                r_space = len(label_unescaped) - len(label_unescaped.rstrip())
                                new_label = label_unescaped[:l_space] + trans + (label_unescaped[len(label_unescaped)-r_space:] if r_space else "")
                                new_tokens_escaped.append(f"[[{escape_twine(new_label)}|{escape_twine(target)}]]")
                                passage_modified = True
                            else:
                                new_tokens_escaped.append(escape_twine(token))
                        elif '->' in body_unescaped:
                            label_unescaped, target = body_unescaped.split('->', 1)
                            str_id = make_id(self.engine, rel_file, [f"passage:{name}", f"token:{idx}", "link_label"], label_unescaped.strip())
                            trans = translations.get(str_id)
                            if trans and trans != label_unescaped.strip():
                                l_space = len(label_unescaped) - len(label_unescaped.lstrip())
                                r_space = len(label_unescaped) - len(label_unescaped.rstrip())
                                new_label = label_unescaped[:l_space] + trans + (label_unescaped[len(label_unescaped)-r_space:] if r_space else "")
                                new_tokens_escaped.append(f"[[{escape_twine(new_label)}->{escape_twine(target)}]]")
                                passage_modified = True
                            else:
                                new_tokens_escaped.append(escape_twine(token))
                        elif '<-' in body_unescaped:
                            target, label_unescaped = body_unescaped.split('<-', 1)
                            str_id = make_id(self.engine, rel_file, [f"passage:{name}", f"token:{idx}", "link_label"], label_unescaped.strip())
                            trans = translations.get(str_id)
                            if trans and trans != label_unescaped.strip():
                                l_space = len(label_unescaped) - len(label_unescaped.lstrip())
                                r_space = len(label_unescaped) - len(label_unescaped.rstrip())
                                new_label = label_unescaped[:l_space] + trans + (label_unescaped[len(label_unescaped)-r_space:] if r_space else "")
                                new_tokens_escaped.append(f"[[{escape_twine(target)}<-{escape_twine(new_label)}]]")
                                passage_modified = True
                            else:
                                new_tokens_escaped.append(escape_twine(token))
                        else:
                            str_id = make_id(self.engine, rel_file, [f"passage:{name}", f"token:{idx}", "link_target"], body_unescaped.strip())
                            trans = translations.get(str_id)
                            if trans and trans != body_unescaped.strip():
                                l_space = len(body_unescaped) - len(body_unescaped.lstrip())
                                r_space = len(body_unescaped) - len(body_unescaped.rstrip())
                                new_label = body_unescaped[:l_space] + trans + (body_unescaped[len(body_unescaped)-r_space:] if r_space else "")
                                new_tokens_escaped.append(f"[[{escape_twine(new_label)}|{escape_twine(body_unescaped)}]]")
                                passage_modified = True
                            else:
                                new_tokens_escaped.append(escape_twine(token))

                    # 2. SugarCube Macro
                    elif token.startswith('<<') and token.endswith('>>'):
                        body = token[2:-2].strip()
                        body_unescaped = html.unescape(body)
                        words = body_unescaped.split()
                        if words:
                            macro_name = words[0].lower()
                            if macro_name in ("button", "dialog", "link", "print", "notify", "append", "prepend", "replace"):
                                quotes = list(re.finditer(r'"([^"]*)"|\'([^\']*)\'', body_unescaped))
                                new_body = body_unescaped
                                has_macro_changes = False
                                for q_idx in reversed(range(len(quotes))):
                                    m = quotes[q_idx]
                                    val = m.group(1) or m.group(2)
                                    str_id = make_id(self.engine, rel_file, [f"passage:{name}", f"token:{idx}", f"macro_str:{q_idx}"], val.strip())
                                    trans = translations.get(str_id)
                                    if trans and trans != val:
                                        start, end = m.span(1 if m.group(1) is not None else 2)
                                        new_body = new_body[:start] + trans + new_body[end:]
                                        has_macro_changes = True
                                if has_macro_changes:
                                    new_tokens_escaped.append(f"&lt;&lt;{escape_twine(new_body)}&gt;&gt;")
                                    passage_modified = True
                                else:
                                    new_tokens_escaped.append(escape_twine(token))
                            else:
                                new_tokens_escaped.append(escape_twine(token))
                        else:
                            new_tokens_escaped.append(escape_twine(token))

                    # 3. HTML Tag
                    elif token.startswith('<') and token.endswith('>'):
                        new_tokens_escaped.append(escape_twine(token))

                    # 4. Text
                    else:
                        str_id = make_id(self.engine, rel_file, [f"passage:{name}", f"token:{idx}", "text"], token.strip())
                        trans = translations.get(str_id)
                        if trans and trans != token.strip():
                            stripped = token.strip()
                            start_idx = token.find(stripped)
                            if start_idx != -1:
                                end_idx = start_idx + len(stripped)
                                margin_left = escape_twine(token[:start_idx])
                                margin_right = escape_twine(token[end_idx:])
                                new_tokens_escaped.append(margin_left + escape_twine(trans) + margin_right)
                            else:
                                new_tokens_escaped.append(escape_twine(trans))
                            passage_modified = True
                        else:
                            new_tokens_escaped.append(escape_twine(token))

                if passage_modified:
                    parts.append("".join(new_tokens_escaped))
                    file_modified = True
                    written += 1
                else:
                    parts.append(content_escaped)

                last_pos = match.end(2)

            parts.append(html_content[last_pos:])

            if file_modified:
                new_html_content = "".join(parts)
                try:
                    with open(abs_path, "w", encoding="utf-8") as f:
                        f.write(new_html_content)
                except Exception as e:
                    logger.error(f"Failed to write twine file {rel_file}: {e}")
                    continue

        return written
