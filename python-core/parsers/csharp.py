"""Parser for C# (.cs) source code files.

Extracts hardcoded string literals and verbatim string literals,
tracks nested class/method scopes for ID stability, and injects
translations back safely.
"""

from __future__ import annotations

import os
import re
from .base import BaseParser, TranslationString, make_id


def tokenize_csharp(text: str):
    """
    Parses C# text and yields tokens of types:
    - 'code': raw C# code text
    - 'comment': single-line or multi-line comment
    - 'char': single character literal, e.g., 'a' or '\\n'
    - 'string': standard double-quoted string literal "..."
    - 'verbatim_string': @-quoted string literal @"..."
    - 'interpolated_string': $"-quoted string literal $"..."
    - 'interpolated_verbatim_string': $@"-quoted string literal $@""..."
    
    Each yielded item is a tuple: (token_type, start_idx, end_idx, raw_value)
    where raw_value is the exact substring from the file.
    """
    n = len(text)
    i = 0
    while i < n:
        # Check comments
        if i + 1 < n and text[i] == '/' and text[i+1] == '/':
            start = i
            i += 2
            while i < n and text[i] != '\n':
                i += 1
            yield ('comment', start, i, text[start:i])
        elif i + 1 < n and text[i] == '/' and text[i+1] == '*':
            start = i
            i += 2
            while i < n and not (text[i] == '*' and i + 1 < n and text[i+1] == '/'):
                i += 1
            if i < n:
                i += 2  # consume '*/'
            yield ('comment', start, i, text[start:i])
        # Check character literals
        elif text[i] == "'":
            start = i
            i += 1
            if i < n and text[i] == '\\':
                i += 2  # consume escape
            else:
                i += 1
            while i < n and text[i] != "'":
                i += 1
            if i < n:
                i += 1
            yield ('char', start, i, text[start:i])
        # Check interpolated verbatim strings ($@" or @$")
        elif (text[i] == '$' and i + 2 < n and text[i+1] == '@' and text[i+2] == '"') or \
             (text[i] == '@' and i + 2 < n and text[i+1] == '$' and text[i+2] == '"'):
            start = i
            i += 3
            brace_depth = 0
            while i < n:
                if text[i] == '"':
                    if i + 1 < n and text[i+1] == '"':
                        i += 2
                    elif brace_depth == 0:
                        i += 1
                        break
                    else:
                        i += 1
                elif text[i] == '{' and brace_depth == 0:
                    if i + 1 < n and text[i+1] == '{':
                        i += 2
                    else:
                        brace_depth += 1
                        i += 1
                elif text[i] == '}' and brace_depth > 0:
                    if i + 1 < n and text[i+1] == '}':
                        i += 2
                    else:
                        brace_depth -= 1
                        i += 1
                else:
                    i += 1
            yield ('interpolated_verbatim_string', start, i, text[start:i])
        # Check interpolated strings ($")
        elif text[i] == '$' and i + 1 < n and text[i+1] == '"':
            start = i
            i += 2
            brace_depth = 0
            while i < n:
                if text[i] == '\\':
                    i += 2
                elif text[i] == '{' and brace_depth == 0:
                    if i + 1 < n and text[i+1] == '{':
                        i += 2
                    else:
                        brace_depth += 1
                        i += 1
                elif text[i] == '}' and brace_depth > 0:
                    if i + 1 < n and text[i+1] == '}':
                        i += 2
                    else:
                        brace_depth -= 1
                        i += 1
                elif text[i] == '"' and brace_depth == 0:
                    i += 1
                    break
                else:
                    i += 1
            yield ('interpolated_string', start, i, text[start:i])
        # Check verbatim strings (@")
        elif text[i] == '@' and i + 1 < n and text[i+1] == '"':
            start = i
            i += 2
            while i < n:
                if text[i] == '"':
                    if i + 1 < n and text[i+1] == '"':
                        i += 2  # escaped double quote ""
                    else:
                        i += 1  # end of verbatim string
                        break
                else:
                    i += 1
            yield ('verbatim_string', start, i, text[start:i])
        # Check standard double-quoted strings
        elif text[i] == '"':
            start = i
            i += 1
            while i < n:
                if text[i] == '\\':
                    i += 2  # skip escaped character
                elif text[i] == '"':
                    i += 1  # end of string
                    break
                else:
                    i += 1
            yield ('string', start, i, text[start:i])
        # Default: code character
        else:
            start = i
            while i < n:
                # Break on special characters that start comments, strings, chars
                if text[i] == '/' and i + 1 < n and (text[i+1] == '/' or text[i+1] == '*'):
                    break
                if text[i] == "'":
                    break
                if text[i] == '"':
                    break
                if text[i] == '@' and i + 1 < n and text[i+1] == '"':
                    break
                if text[i] == '$' and i + 1 < n and text[i+1] == '"':
                    break
                if text[i] == '$' and i + 2 < n and text[i+1] == '@' and text[i+2] == '"':
                    break
                if text[i] == '@' and i + 2 < n and text[i+1] == '$' and text[i+2] == '"':
                    break
                i += 1
            yield ('code', start, i, text[start:i])


