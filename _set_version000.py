import re, sys

v = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"

for f in ["package.json", "src-tauri/tauri.conf.json"]:
    c = open(f, encoding="utf-8").read()
    c = re.sub(r'"version":\s*"[^"]*"', f'"version": "{v}"', c, count=1)
    open(f, "w", encoding="utf-8", newline="").write(c)

f = "src-tauri/Cargo.toml"
c = open(f, encoding="utf-8").read()
c = re.sub(r'^version = "[^"]*"', f'version = "{v}"', c, count=1)
open(f, "w", encoding="utf-8", newline="").write(c)

print(f"Version set to {v}")
