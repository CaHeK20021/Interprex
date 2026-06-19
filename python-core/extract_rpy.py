from parsers.rpa import iter_rpa_files, read_rpa
import os
import sys
import re
from collections import defaultdict

# Setup
game_dir = r'G:\SteamLibrary\steamapps\common\Killer Chat! - Original Edition\game'
rpy_files = []

print("=== Scanning RPA archives for .rpy files ===\n")

for arc in iter_rpa_files(game_dir):
    arc_name = os.path.basename(arc)
    for rf in read_rpa(arc, '.rpy'):
        rpy_files.append((arc_name, rf.path, rf.data))
        print(f"{arc_name}: {rf.path}")

print(f"\n=== Total .rpy files found: {len(rpy_files)} ===\n")

# Now let's analyze for comparison keys
# Patterns for string comparisons
comparison_patterns = [
    (r'==\s*["\']([^"\']*)["\']', 'eq_right'),      # x == "string"
    (r'["\']([^"\']*)["\'] == ', 'eq_left'),         # "string" == x
    (r'!=\s*["\']([^"\']*)["\']', 'ne_right'),       # x != "string"
    (r'["\']([^"\']*)["\'] != ', 'ne_left'),         # "string" != x
    (r'in\s*\[\s*["\']([^"\']*)["\']', 'in_list'),   # in ["string"
    (r'\.get\s*\(\s*["\']([^"\']*)["\']', 'get_key'), # .get("string"
]

comparison_keys = defaultdict(list)  # string -> [(file, line_num, line_text, context_type)]

print("=== Analyzing for comparison key strings ===\n")

for arc_name, rpy_path, content in rpy_files:
    lines = content.split('\n')
    for line_num, line in enumerate(lines, 1):
        # Skip comments
        if line.strip().startswith('#'):
            continue
        
        for pattern, context_type in comparison_patterns:
            matches = re.finditer(pattern, line)
            for match in matches:
                key_string = match.group(1)
                comparison_keys[key_string].append({
                    'file': f"{arc_name}:{rpy_path}",
                    'line_num': line_num,
                    'line_text': line.strip(),
                    'context': context_type
                })

print(f"Found {len(comparison_keys)} unique comparison-key strings\n")

# Output
for string in sorted(comparison_keys.keys()):
    occurrences = comparison_keys[string]
    print(f"STRING: '{string}'")
    for occ in occurrences:
        print(f"  {occ['file']}:{occ['line_num']} [{occ['context']}]")
        print(f"    {occ['line_text']}")
    print()

