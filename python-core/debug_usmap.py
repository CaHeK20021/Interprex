import struct, sys

with open(sys.argv[1], 'rb') as f:
    raw = f.read()

off = 2 + 1 + 4 + 4 + 4
cv = struct.unpack_from('<I', raw, off)[0]; off += 4
off += cv * 20 + 4 + 1 + 4 + 4
body = raw[off:]
boff = 0

nc = struct.unpack_from('<I', body, boff)[0]; boff += 4
names = []
for _ in range(nc):
    sl = body[boff]; boff += 1 + sl
    names.append(body[boff-sl:boff].decode('utf-8', errors='replace').rstrip('\x00'))

ec = struct.unpack_from('<I', body, boff)[0]; boff += 4
for _ in range(ec):
    boff += 4; ne = body[boff]; boff += 1; boff += ne * 4

sc = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"names={nc}, enums={ec}, structs={sc}, body={len(body)}, body_start={off}")

def parse_type_with_log(data, off, depth=0):
    if off >= len(data): return off, "?"
    t = data[off]; off += 1
    name = {0:"Byte",1:"Bool",2:"Int",3:"Float",4:"Object",5:"Name",6:"Delegate",
            7:"WeakObject",8:"Array",9:"Struct",10:"Str",11:"Text",12:"Interface",
            13:"SoftObject",14:"SoftClass",15:"Int64",16:"UInt64",22:"Set",
            24:"Map",25:"Set",26:"Enum",28:"Optional",29:"WeakObject",
            30:"LazyObject",31:"SoftObject",32:"UInt32"}.get(t, f"type{t}")
    extra = ""
    if t == 8:
        off, inner = parse_type_with_log(data, off, depth+1)
        extra = f"Array<{inner}>"
    elif t == 9:
        nidx = struct.unpack_from('<I', data, off)[0]; off += 4
        extra = f"Struct<{names[nidx] if nidx < len(names) else '?'}>"
    elif t == 24:
        off, k = parse_type_with_log(data, off, depth+1)
        off, v = parse_type_with_log(data, off, depth+1)
        extra = f"Map<{k},{v}>"
    elif t == 25:
        off, k = parse_type_with_log(data, off, depth+1)
        extra = f"Set<{k}>"
    elif t == 26:
        off, inner = parse_type_with_log(data, off, depth+1)
        nidx = struct.unpack_from('<I', data, off)[0]; off += 4
        extra = f"Enum<{inner}>"
    elif t == 28:
        off, inner = parse_type_with_log(data, off, depth+1)
        extra = f"Optional<{inner}>"
    return off, f"{name}({extra})"

structs_parsed = 0
last_good_boff = boff

for si in range(sc):
    if boff + 12 > len(body):
        print(f"  STOP at struct {si}: need 12 bytes, only {len(body)-boff} remain")
        break
    ni = struct.unpack_from('<I', body, boff)[0]; boff += 4
    si2 = struct.unpack_from('<I', body, boff)[0]; boff += 4
    pc = struct.unpack_from('<H', body, boff)[0]; boff += 2
    sc2 = struct.unpack_from('<H', body, boff)[0]; boff += 2
    
    struct_boff = boff
    props_ok = True
    prop_details = []
    
    for pi in range(sc2):  # Using serializable_count as current code does
        if boff + 7 > len(body):
            print(f"  STOP at struct {si} prop {pi}: need 7 bytes, only {len(body)-boff} remain")
            props_ok = False
            break
        prop_start = boff
        schema_idx = struct.unpack_from('<H', body, boff)[0]; boff += 2
        array_dim = body[boff]; boff += 1
        name_idx = struct.unpack_from('<I', body, boff)[0]; boff += 4
        if boff >= len(body):
            print(f"  STOP at struct {si} prop {pi}: type read past end")
            props_ok = False
            break
        prop_type = body[boff]
        
        old_boff = boff
        boff, type_desc = parse_type_with_log(body, boff)
        prop_bytes = boff - prop_start
        
        pname = names[name_idx] if name_idx < len(names) else f"?{name_idx}"
        prop_details.append((pname, prop_type, type_desc, prop_bytes, prop_start))
    
    if props_ok:
        consumed = boff - struct_boff
        # Check if pc != sc2 (the known bug)
        if pc != sc2:
            print(f"  struct {si} '{names[ni] if ni < len(names) else '?'}': pc={pc} sc2={sc2} DIFF! consumed={consumed} bytes")
            # Show first few props with mismatch
            for pn, pt, td, pb, ps in prop_details[:5]:
                print(f"    prop: {pn} type={pt}({td}) bytes={pb}")
    
    structs_parsed = si + 1
    last_good_boff = boff