def unescape_csharp_string(raw: str, verbatim: bool) -> str:
    """Decodes C# string literal escapes to return clean original text."""
    if verbatim:
        # Remove @" at start and " at end
        inner = raw[2:-1]
        # Replace "" with "
        return inner.replace('""', '"')
    else:
        # Remove " at start and " at end
        inner = raw[1:-1]
        result = []
        i = 0
        n = len(inner)
        while i < n:
            if inner[i] == '\\' and i + 1 < n:
                ch = inner[i+1]
                if ch == 'n':
                    result.append('\n')
                elif ch == 'r':
                    result.append('\r')
                elif ch == 't':
                    result.append('\t')
                elif ch == '\\':
                    result.append('\\')
                elif ch == '"':
                    result.append('"')
                elif ch == "'":
                    result.append("'")
                elif ch == '0':
                    result.append('\0')
                else:
                    result.append(ch)
                i += 2
            else:
                result.append(inner[i])
                i += 1
        return "".join(result)


def escape_csharp_string(val: str, verbatim: bool) -> str:
    """Escapes raw text into a valid C# string literal representation."""
    if verbatim:
        escaped = val.replace('"', '""')
        return f'@"{escaped}"'
    else:
        result = []
        for ch in val:
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            elif ch == '\\':
                result.append('\\\\')
            elif ch == '"':
                result.append('\\"')
            elif ch == '\0':
                result.append('\\0')
            else:
                result.append(ch)
        escaped = "".join(result)
        return f'"{escaped}"'


def is_technical_string(s: str) -> bool:
    """True if the string should be skipped (e.g. paths, configuration keys, empty)."""
    # Empty or whitespace only
    if not s.strip():
        return True
    # No letters (e.g., just punctuation, digits)
    if not any(c.isalpha() for c in s):
        return True
    # File paths / asset paths
    if "/" in s or "\\" in s:
        parts = s.replace("\\", "/").split("/")
        last_part = parts[-1]
        if "." in last_part:
            ext = last_part.split(".")[-1].lower()
            if ext in {
                "png", "jpg", "jpeg", "tga", "wav", "mp3", "ogg", "prefab",
                "asset", "json", "xml", "txt", "dll", "cs", "shader",
                "cginc", "unity", "fbx", "mat"
            }:
                return True
    # Internal variables / identifiers (starts with letters, has no spaces, has underscores)
    if " " not in s and "_" in s:
        return True
    return False


