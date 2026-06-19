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

with open(rpyc_path, 'rb') as f:
    f.seek(slots[2][0])
    data = f.read(slots[2][1])
    decompressed = zlib.decompress(data)

# Find mayo and show 600 bytes BEFORE it - the pickle ops should reveal the Say node structure
mayo_utf8 = "\u201cThe mayo in your fridge will expire tomorrow.\u201d".encode('utf-8')
idx = decompressed.find(mayo_utf8)
print("Mayo at offset %d" % idx)

# Show 400 bytes before the mayo text
start = idx - 400
end = idx + len(mayo_utf8) + 100
chunk = decompressed[start:end]

# Decode pickle opcodes
import dis
import io

# Try to interpret the pickle opcodes manually
i = 0
opcodes = []
while i < len(chunk):
    op = chunk[i]
    i += 1
    if op == 0x80:  # SHORT_BINUNICODE
        length = chunk[i]; i += 1
        s = chunk[i:i+length].decode('utf-8', errors='replace')
        i += length
        opcodes.append(("SHORT_BINUNICODE", repr(s)))
    elif op == 0x81:  # NEWOBJ
        opcodes.append(("NEWOBJ", ""))
    elif op == 0x85:  # TUPLE1
        opcodes.append(("TUPLE1", ""))
    elif op == 0x86:  # TUPLE2
        opcodes.append(("TUPLE2", ""))
    elif op == 0x87:  # TUPLE3
        opcodes.append(("TUPLE3", ""))
    elif op == 0x88:  # NEWTRUE
        opcodes.append(("NEWTRUE", ""))
    elif op == 0x89:  # NEWFALSE
        opcodes.append(("NEWFALSE", ""))
    elif op == 0x8a:  # LONG_BINPUT
        i += 4
        opcodes.append(("LONG_BINPUT", ""))
    elif op == 0x8b:  # LONG_BINGET
        i += 4
        opcodes.append(("LONG_BINGET", ""))
    elif op == 0x8d:  # STACK_GLOBAL
        opcodes.append(("STACK_GLOBAL", ""))
    elif op == 0x52:  # REDUCE
        opcodes.append(("REDUCE", ""))
    elif op == 0x58:  # POP
        opcodes.append(("POP", ""))
    elif op == 0x4e:  # NONE
        opcodes.append(("NONE", ""))
    elif op == 0x72:  # BINGET
        idx2 = chunk[i]; i += 1
        opcodes.append(("BINGET", str(idx2)))
    elif op == 0x6a:  # BINPUT
        idx2 = chunk[i]; i += 1
        opcodes.append(("BINPUT", str(idx2)))
    elif op == 0x6d:  # MEMOIZE
        opcodes.append(("MEMOIZE", ""))
    elif op == 0x68:  # BINGET (short)
        idx2 = chunk[i]; i += 1
        opcodes.append(("BINGET_SHORT", str(idx2)))
    elif op == 0x33:  # SHORT_BINUNICODE
        length = chunk[i]; i += 1
        s = chunk[i:i+length].decode('utf-8', errors='replace')
        i += length
        opcodes.append(("SHORT_BINUNICODE", repr(s)))
    elif op == 0x62:  # BUILD
        opcodes.append(("BUILD", ""))
    elif op == 0x7d:  # EMPTY_DICT
        opcodes.append(("EMPTY_DICT", ""))
    elif op == 0x28:  # MARK
        opcodes.append(("MARK", ""))
    elif op == 0x29:  # TUPLE
        opcodes.append(("TUPLE", ""))
    elif op == 0x6b:  # TUPLE3
        opcodes.append(("TUPLE3", ""))
    elif op == 0x75:  # SETITEMS
        opcodes.append(("SETITEMS", ""))
    elif op == 0x63:  # GLOBAL
        name = b''
        while chunk[i:i+1] != b'\n':
            name += chunk[i:i+1]
            i += 1
        i += 1
        name2 = b''
        while chunk[i:i+1] != b'\n':
            name2 += chunk[i:i+1]
            i += 1
        i += 1
        opcodes.append(("GLOBAL", name.decode() + ' ' + name2.decode()))
    elif op == 0x65:  # APPENDS
        opcodes.append(("APPENDS", ""))
    elif op == 0x61:  # APPEND
        opcodes.append(("APPEND", ""))
    elif op == 0x30:  # POP_MARK
        opcodes.append(("POP_MARK", ""))
    elif op == 0x71:  # BINBYTES
        i += 4
        opcodes.append(("BINBYTES", ""))
    elif op == 0x46:  # BINPERSID
        opcodes.append(("BINPERSID", ""))
    else:
        opcodes.append(("OP_%02x" % op, ""))

for name, arg in opcodes:
    print("  %s %s" % (name, arg))
