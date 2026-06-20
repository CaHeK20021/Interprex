"""Quick test for Ren'Py token validation + retry logic in the inline translator.

Run: python test_token_validation.py
"""
import re
import sys
sys.path.insert(0, ".")

# --- The validation function (copied from renpy_python_translator.py) ---
_RENPY_VAR_RE = re.compile(r'\[([^\]]+)\]')
_RENPY_TAG_RE = re.compile(r'\{/?[a-zA-Z][^}]*\}')
_RENPY_PERCENT_RE = re.compile(r'%(?:%|\(\w+\)[diouxXeEfFgGcrs]|[-#0+]*\d*\.?\d*[hlL]?[diouxXeEfFgGcrs])')

def _validate_renpy_tokens(old: str, new: str) -> list[str]:
    violations = []
    old_vars = sorted(m.group(0) for m in _RENPY_VAR_RE.finditer(old))
    new_vars = sorted(m.group(0) for m in _RENPY_VAR_RE.finditer(new))
    if old_vars != new_vars:
        violations.append(f"[var] tokens differ: {old_vars} -> {new_vars}")
    old_tags = sorted(m.group(0) for m in _RENPY_TAG_RE.finditer(old))
    new_tags = sorted(m.group(0) for m in _RENPY_TAG_RE.finditer(new))
    if old_tags != new_tags:
        violations.append(f"{{tag}} tokens differ: {old_tags} -> {new_tags}")
    old_fmt = sorted(m.group(0) for m in _RENPY_PERCENT_RE.finditer(old))
    new_fmt = sorted(m.group(0) for m in _RENPY_PERCENT_RE.finditer(new))
    if old_fmt != new_fmt:
        violations.append(f"%-format tokens differ: {old_fmt} -> {new_fmt}")
    return violations


# --- Test cases ---
passed = 0
failed = 0

def check(name, old, new, expect_violations):
    global passed, failed
    viols = _validate_renpy_tokens(old, new)
    has_viols = len(viols) > 0
    ok = has_viols == expect_violations
    status = "PASS" if ok else "FAIL"
    if not ok:
        failed += 1
        print(f"  {status}: {name} -- expected violations={expect_violations}, got={viols}")
    else:
        passed += 1
        print(f"  {status}: {name}")


print("=== [var] interpolation tokens ===")
check("same [var]",
      "Hello [player_name]!", "Привет [player_name]!", False)

check("[var] case change (BUG!)",
      "[VALUE] is here", "[value] is here", True)

check("[var] dropped",
      "Welcome [player_name]", "Добро пожаловать", True)

check("[var] added",
      "Hello", "Привет [новая]", True)

check("multiple [var] same",
      "[first] and [second]", "[first] и [second]", False)

check("multiple [var] one changed",
      "[first] and [second]", "[first] и [другой_second]", True)

check("no [var] at all",
      "Just text", "Просто текст", False)

check("[mc.status] preserved",
      "[mc.status] is active", "[mc.status] активен", False)

check("[mc.status] changed",
      "[mc.status] is active", "[mc.stat] активен", True)


print("\n=== {tag} text tags ===")
check("tags preserved",
      "{b}Bold{/b}", "{b}Жирный{/b}", False)

check("tag dropped (BUG!)",
      "{i}Emphasis{/i}", "Акцент", True)

check("tag case changed",
      "{B}Text{/B}", "{b}Text{/b}", True)

check("color tag preserved",
      "{color=#ff0000}Red{/color}", "{color=#ff0000}Красный{/color}", False)

check("color tag value changed",
      "{color=#ff0000}Red{/color}", "{color=#00ff00}Красный{/color}", True)

check("size tag preserved",
      "{size=20}Big{/size}", "{size=20}Большой{/size}", False)

check("no tags",
      "Plain text", "Обычный текст", False)


print("\n=== %-format strings ===")
check("%(player_name)s preserved",
      "Hello %(player_name)s", "Привет %(player_name)s", False)

check("%% preserved",
      "100%% done", "100%% готово", False)

check("%s dropped (BUG!)",
      "%s is ready", "Готово", True)

check("%d dropped",
      "You have %d items", "У тебя вещи", True)

check("mixed format",
      "%(name)s has %d items", "%(name)s имеет %d вещей", False)

check("no format",
      "No format here", "Без формата", False)


print("\n=== Combined (the FAVOR_EP2 crash case) ===")
check("CRASH BUG: [VALUE] -> [value]",
      "Player: [VALUE]", "Игрок: [value]", True)

check("CRASH BUG: [V] -> [v]",
      "Short: [V]", "Коротко: [v]", True)

check("complex: tags + var",
      "{b}[player_name]{/b} says hi",
      "{b}[player_name]{/b} говорит привет",
      False)

check("complex: tags + var corrupted",
      "{b}[player_name]{/b} says hi",
      "{b}[имя]{/b} говорит привет",
      True)

check("time format preserved",
      "{#file_time}%A, %B %d %Y, %H:%M",
      "{#file_time}%A, %B %d %Y, %H:%M",
      False)


print("\n=== Edge cases ===")
check("nested tags",
      "{b}{i}text{/i}{/b}", "{b}{i}текст{/i}{/b}", False)

check("tag with = attribute",
      "{color=#ff0000}Red{/color}", "{color=#ff0000}Red{/color}", False)

check("tag with = attribute changed",
      "{color=#ff0000}Red{/color}", "{color=#00ff00}Red{/color}", True)

check("empty string pair",
      "", "", False)

check("string with only tags",
      "{b}SNAP{/b}", "{b}SNAP{/b}", False)

check("all three token types",
      "{i}[name] has %s items{/i}",
      "{i}[name] has %s вещей{/i}",
      False)

check("all three corrupted",
      "{i}[name] has %s items{/i}",
      "У [имя] вещи",
      True)

check("escaped percent preserved",
      "100%% done", "100%% готово", False)

check("anchor tag preserved",
      "{a=https://example.com}link{/a}",
      "{a=https://example.com}ссылка{/a}",
      False)

check("anchor tag URL changed",
      "{a=https://example.com}link{/a}",
      "{a=https://other.com}ссылка{/a}",
      True)

check("special_tag {sc} preserved",
      "{sc}whisper{/sc}", "{sc}шёпот{/sc}", False)

check("special_tag {sc} dropped",
      "{sc}whisper{/sc}", "шёпот", True)


print("\n=== False positive checks (should ALL pass = no violation) ===")
check("FP: %s with space after",
      "%s is ready", "%s готов", False)
check("FP: all tokens preserved",
      "{b}[name] has %s items{/b}", "{b}[name] имеет %s вещей{/b}", False)
check("FP: percent in text (not format)",
      "100% done", "100% готово", False)
check("FP: only format",
      "Score: %d", "Очки: %d", False)
check("FP: percent with flags",
      "%+10d items", "%+10d вещей", False)
check("FP: multiple formats",
      "%s is %d items", "%s это %d вещей", False)
check("FP: tags reordered",
      "{b}{i}text{/i}{/b}", "{i}{b}текст{/b}{/i}", False)
check("FP: var preserved different text",
      "[name] says hi", "[name] говорит привет", False)
check("FP: plain text no tokens",
      "Hello world", "Привет мир", False)
check("FP: empty strings",
      "", "", False)


print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("All tests passed!")
