import struct, sys

with open(sys.argv[1], 'rb') as f:
    raw = f.read()

off = 1812
body = raw[off:]

# Parse names
boff = 0
nc = struct.unpack_from('<I', body, boff)[0]; boff += 4
names = []
for _ in range(nc):
    sl = body[boff]; boff += 1 + sl
    names.append(body[boff-sl:boff].decode('utf-8', errors='replace').rstrip('\x00'))

# Parse enums
ec = struct.unpack_from('<I', body, boff)[0]; boff += 4
for _ in range(ec):
    boff += 4; ne = body[boff]; boff += 1; boff += ne * 4

# Parse structs - find exactly where it breaks
sc = struct.unpack_from('<I', body, boff)[0]; boff += 4

# Parse struct 9 in detail
# First, compute where struct 9 starts
test_boff = boff
for si in range(10):
    start = test_boff
    ni = struct.unpack_from('<I', body, test_boff)[0]; test_boff += 4
    spi = struct.unpack_from('<I', body, test_boff)[0]; test_boff += 4
    pc = struct.unpack_from('<H', body, test_boff)[0]; test_boff += 2
    sc2 = struct.unpack_from('<H', body, test_boff)[0]; test_boff += 2
    
    if si == 9:
        print(f"=== STRUCT 9 HEADER ===")
        print(f"  Offset: {start}")
        print(f"  Raw bytes (header): {' '.join(f'{body[start+j]:02x}' for j in range(12))}")
        print(f"  name_idx={ni} ({names[ni] if ni < len(names) else '?'})")
        print(f"  super_idx={spi} ({names[spi] if spi < len(names) and spi != 0xFFFFFFFF else 'None'})")
        print(f"  pc={pc} sc2={sc2}")
        
        # Parse each property in detail
        prop_boff = test_boff
        for pi in range(pc):
            ps = prop_boff
            schema_idx = struct.unpack_from('<H', body, prop_boff)[0]; prop_boff += 2
            array_dim = body[prop_boff]; prop_boff += 1
            pname_idx = struct.unpack_from('<I', body, prop_boff)[0]; prop_boff += 4
            ptype = body[prop_boff]; prop_boff += 1
            
            # Parse sub-data
            sub_start = prop_boff
            if ptype == 8:  # Array
                inner = body[prop_boff]; prop_boff += 1
                if inner == 9: prop_boff += 4
            elif ptype == 9:  # Struct
                prop_boff += 4
            elif ptype == 24:  # Map
                k = body[prop_boff]; prop_boff += 1
                if k == 9: prop_boff += 4
                v = body[prop_boff]; prop_boff += 1
                if v == 9: prop_boff += 4
            elif ptype == 25:  # Set
                k = body[prop_boff]; prop_boff += 1
                if k == 9: prop_boff += 4
            elif ptype == 26:  # Enum
                inner = body[prop_boff]; prop_boff += 1
                if inner == 9: prop_boff += 4
                prop_boff += 4
            elif ptype == 28:  # Optional
                inner = body[prop_boff]; prop_boff += 1
                if inner == 9: prop_boff += 4
            
            sub_bytes = body[sub_start:prop_boff]
            pname = names[pname_idx] if pname_idx < len(names) else f"?{pname_idx}"
            print(f"  prop[{pi}]: '{pname}' type={ptype} schema={schema_idx} dim={array_dim}")
            print(f"    raw: {' '.join(f'{body[ps+j]:02x}' for j in range(prop_boff - ps))}")
            print(f"    sub-data: {' '.join(f'{b:02x}' for b in sub_bytes)} ({len(sub_bytes)} bytes)")
        
        end = prop_boff
        print(f"\n  After struct 9: offset={end}, next 24 bytes: {' '.join(f'{body[end+j]:02x}' for j in range(min(24, len(body)-end)))}")
    
    # Parse properties to advance
    for pi in range(pc):
        test_boff += 2 + 1 + 4  # schema + dim + name
        if test_boff >= len(body): break
        pt = body[test_boff]; test_boff += 1
        if pt == 8:
            inner = body[test_boff]; test_boff += 1
            if inner == 9: test_boff += 4
        elif pt == 9: test_boff += 4
        elif pt == 24:
            k = body[test_boff]; test_boff += 1
            if k == 9: test_boff += 4
            v = body[test_boff]; test_boff += 1
            if v == 9: test_boff += 4
        elif pt == 25:
            k = body[test_boff]; test_boff += 1
            if k == 9: test_boff += 4
        elif pt == 26:
            inner = body[test_boff]; test_boff += 1
            if inner == 9: test_boff += 4
            test_boff += 4
        elif pt == 28:
            inner = body[test_boff]; test_boff += 1
            if inner == 9: test_boff += 4

# Now look at the usmap crate source to understand the property format
# Key question: does the usmap property format have an EXTRA field?
# usmap-rs: SchemaIdx(u16), ArrayDim(u8), NameIdx(u32), PropertyType(EPropertyType)
# Then PropertyType-specific data

# Let me check: what if there's an extra field after PropertyType?
# Like an EnumType name idx for ByteProperty/EnumProperty?
# In UE serialization, ByteProperty and EnumProperty have an extra FName for the enum type

# Let me check the bytes right after the property type byte for struct 9
print(f"\n=== Checking for extra fields after property type ===")
test_boff = boff
for si in range(10):
    test_boff += 12  # header
    pc_val = struct.unpack_from('<H', body, test_boff - 4)[0]
    for pi in range(pc_val):
        test_boff += 2 + 1 + 4  # schema + dim + name
        pt = body[test_boff]; test_boff += 1
        if si == 9:
            # Show the byte right after the type byte
            next_byte = body[test_boff] if test_boff < len(body) else -1
            pname_idx = struct.unpack_from('<I', body, test_boff - 5)[0]
            pname = names[pname_idx] if pname_idx < len(names) else f"?{pname_idx}"
            # Also show the 4 bytes after type for context
            context = body[test_boff:test_boff+4]
            print(f"  prop {pi} '{pname}' type={pt} next_byte={next_byte} context={' '.join(f'{b:02x}' for b in context)}")
        
        # Advance past sub-data (same as before)
        if pt == 8:
            inner = body[test_boff]; test_boff += 1
            if inner == 9: test_boff += 4
        elif pt == 9: test_boff += 4
        elif pt == 24:
            k = body[test_boff]; test_boff += 1
            if k == 9: test_boff += 4
            v = body[test_boff]; test_boff += 1
            if v == 9: test_boff += 4
        elif pt == 25:
            k = body[test_boff]; test_boff += 1
            if k == 9: test_boff += 4
        elif pt == 26:
            inner = body[test_boff]; test_boff += 1
            if inner == 9: test_boff += 4
            test_boff += 4
        elif pt == 28:
            inner = body[test_boff]; test_boff += 1
            if inner == 9: test_boff += 4
