"""Test autofix with official Google GenAI SDK + Pydantic schema."""
import json, re, sys
sys.path.insert(0, ".")
from validators.renpy import RenpyValidator
from google import genai
from google.genai import types
from pydantic import BaseModel

API_KEY = "AQ.Ab8RN6JM8Y8uLVnDneOHqjtSni6MtFju5ReR6H9H1q_iN0-t7g"
MODEL = "gemma-4-31b-it"
TEST_FILE = r"C:\Users\Alexandr\Desktop\Interprex\test_autofix_broken.rpy"


class LineFix(BaseModel):
    line: int
    old: str
    new: str


client = genai.Client(api_key=API_KEY)


def llm_fix(line_no, error_msg, broken_line):
    prompt = (
        "Fix this broken Python line in a Ren'Py script.\n"
        f"Line {line_no}: {broken_line}\n"
        f"Error: {error_msg}\n\n"
        "Return the fix with the corrected line."
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=LineFix,
        ),
    )
    raw = response.text
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    raw = re.sub(r"\n?```\s*$", "", raw).strip()
    print(f"  RAW: {raw[:500]!r}")
    return json.loads(raw)


v = RenpyValidator()
errors = v.validate_file(TEST_FILE)
print(f"=== Found {len(errors)} errors ===")
for e in errors:
    print(f"  Line {e.line}: {e.message}")

with open(TEST_FILE, "r", encoding="utf-8") as f:
    content = f.read()
lines = content.split("\n")

for e in errors:
    broken_line = lines[e.line - 1].strip()
    print(f"\n--- Fixing line {e.line}: {broken_line!r} ---")
    try:
        fix = llm_fix(e.line, e.message, broken_line)
        print(f"  LLM fix: {fix}")
        fix_line = fix["line"] - 1
        if 0 <= fix_line < len(lines) and lines[fix_line].strip() == fix["old"].strip():
            orig_indent = len(lines[fix_line]) - len(lines[fix_line].lstrip())
            fixed_line = fix["new"]
            if not fixed_line.startswith(" " * orig_indent) and not fixed_line.startswith("\t"):
                fixed_line = " " * orig_indent + fixed_line.lstrip()
            lines[fix_line] = fixed_line
            print(f"  APPLIED!")
        else:
            got = lines[fix_line].strip() if fix_line < len(lines) else "<out of range>"
            print(f"  MISMATCH: expected {fix['old'].strip()!r}, got {got!r}")
    except Exception as ex:
        print(f"  ERROR: {ex}")

fixed = "\n".join(lines)
errors_after = v.validate(TEST_FILE, fixed)
print(f"\n=== After fix: {len(errors_after)} errors ===")
if not errors_after:
    print("ALL FIXED!")
    with open(TEST_FILE, "w", encoding="utf-8") as f:
        f.write(fixed)
    print("File written.")
