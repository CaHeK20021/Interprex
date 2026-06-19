import os
import sys
import io

# Force UTF-8 standard output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

SCREENS_FILE = r"C:\GOG Games\QSP games\SaraGame\SaraGame 2\SaraGame2-0.8-pc\game\screens.rpy"

def inspect_lines():
    if not os.path.exists(SCREENS_FILE):
        print(f"File not found: {SCREENS_FILE}")
        return
        
    with open(SCREENS_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    targets = [
        "horizontal_[prefix_]thumb.png",
        "navigation",
        "overlay/main_menu.png",
        "font",
        "#000",
        "window_background"
    ]
    
    for target in targets:
        print(f"\n=== Searching for: {repr(target)} ===")
        found = 0
        for idx, line in enumerate(lines):
            if target in line:
                print(f"Line {idx + 1}: {repr(line)}")
                found += 1
                if found >= 3:
                    break

if __name__ == "__main__":
    inspect_lines()
