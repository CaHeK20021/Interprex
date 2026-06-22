import sys
import os

# Add python-core to path
parser_dir = r"c:\Users\Alexandr\Desktop\Interprex\python-core"
sys.path.append(parser_dir)

from parsers import get_parser

root = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory"
sub_paths = ["FactoryGame/Mods/GameFeatures/InfiniteNudge"]

print("Testing extraction for InfiniteNudge...")
try:
    parser = get_parser("unreal4_5")
    strings = parser.extract(root, sub_paths)
    print(f"SUCCESS: Extracted {len(strings)} strings")
    for i, s in enumerate(strings[:10]):
        print(f"{i+1}. ID: {s.id} | Original: {s.original} | Context: {s.context} | File: {s.file}")
except Exception as e:
    import traceback
    print("FAILED extraction:")
    traceback.print_exc()