class CSharpParser(BaseParser):
    """Parser for C# source code files (*.cs)."""
    engine = "csharp"

    @staticmethod
    def detect(root: str) -> bool:
        """True if any .cs files are present in the directory structure."""
        ignore_dirs = {
            "bin", "obj", ".vs", "node_modules", "venv", ".git", ".interprex_backups"
        }
        root_depth = root.count(os.path.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.count(os.path.sep) - root_depth
            if depth >= 3:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d.lower() not in ignore_dirs and not d.startswith(".")]
            for f in filenames:
                if f.endswith(".cs"):
                    return True
        return False

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results = []
        ignore_dirs = {
            "bin", "obj", ".vs", "node_modules", "venv", ".git", ".interprex_backups"
        }
        
        paths_to_walk = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        for start_path in paths_to_walk:
            for dirpath, dirnames, filenames in os.walk(start_path):
                dirnames[:] = [d for d in dirnames if d.lower() not in ignore_dirs and not d.startswith(".")]
                
                for f in filenames:
                    if f.endswith(".cs"):
                        fpath = os.path.join(dirpath, f)
                        rel_path = os.path.relpath(fpath, root).replace("\\", "/")
                        
                        try:
                            with open(fpath, "r", encoding="utf-8") as file_obj:
                                content = file_obj.read()
                        except Exception:
                            continue
                        
                        file_strings = self._parse_file_strings(content, rel_path)
                        results.extend(file_strings)
                        
        return results

    def _parse_file_strings(self, content: str, rel_path: str) -> list[TranslationString]:
        tokens = list(tokenize_csharp(content))
        
        # Build clean code for scope parsing
        clean_chars = list(content)
        for tok_type, start, end, val in tokens:
            if tok_type != 'code':
                for idx in range(start, end):
                    if clean_chars[idx] not in ('\n', '\r'):
                        clean_chars[idx] = ' '
        clean_code = "".join(clean_chars)
        
        scope_stack = []
        brace_depth = 0
        last_boundary = 0
        token_scopes = {}
        
        class_re = re.compile(r'\b(?:class|struct|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)')
        method_re = re.compile(r'\b(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)\s*$')
        keywords = {
            'if', 'while', 'foreach', 'switch', 'for', 'using', 'catch', 'lock',
            'typeof', 'sizeof', 'new', 'return', 'throw', 'delegate'
        }
        
        for idx, char in enumerate(clean_code):
            if char == '{':
                header = clean_code[last_boundary:idx]
                class_match = list(class_re.finditer(header))
                method_match = list(method_re.finditer(header))
                
                scope_name = ""
                scope_type = "other"
                
                if class_match:
                    scope_name = class_match[-1].group("name")
                    scope_type = "class"
                elif method_match:
                    mname = method_match[-1].group("name")
                    if mname not in keywords:
                        scope_name = mname
                        scope_type = "method"
                
                scope_stack.append((scope_type, scope_name, brace_depth))
                brace_depth += 1
                last_boundary = idx + 1
            elif char == '}':
                brace_depth -= 1
                while scope_stack and scope_stack[-1][2] >= brace_depth:
                    scope_stack.pop()
                last_boundary = idx + 1
            
            classes = [name for stype, name, _ in scope_stack if stype == "class" and name]
            class_name = ".".join(classes) if classes else ""
            methods = [name for stype, name, _ in scope_stack if stype == "method" and name]
            method_name = methods[-1] if methods else ""
            token_scopes[idx] = (class_name, method_name)
            
        results = []
        string_count_in_method = {}
        
        for tok_type, start, end, val in tokens:
            if tok_type in ('string', 'verbatim_string'):
                is_verbatim = (tok_type == 'verbatim_string')
                original = unescape_csharp_string(val, is_verbatim)
                
                if is_technical_string(original):
                    continue
                
                class_name, method_name = token_scopes.get(start, ("", ""))
                scope_key = (class_name, method_name)
                str_idx = string_count_in_method.get(scope_key, 0)
                string_count_in_method[scope_key] = str_idx + 1
                
                path = []
                if class_name:
                    path.append(class_name)
                if method_name:
                    path.append(method_name)
                path.append(f"str_{str_idx}")
                
                context_str = f"Class: {class_name}, Method: {method_name}" if class_name or method_name else "Global scope"
                results.append(self._mk(rel_path, path, original, context_str))
                
        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0
        ignore_dirs = {
            "bin", "obj", ".vs", "node_modules", "venv", ".git", ".interprex_backups"
        }
        
        paths_to_walk = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        for start_path in paths_to_walk:
            for dirpath, dirnames, filenames in os.walk(start_path):
                dirnames[:] = [d for d in dirnames if d.lower() not in ignore_dirs and not d.startswith(".")]
                
                for f in filenames:
                    if f.endswith(".cs"):
                        fpath = os.path.join(dirpath, f)
                        rel_path = os.path.relpath(fpath, root).replace("\\", "/")
                        
                        try:
                            with open(fpath, "r", encoding="utf-8") as file_obj:
                                content = file_obj.read()
                        except Exception:
                            continue
                        
                        tokens = list(tokenize_csharp(content))
                        
                        # Compute scopes exactly as in extract
                        clean_chars = list(content)
                        for tok_type, start, end, val in tokens:
                            if tok_type != 'code':
                                for idx in range(start, end):
                                    if clean_chars[idx] not in ('\n', '\r'):
                                        clean_chars[idx] = ' '
                        clean_code = "".join(clean_chars)
                        
                        scope_stack = []
                        brace_depth = 0
                        last_boundary = 0
                        token_scopes = {}
                        
                        class_re = re.compile(r'\b(?:class|struct|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)')
                        method_re = re.compile(r'\b(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)\s*$')
                        keywords = {
                            'if', 'while', 'foreach', 'switch', 'for', 'using', 'catch', 'lock',
                            'typeof', 'sizeof', 'new', 'return', 'throw', 'delegate'
                        }
                        
                        for idx, char in enumerate(clean_code):
                            if char == '{':
                                header = clean_code[last_boundary:idx]
                                class_match = list(class_re.finditer(header))
                                method_match = list(method_re.finditer(header))
                                
                                scope_name = ""
                                scope_type = "other"
                                
                                if class_match:
                                    scope_name = class_match[-1].group("name")
                                    scope_type = "class"
                                elif method_match:
                                    mname = method_match[-1].group("name")
                                    if mname not in keywords:
                                        scope_name = mname
                                        scope_type = "method"
                                
                                scope_stack.append((scope_type, scope_name, brace_depth))
                                brace_depth += 1
                                last_boundary = idx + 1
                            elif char == '}':
                                brace_depth -= 1
                                while scope_stack and scope_stack[-1][2] >= brace_depth:
                                    scope_stack.pop()
                                last_boundary = idx + 1
                            
                            classes = [name for stype, name, _ in scope_stack if stype == "class" and name]
                            class_name = ".".join(classes) if classes else ""
                            methods = [name for stype, name, _ in scope_stack if stype == "method" and name]
                            method_name = methods[-1] if methods else ""
                            token_scopes[idx] = (class_name, method_name)
                        
                        string_count_in_method = {}
                        replacements = []
                        
                        for tok_type, start, end, val in tokens:
                            if tok_type in ('string', 'verbatim_string'):
                                is_verbatim = (tok_type == 'verbatim_string')
                                original = unescape_csharp_string(val, is_verbatim)
                                
                                if is_technical_string(original):
                                    continue
                                
                                class_name, method_name = token_scopes.get(start, ("", ""))
                                scope_key = (class_name, method_name)
                                str_idx = string_count_in_method.get(scope_key, 0)
                                string_count_in_method[scope_key] = str_idx + 1
                                
                                path = []
                                if class_name:
                                    path.append(class_name)
                                if method_name:
                                    path.append(method_name)
                                path.append(f"str_{str_idx}")
                                
                                sid = make_id(self.engine, rel_path, path, original)
                                
                                if sid in translations:
                                    translated_text = translations[sid]
                                    new_literal = escape_csharp_string(translated_text, is_verbatim)
                                    replacements.append((start, end, new_literal))
                        
                        if replacements:
                            self.backup_file(root, fpath)
                            
                            replacements.sort(key=lambda x: x[0], reverse=True)
                            content_list = list(content)
                            for start, end, new_literal in replacements:
                                content_list[start:end] = list(new_literal)
                                written += 1
                            
                            with open(fpath, "w", encoding="utf-8") as file_obj:
                                file_obj.write("".join(content_list))
                                
        return written
