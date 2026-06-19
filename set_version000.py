import re, subprocess, sys

v = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "set" else "0.0.0"

for f in ["package.json", "src-tauri/tauri.conf.json"]:
    c = open(f, encoding="utf-8").read()
    c = re.sub(r'"version":\s*"[^"]*"', '"version": "' + v + '"', c, count=1)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(c)

f = "src-tauri/Cargo.toml"
c = open(f, encoding="utf-8").read()
c = re.sub(r'^(version\s*=\s*)"[^"]*"', r'\g<1>"' + v + '"', c, count=1, flags=re.MULTILINE)
with open(f, "w", encoding="utf-8") as fh:
    fh.write(c)

# Only delete tags when resetting to 0.0.0
if v == "0.0.0":
    tags = subprocess.check_output(["git", "tag", "-l"], text=True).strip().split()
    for tag in tags:
        subprocess.run(["git", "tag", "-d", tag], capture_output=True)
        print("Deleted local tag:", tag)
    for tag in tags:
        subprocess.run(["git", "push", "origin", "--delete", tag], capture_output=True)
        print("Deleted remote tag:", tag)

print("Version set to", v)
