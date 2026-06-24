import struct, sys

with open(sys.argv[1], 'rb') as f:
    raw = f.read()

# Header: magic(2)+ver(1)+has_ver(4)+fv4(4)+fv5(4)+cv_count(4)+cv[89*20]+net_cl(4)+compression(1)+csize(4)+dsize(4)
off = 1812
body = raw[off:]
print(f"Body starts at {off}, body size: {len(body)}")

# Dump first 200 bytes of body in hex + interpretation
print(f"\n=== First 200 bytes of body ===")
for i in range(0, min(200, len(body)), 16):
    hex_str = ' '.join(f'{body[i+j]:02x}' for j in range(min(16, len(body)-i)))
    # Try to interpret as u32
    vals = []
    for j in range(0, min(16, len(body)-i), 4):
        v = struct.unpack_from('<I', body, i+j)[0]
        vals.append(f"u32={v}")
    print(f"  {i:6d}: {hex_str:<48s}  {' '.join(vals)}")

# The first u32 should be name_count
nc = struct.unpack_from('<I', body, 0)[0]
print(f"\nFirst u32 (name_count?): {nc}")

# Read first name to check
if nc > 0:
    name_off = 4
    sl = body[name_off]
    name = body[name_off+1:name_off+1+sl].decode('utf-8', errors='replace')
    print(f"First name at offset {name_off}: len={sl}, name='{name}'")

# Try reading as if names section has different structure
# What if there's no name_count prefix and names start directly?
print(f"\n=== What if body starts with name string directly? ===")
if body[4] > 0 and body[4] < 200:
    sl = body[4]
    name = body[5:5+sl].decode('utf-8', errors='replace')
    print(f"  byte[4]={sl} (name len), name='{name}'")

# Let me look at the usmap-rs source format more carefully
# The usmap crate reads:
# 1. magic u16
# 2. version u8
# 3. if version >= 1: has_versioning(i32), file_version_ue4(i32), file_version_ue5(i32), 
#    custom_version_count(i32), custom_versions[Guid(16)+i32]*count, net_cl(i32)
# 4. compression_method(u8), compressed_size(u32), decompressed_size(u32)
# 5. if compressed: decompress
# 6. names: i32 count, then for each: u8 len, bytes[len]
# 7. enums: i32 count, then for each: i32 name_idx, u8 entry_count, u32[entry_count]
# 8. structs: i32 count, then for each: i32 name_idx, i32 super_idx, u16 prop_count, u16 serializable_count, props[prop_count]

# Wait - the body starts at offset 1812 which is correct.
# Let me check if the name at index 0 is what we expect
print(f"\n=== Verify name parsing ===")
boff = 0
nc = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"Name count: {nc}")
first_names = []
for i in range(min(10, nc)):
    sl = body[boff]
    name = body[boff+1:boff+1+sl].decode('utf-8', errors='replace').rstrip('\x00')
    first_names.append(name)
    print(f"  name[{i}] at offset {boff}: len={sl} name='{name}'")
    boff += 1 + sl

# Find /Script/FactoryGame
print(f"\n=== Looking for /Script/FactoryGame ===")
boff2 = 4
for i in range(nc):
    sl = body[boff2]
    name = body[boff2+1:boff2+1+sl].decode('utf-8', errors='replace').rstrip('\x00')
    if name == '/Script/FactoryGame':
        print(f"  Found at name index {i}, offset {boff2}")
        break
    boff2 += 1 + sl

# Now parse enums
ec = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"\nEnum count: {ec}")
# Parse first few enums
for ei in range(min(3, ec)):
    ename_idx = struct.unpack_from('<I', body, boff)[0]; boff += 4
    entry_count = body[boff]; boff += 1
    ename = first_names[ename_idx] if ename_idx < len(first_names) else f"?{ename_idx}"
    print(f"  enum[{ei}]: name_idx={ename_idx} name='{ename}' entries={entry_count}")
    for j in range(min(entry_count, 5)):
        ev = struct.unpack_from('<I', body, boff)[0]
        evname = first_names[ev] if ev < len(first_names) else f"?{ev}"
        print(f"    entry[{j}] = {ev} ('{evname}')")
        boff += 4
    if entry_count > 5:
        boff += (entry_count - 5) * 4

# Now parse structs - the critical part
sc = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"\nStruct count: {sc}")

# Parse first 5 structs with detailed byte dumps
for si in range(min(5, sc)):
    start = boff
    ni = struct.unpack_from('<I', body, boff)[0]; boff += 4
    spi = struct.unpack_from('<I', body, boff)[0]; boff += 4
    pc = struct.unpack_from('<H', body, boff)[0]; boff += 2
    sc2 = struct.unpack_from('<H', body, boff)[0]; boff += 2
    print(f"\n  struct[{si}]: offset={start}")
    print(f"    raw bytes: {' '.join(f'{body[start+j]:02x}' for j in range(min(16, len(body)-start)))}")
    print(f"    name_idx={ni} super_idx={spi} prop_count={pc} serializable_count={sc2}")
    nname = names[ni] if ni < len(names) else f"?{ni}"
    sname = names[spi] if spi < len(names) and spi != 0xFFFFFFFF else "None"
    print(f"    name='{nname}' super='{sname}'")
    
    # Parse props
    for pi in range(pc):
        pstart = boff
        schema_idx = struct.unpack_from('<H', body, boff)[0]; boff += 2
        array_dim = body[boff]; boff += 1
        pname_idx = struct.unpack_from('<I', body, boff)[0]; boff += 4
        ptype = body[boff]; boff += 1
        
        # Handle type sub-data
        if ptype == 8:  # Array
            inner = body[boff]; boff += 1
            if inner == 9: boff += 4  # Struct inner
            elif inner == 8:  # Nested array
                inner2 = body[boff]; boff += 1
                if inner2 == 9: boff += 4
        elif ptype == 9:  # Struct
            boff += 4  # struct type name idx
        elif ptype == 24:  # Map
            k = body[boff]; boff += 1
            if k == 9: boff += 4
            elif k == 8:
                inner = body[boff]; boff += 1
                if inner == 9: boff += 4
            elif k == 26:
                inner = body[boff]; boff += 1
                if inner == 9: boff += 4
                boff += 4
            elif k == 28:
                inner = body[boff]; boff += 1
                if inner == 9: boff += 4
            v = body[boff]; boff += 1
            if v == 9: boff += 4
            elif v == 8:
                inner = body[boff]; boff += 1
                if inner == 9: boff += 4
        elif ptype == 25:  # Set
            k = body[boff]; boff += 1
            if k == 9: boff += 4
        elif ptype == 26:  # Enum
            inner = body[boff]; boff += 1
            if inner == 9: boff += 4
            elif inner == 8:
                inner2 = body[boff]; boff += 1
                if inner2 == 9: boff += 4
            boff += 4  # enum type name idx
        elif ptype == 28:  # Optional
            inner = body[boff]; boff += 1
            if inner == 9: boff += 4
        
        pname = names[pname_idx] if pname_idx < len(names) else f"?{pname_idx}"
        print(f"    prop[{pi}]: '{pname}' type={ptype} schema={schema_idx} dim={array_dim} bytes={boff-pstart}")

print(f"\nAfter 5 structs: boff={boff}, remaining={len(body)-boff}")
