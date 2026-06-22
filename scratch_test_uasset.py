import sys
import os
import shutil
from pathlib import Path

# Add python-core to path
parser_dir = r"c:\Users\Alexandr\Desktop\Interprex\python-core"
sys.path.append(parser_dir)

from parsers import get_parser

root = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory"
mod_path = "FactoryGame/Mods/InstantCraftBench"

print("1. Extracting strings from InstantCraftBench...")
parser = get_parser("unreal4_5")
strings = parser.extract(root, [mod_path])

print(f"Extracted {len(strings)} strings.")
if not strings:
    print("FAILED: No strings extracted.")
    sys.exit(1)

for i, s in enumerate(strings):
    print(f"  {i+1}. ID: {s.id} | Original: {s.original} | Path: {s.path} | File: {s.file}")

translations = {}
for s in strings:
    translations[s.id] = f"[RU] {s.original}"

print("\n2. Injecting translations (generating ContentLib patches)...")
configs_dir = Path(root) / "FactoryGame" / "Configs" / "ContentLib"
if configs_dir.exists():
    print(f"Cleaning up existing test configs at {configs_dir}...")
    shutil.rmtree(configs_dir)

backup_dir = Path(root) / ".interprex_backups"
if backup_dir.exists():
    shutil.rmtree(backup_dir)

written = parser.inject(root, translations, "Russian", [mod_path])
print(f"Injected {written} translations.")

recipe_patches = list((configs_dir / "RecipePatches").glob("*.json")) if (configs_dir / "RecipePatches").exists() else []
item_patches = list((configs_dir / "ItemPatches").glob("*.json")) if (configs_dir / "ItemPatches").exists() else []

print(f"\n3. Verifying generated files:")
print(f"  Recipe patches found: {len(recipe_patches)}")
for p in recipe_patches:
    print(f"    - {p.name}")
    with open(p, "r", encoding="utf-8") as f:
        print(f"      Content: {f.read().strip()}")

print(f"  Item patches found: {len(item_patches)}")
for p in item_patches:
    print(f"    - {p.name}")
    with open(p, "r", encoding="utf-8") as f:
        print(f"      Content: {f.read().strip()}")

if not recipe_patches and not item_patches:
    print("FAILED: No patch files were created.")
    sys.exit(1)

print("\n4. Verifying backup status...")
import main
status = main.backup_status(main.BackupStatusReq(root=root))
print(f"  Has backup: {status['has_backup']}")

print("\n5. Restoring backup (should delete patches)...")
restore_res = main.backup_restore(main.BackupRestoreReq(root=root))
print(f"  Restore success: {restore_res.get('success')}")

recipe_patches_after = list((configs_dir / "RecipePatches").glob("*.json")) if (configs_dir / "RecipePatches").exists() else []
item_patches_after = list((configs_dir / "ItemPatches").glob("*.json")) if (configs_dir / "ItemPatches").exists() else []
print(f"  Recipe patches after restore: {len(recipe_patches_after)}")
print(f"  Item patches after restore: {len(item_patches_after)}")

if len(recipe_patches_after) > 0 or len(item_patches_after) > 0:
    print("FAILED: Patches were not deleted after restore.")
    sys.exit(1)

print("\nALL TESTS PASSED SUCCESSFULLY! UAsset -> ContentLib JSON flow works perfectly!")
