import sys
import subprocess
import re
from pathlib import Path

retoc_bin = r"c:\Users\Alexandr\Desktop\Interprex\python-core\bin\retoc.exe"
mods_dir = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory\FactoryGame\Mods")

print("Scanning all .utoc files in mods directory...")
utoc_files = list(mods_dir.rglob("*.utoc"))

for uf in utoc_files:
    print(f"\n========================================\nMod: {uf.parents[3].name if len(uf.parts) > 4 else uf.name}")
    print(f"File: {uf.name}")
    try:
        res = subprocess.run([retoc_bin, "list", "--path", str(uf)], capture_output=True, text=True, check=True)
        lines = res.stdout.splitlines()
        print(f"Total files in container: {len(lines)}")
        
        locres_files = []
        other_files = []
        for line in lines:
            if ".locres" in line.lower() or "localization" in line.lower() or "locale" in line.lower():
                locres_files.append(line.strip())
            else:
                other_files.append(line.strip())
                
        if locres_files:
            print("FOUND LOCALIZATION FILES:")
            for lf in locres_files:
                print(f"  - {lf}")
        else:
            print("No localization (.locres) files found.")
            if other_files:
                print("Sample files inside:")
                for of in other_files[:5]:
                    print(f"  - {of}")
    except Exception as e:
        print(f"Error running retoc for {uf.name}: {e}")
