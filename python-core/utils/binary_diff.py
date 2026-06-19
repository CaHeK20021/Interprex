import struct
import hashlib
import zlib
import bisect
import os

# Limit 4GB (2**32 - 1)
MAX_FILE_SIZE = 4294967295

def get_sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

def make_patch(orig_bytes: bytes, mod_bytes: bytes) -> bytes:
    # 1. Size checks
    if len(orig_bytes) >= MAX_FILE_SIZE or len(mod_bytes) >= MAX_FILE_SIZE:
        raise ValueError("File size exceeds the 4 GB limit of IDXP format")
        
    orig_sha = get_sha256(orig_bytes)
    mod_sha = get_sha256(mod_bytes)
    
    # 2. Short-circuit: identical files
    if orig_sha == mod_sha:
        header = struct.pack("<4sH32s32s", b"IDXP", 1, orig_sha, mod_sha)
        return zlib.compress(header)
        
    # 3. Short-circuit: small files (< 16 bytes)
    K = 16
    if len(orig_bytes) < K or len(mod_bytes) < K:
        header = struct.pack("<4sH32s32s", b"IDXP", 1, orig_sha, mod_sha)
        # Single replacement instruction
        inst = struct.pack("<III", 0, len(orig_bytes), len(mod_bytes)) + orig_bytes + mod_bytes
        return zlib.compress(header + inst)
        
    # 4. Build index for B (mod_bytes) with step K=16 using fast struct unpack, slices and zip
    from collections import defaultdict
    b_blocks = defaultdict(list)
    num_blocks = len(mod_bytes) // 16
    unpacked = struct.unpack(f"<{num_blocks * 2}Q", mod_bytes[:num_blocks * 16])
    keys = list(zip(unpacked[0::2], unpacked[1::2]))
    for idx, key in enumerate(keys):
        b_blocks[key].append(idx * 16)
        
    # 5. Scanning A (orig_bytes) and B (mod_bytes)
    i = 0
    j = 0
    last_sync_i = 0
    last_sync_j = 0
    W = 1048576 # 1 MB window
    instructions = []
    
    len_A = len(orig_bytes)
    len_B = len(mod_bytes)
    
    mv_A = memoryview(orig_bytes)
    mv_B = memoryview(mod_bytes)
    
    while i < len_A and j < len_B:
        # Fast-forward matching blocks using memoryview slices (no memory copies, extremely fast in C)
        block_size = 65536
        while i + block_size <= len_A and j + block_size <= len_B:
            if mv_A[i : i + block_size] == mv_B[j : j + block_size]:
                i += block_size
                j += block_size
            else:
                break

        # Find the exact mismatch point using binary search (dichotomy)
        low = 0
        high = min(len_A - i, len_B - j)
        while low < high:
            mid = (low + high) // 2
            if mv_A[i : i + mid] == mv_B[j : j + mid]:
                low = mid + 1
            else:
                high = mid
        if low > 0:
            i += low - 1
            j += low - 1

        if i == len_A or j == len_B:
            break
            
        # Mismatch detected at i, j!
        # Search for synchronization anchor A[i+d : i+d+K]
        found = False
        match_start_i = 0
        match_start_j = 0
        match_end_i = 0
        match_end_j = 0
        
        for d in range(0, W + 1):
            if i + d + K > len_A:
                break
                
            block_bytes = mv_A[i+d : i+d+K]
            val1, val2 = struct.unpack("<QQ", block_bytes)
            key = (val1, val2)
            if key in b_blocks:
                # First hit! Now find the best candidate in B using bisect (must be >= j)
                offsets = b_blocks[key]
                idx_bisect = bisect.bisect_left(offsets, j)
                
                best_j_prime = None
                if idx_bisect < len(offsets):
                    j_prime = offsets[idx_bisect]
                    if j_prime - j <= W: # Close shift threshold
                        best_j_prime = j_prime
                            
                if best_j_prime is not None:
                    # Synchronizing pair found: A[i+d : i+d+K] matches B[best_j_prime : best_j_prime+K]
                    found = True
                    match_pos_i = i + d
                    match_pos_j = best_j_prime
                    
                    # Match extension backwards (clamped to i and j)
                    match_start_i = match_pos_i
                    match_start_j = match_pos_j
                    
                    while match_start_i > i and match_start_j > j:
                        if orig_bytes[match_start_i - 1] == mod_bytes[match_start_j - 1]:
                            match_start_i -= 1
                            match_start_j -= 1
                        else:
                            break
                            
                    # Match extension forwards using fast blocks and binary search (dichotomy)
                    match_end_i = match_pos_i + K
                    match_end_j = match_pos_j + K
                    
                    block_size = 65536
                    while match_end_i + block_size <= len_A and match_end_j + block_size <= len_B:
                        if mv_A[match_end_i : match_end_i + block_size] == mv_B[match_end_j : match_end_j + block_size]:
                            match_end_i += block_size
                            match_end_j += block_size
                        else:
                            break
                            
                    low = 0
                    high = min(len_A - match_end_i, len_B - match_end_j)
                    while low < high:
                        mid = (low + high) // 2
                        if mv_A[match_end_i : match_end_i + mid] == mv_B[match_end_j : match_end_j + mid]:
                            low = mid + 1
                        else:
                            high = mid
                            
                    if low > 0:
                        match_end_i += low - 1
                        match_end_j += low - 1
                            
                    break # Stop looking for d since we have First Hit
                    
        if found:
            # Emit instruction
            orig_skip_len = i - last_sync_i
            orig_len = match_start_i - i
            orig_bytes_chunk = orig_bytes[i:match_start_i]
            mod_len = match_start_j - j
            mod_bytes_chunk = mod_bytes[j:match_start_j]
            
            instructions.append((orig_skip_len, orig_len, orig_bytes_chunk, mod_len, mod_bytes_chunk))
            
            # Advance pointers
            i = match_end_i
            j = match_end_j
            # Set last sync points to match_start_i / match_start_j so the matched segment
            # is copied as part of the skip bytes of the next instruction
            last_sync_i = match_start_i
            last_sync_j = match_start_j
        else:
            # Resynchronization failed, emit remaining tail as a replacement
            orig_skip_len = i - last_sync_i
            orig_len = len_A - i
            orig_bytes_chunk = orig_bytes[i:]
            mod_len = len_B - j
            mod_bytes_chunk = mod_bytes[j:]
            instructions.append((orig_skip_len, orig_len, orig_bytes_chunk, mod_len, mod_bytes_chunk))
            
            # End scan loop
            last_sync_i = len_A
            last_sync_j = len_B
            break
            
    # Handle end of files
    if i == len_A and j < len_B:
        instructions.append((i - last_sync_i, 0, b"", len_B - j, mod_bytes[j:]))
    elif j == len_B and i < len_A:
        instructions.append((i - last_sync_i, len_A - i, orig_bytes[i:], 0, b""))
            
    # Serialize patch
    header = struct.pack("<4sH32s32s", b"IDXP", 1, orig_sha, mod_sha)
    payload = bytearray(header)
    for orig_skip_len, orig_len, orig_bytes_chunk, mod_len, mod_bytes_chunk in instructions:
        payload.extend(struct.pack("<III", orig_skip_len, orig_len, mod_len))
        payload.extend(orig_bytes_chunk)
        payload.extend(mod_bytes_chunk)
        
    return zlib.compress(bytes(payload))

