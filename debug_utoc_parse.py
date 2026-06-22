import sys
import re
import subprocess
from pathlib import Path

retoc_bin = r"c:\Users\Alexandr\Desktop\Interprex\python-core\bin\retoc.exe"
uf = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory\FactoryGame\Mods\InstantCraftBench\Content\Paks\Windows\InstantCraftBenchFactoryGame-Windows.utoc"

def _is_translatable_uasset(inner_path: str) -> bool:
    path_lower = inner_path.lower()
    name = path_lower.split("/")[-1]
    
    # Check prefixes
    if name.startswith(("recipe_", "desc_", "build_", "schem_", "rec_")):
        return True
        
    # Check folder segments
    segments = set(path_lower.split("/"))
    if segments.intersection({"recipes", "items", "buildable", "schematics"}):
        return True
        
    return False

list_re = re.compile(r"^(?:\S+\s+)?(?P<chunk_id>[0-9a-fA-F]{16,32})\s+.*?\s+(?P<inner_path>.*\.uasset)$")

try:
    res = subprocess.run([retoc_bin, "list", "--path", uf], capture_output=True, text=True, check=True)
    print("Stdout:")
    print(res.stdout)
    
    print("\nMatching lines:")
    for line in res.stdout.splitlines():
        l_strip = line.strip()
        m = list_re.match(l_strip)
        if m:
            gd = m.groupdict()
            trans = _is_translatable_uasset(gd["inner_path"])
            print(f"Match: chunk={gd['chunk_id']}, path={gd['inner_path']} | IsTranslatable={trans}")
        else:
            print(f"NO Match: {l_strip}")
except Exception as e:
    print("Error:", e)
