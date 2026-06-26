import sys
import os
import shutil

parser_dir = r"C:\Users\Alexandr\Desktop\Interprex\python-core"
sys.path.append(parser_dir)

from parsers import get_parser
from parsers.i18n import count_generated_definjected, _target_definjected_keys

root = r"G:\SteamLibrary\steamapps\workshop\content\294100"
mod_id = "1949064302"
mod_path = os.path.join(root, mod_id)

print(f"Mod path: {mod_path}")
print("Checking target author keys...")
author_keys = _target_definjected_keys(mod_path, "Russian")
print(f"Loaded {len(author_keys)} author keys (ThingDefs/RecipeDefs etc normalized).")

# Let's count missing strings
missing_cnt = count_generated_definjected(mod_path, "Russian")
print(f"Missing generated DefInjected strings to translate: {missing_cnt}")

# Extract
print("Extracting strings...")
parser = get_parser("i18n")
strings = parser.extract(root, [mod_id])
print(f"Extracted total of {len(strings)} strings from mod {mod_id}")

defs_strings = [s for s in strings if "RimWorld Defs |" in s.context]
print(f"Extracted {len(defs_strings)} strings generated from Defs/*.xml")

# Let's test inject. We will write to a temp directory to avoid modifying the real mod folder!
import tempfile
temp_mod_root = tempfile.mkdtemp(prefix="interprex_rimworld_real_test_")
temp_mod_path = os.path.join(temp_mod_root, mod_id)
print(f"Copying real mod to temp path: {temp_mod_path}")
shutil.copytree(mod_path, temp_mod_path)

try:
    # Now run extract and inject on the temp directory
    p_temp = get_parser("i18n")
    extracted_temp = p_temp.extract(temp_mod_root, [mod_id])
    print(f"Extracted {len(extracted_temp)} strings in temp path.")
    
    # Check if muscle/stimulator is in extracted strings
    stim_strings = [s for s in extracted_temp if any(x in (s.original or "").lower() for x in ("muscle", "stimulator"))]
    print(f"Found {len(stim_strings)} strings containing 'muscle' or 'stimulator':")
    for s in stim_strings[:10]:
        print(f"  ID: {s.id} | Original: {s.original} | File: {s.file}")
        
    # Translate all strings to upper case
    translations = {s.id: s.original.upper() if s.original else "UPPER" for s in extracted_temp}
    
    # Inject
    print("Injecting translations...")
    written = p_temp.inject(temp_mod_root, translations, "Russian", [mod_id])
    print(f"Injected {written} strings.")
    
    # Let's inspect the generated DefInjected folder
    gen_dir = os.path.join(temp_mod_path, "1.6", "Languages", "Russian (Русский)", "DefInjected")
    if not os.path.isdir(gen_dir):
        print("Generated DefInjected directory not found at 1.6 path, checking other folders...")
        for root_d, dirs_d, files_d in os.walk(os.path.join(temp_mod_path, "Languages")):
            print(f"Found dir/file: {root_d}")
    else:
        # Check generated files in gen_dir
        print(f"Listing generated DefInjected files in {gen_dir}:")
        import xml.etree.ElementTree as ET
        warnings_found = 0
        for r_dp, _, r_fns in os.walk(gen_dir):
            for r_fn in r_fns:
                if r_fn.endswith(".xml"):
                    full_p = os.path.join(r_dp, r_fn)
                    rel_p = os.path.relpath(full_p, gen_dir)
                    tree = ET.parse(full_p)
                    root_el = tree.getroot()
                    keys_in_file = [ch.tag for ch in root_el if isinstance(ch.tag, str)]
                    print(f"  File: {rel_p} ({len(keys_in_file)} keys)")
                    
                    # Ensure no key is also in author_keys
                    def_type = os.path.basename(r_dp)
                    from parsers.i18n import _normalize_def_type, _dedup_field_key
                    dt_norm = _normalize_def_type(def_type)
                    
                    for k in keys_in_file:
                        k_norm = _dedup_field_key(k)
                        if (dt_norm, k_norm) in author_keys:
                            print(f"    WARNING: Key {k} (normalized: {dt_norm}, {k_norm}) in generated file is ALSO in author keys!")
                            warnings_found += 1
        if warnings_found == 0:
            print("SUCCESS: Zero duplicate keys generated! The deduplication works perfectly.")
        else:
            print(f"FAILURE: Found {warnings_found} duplicate keys.")
                    
finally:
    print(f"Cleaning up {temp_mod_root}")
    shutil.rmtree(temp_mod_root, ignore_errors=True)
print("Done.")
