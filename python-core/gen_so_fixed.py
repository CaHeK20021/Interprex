import struct, json, sys, hashlib

def cityhash64_path(path):
    lower = path.lower().replace(':', '/').replace('.', '/')
    utf16 = lower.encode('utf-16-le')
    h = hashlib.blake2b(utf16, digest_size=8).digest()
    return int.from_bytes(h, 'little') & ~(3 << 62)

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

def skip_type(data, off):
    if off >= len(data): return off
    t = data[off]; off += 1
    if t == 8: off = skip_type(data, off)   # Array → inner
    elif t == 9: off += 4                     # Struct → u32 name
    elif t == 24: off = skip_type(data, off); off = skip_type(data, off)  # Map → key + value
    elif t == 25: off = skip_type(data, off)  # Set → key
    elif t == 26: off = skip_type(data, off); off += 4  # Enum → inner + u32 name
    elif t == 28: off = skip_type(data, off)  # Optional → inner
    # All others (0,1,2,3,4,5,6,7,10-23,27,29,30): 0 bytes sub-data
    return off

struct_list = []
for si in range(sc):
    if boff + 12 > len(body): break
    ni = struct.unpack_from('<I', body, boff)[0]; boff += 4
    si2 = struct.unpack_from('<I', body, boff)[0]; boff += 4
    pc = struct.unpack_from('<H', body, boff)[0]; boff += 2
    sc2 = struct.unpack_from('<H', body, boff)[0]; boff += 2
    for pi in range(sc2):
        if boff + 8 > len(body): break
        boff += 2 + 1 + 4
        if boff >= len(body): break
        boff = skip_type(body, boff)
        if boff > len(body): break
    name = names[ni] if ni < len(names) else ''
    super_name = names[si2] if si2 != 0xFFFFFFFF and si2 < len(names) else None
    if name and not name.startswith('/'):
        struct_list.append({'name': name, 'super': super_name})

# Build ScriptObjects
name_map = {}
script_objects = []
for s in struct_list:
    full_path = '/Script/FactoryGame.' + s['name']
    h = cityhash64_path(full_path)
    outer_h = cityhash64_path('/script/factorygame')
    idx = len(name_map)
    name_map[s['name']] = idx
    script_objects.append({
        'object_name': s['name'],
        'object_name_idx': idx,
        'global_index': hex(h | (1 << 62)),
        'outer_index': hex(outer_h | (1 << 62)),
        'cdo_class_index': '0x0',
    })

output = {'name_map': name_map, 'script_objects': script_objects}
with open(sys.argv[2], 'w') as f:
    json.dump(output, f)
print(f'Parsed {len(struct_list)} structs, generated {len(script_objects)} script objects')
print(f'Final boff: {boff}, body: {len(body)}, remaining: {len(body)-boff}')