# After parsing, check for drift
print(f"\nParsed {structs_parsed} structs")
print(f"Final boff: {boff}, body: {len(body)}, remaining: {len(body)-boff}")

# Now re-parse but use prop_count instead of serializable_count
print(f"\n=== Re-parsing with prop_count (pc) instead of serializable_count (sc2) ===")
boff2 = 0
nc2 = struct.unpack_from('<I', body, boff2)[0]; boff2 += 4
for _ in range(nc2):
    sl = body[boff2]; boff2 += 1 + sl
ec2 = struct.unpack_from('<I', body, boff2)[0]; boff2 += 4
for _ in range(ec2):
    boff2 += 4; ne = body[boff2]; boff2 += 1; boff2 += ne * 4
sc2_val = struct.unpack_from('<I', body, boff2)[0]; boff2 += 4

structs2 = 0
for si in range(sc2_val):
    if boff2 + 12 > len(body): break
    ni = struct.unpack_from('<I', body, boff2)[0]; boff2 += 4
    si2 = struct.unpack_from('<I', body, boff2)[0]; boff2 += 4
    pc = struct.unpack_from('<H', body, boff2)[0]; boff2 += 2
    sc2_val2 = struct.unpack_from('<H', body, boff2)[0]; boff2 += 2
    
    props_ok = True
    for pi in range(pc):  # Using prop_count
        if boff2 + 7 > len(body): props_ok = False; break
        boff2 += 2 + 1 + 4  # schema_idx + array_dim + name_idx
        if boff2 >= len(body): props_ok = False; break
        boff2, _ = parse_type_with_log(body, boff2)
        if boff2 > len(body): props_ok = False; break
    
    if not props_ok: break
    structs2 = si + 1

print(f"With prop_count: {structs2} structs, final boff2: {boff2}, remaining: {len(body)-boff2}")

# Also count ByteProperty occurrences in first 1050 structs
boff3 = 0
nc3 = struct.unpack_from('<I', body, boff3)[0]; boff3 += 4
for _ in range(nc3):
    sl = body[boff3]; boff3 += 1 + sl
ec3 = struct.unpack_from('<I', body, boff3)[0]; boff3 += 4
for _ in range(ec3):
    boff3 += 4; ne = body[boff3]; boff3 += 1; boff3 += ne * 4
sc3 = struct.unpack_from('<I', body, boff3)[0]; boff3 += 4

byte_prop_count = 0
prop_type_counts = {}
for si in range(min(1050, sc3)):
    if boff3 + 12 > len(body): break
    boff3 += 4 + 4 + 2 + 2  # name + super + pc + sc2
    pc3 = struct.unpack_from('<H', body, boff3 - 4)[0]
    for pi in range(pc3):
        if boff3 + 7 > len(body): break
        boff3 += 2 + 1 + 4
        if boff3 >= len(body): break
        pt = body[boff3]
        prop_type_counts[pt] = prop_type_counts.get(pt, 0) + 1
        if pt == 0: byte_prop_count += 1
        boff3, _ = parse_type_with_log(body, boff3)
        if boff3 > len(body): break

print(f"\nProperty type distribution (first 1050 structs, using prop_count):")
for t, c in sorted(prop_type_counts.items()):
    print(f"  type {t}: {c} occurrences")
print(f"ByteProperty (type 0): {byte_prop_count}")
