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

sc = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"names={nc}, enums={ec}, structs={sc}, body={len(body)}")

def skip_type(data, off):
    if off >= len(data): return off
    t = data[off]; off += 1
    if t == 8: off = skip_type(data, off)
    elif t == 9: off += 4
    elif t == 24: off = skip_type(data, off); off = skip_type(data, off)
    elif t == 25: off = skip_type(data, off)
    elif t == 26: off = skip_type(data, off); off += 4
    elif t == 28: off = skip_type(data, off)
    return off

struct_start_boff = boff
struct_positions = []

for si in range(sc):
    start = boff
    if boff + 12 > len(body):
        print(f"STOP at struct {si}: need 12 bytes, only {len(body)-boff}")
        break
    
    ni = struct.unpack_from('<I', body, boff)[0]; boff += 4
    spi = struct.unpack_from('<I', body, boff)[0]; boff += 4
    pc = struct.unpack_from('<H', body, boff)[0]; boff += 2
    sc2 = struct.unpack_from('<H', body, boff)[0]; boff += 2
    
    nname = names[ni] if ni < len(names) else f"?{ni}"
    
    # Parse sc2 properties
    props_ok = True
    for pi in range(sc2):
        if boff + 7 > len(body):
            print(f"STOP at struct {si} prop {pi}: need 7 bytes, only {len(body)-boff}")
            props_ok = False
            break
        boff += 2 + 1 + 4  # schema + dim + name
        if boff >= len(body):
            props_ok = False
            break
        boff = skip_type(body, boff)
        if boff > len(body):
            print(f"SKIP overrun at struct {si} prop {pi}: boff={boff} > body={len(body)}")
            props_ok = False
            break
    
    if not props_ok:
        break
    
    struct_positions.append((si, start, nname, pc, sc2, boff - start))
    if si < 5 or (si >= 1040 and si <= 1060):
        print(f"  [{si}] '{nname}' @ {start} pc={pc} sc2={sc2} end={boff} consumed={boff-start}")

# Now find the exact point of drift
# We know boff should end at exactly len(body) after all structs
# With 2 bytes remaining, the drift started somewhere

# Let's check: after parsing all successful structs, what's at boff?
remaining = len(body) - boff
print(f"\nParsed {len(struct_positions)} structs, boff={boff}, remaining={remaining}")

# Binary search: find the FIRST struct where skipping it causes boff to be wrong
# Strategy: parse first N structs, then check if the next struct header looks valid

def is_valid_struct_header(data, off):
    """Check if bytes at off look like a valid struct header"""
    if off + 12 > len(data): return False
    ni = struct.unpack_from('<I', data, off)[0]
    spi = struct.unpack_from('<I', data, off+4)[0]
    pc = struct.unpack_from('<H', data, off+8)[0]
    sc2 = struct.unpack_from('<H', data, off+10)[0]
    # Valid: name_idx within range, sc2 reasonable, pc >= sc2
    if ni >= len(names): return False
    if sc2 > 10000: return False  # unreasonable
    if pc < sc2: return False  # prop_count must be >= serializable_count
    return True

# Check each struct boundary
print(f"\n=== Checking struct boundaries ===")
drift_start = -1
for i in range(1, len(struct_positions)):
    si, start, nname, pc, sc2, consumed = struct_positions[i]
    prev_si, prev_start, prev_name, prev_pc, prev_sc2, prev_consumed = struct_positions[i-1]
    expected_start = prev_start + prev_consumed
    if start != expected_start:
        print(f"  DRIFT at struct {si}: expected start={expected_start}, actual={start}, diff={start-expected_start}")
        drift_start = si
        break

if drift_start == -1:
    print("  No drift detected in parsed structs")
    # Check if the struct right after the last parsed one is valid
    if struct_positions:
        last = struct_positions[-1]
        next_start = last[1] + last[5]
        print(f"  Next struct should be at {next_start}, is_valid={is_valid_struct_header(body, next_start)}")
        if next_start < len(body):
            ni = struct.unpack_from('<I', body, next_start)[0]
            spi = struct.unpack_from('<I', body, next_start+4)[0]
            pc = struct.unpack_from('<H', body, next_start+8)[0]
            sc2 = struct.unpack_from('<H', body, next_start+10)[0]
            print(f"  Raw: name_idx={ni} super={spi} pc={pc} sc2={sc2}")
            print(f"  Hex: {' '.join(f'{body[next_start+j]:02x}' for j in range(12))}")

# Also check: does the body end with extensions?
print(f"\n=== Checking for extensions at end ===")
# After all structs, there should be extension tags (CEXT, PPTH, etc.)
# or just EOF
if boff < len(body):
    tail = body[boff:boff+20]
    print(f"  Bytes at boff: {' '.join(f'{b:02x}' for b in tail)}")
    # Check for extension magic
    if boff + 4 <= len(body):
        tag = body[boff:boff+4]
        if tag in [b'CEXT', b'PPTH', b'EATR', b'ENVP']:
            print(f"  Found extension tag: {tag}")