def apply_patch(orig_bytes: bytes, patch_bytes: bytes, strict: bool = True) -> bytes:
    if len(orig_bytes) >= MAX_FILE_SIZE:
        raise ValueError("File size exceeds the 4 GB limit of IDXP format")
        
    try:
        data = zlib.decompress(patch_bytes)
    except Exception as e:
        raise ValueError(f"Invalid patch: decompression failed. {e}")
        
    if len(data) < 70:
        raise ValueError("Invalid patch: header too short")
        
    magic, version, orig_sha, mod_sha = struct.unpack("<4sH32s32s", data[:70])
    if magic != b"IDXP":
        raise ValueError("Invalid patch magic header")
    if version != 1:
        raise ValueError(f"Unsupported patch version: {version}")
        
    if get_sha256(orig_bytes) != orig_sha:
        raise ValueError(
            "Похоже, файл игры был изменен внешней программой или обновлением. "
            "Чтобы восстановить оригинальное состояние и обновить патч, "
            "пожалуйста, выполните повторную инъекцию перевода в приложении."
        )
        
    pos = 70
    n = len(data)
    
    import io
    in_stream = io.BytesIO(orig_bytes)
    out_stream = io.BytesIO()
    
    while pos < n:
        if pos + 12 > n:
            raise ValueError("Unexpected end of patch file data")
        orig_skip_len, orig_len, mod_len = struct.unpack("<III", data[pos:pos+12])
        pos += 12
        
        if pos + orig_len + mod_len > n:
            raise ValueError("Unexpected end of patch file data")
            
        orig_bytes_chunk = data[pos : pos + orig_len]
        mod_bytes_chunk = data[pos + orig_len : pos + orig_len + mod_len]
        
        # Read and verify skip bytes
        skip_bytes = in_stream.read(orig_skip_len)
        if len(skip_bytes) < orig_skip_len:
            raise ValueError("Unexpected end of file while reading original file. File may be truncated or corrupted.")
        out_stream.write(skip_bytes)
        
        # Read and verify orig bytes
        target_orig = in_stream.read(orig_len)
        if len(target_orig) < orig_len:
            raise ValueError("Unexpected end of file while reading original file. File may be truncated or corrupted.")
            
        if strict and target_orig != orig_bytes_chunk:
            raise ValueError("Original file content mismatch during patching. The file might be corrupted.")
            
        out_stream.write(mod_bytes_chunk)
        pos += orig_len + mod_len
        
    # Copy remaining bytes from A
    remaining = in_stream.read()
    out_stream.write(remaining)
    
    result = out_stream.getvalue()
    if get_sha256(result) != mod_sha:
        raise ValueError("Patched file verification failed (SHA256 mismatch)")
        
    return result

