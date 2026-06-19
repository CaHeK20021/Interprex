import sys, os, tempfile, struct, zlib
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\Alexandr\Desktop\Interprex\python-core')
from parsers import rpa as rpamod

temp = tempfile.mkdtemp(prefix='debug_rpyc_')
game_dir = r'C:\Users\Alexandr\Desktop\OnlineObsessionDemo-0.1.0-win'
rpamod.extract_rpa_file(os.path.join(game_dir, 'game', 'scripts.rpa'), 'script-1st-stage.rpyc', os.path.join(temp, 'script-1st-stage.rpyc'))

rpyc_path = os.path.join(temp, 'script-1st-stage.rpyc')

RPYC2_HEADER = b"RENPY RPC2"

with open(rpyc_path, 'rb') as f:
    header = f.read(1024)

pos = len(RPYC2_HEADER)
slots = {}
for i in range(10):
    slot_id, start, length = struct.unpack('III', header[pos:pos+12])
    if slot_id == 0:
        break
    slots[slot_id] = (start, length)
    pos += 12

# Read slot 2 (after static transforms - this is what gets loaded)
with open(rpyc_path, 'rb') as f:
    f.seek(slots[2][0])
    data = f.read(slots[2][1])
    decompressed = zlib.decompress(data)
    print("Slot 2 decompressed: %d bytes" % len(decompressed))

    # Search for mayo text
    mayo_utf8 = "\u201cThe mayo in your fridge will expire tomorrow.\u201d".encode('utf-8')
    idx = decompressed.find(mayo_utf8)
    if idx >= 0:
        print("Found mayo text at offset %d" % idx)
        # Show context around it
        start = max(0, idx - 200)
        end = min(len(decompressed), idx + len(mayo_utf8) + 200)
        context = decompressed[start:end]
        # Print as hex + ascii
        for i in range(0, len(context), 16):
            chunk = context[i:i+16]
            hex_str = ' '.join('%02x' % b for b in chunk)
            ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            print("  %06x: %-48s %s" % (start + i, hex_str, ascii_str))
    else:
        print("Mayo text NOT found in slot 2")
        # Try slot 1
        f.seek(slots[1][0])
        data = f.read(slots[1][1])
        decompressed1 = zlib.decompress(data)
        idx = decompressed1.find(mayo_utf8)
        if idx >= 0:
            print("Found mayo in slot 1 at offset %d" % idx)
        else:
            print("Mayo NOT found in slot 1 either")

    # Search for f5004a22
    idx = decompressed.find(b'f5004a22')
    if idx >= 0:
        print("\nFound f5004a22 at offset %d" % idx)
        print("Context:", decompressed[max(0,idx-20):idx+30])
    else:
        print("\nf5004a22 NOT found in slot 2")
