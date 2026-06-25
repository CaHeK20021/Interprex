"""
Verify that:
1. parsers/renpy.py has valid Python syntax (already checked via py_compile)
2. The generated init python block code is syntactically valid Python
3. The init python block appears before the translate block in the generated content
"""
import ast
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Build the exact same strings as in _write_native_font
init_block_lines = [
    "init python:",
    "    def _set_dominant_role(self, val):",
    "        self.__dict__['dominant_role'] = val",
    "    def _get_dominant_role(self, *a):",
    "        return self.__dict__.get('dominant_role', '')",
    "    def _set_role_name(self, val):",
    "        self.__dict__['name'] = val",
    "    def _get_role_name(self, *a):",
    "        return self.__dict__.get('name', '')",
]
full_block = "\n".join(init_block_lines)  # full block including "def" lines (no class wrapper needed)
# Parse as a module (the defs are top-level after the init python: header is stripped)
# In Ren'Py, init python: body = indented Python, so we dedent and parse
import textwrap
body_only = textwrap.dedent("\n".join(l[4:] if l.startswith("    ") else l for l in init_block_lines[1:]))

try:
    ast.parse(body_only)
    print("OK: init python block body is valid Python")
except SyntaxError as e:
    print(f"FAIL: SyntaxError in init python block: {e}")
    sys.exit(1)

# Verify the actual string literals we emit in _write_native_font
expected_stubs = [
    "\"init python:\\n\"",
    "\"    def _set_dominant_role(self, val):\\n\"",
    "\"        self.__dict__['dominant_role'] = val\\n\"",
    "\"    def _get_dominant_role(self, *a):\\n\"",
    "\"        return self.__dict__.get('dominant_role', '')\\n\"",
    "\"    def _set_role_name(self, val):\\n\"",
    "\"        self.__dict__['name'] = val\\n\"",
    "\"    def _get_role_name(self, *a):\\n\"",
    "\"        return self.__dict__.get('name', '')\\n\"",
]

renpy_py = os.path.join(os.path.dirname(__file__), '..', 'parsers', 'renpy.py')
source = open(renpy_py, encoding='utf-8').read()

missing = []
for stub in expected_stubs:
    if stub not in source:
        missing.append(stub)

if missing:
    print(f"FAIL: These expected strings not found in renpy.py:")
    for m in missing:
        print(f"  {m}")
    sys.exit(1)
else:
    print(f"OK: All {len(expected_stubs)} init python stub strings found in renpy.py")

# Verify ordering: init python: comes before translate {lang} python:
init_idx = source.find('"init python:\\n"')
translate_idx = source.find('f"translate {lang} python:\\n"')
if init_idx < translate_idx:
    print(f"OK: init python block (pos {init_idx}) comes before translate block (pos {translate_idx})")
else:
    print(f"FAIL: init python block (pos {init_idx}) is NOT before translate block (pos {translate_idx})")
    sys.exit(1)

print("\nAll checks passed.")
