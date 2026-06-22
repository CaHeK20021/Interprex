import sys
import os
from pathlib import Path

# Add python-core to path
parser_dir = r"c:\Users\Alexandr\Desktop\Interprex\python-core"
sys.path.append(parser_dir)

from parsers import get_parser, detect_engine

root = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory"
mods_dir = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory\FactoryGame\Mods")

parser = get_parser("unreal4_5")

print("Scanning each mod folder individually...")

def scan_folder(full_path):
    rel_path = os.path.relpath(full_path, root).replace("\\", "/")
    engine = detect_engine(str(full_path))
    if not engine:
        print(f"Mod: {full_path.name} | Engine: None")
        return
        
    try:
        strings = parser.extract(root, [rel_path])
        print(f"Mod: {full_path.name} | Engine: {engine} | Strings: {len(strings)}")
        # Print first 2 files where strings were found
        if strings:
            files = set(s.file for s in strings)
            print(f"  Found in files: {list(files)}")
    except Exception as e:
        print(f"Mod: {full_path.name} | Error: {e}")

# Scan direct subdirs
for item in mods_dir.iterdir():
    if not item.is_dir() or item.name.startswith("."):
        continue
    if item.name.lower() == "gamefeatures":
        for sub in item.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                scan_folder(sub)
    else:
        scan_folder(item)
