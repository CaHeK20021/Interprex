import sys, hashlib, re, os, tempfile
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

tl_path = r'C:\Users\Alexandr\Desktop\OnlineObsessionDemo-0.1.0-win\game\tl\russian\script-1st-stage.rpy'
with open(tl_path, encoding='utf-8') as f:
    tl_content = f.read()

# Check mayo too
mayo_lines = []
for i, line in enumerate(lines):
    l = line.strip()
    if 'mayo' in l.lower() or 'antimalware' in l.lower():
        what = l
        # Strip outer quotes for say
        if what.startswith('"') and what.endswith('"'):
            what_inner = what[1:-1]
        else:
            what_inner = what
        
        s = what_inner
        s = s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        s = re.sub(r'(?<= ) ', '\\ ', s)
        get_code = '"' + s + '"'
        digest = hashlib.md5((get_code + "\r\n").encode("utf-8")).hexdigest()[:8]
        full_id = "morning_1_" + digest
        
        found = full_id in tl_content
        print("Line %d: id=%s found_in_tl=%s" % (i + 1, full_id, found))
        if not found:
            print("  TEXT: %s" % l[:120])
        print()
