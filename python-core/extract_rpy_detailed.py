from parsers.rpa import iter_rpa_files, read_rpa
import os
import re
from collections import defaultdict

game_dir = r'G:\SteamLibrary\steamapps\common\Killer Chat! - Original Edition\game'
rpy_files = []

# Extract all .rpy files
for arc in iter_rpa_files(game_dir):
    arc_name = os.path.basename(arc)
    for rf in read_rpa(arc, '.rpy'):
        rpy_files.append((arc_name, rf.path, rf.data))

print(f"Total .rpy files: {len(rpy_files)}\n")

# Patterns for string comparisons
comparison_patterns = [
    (r'==\s*["\']([^"\']*)["\']', 'eq_right'),      
    (r'["\']([^"\']*)["\'] == ', 'eq_left'),         
    (r'!=\s*["\']([^"\']*)["\']', 'ne_right'),       
    (r'["\']([^"\']*)["\'] != ', 'ne_left'),         
    (r'in\s*\[\s*["\']([^"\']*)["\']', 'in_list'),   
    (r'\.get\s*\(\s*["\']([^"\']*)["\']', 'get_key'),
]

comparison_keys = defaultdict(list)

# Analyze for comparison keys
for arc_name, rpy_path, content in rpy_files:
    lines = content.split('\n')
    for line_num, line in enumerate(lines, 1):
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

# Count and display
print(f"=== STATISTICS ===")
print(f"Total unique comparison-key strings: {len(comparison_keys)}\n")

# Separate into prose vs technical
prose_keywords = ['the', 'a', 'an', 'and', 'or', 'murder', 'day', 'route', 'reason', 'motivation', 
                  'offline', 'online', 'away', 'invisible', 'do not', 'dont']
technical_keywords = ['.rpa', '.exe', '.png', '.ogg', '.rpy', 'file', 'path', 'code', 'script']

prose_strings = {}
technical_strings = {}
ambiguous_strings = {}

for string in comparison_keys.keys():
    lower = string.lower()
    is_technical = any(kw in lower for kw in technical_keywords) or ('/' in string) or ('\' in string)
    is_prose = any(kw in lower for kw in prose_keywords)
    
    if is_technical:
        technical_strings[string] = comparison_keys[string]
    elif is_prose:
        prose_strings[string] = comparison_keys[string]
    else:
        ambiguous_strings[string] = comparison_keys[string]

print(f"Prose strings (player-visible): {len(prose_strings)}")
print(f"Technical strings: {len(technical_strings)}")
print(f"Ambiguous: {len(ambiguous_strings)}\n")

# Now look for assignments (to understand if same string is assigned and compared)
assignment_patterns = [
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*["\']([^"\']*)["\']',  # x = "string"
    r'\.append\(["\']([^"\']*)["\']',  # .append("string")
]

assignments = defaultdict(list)

for arc_name, rpy_path, content in rpy_files:
    lines = content.split('\n')
    for line_num, line in enumerate(lines, 1):
        if line.strip().startswith('#'):
            continue
        
        for pattern in assignment_patterns:
            matches = re.finditer(pattern, line)
            for match in matches:
                if len(match.groups()) >= 2:
                    string_val = match.group(2)
                else:
                    string_val = match.group(1)
                
                assignments[string_val].append({
                    'file': f"{arc_name}:{rpy_path}",
                    'line_num': line_num,
                    'line_text': line.strip()
                })

# Find strings that are BOTH assigned and compared
print("=== STRINGS THAT ARE BOTH ASSIGNED AND COMPARED ===\n")
both = []
for string in comparison_keys.keys():
    if string in assignments:
        both.append(string)
        print(f"STRING: '{string}'")
        print(f"  ASSIGNED at:")
        for a in assignments[string][:3]:
            print(f"    {a['file']}:{a['line_num']}")
            print(f"      {a['line_text']}")
        print(f"  COMPARED at:")
        for c in comparison_keys[string][:3]:
            print(f"    {c['file']}:{c['line_num']} [{c['context']}]")
            print(f"      {c['line_text']}")
        print()

print(f"\n=== SUMMARY ===")
print(f"Strings found in both assignments and comparisons: {len(both)}")
print(f"They are: {sorted(both)}\n")

