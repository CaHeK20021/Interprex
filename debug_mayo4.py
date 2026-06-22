import sys
import os

# Set up python-core path
sys.path.append(os.path.join(os.path.dirname(__file__), "python-core"))

from parsers.unreal4_5 import UnrealEngine4_5Parser
from main import detect_mods, DetectModsReq

root = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory\FactoryGame\Mods"
res = detect_mods(DetectModsReq(root=root))
mods = res["mods"]
print(f"Detected {len(mods)} mods:")
for m in mods:
    print(f"  Name: {m['name']}, Path: {m['path']}, Engine: {m['engine']}")

# Let's extract from a few mods and see the paths
parser = UnrealEngine4_5Parser()
# Paths relative to game_root
game_root = res["game_root"]
print("Game root:", game_root)

# Let's extract from all detected mods
sub_paths = [m["path"] for m in mods if m["engine"] == "unreal4_5"]
print(f"Extracting from {len(sub_paths)} sub-paths...")
strings = parser.extract(game_root, sub_paths)
print(f"Total strings extracted: {len(strings)}")

# Group by the first part of the file path
file_groups = {}
for s in strings:
    file_groups[s.file] = file_groups.get(s.file, 0) + 1

print("\nUnique files in strings (top 30):")
for f, count in sorted(file_groups.items(), key=lambda x: -x[1])[:30]:
    print(f"  {f}: {count} strings")
