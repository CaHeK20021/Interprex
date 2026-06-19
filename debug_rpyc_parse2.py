import sys, os, tempfile, struct, zlib, pickle
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

print("Header matches RPYC2:", header[:len(RPYC2_HEADER)] == RPYC2_HEADER)
print("Header hex:", header[:40].hex())
print("Header ascii:", header[:20])

if header[:len(RPYC2_HEADER)] == RPYC2_HEADER:
    pos = len(RPYC2_HEADER)
    slots = {}
    for i in range(10):
        try:
            slot_id, start, length = struct.unpack('III', header[pos:pos+12])
            if slot_id == 0:
                break
            slots[slot_id] = (start, length)
            print("Slot %d: offset=%d, length=%d" % (slot_id, start, length))
            pos += 12
        except:
            break

    with open(rpyc_path, 'rb') as f:
        for slot_id, (start, length) in sorted(slots.items()):
            f.seek(start)
            data = f.read(length)
            try:
                decompressed = zlib.decompress(data)
                print("\nSlot %d: decompressed %d bytes" % (slot_id, len(decompressed)))
                unpickled = pickle.loads(decompressed)
                if isinstance(unpickled, tuple) and len(unpickled) == 2:
                    data_part, stmt_list = unpickled
                    print("  stmts type: %s, len: %d" % (type(stmt_list).__name__, len(stmt_list)))
                    for node in stmt_list:
                        if hasattr(node, 'what') and hasattr(node, 'who') and node.__class__.__name__ == 'Say':
                            what = getattr(node, 'what', '')
                            if 'mayo' in str(what).lower():
                                who_val = getattr(node, 'who', None)
                                ident = getattr(node, 'identifier', 'NOT SET')
                                expl = getattr(node, 'explicit_identifier', 'NOT SET')
                                print("\n  === SAY NODE FOUND ===")
                                print("    who:", repr(who_val))
                                print("    what:", repr(what))
                                print("    identifier:", repr(ident))
                                print("    explicit_identifier:", repr(expl))
                                if hasattr(node, '__slots__'):
                                    for s in node.__slots__:
                                        val = getattr(node, s, 'MISSING')
                                        if val is not None and val != '' and val != False:
                                            print("    %s: %r" % (s, val))
            except Exception as e:
                import traceback
                traceback.print_exc()
else:
    # Legacy format: try zlib decompress from start
    f = open(rpyc_path, 'rb')
    data = f.read()
    f.close()
    try:
        decompressed = zlib.decompress(data)
        print("Legacy format: decompressed %d bytes" % len(decompressed))
        unpickled = pickle.loads(decompressed)
        print("Unpickled type:", type(unpickled))
    except:
        print("Not legacy format either")
        # Try reading from offset
        try:
            decompressed = zlib.decompress(data[10:])
            print("Offset 10: decompressed %d bytes" % len(decompressed))
        except:
            print("Failed at offset 10 too")
