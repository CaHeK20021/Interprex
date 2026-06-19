import sys, os, tempfile, hashlib, re
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\Alexandr\Desktop\Interprex\python-core')
from parsers import rpa as rpamod

temp = tempfile.mkdtemp(prefix='debug_rpyc_')
game_dir = r'C:\Users\Alexandr\Desktop\OnlineObsessionDemo-0.1.0-win'
rpamod.extract_rpa_file(os.path.join(game_dir, 'game', 'scripts.rpa'), 'script-1st-stage.rpyc', os.path.join(temp, 'script-1st-stage.rpyc'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(r'C:\Users\Alexandr\Desktop\Interprex\python-core\parsers\renpy.py'))), 'tools', 'unrpyc'))
import unrpyc
from pathlib import Path
unrpyc.decompile_rpyc(Path(os.path.join(temp, 'script-1st-stage.rpyc')), overwrite=True)
with open(os.path.join(temp, 'script-1st-stage.rpy'), 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Get the exact source line (line 623, 0-indexed = 622)
line = lines[622]
print("Source line repr:", repr(line.strip()))

# Extract the say text (between outer quotes)
stripped = line.strip()
what = stripped[1:-1]  # strip outer ASCII quotes
print("what repr:", repr(what))
print("what codepoints:")
for i, ch in enumerate(what):
    print(f"  [{i}] U+{ord(ch):04X} ({ch})")

# encode_say_string simulation
s = what
s = s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
s = re.sub(r'(?<= ) ', '\\ ', s)
encoded = '"' + s + '"'
get_code = encoded
raw = (get_code + "\r\n").encode("utf-8")
digest = hashlib.md5(raw).hexdigest()[:8]
print(f"\nEngine digest: {digest}")
print(f"TL block id:   f5004a22")
print(f"Match: {digest == 'f5004a22'}")

# Now check: Ren'Py lexer normalizes \u201c/\u201d?
# The lexer's string() method reads between " delimiters
# Then it does NOT normalize Unicode quotes - they stay as-is
# So the what value should include the curly quotes

# Check if there's a _() or __() wrapper that might change behavior
print(f"\nRaw source bytes around mayo:")
raw_bytes = line.encode('utf-8')
print(raw_bytes.hex())
