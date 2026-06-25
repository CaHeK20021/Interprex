import struct, sys

with open(sys.argv[1], 'rb') as f:
    raw = f.read()

off = 1812
body = raw[off:]

# Parse names - try both u8 and u16 name lengths
boff = 0
nc = struct.unpack_from('<I', body, boff)[0]; boff += 4
print(f"Name count: {nc}")

names_u8 = []
b = boff
for _ in range(nc):
    sl = body[b]; b += 1 + sl
    names_u8.append(body[b-sl:b].decode('utf-8', errors='replace').rstrip('\x00'))

names_u16 = []
b = boff
for _ in range(nc):
    sl = struct.unpack_from('<H', body, b)[0]; b += 2 + sl
    names_u16.append(body[b-sl:b].decode('utf-8', errors='replace').rstrip('\x00'))

print(f"u8 names end at: {b if False else names_u8 and boff + sum(1 + len(n.encode()) for n in names_u8)}")  
# Actually compute properly
b_u8 = boff
for _ in range(nc):
    sl = body[b_u8]; b_u8 += 1 + sl
b_u16 = boff
for _ in range(nc):
    sl = struct.unpack_from('<H', body, b_u16)[0]; b_u16 += 2 + sl
print(f"Names section: u8 ends at {b_u8}, u16 ends at {b_u16}")
print(f"First name (u8): '{names_u8[0]}'")
print(f"First name (u16): '{names_u16[0]}'")
print(f"Names at idx 19378 (u8): '{names_u8[19378] if 19378 < len(names_u8) else '?'}'")
print(f"Names at idx 19378 (u16): '{names_u16[19378] if 19378 < len(names_u16) else '?'}'")

# Parse enums with u8 entry count
ec = struct.unpack_from('<I', body, b_u8)[0]
print(f"\nEnum count (after u8 names): {ec}")
eb_u8 = b_u8 + 4
for _ in range(ec):
    eb_u8 += 4; ne = body[eb_u8]; eb_u8 += 1; eb_u8 += ne * 4
print(f"Enums (u8 entries, u32 values): end at {eb_u8}")

# Parse enums with u16 entry count
ec16 = struct.unpack_from('<I', body, b_u16)[0]
print(f"Enum count (after u16 names): {ec16}")
eb_u16 = b_u16 + 4
for _ in range(ec16):
    eb_u16 += 4; ne = struct.unpack_from('<H', body, eb_u16)[0]; eb_u16 += 2; eb_u16 += ne * 4
print(f"Enums (u16 entries, u32 values): end at {eb_u16}")

# Parse enums with u8 entry count + i64 values (ExplicitEnumValues)
eb_u8_i64 = b_u8 + 4
for _ in range(ec):
    eb_u8_i64 += 4; ne = body[eb_u8_i64]; eb_u8_i64 += 1; eb_u8_i64 += ne * (8 + 4)  # i64 + u32 name
print(f"Enums (u8 entries, i64+u32): end at {eb_u8_i64}")

# Parse enums with u16 entry count + i64 values
eb_u16_i64 = b_u16 + 4
for _ in range(ec16):
    eb_u16_i64 += 4; ne = struct.unpack_from('<H', body, eb_u16_i64)[0]; eb_u16_i64 += 2; eb_u16_i64 += ne * (8 + 4)
print(f"Enums (u16 entries, i64+u32): end at {eb_u16_i64}")

# Which combination gives a valid struct count?
for name_end, enum_end, enum_label in [
    (b_u8, eb_u8, "u8_names + u8_enum + u32_values"),
    (b_u8, eb_u8_i64, "u8_names + u8_enum + i64+u32_values"),
    (b_u16, eb_u16, "u16_names + u16_enum + u32_values"),
    (b_u16, eb_u16_i64, "u16_names + u16_enum + i64+u32_values"),
]:
    scandidate = struct.unpack_from('<I', body, enum_end)[0]
    print(f"\n{enum_label}: struct count = {scandidate}")
    
    # Check if struct parsing works from this offset
    soff = enum_end + 4
    ok_count = 0
    for si in range(min(20, scandidate)):
        if soff + 12 > len(body): break
        ni = struct.unpack_from('<I', body, soff)[0]
        spi = struct.unpack_from('<I', body, soff+4)[0]
        pc = struct.unpack_from('<H', body, soff+8)[0]
        sc2 = struct.unpack_from('<H', body, soff+10)[0]
        
        # Quick validity check
        if ni >= nc or sc2 > 10000 or pc < sc2:
            print(f"  Invalid struct header at offset {soff}: ni={ni} pc={pc} sc2={sc2}")
            break
        
        nname = names_u8[ni] if ni < len(names_u8) else f"?{ni}"
        soff += 12
        
        # Skip sc2 properties (simplified)
        for pi in range(sc2):
            if soff + 7 > len(body): break
            soff += 2 + 1 + 4  # schema + dim + name
            if soff >= len(body): break
            pt = body[soff]; soff += 1
            if pt == 8: 
                inner = body[soff]; soff += 1
                if inner == 9: soff += 4
            elif pt == 9: soff += 4
            elif pt == 24:
                k = body[soff]; soff += 1
                if k == 9: soff += 4
                v = body[soff]; soff += 1
                if v == 9: soff += 4
            elif pt == 25:
                k = body[soff]; soff += 1
                if k == 9: soff += 4
            elif pt == 26:
                inner = body[soff]; soff += 1
                if inner == 9: soff += 4
                soff += 4
            elif pt == 28:
                inner = body[soff]; soff += 1
                if inner == 9: soff += 4
        
        ok_count = si + 1
        if si < 5:
            print(f"  [{si}] '{nname}' pc={pc} sc2={sc2}")
    
    print(f"  Successfully parsed {ok_count} structs")
