"""Test extraction from real Satisfactory mods with full logging."""
import sys, os, logging, time
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.append(r"C:\Users\Alexandr\Desktop\Interprex\python-core")

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

from parsers import get_parser
from pathlib import Path

root = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory"
mods_dir = Path(root) / "FactoryGame" / "Mods"

SKIP = {"contentlib", "sml", "filesystemlibrary", "jeanmichelcommonlib", "mkpluslibs"}
parser = get_parser("unreal4_5")

# Collect all mod paths
mod_paths = []
for mod_dir in sorted(mods_dir.iterdir()):
    if not mod_dir.is_dir() or mod_dir.name.lower() in SKIP:
        continue
    mod_paths.append((mod_dir.name, f"FactoryGame/Mods/{mod_dir.name}"))
    if mod_dir.name == "GameFeatures":
        for sub in sorted(mod_dir.iterdir()):
            if sub.is_dir() and sub.name.lower() not in SKIP:
                mod_paths.append((sub.name, f"FactoryGame/Mods/GameFeatures/{sub.name}"))

all_paths = [p for _, p in mod_paths]

print(f"\n{'='*60}")
print(f"Testing extract from {len(mod_paths)} mods with ProcessPoolExecutor")
print(f"{'='*60}\n")

start = time.time()
try:
    strings = parser.extract(root, all_paths)
    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"RESULT: {len(strings)} strings in {elapsed:.1f}s")
    print(f"{'='*60}")

    # Per-mod breakdown
    by_mod = {}
    for s in strings:
        for mn, mp in mod_paths:
            if s.file.startswith(f"uasset://{mp}") or mp in s.file:
                by_mod.setdefault(mn, 0)
                by_mod[mn] += 1
                break

    for mn, mp in mod_paths:
        count = by_mod.get(mn, 0)
        status = "OK" if count > 0 else "EMPTY"
        print(f"  [{status}] {mn}: {count}")

except Exception as e:
    elapsed = time.time() - start
    print(f"\nFAILED after {elapsed:.1f}s: {e}")
    import traceback
    traceback.print_exc()
