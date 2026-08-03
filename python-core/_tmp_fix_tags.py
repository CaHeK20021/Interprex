from pathlib import Path
import re
tl = Path(r"G:\5inchENG\13\Takeis-Journey-0.37-pc\game\tl\russian")
pat = re.compile(r'(\{i\})((?:(?!\{color)(?!\{/color\})(?!\{/i\}).)*)(\{/color\})', re.DOTALL)
for name in ("himawarievents.rpy", "namidaevents.rpy"):
    f = tl / name
    text = f.read_text(encoding="utf-8")
    new, n = pat.subn(lambda m: m.group(1)+m.group(2)+"{/i}", text)
    print(name, "matches", n)
    if n:
        f.write_text(new, encoding="utf-8", newline="\n")
    # show bad remaining
    bad = list(pat.finditer(new if n else text))
    print(" remaining", len(bad))