def reverse_patch(mod_bytes: bytes, patch_bytes: bytes, strict: bool = True) -> bytes:
    if len(mod_bytes) >= MAX_FILE_SIZE:
        raise ValueError("File size exceeds the 4 GB limit of IDXP format")
        
    try:
        data = zlib.decompress(patch_bytes)
    except Exception as e:
        raise ValueError(f"Invalid patch: decompression failed. {e}")
        
    if len(data) < 70:
        raise ValueError("Invalid patch: header too short")
        
    magic, version, orig_sha, mod_sha = struct.unpack("<4sH32s32s", data[:70])
    if magic != b"IDXP":
        raise ValueError("Invalid patch magic header")
    if version != 1:
        raise ValueError(f"Unsupported patch version: {version}")
        
    if get_sha256(mod_bytes) != mod_sha:
        raise ValueError(
            "Файл перевода был изменен внешней программой. "
            "Восстановление исходного состояния отменено для предотвращения повреждения данных. "
            "Для восстановления запустите повторный импорт перевода."
        )
        
    pos = 70
    n = len(data)
    
    import io
    in_stream = io.BytesIO(mod_bytes)
    out_stream = io.BytesIO()
    
    while pos < n:
        if pos + 12 > n:
            raise ValueError("Unexpected end of patch file data")
        orig_skip_len, orig_len, mod_len = struct.unpack("<III", data[pos:pos+12])
        pos += 12
        
        if pos + orig_len + mod_len > n:
            raise ValueError("Unexpected end of patch file data")
            
        orig_bytes_chunk = data[pos : pos + orig_len]
        mod_bytes_chunk = data[pos + orig_len : pos + orig_len + mod_len]
        
        # Read and verify skip bytes
        skip_bytes = in_stream.read(orig_skip_len)
        if len(skip_bytes) < orig_skip_len:
            raise ValueError("Unexpected end of file while reading modified file. File may be truncated or corrupted.")
        out_stream.write(skip_bytes)
        
        # Read and verify mod bytes
        target_mod = in_stream.read(mod_len)
        if len(target_mod) < mod_len:
            raise ValueError("Unexpected end of file while reading modified file. File may be truncated or corrupted.")
            
        if strict and target_mod != mod_bytes_chunk:
            raise ValueError("Modified file content mismatch during patching.")
            
        out_stream.write(orig_bytes_chunk)
        pos += orig_len + mod_len
        
    # Copy remaining bytes from B
    remaining = in_stream.read()
    out_stream.write(remaining)
    
    result = out_stream.getvalue()
    if get_sha256(result) != orig_sha:
        raise ValueError("Reverted file verification failed (SHA256 mismatch)")
        
    return result
