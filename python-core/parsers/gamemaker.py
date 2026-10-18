"""GameMaker engine parser (GM:Studio 1.2 / 1.4, GMS 2.x, GameMaker 2022-2024+).

Extracts and injects translatable strings from binary GameMaker resource archives
(data.win, game.unx, game.ios, game.droid, *.win) and external localization files
(lang/*.json, localizations/*.ini, *.csv, *.txt).

Key Architectural Guarantees:
1. Relocated Append in STRG: Original strings and technical identifiers remain at
   their exact byte offsets. Translations are appended at the end of STRG, aligned to
   128 bytes, with TXTR and AUDO pointer shifts. Chunks before STRG never move.
2. Smart Bytecode Walker: Only strings referenced by push.s in CODE (or LANG/ROOM)
   are extracted, excluding state-machine keys (cmp) and technical function arguments.
3. Raster Font Cyrillic Auto-generator: Extends ASCII font texture atlases with
   Cyrillic glyphs rendered via Pillow from bundled NotoSans/Zpix fonts.
4. Tag Sanitization: Preserves Risk of Rain (&y&), Undertale/Deltarune (\\cY, %%),
   Scribble ([c_red]), and syncs GMS 1.x '#' newlines.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
from pathlib import Path
from typing import Any

from .base import BaseParser, TranslationString, make_id, read_backup_original

logger = logging.getLogger("gamemaker_parser")

# Technical function names whose string arguments should never be extracted as game text.
TECH_FUNCS = frozenset({
    "asset_get_index", "variable_instance_get", "variable_instance_set",
    "variable_global_get", "variable_global_set", "ds_map_find_value",
    "ds_map_exists", "ds_map_delete", "ini_open", "ini_key_exists",
    "ini_section_exists", "file_text_open_read", "file_text_open_write",
    "file_exists", "directory_exists", "layer_get_id", "layer_get_name",
    "instance_create_layer", "shader_get_uniform", "audio_group_load",
    "struct_get", "variable_struct_get", "variable_struct_exists",
    "event_perform", "script_execute", "room_goto", "audio_play_sound",
    "font_add_sprite", "sprite_get_texture", "texture_get_width",
})

# Explicit text-drawing functions whose string arguments are user-facing.
DRAW_FUNCS = frozenset({
    "draw_text", "draw_text_ext", "draw_text_color", "draw_text_transformed",
    "draw_text_ext_color", "draw_text_transformed_color", "draw_text_ext_transformed",
    "draw_text_ext_transformed_color", "show_message", "show_message_async",
    "show_question", "show_question_async", "scr_text", "scr_dialog", "draw_set_font",
})

# Chunks containing technical string pointers that must not be extracted.
METADATA_CHUNKS = (
    b"OBJT", b"SPRT", b"VARI", b"FUNC", b"BGND", b"PATH", b"SCPT", b"SHDR",
    b"SOND", b"TMLN", b"ACRV", b"AGRP", b"DAFL", b"EXTN", b"FEAT", b"FONT",
    b"PSEM", b"PSYS", b"SEQN", b"TAGS", b"TGIN",
)


def _u16(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _i16(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def _u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _i32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _parse_form_chunks(data: bytes | bytearray) -> list[tuple[str, int, int]]:
    """Parse IFF FORM chunk headers: returns [(tag, data_offset, size), ...].
    `data_offset` is the offset from byte 0 where chunk payload begins (pos + 8)."""
    if len(data) < 8 or data[:4] != b"FORM":
        return []
    form_size = _u32(data, 4)
    limit = min(len(data), form_size + 8)
    pos = 8
    chunks = []
    while pos + 8 <= limit:
        tag = data[pos : pos + 4].decode("latin1", errors="replace")
        size = _u32(data, pos + 4)
        chunks.append((tag, pos + 8, size))
        pos += 8 + size
    return chunks


def _next_pow2(n: int) -> int:
    """Return smallest power of two >= n."""
    return 1 << (max(1, n) - 1).bit_length()


def _is_tex_blob(data: bytes | bytearray, off: int, txtr_off: int, txtr_sz: int) -> bool:
    """Check if `off` is a valid texture blob offset within the TXTR chunk."""
    if not (txtr_off < off < txtr_off + txtr_sz and off + 4 <= len(data)):
        return False
    magic4 = bytes(data[off:off + 4])
    magic2 = bytes(data[off:off + 2])
    # PNG: \x89PNG, QOI: qoif / fioq, BZ2/QOI-chunk: 2q
    if magic4 == b"\x89PNG" or magic4 in (b"qoif", b"fioq") or magic2 == b"2q":
        return True
    if magic4.startswith(b"DUMM"):
        return True
    return False


def _get_txtr_blob_info(
    data: bytes | bytearray, txtr_off: int, txtr_sz: int, txtr_ptrs: list[int], tex_id: int
) -> tuple[int, int] | None:
    """Find (blob_ptr_pos, blob_offset) for texture page entry tex_id."""
    if 0 <= tex_id < len(txtr_ptrs):
        ep = txtr_ptrs[tex_id]
        ep_end = txtr_ptrs[tex_id + 1] if tex_id + 1 < len(txtr_ptrs) else min(len(data), ep + 32)
        for p in range(ep_end - 4, ep, -4):
            if p + 4 <= len(data):
                val = _u32(data, p)
                if _is_tex_blob(data, val, txtr_off, txtr_sz):
                    return (p, val)
    return None


def _decode_gm_texture(blob: bytes) -> Any:
    """Decode GM texture blob from PNG, QOI (fioq/qoif) or BZ2+QOI (2q) into a PIL RGBA Image."""
    if not blob:
        return None
    from PIL import Image

    # 1. Standard PNG
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return Image.open(io.BytesIO(blob)).convert("RGBA")
        except Exception:
            return None

    # 2. BZ2-compressed QOI (2q)
    data = blob
    if blob[:2] == b"2q":
        import bz2
        bz_idx = blob.find(b"BZh")
        if bz_idx != -1:
            try:
                data = bz2.decompress(blob[bz_idx:])
            except Exception:
                return None
        else:
            return None

    # 3. GameMaker / standard QOI
    if len(data) >= 12 and (data[:4] == b"fioq" or data[:4] == b"qoif"):
        try:
            if data[:4] == b"fioq":
                # GameMaker Little-Endian QOI: 4s magic, <HH (w, h), <I length
                w = data[4] | (data[5] << 8)
                h = data[6] | (data[7] << 8)
                pix_data = data[12:]
            else:
                # Standard QOI: 4s magic, >II (w, h), 1B channels, 1B colorspace
                w = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]
                h = (data[8] << 24) | (data[9] << 16) | (data[10] << 8) | data[11]
                pix_data = data[14:]

            total_pixels = w * h
            out_rgba = bytearray(total_pixels * 4)
            out_pos = 0
            pos = 0
            run = 0
            r, g, b, a = 0, 0, 0, 255
            index = bytearray(64 * 4)

            T2 = (0, 1, -2, -1)
            pix_len = len(pix_data)

            while out_pos < len(out_rgba):
                if run > 0:
                    run -= 1
                elif pos < pix_len:
                    b1 = pix_data[pos]
                    pos += 1

                    if (b1 & 0xC0) == 0x00:  # QOI_INDEX
                        idx = (b1 & 0x3F) << 2
                        r = index[idx]
                        g = index[idx + 1]
                        b = index[idx + 2]
                        a = index[idx + 3]
                    elif (b1 & 0xE0) == 0x40:  # QOI_RUN_8
                        run = b1 & 0x1F
                    elif (b1 & 0xE0) == 0x60:  # QOI_RUN_16
                        if pos < pix_len:
                            b2 = pix_data[pos]
                            pos += 1
                            run = (((b1 & 0x1F) << 8) | b2) + 32
                    elif (b1 & 0xC0) == 0x80:  # QOI_DIFF_8
                        r = (r + T2[(b1 >> 4) & 3]) & 0xFF
                        g = (g + T2[(b1 >> 2) & 3]) & 0xFF
                        b = (b + T2[b1 & 3]) & 0xFF
                    elif (b1 & 0xE0) == 0xC0:  # QOI_DIFF_16
                        if pos < pix_len:
                            b2 = pix_data[pos]
                            pos += 1
                            merged = (b1 << 8) | b2
                            vr = (merged >> 8) & 0x1F
                            vg = (merged >> 4) & 0x0F
                            vb = merged & 0x0F
                            r = (r + (vr if vr < 16 else vr - 32)) & 0xFF
                            g = (g + (vg if vg < 8 else vg - 16)) & 0xFF
                            b = (b + (vb if vb < 8 else vb - 16)) & 0xFF
                    elif (b1 & 0xF0) == 0xE0:  # QOI_DIFF_24
                        if pos + 1 < pix_len:
                            b2 = pix_data[pos]
                            b3 = pix_data[pos + 1]
                            pos += 2
                            merged = (b1 << 16) | (b2 << 8) | b3
                            vr = (merged >> 15) & 0x1F
                            vg = (merged >> 10) & 0x1F
                            vb = (merged >> 5) & 0x1F
                            va = merged & 0x1F
                            r = (r + (vr if vr < 16 else vr - 32)) & 0xFF
                            g = (g + (vg if vg < 16 else vg - 32)) & 0xFF
                            b = (b + (vb if vb < 16 else vb - 32)) & 0xFF
                            a = (a + (va if va < 16 else va - 32)) & 0xFF
                    elif (b1 & 0xF0) == 0xF0:  # QOI_COLOR
                        if b1 & 8 and pos < pix_len:
                            r = pix_data[pos]; pos += 1
                        if b1 & 4 and pos < pix_len:
                            g = pix_data[pos]; pos += 1
                        if b1 & 2 and pos < pix_len:
                            b = pix_data[pos]; pos += 1
                        if b1 & 1 and pos < pix_len:
                            a = pix_data[pos]; pos += 1

                    idx2 = ((r ^ g ^ b ^ a) & 63) << 2
                    index[idx2] = r
                    index[idx2 + 1] = g
                    index[idx2 + 2] = b
                    index[idx2 + 3] = a

                out_rgba[out_pos] = r
                out_rgba[out_pos + 1] = g
                out_rgba[out_pos + 2] = b
                out_rgba[out_pos + 3] = a
                out_pos += 4

            return Image.frombytes("RGBA", (w, h), bytes(out_rgba))
        except Exception as e:
            logger.warning("Failed decoding QOI texture: %s", e)
            return None

    return None


def _sanitize_gm_translation(original: str, translated: str, bytecode_version: int) -> str:
    """Sanitize GameMaker formatting codes in LLM translation."""
    if not translated:
        return translated

    # 1. Sync '#' newlines (GM:Studio 1.x)
    if "#" in original and "\n" not in original and "\n" in translated:
        translated = translated.replace("\r\n", "\n").replace("\n", "#")
    elif bytecode_version <= 16 and "#" not in original and "#" in translated:
        # In GM:Studio <= 1.x unescaped '#' breaks dialogue. Replace accidental '#' with '№' or '\#'.
        translated = re.sub(r'(?<!\\)#', '№', translated)

    # 2. Restore dialogue terminators from Undertale/Deltarune
    for term in ("%%", "%", "/"):
        if original.endswith(term) and not translated.endswith(term):
            translated = translated.rstrip(" \t") + term
            break

    # 3. Preserve Risk of Rain closing color tags
    if "&!&" in original and "&!&" not in translated and any(f"&{c}&" in translated for c in "yrbgwp"):
        translated = translated + "&!&"

    return translated


class GameMakerParser(BaseParser):
    engine: str = "gamemaker"

    @staticmethod
    def _is_valid_gamemaker_archive(fpath: str) -> bool:
        """Check if file has FORM magic and contains both GEN8 and STRG chunks."""
        try:
            if not os.path.isfile(fpath) or os.path.getsize(fpath) < 32:
                return False
            with open(fpath, "rb") as f:
                hdr = f.read(12)
                if len(hdr) < 12 or hdr[:4] != b"FORM":
                    return False
                # Read chunks to verify GEN8 and STRG presence
                f.seek(8)
                tags = set()
                while True:
                    ch_hdr = f.read(8)
                    if len(ch_hdr) < 8:
                        break
                    tag = ch_hdr[:4]
                    size = struct.unpack("<I", ch_hdr[4:8])[0]
                    tags.add(tag)
                    if b"GEN8" in tags and b"STRG" in tags:
                        return True
                    f.seek(size, 1)
        except Exception:
            return False
        return False

    @staticmethod
    def detect(root: str) -> bool:
        if not os.path.isdir(root):
            return False
        for dirpath, _, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > 2:
                continue
            for fname in filenames:
                lower = fname.lower()
                if lower.startswith("audiogroup") and lower.endswith(".dat"):
                    continue
                if lower in ("data.win", "game.unx", "game.ios", "game.droid") or (
                    lower.endswith(".win") and not lower.startswith("audiogroup")
                ):
                    fpath = os.path.join(dirpath, fname)
                    if GameMakerParser._is_valid_gamemaker_archive(fpath):
                        return True
        return False

    def engine_prompt_addon(self) -> str:
        return (
            "GAMEMAKER CONTROL CODES & MARKUP:\n"
            "- Risk of Rain color codes: preserve &y&, &r&, &b&, &g&, &w&, &p&, &lt&, &!& exactly.\n"
            "- Undertale / Deltarune text codes: preserve \\cY, \\cR, \\cB, \\cG, \\cW, \\c0, timings ^1..^9, "
            "emotions \\E0..\\E9, \\F0..\\F9, markers * and especially terminating %, %%, / at the very end of lines.\n"
            "- Scribble rich text (GMS2): preserve [c_red], [/c], [wave], [/wave], [shake], [scale,...], [spr_...].\n"
            "- Newline markers: preserve '#' if the original uses '#' as a newline.\n"
            "- Formatting placeholders: preserve {0}, {1}, %s, %d, %f exactly.\n"
        )

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        results: list[TranslationString] = []

        # 1. Scan for GameMaker binary archives (data.win, game.unx, etc.)
        archives: list[tuple[str, str]] = []
        for dirpath, _, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > 2:
                continue
            for fname in filenames:
                lower = fname.lower()
                if lower.startswith("audiogroup") and lower.endswith(".dat"):
                    continue
                if lower in ("data.win", "game.unx", "game.ios", "game.droid") or (
                    lower.endswith(".win") and not lower.startswith("audiogroup")
                ):
                    full_p = os.path.join(dirpath, fname)
                    rel_p = os.path.relpath(full_p, root).replace("\\", "/")
                    if GameMakerParser._is_valid_gamemaker_archive(full_p):
                        archives.append((full_p, rel_p))

        for full_p, rel_p in archives:
            if sub_paths and rel_p not in sub_paths:
                continue
            # Read backup original if present to guarantee stable baseline
            raw = read_backup_original(root, rel_p)
            if raw is None:
                try:
                    with open(full_p, "rb") as f:
                        raw = f.read()
                except OSError as e:
                    logger.warning("Could not read %s: %e", full_p, e)
                    continue

            results.extend(self._extract_from_data_win(raw, rel_p))

        # 2. Scan for external localization files (lang/*.json, localizations/*.ini, etc.)
        results.extend(self._extract_external_files(root, sub_paths))
        return results

    def _extract_from_data_win(self, data: bytes, rel_p: str) -> list[TranslationString]:
        chunks = _parse_form_chunks(data)
        chunk_map = {tag: (off, size) for tag, off, size in chunks}
        if "GEN8" not in chunk_map or "STRG" not in chunk_map:
            return []

        gen8_off, _ = chunk_map["GEN8"]
        strg_off, strg_size = chunk_map["STRG"]
        bytecode_version = data[gen8_off + 1]

        # Read all strings from STRG chunk
        strg_count = _u32(data, strg_off)
        str_ptrs = [_u32(data, strg_off + 4 + i * 4) for i in range(strg_count)]
        char_ptr_to_idx: dict[int, int] = {}
        strings: dict[int, str] = {}

        for i, p in enumerate(str_ptrs):
            if p + 4 > len(data):
                continue
            slen = _u32(data, p)
            s_bytes = data[p + 4 : p + 4 + slen]
            char_ptr_to_idx[p] = i
            char_ptr_to_idx[p + 4] = i
            try:
                strings[i] = s_bytes.decode("utf-8")
            except UnicodeDecodeError:
                strings[i] = s_bytes.decode("latin1", errors="replace")

        # Scan metadata chunks for technical string pointers
        technical_indices: set[int] = set()
        for tag in METADATA_CHUNKS:
            if tag.decode("latin1") in chunk_map:
                c_off, c_sz = chunk_map[tag.decode("latin1")]
                limit = c_off + c_sz
                for p in range(c_off, limit - 3, 4):
                    val = _u32(data, p)
                    if val in char_ptr_to_idx:
                        technical_indices.add(char_ptr_to_idx[val])

        # Build function call address map from FUNC chunk if present
        call_addr_to_func: dict[int, str] = {}
        if "FUNC" in chunk_map and bytecode_version >= 15 and "CODE" in chunk_map:
            func_off, func_sz = chunk_map["FUNC"]
            code_off, code_sz = chunk_map["CODE"]
            func_count = _u32(data, func_off)
            for i in range(func_count):
                entry = func_off + 4 + i * 12
                if entry + 12 > func_off + func_sz:
                    break
                name_ptr, occurrences, first_addr = struct.unpack_from("<III", data, entry)
                if occurrences == 0 or first_addr == 0xFFFFFFFF:
                    continue
                fname = strings.get(char_ptr_to_idx.get(name_ptr, -1), "")
                curr = first_addr
                visited = set()
                while curr != 0 and curr not in visited and len(visited) < occurrences and (code_off <= curr < code_off + code_sz):
                    visited.add(curr)
                    # Determine whether curr points to instruction word0 (0xD9) or operand word1 (pos + 4)
                    if curr + 8 <= len(data) and ((_u32(data, curr) >> 24) & 0xFF) in (0xD9, 0xDA):
                        inst_addr = curr
                        link_addr = curr + 4
                    elif curr >= code_off + 4 and ((_u32(data, curr - 4) >> 24) & 0xFF) in (0xD9, 0xDA):
                        inst_addr = curr - 4
                        link_addr = curr
                    else:
                        inst_addr = curr
                        link_addr = curr + 4
                    call_addr_to_func[inst_addr] = fname
                    if link_addr + 4 > len(data):
                        break
                    raw_link = _u32(data, link_addr) & 0x00FFFFFF
                    if raw_link == 0 or raw_link == 0x00FFFFFF:
                        break
                    # 24-bit signed offset
                    next_delta = raw_link - 0x01000000 if (raw_link & 0x00800000) else raw_link
                    if next_delta == 0:
                        break
                    curr = curr + next_delta

        # Walk VM instructions in CODE chunk
        code_usage: dict[int, list[tuple[str, str]]] = {}
        tech_call_indices: set[int] = set()
        compared_indices: set[int] = set()
        text_draw_indices: set[int] = set()
        code_count = 0

        if "CODE" in chunk_map:
            code_off, code_sz = chunk_map["CODE"]
            code_count = _u32(data, code_off)
            code_ptrs = [_u32(data, code_off + 4 + i * 4) for i in range(code_count)]

            for cp in code_ptrs:
                if cp + 12 > len(data):
                    continue
                name_ptr = _u32(data, cp)
                code_name = strings.get(char_ptr_to_idx.get(name_ptr, -1), "")
                bc_len = _u32(data, cp + 4)
                if bytecode_version <= 14:
                    bc_start = cp + 8
                else:
                    if cp + 16 > len(data):
                        continue
                    rel_off = _i32(data, cp + 12)
                    bc_start = cp + 12 + rel_off

                pos = bc_start
                end = bc_start + bc_len
                push_stack: list[int | None] = []

                while pos + 4 <= end and pos + 4 <= len(data):
                    w0 = _u32(data, pos)
                    op = (w0 >> 24) & 0xFF
                    t1 = (w0 >> 16) & 0x0F

                    if op in (0xC0, 0xC1, 0xC2, 0xC3):  # Push, PushLoc, PushGlb, PushBltn
                        if t1 == 0x06:  # String
                            if pos + 8 > len(data):
                                break
                            w1 = _u32(data, pos + 4)
                            str_idx = w1 & 0x00FFFFFF
                            push_stack.append(str_idx)
                            code_usage.setdefault(str_idx, []).append((code_name, ""))

                            # Lookahead for cmp instruction (0x11..0x16 covers both BC<=14 and BC>=15 0x15)
                            next_pos = pos + 8
                            if next_pos + 4 <= end:
                                next_w0 = _u32(data, next_pos)
                                next_op = (next_w0 >> 24) & 0xFF
                                if 0x11 <= next_op <= 0x16:
                                    compared_indices.add(str_idx)
                            pos += 8
                        elif t1 in (0x00, 0x03):  # Double, Int64 (12 bytes)
                            push_stack.append(None)
                            pos += 12
                        elif t1 == 0x0F:  # Int16 (4 bytes)
                            push_stack.append(None)
                            pos += 4
                        else:  # Int32, Float32, Variable, etc. (8 bytes)
                            push_stack.append(None)
                            pos += 8
                    elif op == 0x84:  # PushI (Int16 immediate, 4 bytes)
                        push_stack.append(None)
                        pos += 4
                    elif op == 0xFF and t1 == 0x02:  # GMS 2.3+ extended Break with Int32 operand (e.g. pushref, 8 bytes)
                        push_stack.append(None)
                        pos += 8
                    elif op == 0x45:  # pop
                        if push_stack:
                            push_stack.pop()
                        pos += 8
                    elif op in (0xD9, 0xDA):  # call / callv
                        func_name = call_addr_to_func.get(pos, "")
                        argc = w0 & 0xFFFF
                        call_args = push_stack[-argc:] if argc > 0 and len(push_stack) >= argc else list(push_stack)
                        valid_args = [x for x in call_args if x is not None]

                        if func_name:
                            if func_name.startswith("ini_read_string"):
                                # For ini_read_string(sec, key, default), only mark section and key as technical!
                                tech_call_indices.update(valid_args[:2])
                            elif func_name in TECH_FUNCS:
                                tech_call_indices.update(valid_args)
                            elif func_name in DRAW_FUNCS:
                                text_draw_indices.update(valid_args)

                            for s_idx in valid_args:
                                code_usage.setdefault(s_idx, []).append((code_name, func_name))

                        push_stack.clear()
                        pos += 8
                    elif 0x11 <= op <= 0x16:  # cmp (0x11..0x16)
                        if push_stack and push_stack[-1] is not None:
                            compared_indices.add(push_stack[-1])
                        pos += 4
                    elif op in (0xB7, 0xB8, 0xB9, 0x9C, 0x9D):  # branch / exit
                        push_stack.clear()
                        pos += 4
                    else:
                        pos += 4

        # Read strings from LANG (GMS2 built-in localization) and ROOM if available
        lang_indices: set[int] = set()
        if "LANG" in chunk_map:
            l_off, l_sz = chunk_map["LANG"]
            for p in range(l_off, l_off + l_sz - 3, 4):
                val = _u32(data, p)
                if val in char_ptr_to_idx:
                    lang_indices.add(char_ptr_to_idx[val])

        # Technical identifier patterns to filter out
        ident_pattern = re.compile(
            r'^(?:[a-zA-Z_][a-zA-Z0-9_]*\.(?:png|ogg|wav|ini|json|sav|txt|dat|dll|yy|gml)|'
            r'[a-zA-Z0-9]*_[a-zA-Z0-9_]+|'  # true snake_case containing an underscore
            r'[a-zA-Z]+[0-9]+[a-zA-Z0-9]*|'  # identifiers with numbers
            r'(?:gml_|@@|obj_|scr_|rm_|snd_|fnt_|bg_|spr_|mus_|sfx_)[a-zA-Z0-9_]+|'
            r'#[0-9a-fA-F]{6}|rgba?\(.*\)|https?://.*)$'
        )

        strings_out: list[TranslationString] = []
        for idx, text in strings.items():
            if not text or not text.strip():
                continue
            stripped = text.strip()
            # Must have letters
            if not any(c.isalpha() for c in stripped):
                continue
            # Single-letter tokens are almost always internal formatting codes or variable names
            if len(stripped) <= 1 and not (stripped.isupper() or stripped in ("Я", "I", "A")):
                continue
            # If VM game with CODE records, string must be referenced by push.s or LANG
            if code_count > 0 and (idx not in code_usage and idx not in lang_indices):
                continue
            if idx in tech_call_indices:
                continue
            # Comparisons: exclude short state machine keys (e.g. "idle", "walk", "attack")
            if idx in compared_indices:
                if len(stripped) <= 25 or " " not in stripped:
                    continue
            # Technical index check
            if idx in technical_indices and idx not in text_draw_indices and idx not in lang_indices:
                if len(stripped) <= 20 and ident_pattern.match(stripped):
                    continue

            # Reject code identifiers
            if ident_pattern.match(stripped) and len(stripped) <= 25 and " " not in stripped:
                continue

            ctx_parts = []
            if idx in code_usage:
                cname, fname = code_usage[idx][0]
                cleaned_cname = re.sub(r'^(?:gml_Object_|gml_Script_|gml_Room_)', '', cname)
                if fname:
                    ctx_parts.append(f"GML: {cleaned_cname} -> {fname}()")
                else:
                    ctx_parts.append(f"GML: {cleaned_cname}")
            elif idx in lang_indices:
                ctx_parts.append("LANG")

            strings_out.append(
                self._mk(
                    file=rel_p,
                    path=["STRG", str(idx)],
                    original=text,
                    context=" | ".join(ctx_parts) if ctx_parts else "GameMaker",
                )
            )

        return strings_out

    def _extract_external_files(
        self, root: str, sub_paths: list[str] | None = None
    ) -> list[TranslationString]:
        """Extract strings from external localization files (lang/*.json, etc.)."""
        results: list[TranslationString] = []
        loc_patterns = [
            re.compile(r'^(?:lang|languages|localizations?|strings|data)/.*\.(json|ini|csv|txt)$', re.I)
        ]

        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                lower = fname.lower()
                if lower in ("options.ini", "steam_appid.txt", "version.txt", "credits.txt"):
                    continue
                full_p = os.path.join(dirpath, fname)
                rel_p = os.path.relpath(full_p, root).replace("\\", "/")
                if sub_paths and rel_p not in sub_paths:
                    continue

                if any(p.match(rel_p) for p in loc_patterns):
                    raw = read_backup_original(root, rel_p)
                    if raw is None:
                        try:
                            with open(full_p, "rb") as f:
                                raw = f.read()
                        except OSError:
                            continue

                    ext = os.path.splitext(lower)[1]
                    if ext == ".json":
                        try:
                            data = json.loads(raw.decode("utf-8"))
                            self._extract_json_rec(rel_p, [], data, results)
                        except Exception:
                            pass
                    elif ext == ".ini":
                        try:
                            text = raw.decode("utf-8", errors="replace")
                            curr_sec = "root"
                            for line in text.splitlines():
                                line = line.strip()
                                if line.startswith("[") and line.endswith("]"):
                                    curr_sec = line[1:-1]
                                elif "=" in line and not line.startswith(";"):
                                    k, v = line.split("=", 1)
                                    v = v.strip()
                                    if v and any(c.isalpha() for c in v):
                                        results.append(self._mk(rel_p, [curr_sec, k.strip()], v, f"INI: [{curr_sec}]"))
                        except Exception:
                            pass
                    elif ext == ".csv":
                        import csv
                        try:
                            lines = raw.decode("utf-8-sig", errors="replace").splitlines()
                            reader = list(csv.reader(lines))
                            if not reader:
                                continue
                            start_row = 0
                            row0 = reader[0]
                            header_names = {
                                "id", "key", "text", "string", "source", "target", "value",
                                "translation", "dialogue", "msg", "message", "name", "desc",
                                "description", "en", "ru", "comment", "context",
                            }
                            if any(c.strip().lower() in header_names for c in row0) and all(
                                len(c.strip().split()) <= 1 for c in row0 if c.strip()
                            ):
                                start_row = 1

                            for row_idx in range(start_row, len(reader)):
                                row = reader[row_idx]
                                if not row:
                                    continue
                                start_col = 1 if len(row) > 1 else 0
                                for col_idx in range(start_col, len(row)):
                                    val = row[col_idx].strip()
                                    if val and any(c.isalpha() for c in val):
                                        results.append(self._mk(rel_p, [str(row_idx), str(col_idx)], row[col_idx], f"CSV: row {row_idx}, col {col_idx}"))
                        except Exception:
                            pass
                    elif ext == ".txt":
                        try:
                            lines = raw.decode("utf-8-sig", errors="replace").splitlines()
                            for line_idx, line in enumerate(lines):
                                sline = line.strip()
                                if sline and any(c.isalpha() for c in sline):
                                    results.append(self._mk(rel_p, [str(line_idx)], line, f"TXT: line {line_idx}"))
                        except Exception:
                            pass

        return results

    def _extract_json_rec(
        self, file: str, path: list[str], obj: Any, results: list[TranslationString]
    ) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                self._extract_json_rec(file, path + [k], v, results)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._extract_json_rec(file, path + [str(i)], v, results)
        elif isinstance(obj, str):
            if any(c.isalpha() for c in obj):
                results.append(self._mk(file, path, obj, f"JSON: {'.'.join(path)}"))

    def inject(
        self,
        root: str,
        translations: dict[str, str],
        target_lang: str | None = None,
        sub_paths: list[str] | None = None,
    ) -> int:
        if not translations:
            return 0

        # Group translations by relative file path
        by_file: dict[str, dict[str, str]] = {}
        for s in self.extract(root, sub_paths):
            if s.id in translations:
                by_file.setdefault(s.file, {})[s.id] = translations[s.id]

        total_written = 0
        for rel_p, file_tr in by_file.items():
            full_p = os.path.join(root, rel_p.replace("/", os.sep))
            if not os.path.exists(full_p):
                continue

            lower = os.path.basename(rel_p).lower()
            if lower in ("data.win", "game.unx", "game.ios", "game.droid") or lower.endswith(".win"):
                total_written += self._inject_data_win(root, rel_p, file_tr, target_lang)
            else:
                total_written += self._inject_external_file(root, rel_p, file_tr)

        self.finalize_backups(root)
        return total_written

    def _inject_data_win(
        self, root: str, rel_p: str, file_tr: dict[str, str], target_lang: str | None
    ) -> int:
        full_p = os.path.join(root, rel_p.replace("/", os.sep))
        # Read clean original from backup if already staged/finalized
        raw = read_backup_original(root, rel_p)
        if raw is None:
            with open(full_p, "rb") as f:
                raw = f.read()

        self.backup_file(root, full_p)
        chunks = _parse_form_chunks(raw)
        chunk_map = {tag: (off, size) for tag, off, size in chunks}
        if "STRG" not in chunk_map or "GEN8" not in chunk_map:
            return 0

        strg_off, strg_size = chunk_map["STRG"]
        gen8_off, _ = chunk_map["GEN8"]
        bytecode_version = raw[gen8_off + 1]

        # Read original strings
        strg_count = _u32(raw, strg_off)
        str_ptrs = [_u32(raw, strg_off + 4 + i * 4) for i in range(strg_count)]

        # Map string ID to index
        id_to_idx: dict[str, tuple[int, str]] = {}
        for s in self._extract_from_data_win(raw, rel_p):
            if len(s.path) >= 2 and s.path[0] == "STRG":
                idx = int(s.path[1])
                id_to_idx[s.id] = (idx, s.original)

        # Build appended STRG buffer for translated strings
        # Note: All original UndertaleString records remain untouched at their old byte offsets.
        appended_strg_buf = bytearray()
        new_str_ptrs: dict[int, int] = {}
        char_remap: dict[int, int] = {}
        written_count = 0

        for sid, tr_text in file_tr.items():
            if sid not in id_to_idx:
                continue
            idx, orig_text = id_to_idx[sid]
            clean_tr = _sanitize_gm_translation(orig_text, tr_text, bytecode_version)
            utf8_bytes = clean_tr.encode("utf-8")

            # Offset of new string record: end of STRG chunk + current appended buffer length
            new_record_off = (strg_off + strg_size) + len(appended_strg_buf)
            new_str_ptrs[idx] = new_record_off
            char_remap[str_ptrs[idx] + 4] = new_record_off + 4

            # Record structure: 4-byte length, UTF-8 bytes, null terminator, 4-byte aligned
            rec = struct.pack("<I", len(utf8_bytes)) + utf8_bytes + b"\x00"
            pad = (4 - (len(rec) % 4)) % 4
            rec += b"\x00" * pad
            appended_strg_buf.extend(rec)
            written_count += 1

        if not written_count:
            return 0

        buf = bytearray(raw)

        # Optional: Auto-generate Cyrillic glyphs in FONT if translating to Russian/Cyrillic
        is_cyrillic = bool(
            target_lang in ("Russian", "Ukrainian", "Belarusian", "ru", "uk", "be")
            or any(re.search(r'[\u0400-\u04FF]', t) for t in file_tr.values())
        )
        new_font_ptrs: dict[int, int] = {}
        modified_pages: dict[int, tuple[int, int, bytes, int, int]] = {}
        if is_cyrillic and "FONT" in chunk_map:
            try:
                new_font_ptrs, font_records, modified_pages = self._generate_cyrillic_fonts(
                    raw, chunk_map, (strg_off + strg_size) + len(appended_strg_buf), buf
                )
                if font_records:
                    appended_strg_buf.extend(font_records)
            except Exception as e:
                logger.warning("Cyrillic font generation skipped: %s", e)

        # Align appended STRG buffer to 128 bytes
        strg_delta = len(appended_strg_buf)
        pad128 = (128 - (strg_delta % 128)) % 128
        appended_strg_buf.extend(b"\x00" * pad128)
        strg_delta = len(appended_strg_buf)

        # 1. Update STRG pointers table for translated strings
        for idx, new_p in new_str_ptrs.items():
            struct.pack_into("<I", buf, strg_off + 4 + idx * 4, new_p)

        # 2. Update FONT pointers table if fonts were augmented
        if "FONT" in chunk_map and new_font_ptrs:
            font_off, _ = chunk_map["FONT"]
            for f_idx, new_fp in new_font_ptrs.items():
                struct.pack_into("<I", buf, font_off + 4 + f_idx * 4, new_fp)

        # 3. Update LANG pointers if present before STRG
        if "LANG" in chunk_map and char_remap:
            lang_off, lang_sz = chunk_map["LANG"]
            if lang_off < strg_off:
                for p in range(lang_off, lang_off + lang_sz - 3, 4):
                    val = _u32(buf, p)
                    if val in char_remap:
                        struct.pack_into("<I", buf, p, char_remap[val])

        # 4. Update STRG chunk size in header
        struct.pack_into("<I", buf, strg_off - 4, strg_size + strg_delta)

        # 5. Handle TXTR chunk: in-place replacement maintaining strict blob_ptr monotonicity
        txtr_delta = 0
        new_blob_area = bytearray()
        first_blob_off = 0
        sorted_blob_offsets: list[int] = []

        if "TXTR" in chunk_map:
            txtr_off, txtr_sz = chunk_map["TXTR"]
            txtr_count = _u32(raw, txtr_off)
            txtr_ptrs = [_u32(raw, txtr_off + 4 + i * 4) for i in range(txtr_count)]

            # Map each unique blob offset to the entries pointing to it
            blob_to_entries: dict[int, list[tuple[int, int, int]]] = {}  # old_blob_off -> [(tex_id, ep, blob_ptr_pos), ...]
            for i in range(txtr_count):
                info = _get_txtr_blob_info(raw, txtr_off, txtr_sz, txtr_ptrs, i)
                if info:
                    b_pos, b_off = info
                    blob_to_entries.setdefault(b_off, []).append((i, txtr_ptrs[i], b_pos))

            sorted_blob_offsets = sorted(blob_to_entries.keys())
            if sorted_blob_offsets:
                first_blob_off = sorted_blob_offsets[0]
                current_shift = 0

                for idx, old_blob_off in enumerate(sorted_blob_offsets):
                    next_off = sorted_blob_offsets[idx + 1] if idx + 1 < len(sorted_blob_offsets) else (txtr_off + txtr_sz)
                    old_len = next_off - old_blob_off
                    entries = blob_to_entries[old_blob_off]

                    # Check if any tex_id using this blob was modified
                    mod_item = next((modified_pages[t_id] for t_id, _, _ in entries if t_id in modified_pages), None)

                    if mod_item is not None:
                        _, _, png_bytes, new_w, new_h = mod_item
                        L = len(png_bytes)
                        if L > old_len:
                            diff = L - old_len
                            pad128 = (128 - (diff % 128)) % 128
                            blob_delta = diff + pad128
                            new_alloc = old_len + blob_delta
                        else:
                            blob_delta = 0
                            new_alloc = old_len

                        new_file_blob_off = old_blob_off + strg_delta + current_shift

                        new_blob_area.extend(png_bytes)
                        if new_alloc > L:
                            new_blob_area.extend(b"\x00" * (new_alloc - L))

                        for t_id, ep, b_pos in entries:
                            struct.pack_into("<I", buf, b_pos, new_file_blob_off)
                            if b_pos >= ep + 12:
                                # GMS 2022.3+ and 2023.1+ store TextureBlockSize at ep + 8
                                struct.pack_into("<I", buf, ep + 8, L)
                            if b_pos >= ep + 20:
                                # GMS 2022.9+ / 2023.1+ store TextureWidth at ep + 12 and TextureHeight at ep + 16
                                struct.pack_into("<I", buf, ep + 12, new_w)
                                struct.pack_into("<I", buf, ep + 16, new_h)

                        current_shift += blob_delta
                    else:
                        # Unmodified texture: preserve exact bytes from raw
                        new_file_blob_off = old_blob_off + strg_delta + current_shift
                        new_blob_area.extend(raw[old_blob_off:next_off])
                        for t_id, ep, b_pos in entries:
                            struct.pack_into("<I", buf, b_pos, new_file_blob_off)

                txtr_delta = current_shift

            # Update TXTR table pointers: ep + strg_delta
            for i, ep in enumerate(txtr_ptrs):
                struct.pack_into("<I", buf, txtr_off + 4 + i * 4, ep + strg_delta)

            # Update TXTR chunk size in header
            if txtr_delta > 0:
                struct.pack_into("<I", buf, txtr_off - 4, txtr_sz + txtr_delta)

        # 6. Shift AUDO chunk pointers (AUDO sits after TXTR)
        if "AUDO" in chunk_map:
            audo_off, audo_sz = chunk_map["AUDO"]
            audo_count = _u32(buf, audo_off)
            for i in range(audo_count):
                pos = audo_off + 4 + i * 4
                if pos + 4 <= len(buf):
                    old_ap = _u32(buf, pos)
                    struct.pack_into("<I", buf, pos, old_ap + strg_delta + txtr_delta)

        # 7. Shift LANG pointers if after STRG / TXTR
        if "LANG" in chunk_map:
            lang_off, lang_sz = chunk_map["LANG"]
            txtr_off, txtr_sz = chunk_map.get("TXTR", (0, 0))
            if txtr_off and lang_off > txtr_off:
                for p in range(lang_off, lang_off + lang_sz - 3, 4):
                    val = _u32(buf, p)
                    if val >= strg_off:
                        struct.pack_into("<I", buf, p, val + strg_delta + txtr_delta)
            elif lang_off > strg_off:
                for p in range(lang_off, lang_off + lang_sz - 3, 4):
                    val = _u32(buf, p)
                    if val >= strg_off:
                        struct.pack_into("<I", buf, p, val + strg_delta)

        # 8. Replace TXTR blob area in-place, and insert appended STRG buffer at end of STRG
        if modified_pages and sorted_blob_offsets:
            buf[first_blob_off : txtr_off + txtr_sz] = new_blob_area

        strg_insert_pos = strg_off + strg_size
        buf[strg_insert_pos:strg_insert_pos] = appended_strg_buf

        # 9. Update FORM total size in header
        old_form_sz = _u32(buf, 4)
        struct.pack_into("<I", buf, 4, old_form_sz + strg_delta + txtr_delta)

        # Write modified data.win atomically
        import tempfile
        dir_name = os.path.dirname(full_p)
        os.makedirs(dir_name, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=dir_name, prefix="tmp_gm_", delete=False) as tf:
            tf.write(buf)
            temp_path = tf.name
        try:
            os.replace(temp_path, full_p)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        return written_count

    def _generate_cyrillic_fonts(
        self,
        data: bytes,
        chunk_map: dict[str, tuple[int, int]],
        base_appended_off: int,
        buf: bytearray,
    ) -> tuple[dict[int, int], bytearray, dict[int, tuple[int, int, bytes, int, int]]]:
        """Check fonts in FONT chunk; if missing Cyrillic, augment them with Cyrillic glyphs rendered via Pillow."""
        font_off, _ = chunk_map["FONT"]
        font_count = _u32(data, font_off)
        font_ptrs = [_u32(data, font_off + 4 + i * 4) for i in range(font_count)]
        gen8_off, _ = chunk_map["GEN8"]
        bytecode_version = data[gen8_off + 1]

        # Prepare TTF font paths from python-core/assets/fonts
        assets_font_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
        zpix_path = os.path.join(assets_font_dir, "Zpix.ttf")
        noto_path = os.path.join(assets_font_dir, "NotoSans-Regular.ttf")

        txtr_off, txtr_sz = chunk_map.get("TXTR", (0, 0))
        txtr_count = _u32(data, txtr_off) if txtr_off else 0
        txtr_ptrs = [_u32(data, txtr_off + 4 + i * 4) for i in range(txtr_count)] if txtr_off else []

        # Cache open PIL images for texture pages: tex_id -> [Image, is_modified, blob_ptr_pos, blob_off]
        page_images: dict[int, list[Any]] = {}

        new_font_ptrs: dict[int, int] = {}
        appended_buf = bytearray()

        for f_idx, fp in enumerate(font_ptrs):
            if fp + 40 > len(data):
                continue
            r_start = _u16(data, fp + 20)
            r_end = _u32(data, fp + 24)
            anti_alias = data[fp + 23] if fp + 23 < len(data) else 1

            # Find glyph count position
            gc_pos = None
            for pos in range(fp + 40, fp + 68, 4):
                if pos + 8 > len(data):
                    break
                gc = _u32(data, pos)
                first_p = _u32(data, pos + 4)
                if gc > 0 and first_p == pos + 4 + gc * 4:
                    gc_pos = pos
                    break

            if gc_pos is None:
                continue

            glyph_count = _u32(data, gc_pos)
            g_ptrs = [_u32(data, gc_pos + 4 + i * 4) for i in range(glyph_count)]
            glyphs = []
            has_cyrillic = False

            for gp in g_ptrs:
                if gp + 16 > len(data):
                    continue
                char_c, gx, gy, gw, gh, gshift, goffset, kcount = struct.unpack_from("<HHHHHhhH", data, gp)
                if 0x0410 <= char_c <= 0x044F:
                    has_cyrillic = True
                glyphs.append({
                    "char": char_c, "gx": gx, "gy": gy, "gw": gw, "gh": gh,
                    "shift": gshift, "offset": goffset, "kcount": kcount,
                    "kerning": data[gp + 16 : gp + 16 + kcount * 4] if kcount > 0 else b"",
                })

            if has_cyrillic or len(glyphs) < 26:
                continue

            cap_glyph = next((g for g in glyphs if g["char"] in (65, 72)), None)
            if not cap_glyph:
                continue

            # Read TPAG item for font
            tpag_ptr = _u32(data, fp + 28)
            has_valid_tpag = False
            src_x = src_y = src_w = src_h = 0
            tex_page_id = -1
            if tpag_ptr > 0 and tpag_ptr + 22 <= len(data):
                src_x, src_y, src_w, src_h, tgt_x, tgt_y, tgt_w, tgt_h, bnd_w, bnd_h, tex_page_id = struct.unpack_from("<10Hh", data, tpag_ptr)
                has_valid_tpag = True

            # Attempt Pillow rendering if texture page is available
            rendered_via_pillow = False
            if has_valid_tpag and tex_page_id >= 0:
                blob_info = _get_txtr_blob_info(data, txtr_off, txtr_sz, txtr_ptrs, tex_page_id)
                if blob_info:
                    blob_ptr_pos, blob_off = blob_info
                    if tex_page_id not in page_images:
                        img = _decode_gm_texture(data[blob_off:])
                        if img is not None:
                            page_images[tex_page_id] = [img, False, blob_ptr_pos, blob_off, img.height]
                    if tex_page_id in page_images:
                        try:
                            from PIL import Image, ImageDraw, ImageFont
                            tex_img = page_images[tex_page_id][0]

                            cap_h = max(8, cap_glyph["gh"])
                            font_path = zpix_path if (anti_alias == 0 or cap_h <= 13) and os.path.exists(zpix_path) else noto_path

                            # Calibrate pil_size
                            pil_size = cap_h
                            if os.path.exists(font_path):
                                best_diff = 999
                                best_sz = cap_h
                                for sz in range(max(6, cap_h - 4), cap_h + 6):
                                    tf = ImageFont.truetype(font_path, sz)
                                    tb = tf.getbbox("А")
                                    diff = abs((tb[3] - tb[1]) - cap_h)
                                    if diff < best_diff:
                                        best_diff = diff
                                        best_sz = sz
                                pil_size = best_sz
                                pil_font = ImageFont.truetype(font_path, pil_size)
                            else:
                                pil_font = ImageFont.load_default()

                            # Check ALL-CAPS font
                            lower_a = next((g for g in glyphs if g["char"] == 97), None)
                            is_all_caps = (lower_a is None or (lower_a["gx"] == cap_glyph["gx"] and lower_a["gy"] == cap_glyph["gy"]))

                            orig_crop = tex_img.crop((src_x, src_y, src_x + src_w, src_y + src_h))
                            atlas_w = max(src_w, 256)
                            cur_x = 1
                            cur_y = src_h + 2

                            cyr_chars = [0x0401] + list(range(0x0410, 0x0450)) + [0x0451, 0x2116, 0x00AB, 0x00BB, 0x2014, 0x2026]
                            dummy_img = Image.new("RGBA", (10, 10))
                            dummy_draw = ImageDraw.Draw(dummy_img)

                            # Baseline & top metrics based on capital 'А' and Cyrillic glyphs
                            tb_cap = dummy_draw.textbbox((0, 0), "А", font=pil_font)
                            base_y = tb_cap[3]
                            all_tbs = [dummy_draw.textbbox((0, 0), chr(c), font=pil_font) for c in cyr_chars]
                            top_y = min(tb_cap[1], min(t[1] for t in all_tbs))

                            new_cyr_glyphs = []
                            for code in cyr_chars:
                                ch = chr(code)
                                if is_all_caps and 0x0430 <= code <= 0x044F:
                                    ch = ch.upper()
                                tb = dummy_draw.textbbox((0, 0), ch, font=pil_font)
                                gw = max(2, (tb[2] - tb[0]) + 1)
                                gh = max(2, (tb[3] - top_y) + 1)
                                new_cyr_glyphs.append((code, ch, gw, gh, tb))

                            row_h = max(gh for _, _, _, gh, _ in new_cyr_glyphs) + 4

                            positioned_glyphs = []
                            for code, ch, gw, gh, tb in new_cyr_glyphs:
                                if cur_x + gw + 2 >= atlas_w:
                                    cur_x = 1
                                    cur_y += row_h + 2
                                positioned_glyphs.append((code, ch, cur_x, cur_y, gw, gh, tb))
                                cur_x += gw + 2

                            atlas_h = cur_y + row_h + 4
                            font_canvas = Image.new("RGBA", (atlas_w, atlas_h), (0, 0, 0, 0))
                            font_canvas.paste(orig_crop, (0, 0))
                            cdraw = ImageDraw.Draw(font_canvas)

                            for code, ch, gx, gy, gw, gh, tb in positioned_glyphs:
                                cdraw.text((gx - tb[0], gy - top_y), ch, font=pil_font, fill=(255, 255, 255, 255))
                                glyphs.append({
                                    "char": code, "gx": gx, "gy": gy, "gw": gw, "gh": gh,
                                    "shift": gw + 1, "offset": 0, "kcount": 0, "kerning": b"",
                                })

                            if (
                                src_x == 0
                                and src_y == 0
                                and src_w == tex_img.width
                                and src_h == tex_img.height
                                and not page_images[tex_page_id][1]
                            ):
                                page_images[tex_page_id][0] = font_canvas
                                page_images[tex_page_id][1] = True
                                page_images[tex_page_id][4] = atlas_h + 2
                                struct.pack_into(
                                    "<10H", buf, tpag_ptr, 0, 0, atlas_w, atlas_h, 0, 0, atlas_w, atlas_h, atlas_w, atlas_h
                                )
                            else:
                                paste_y = page_images[tex_page_id][4]
                                expanded = Image.new(
                                    "RGBA",
                                    (max(tex_img.width, atlas_w), paste_y + atlas_h),
                                    (0, 0, 0, 0),
                                )
                                expanded.paste(tex_img, (0, 0))
                                expanded.paste(font_canvas, (0, paste_y))
                                page_images[tex_page_id][0] = expanded
                                page_images[tex_page_id][1] = True
                                page_images[tex_page_id][4] = paste_y + atlas_h + 2
                                struct.pack_into(
                                    "<10H",
                                    buf,
                                    tpag_ptr,
                                    0,
                                    paste_y,
                                    atlas_w,
                                    atlas_h,
                                    0,
                                    0,
                                    atlas_w,
                                    atlas_h,
                                    atlas_w,
                                    atlas_h,
                                )

                            rendered_via_pillow = True
                        except Exception as e:
                            logger.warning("Pillow rendering failed, using fallback: %s", e)

            if not rendered_via_pillow:
                # Fallback: copy 'A' metrics so the engine does not drop characters
                ref_gw = cap_glyph["gw"]
                ref_gh = cap_glyph["gh"]
                ref_shift = cap_glyph["shift"]
                ref_offset = cap_glyph["offset"]
                ref_gx = cap_glyph["gx"]
                ref_gy = cap_glyph["gy"]
                for code in list(range(0x0410, 0x0450)) + [0x0401, 0x0451]:
                    glyphs.append({
                        "char": code, "gx": ref_gx, "gy": ref_gy, "gw": ref_gw, "gh": ref_gh,
                        "shift": ref_shift, "offset": ref_offset, "kcount": 0, "kerning": b"",
                    })

            # For legacy runtimes (bytecode_version <= 14), alias CP1251 codes if free
            if bytecode_version <= 14:
                existing_codes = {g["char"] for g in glyphs}
                cp1251_map = {
                    0xA8: 0x0401,  # Ё
                    0xB8: 0x0451,  # ё
                }
                for c in range(0xC0, 0x100):
                    cp1251_map[c] = 0x0410 + (c - 0xC0)  # А..я
                for cp_code, uni_code in cp1251_map.items():
                    if cp_code not in existing_codes:
                        target_g = next((g for g in glyphs if g["char"] == uni_code), None)
                        if target_g:
                            cp_g = dict(target_g)
                            cp_g["char"] = cp_code
                            glyphs.append(cp_g)

            # Sort glyphs strictly by char code ascending (required for binary search std::lower_bound)
            glyphs.sort(key=lambda g: g["char"])

            # Build new UndertaleFont record
            new_fp = base_appended_off + len(appended_buf)
            new_font_ptrs[f_idx] = new_fp

            # Copy header up to gc_pos, updating range_end
            header_prefix = bytearray(data[fp:gc_pos])
            struct.pack_into("<I", header_prefix, 24, max(r_end, 0x0451))
            appended_buf.extend(header_prefix)

            # Write new glyph count and pointer table
            new_gc = len(glyphs)
            appended_buf.extend(struct.pack("<I", new_gc))
            g_table_start = len(appended_buf)
            appended_buf.extend(b"\x00" * (new_gc * 4))

            # Write glyphs
            new_g_ptrs = []
            for g in glyphs:
                gp_abs = base_appended_off + len(appended_buf)
                new_g_ptrs.append(gp_abs)
                appended_buf.extend(
                    struct.pack(
                        "<HHHHHhhH",
                        g["char"], g["gx"], g["gy"], g["gw"], g["gh"],
                        g["shift"], g["offset"], g["kcount"],
                    )
                )
                if g["kerning"]:
                    appended_buf.extend(g["kerning"])

            # Fill pointer table
            for i, gp_abs in enumerate(new_g_ptrs):
                struct.pack_into("<I", appended_buf, g_table_start + i * 4, gp_abs)

            # Align to 4 bytes
            pad = (4 - (len(appended_buf) % 4)) % 4
            appended_buf.extend(b"\x00" * pad)

        # Return modified texture page images: tex_id -> (blob_ptr_pos, old_blob_off, png_bytes, width, height)
        modified_pages: dict[int, tuple[int, int, bytes, int, int]] = {}
        for tex_id, (mimg, is_mod, blob_ptr_pos, old_blob_off, used_h) in page_images.items():
            if is_mod:
                pow2_w = _next_pow2(mimg.width)
                pow2_h = _next_pow2(mimg.height)
                if (pow2_w, pow2_h) != mimg.size:
                    padded = Image.new("RGBA", (pow2_w, pow2_h), (0, 0, 0, 0))
                    padded.paste(mimg, (0, 0))
                    mimg = padded
                bio = io.BytesIO()
                mimg.save(bio, format="PNG")
                png_bytes = bio.getvalue()
                modified_pages[tex_id] = (blob_ptr_pos, old_blob_off, png_bytes, mimg.width, mimg.height)

        return new_font_ptrs, appended_buf, modified_pages

    def _inject_external_file(self, root: str, rel_p: str, file_tr: dict[str, str]) -> int:
        full_p = os.path.join(root, rel_p.replace("/", os.sep))
        raw = read_backup_original(root, rel_p)
        if raw is None:
            with open(full_p, "rb") as f:
                raw = f.read()

        self.backup_file(root, full_p)
        lower = rel_p.lower()
        written = 0

        if lower.endswith(".json"):
            try:
                data = json.loads(raw.decode("utf-8"))
                for s in self._extract_external_files(root, [rel_p]):
                    if s.id in file_tr:
                        self._set_json_path(data, s.path, file_tr[s.id])
                        written += 1
                out_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                with open(full_p, "wb") as f:
                    f.write(out_bytes)
            except Exception as e:
                logger.warning("Failed injecting JSON %s: %s", rel_p, e)
        elif lower.endswith(".ini"):
            try:
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()
                tr_map = {}
                for s in self._extract_external_files(root, [rel_p]):
                    if s.id in file_tr and len(s.path) >= 2:
                        tr_map[(s.path[0], s.path[1])] = file_tr[s.id]

                curr_sec = "root"
                new_lines = []
                for line in lines:
                    sline = line.strip()
                    if sline.startswith("[") and sline.endswith("]"):
                        curr_sec = sline[1:-1]
                        new_lines.append(line)
                    elif "=" in line and not sline.startswith(";"):
                        k, v = line.split("=", 1)
                        key = k.strip()
                        if (curr_sec, key) in tr_map:
                            new_lines.append(f"{k}={tr_map[(curr_sec, key)]}")
                            written += 1
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)

                with open(full_p, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_lines))
            except Exception as e:
                logger.warning("Failed injecting INI %s: %s", rel_p, e)
        elif lower.endswith(".csv"):
            import csv
            try:
                text = raw.decode("utf-8-sig", errors="replace")
                lines = text.splitlines()
                reader = list(csv.reader(lines))
                tr_map = {}
                for s in self._extract_external_files(root, [rel_p]):
                    if s.id in file_tr and len(s.path) >= 2:
                        tr_map[(int(s.path[0]), int(s.path[1]))] = file_tr[s.id]
                for (r, c), val in tr_map.items():
                    if r < len(reader) and c < len(reader[r]):
                        reader[r][c] = val
                        written += 1
                out = io.StringIO()
                writer = csv.writer(out, lineterminator="\n")
                writer.writerows(reader)
                with open(full_p, "w", encoding="utf-8") as f:
                    f.write(out.getvalue())
            except Exception as e:
                logger.warning("Failed injecting CSV %s: %s", rel_p, e)
        elif lower.endswith(".txt"):
            try:
                lines = raw.decode("utf-8-sig", errors="replace").splitlines()
                tr_map = {}
                for s in self._extract_external_files(root, [rel_p]):
                    if s.id in file_tr and len(s.path) >= 1:
                        tr_map[int(s.path[0])] = file_tr[s.id]
                new_lines = []
                for line_idx, line in enumerate(lines):
                    if line_idx in tr_map:
                        new_lines.append(tr_map[line_idx])
                        written += 1
                    else:
                        new_lines.append(line)
                with open(full_p, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_lines))
            except Exception as e:
                logger.warning("Failed injecting TXT %s: %s", rel_p, e)
        return written

    def _set_json_path(self, data: Any, path: list[str], val: str) -> None:
        curr = data
        for p in path[:-1]:
            if isinstance(curr, dict):
                curr = curr[p]
            elif isinstance(curr, list):
                curr = curr[int(p)]
        last = path[-1]
        if isinstance(curr, dict):
            curr[last] = val
        elif isinstance(curr, list):
            curr[int(last)] = val
