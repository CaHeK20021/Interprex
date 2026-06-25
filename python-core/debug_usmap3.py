import struct, sys

with open(sys.argv[1], 'rb') as f:
    raw = f.read()

off = 1812
body = raw[off:]

# Parse ALL names
boff = 0
nc = struct.unpack_from('<I', body, boff)[0]; boff += 4
names = []
for _ in range(nc):
    sl = body[boff]; boff += 1 + sl
    names.append(body[boff-sl:boff].decode('utf-8', errors='replace').rstrip('\x00'))

# Parse ALL enums
ec = struct.unpack_from('<I', body, boff)[0]; boff += 4
for _ in range(ec):
    boff += 4; ne = body[boff]; boff += 1; boff += ne * 4

# Parse structs
sc = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"names={nc}, enums={ec}, structs={sc}, body={len(body)}")

# Parse first 30 structs with raw hex dumps
for si in range(min(30, sc)):
    start = boff
    ni = struct.unpack_from('<I', body, boff)[0]; boff += 4
    spi = struct.unpack_from('<I', body, boff)[0]; boff += 4
    pc = struct.unpack_from('<H', body, boff)[0]; boff += 2
    sc2 = struct.unpack_from('<H', body, boff)[0]; boff += 2
    header_end = boff
    
    nname = names[ni] if ni < len(names) else f"?{ni}"
    sname = names[spi] if spi < len(names) and spi != 0xFFFFFFFF else "None"
    
    # Parse properties
    prop_start = boff
    props_parsed = 0
    for pi in range(pc):
        if boff + 7 > len(body): break
        ps = boff
        schema_idx = struct.unpack_from('<H', body, boff)[0]; boff += 2
        array_dim = body[boff]; boff += 1
        pname_idx = struct.unpack_from('<I', body, boff)[0]; boff += 4
        if boff >= len(body): break
        ptype = body[boff]; boff += 1
        
        # Handle type sub-data
        if ptype == 8:  # Array
            inner = body[boff]; boff += 1
            if inner == 9: boff += 4
        elif ptype == 9:  # Struct
            boff += 4
        elif ptype == 24:  # Map
            k = body[boff]; boff += 1
            if k == 9: boff += 4
            v = body[boff]; boff += 1
            if v == 9: boff += 4
        elif ptype == 25:  # Set
            k = body[boff]; boff += 1
            if k == 9: boff += 4
        elif ptype == 26:  # Enum
            inner = body[boff]; boff += 1
            if inner == 9: boff += 4
            boff += 4
        elif ptype == 28:  # Optional
            inner = body[boff]; boff += 1
            if inner == 9: boff += 4
        
        props_parsed = pi + 1
        if boff > len(body): break
    
    consumed = boff - start
    print(f"  struct[{si}]: name='{nname}' super='{sname}' pc={pc} sc2={sc2} props_parsed={props_parsed} consumed={consumed} hex_start={' '.join(f'{body[start+j]:02x}' for j in range(min(12, len(body)-start)))}")

print(f"\nFinal: boff={boff}, remaining={len(body)-boff}")
