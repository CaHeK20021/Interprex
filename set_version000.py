import re, os

v = "0.0.0"

for f in ["package.json", "src-tauri/tauri.conf.json"]:
    c = open(f, encoding="utf-8").read()
    c = re.sub(r'"version":\s*"[^"]*"', '"version": "' + v + '"', c, count=1)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(c)

f = "src-tauri/Cargo.toml"
c = open(f, encoding="utf-8").read()
# Only replace the first 'version = "..."' line (under [package])
c = re.sub(r'^(version\s*=\s*)"[^"]*"', r'\g<1>"' + v + '"', c, count=1, flags=re.MULTILINE)
with open(f, "w", encoding="utf-8") as fh:
    fh.write(c)

print("Version set to", v)
