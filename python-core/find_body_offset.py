import struct, sys

with open(sys.argv[1], 'rb') as f:
    raw = f.read()

print(f"File size: {len(raw)} bytes")
print(f"Magic: {raw[0]:02x} {raw[1]:02x}")
print(f"Version: {raw[2]}")

# Try to find the right body offset by brute-force scanning
# We know: names count should be ~57917, enums ~2594, structs ~13795
# Body starts after the header

# Parse header manually
off = 0
magic = struct.unpack_from('<H', raw, off)[0]; off += 2
version = raw[off]; off += 1
has_ver = struct.unpack_from('<i', raw, off)[0]; off += 4
fv4 = struct.unpack_from('<i', raw, off)[0]; off += 4
fv5 = struct.unpack_from('<i', raw, off)[0]; off += 4
print(f"Header: magic=0x{magic:04X} ver={version} has_ver={has_ver} fv4={fv4} fv5={fv5}")

if version >= 1 and has_ver:
    cv_count = struct.unpack_from('<I', raw, off)[0]; off += 4
    print(f"Custom version count: {cv_count}")
    # Each custom version: GUID(16 bytes) + version_number(i32) = 20 bytes
    for i in range(min(cv_count, 3)):
        guid = raw[off:off+16]
        ver_num = struct.unpack_from('<i', raw, off+16)[0]
        print(f"  CV[{i}]: guid={guid.hex()[:32]}... ver={ver_num}")
        off += 20
    # Skip rest
    off += (cv_count - min(cv_count, 3)) * 20
    
    net_cl = struct.unpack_from('<i', raw, off)[0]; off += 4
    compression = raw[off]; off += 1
    csize = struct.unpack_from('<I', raw, off)[0]; off += 4
    dsize = struct.unpack_from('<I', raw, off)[0]; off += 4
    print(f"net_cl={net_cl} compression={compression} csize={csize} dsize={dsize}")
    print(f"Header ends at offset: {off}")

# Now scan for valid name count at various offsets
print(f"\n=== Scanning for valid body start ===")
for test_off in range(off - 4, off + 200):
    if test_off + 12 > len(raw):
        break
    nc = struct.unpack_from('<I', raw, test_off)[0]
    # Try to read first name
    name_len = raw[test_off + 4]
    if nc > 50000 and nc < 65000 and name_len > 0 and name_len < 200:
        # Try to parse a few names
        nboff = test_off + 4
        ok = True
        first_name = ""
        for ni in range(min(5, nc)):
            if nboff >= len(raw): ok = False; break
            sl = raw[nboff]
            if sl == 0 or nboff + 1 + sl > len(raw): ok = False; break
            name = raw[nboff+1:nboff+1+sl].decode('utf-8', errors='replace').rstrip('\x00')
            if ni == 0:
                first_name = name
            nboff += 1 + sl
        if ok and first_name:
            # After names, check enum count
            if nboff + 4 <= len(raw):
                ec = struct.unpack_from('<I', raw, nboff)[0]
                if ec > 2000 and ec < 4000:
                    print(f"  offset {test_off}: names={nc} first='{first_name}' enums={ec} — CANDIDATE")
                    # Try full parse from here
                    boff = test_off
                    nc2 = struct.unpack_from('<I', raw, boff)[0]; boff += 4
                    names = []
                    for _ in range(nc2):
                        sl = raw[boff]; boff += 1 + sl
                        names.append(raw[boff-sl:boff].decode('utf-8', errors='replace').rstrip('\x00'))
                    ec2 = struct.unpack_from('<I', raw, boff)[0]; boff += 4
                    for _ in range(ec2):
                        boff += 4; ne = raw[boff]; boff += 1; boff += ne * 4
                    sc2 = struct.unpack_from('<I', raw, boff)[0]; boff += 4
                    print(f"    Parsed: names={nc2} enums={ec2} structs={sc2} body_rem={len(raw)-boff}")
                    # Check if struct parsing works
                    ok2 = True
                    count = 0
                    soff = boff
                    for si in range(min(20, sc2)):
                        if soff + 12 > len(raw): ok2 = False; break
                        ni = struct.unpack_from('<I', raw, soff)[0]; soff += 4
                        spi = struct.unpack_from('<I', raw, soff)[0]; soff += 4
                        pc = struct.unpack_from('<H', raw, soff)[0]; soff += 2
                        sc3 = struct.unpack_from('<H', raw, soff)[0]; soff += 2
                        for pi in range(sc3):
                            if soff + 7 > len(raw): ok2 = False; break
                            soff += 2 + 1 + 4
                            if soff >= len(raw): ok2 = False; break
                            pt = raw[soff]; soff += 1
                            if pt == 8: 
                                inner = raw[soff]; soff += 1
                                if inner == 9: soff += 4
                            elif pt == 9: soff += 4
                            elif pt == 24:
                                k = raw[soff]; soff += 1
                                if k == 9: soff += 4
                                v = raw[soff]; soff += 1
                                if v == 9: soff += 4
                            elif pt == 25:
                                k = raw[soff]; soff += 1
                                if k == 9: soff += 4
                            elif pt == 26:
                                inner = raw[soff]; soff += 1
                                if inner == 9: soff += 4
                                soff += 4
                            elif pt == 28:
                                inner = raw[soff]; soff += 1
                                if inner == 9: soff += 4
                            count = si + 1
                        if not ok2: break
                    print(f"    First 20 structs parsed: {count}, soff={soff}")

# Also check: what is the actual file size?
print(f"\nExpected EOF: 1812 + 2678775 = {1812 + 2678775}")
print(f"Actual file size: {len(raw)}")
print(f"Difference: {len(raw) - (1812 + 2678775)}")
