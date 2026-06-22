import os
import json

root = r"C:\Program Files (x86)\Steam\steamapps\common\Satisfactory"
project_path = os.path.join(root, "Interprex", "project.json")
if not os.path.exists(project_path):
    project_path = os.path.join(root, "FactoryGame", "Mods", "Interprex", "project.json")
if not os.path.exists(project_path):
    # Try finding any project.json in subfolders
    for r_dir, dirs, files in os.walk(root):
        if "project.json" in files:
            project_path = os.path.join(r_dir, "project.json")
            break

print("Found project.json path:", project_path)

if os.path.exists(project_path):
    with open(project_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    strings = data.get("strings", {})
    print(f"Total strings in project: {len(strings)}")
    
    # Wait, the project.json contains id -> entry. Let's see some keys
    first_keys = list(strings.keys())[:10]
    for k in first_keys:
        print(k, ":", str(strings[k])[:200])
else:
    print("project.json not found!")
