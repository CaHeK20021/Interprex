"""Parser contract shared by every engine.

A parser does exactly two things and knows nothing about LLMs:
  extract(root) -> list[TranslationString]   read game files -> strings
  inject(root, translations)                 write translations back

The id algorithm here MUST stay byte-for-byte identical to makeId() in
src/lib/types.ts, otherwise ids drift between the two sides and saved
translations stop matching. It is FNV-1a (32-bit), hex, over
engine + \x00 + file + \x00 + "\x01".join(path) + \x00 + original.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict


# Folder Interprex writes its data into, inside the game root. Mirrors
# INTERPREX_DIR in src/lib/types.ts — keep the name in sync. Project file +
# caches live here so the game root stays clean (no scattered dotfiles).
INTERPREX_DIR = "Interprex"
# Project filename inside INTERPREX_DIR. Mirrors PROJECT_FILENAME in types.ts.
PROJECT_FILENAME = "project.json"


def interprex_dir(root: str) -> str:
    """Absolute path to the game's Interprex/ data folder (not created here)."""
    import os
    return os.path.join(root, INTERPREX_DIR)


def project_file_path(root: str) -> str:
    """Absolute path to the project file inside Interprex/."""
    import os
    return os.path.join(interprex_dir(root), PROJECT_FILENAME)


@dataclass
class TranslationString:
    id: str
    original: str
    context: str
    file: str            # relative to root, forward slashes
    path: list[str]
    engine: str

    def to_dict(self) -> dict:
        return asdict(self)


def make_id(engine: str, file: str, path: list[str], original: str) -> str:
    """Mirror of makeId() in src/lib/types.ts. Keep in sync."""
    path_str = "\x01".join(path)
    key = f"{engine}\x00{file}\x00{path_str}\x00{original}"
    h = 0x811C9DC5  # FNV offset basis
    for ch in key:
        h ^= ord(ch)
        # 32-bit FNV prime multiply, masked to stay unsigned 32-bit.
        h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) & 0xFFFFFFFF
    return format(h, "08x")


def update_metadata(root: str, rel_path: str, orig_sha: str, mod_sha: str, backup_type: str) -> None:
    import os
    import json
    metadata_path = os.path.join(root, ".interprex_backups", "metadata.json")
    
    # Load existing metadata
    metadata = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception:
            pass
            
    # Update entry
    metadata[rel_path] = {
        "orig_sha256": orig_sha,
        "mod_sha256": mod_sha,
        "type": backup_type
    }
    
    # Save atomically
    import tempfile
    dir_name = os.path.dirname(metadata_path)
    os.makedirs(dir_name, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_name, prefix="tmp_meta_", delete=False, encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        temp_path = f.name
        
    try:
        import time
        delays = [0.1, 0.2, 0.4, 0.8]
        for attempt, delay in enumerate(delays):
            try:
                os.replace(temp_path, metadata_path)
                break
            except PermissionError:
                if attempt == len(delays) - 1:
                    raise
                time.sleep(delay)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def read_backup_original(root: str, rel_path: str) -> bytes | None:
    """Return the ORIGINAL (pre-inject) bytes of `rel_path` from the backup, or
    None if there is no backup for it.

    Backups are stored as reverse `patch`es (utils/binary_diff IDXP format). So a
    parser that wants the original text back after an inject MUST decode through
    here — there is no plain copy on disk to read. Two on-disk states:

      - staged   `<rel>.orig_temp` exists  → inject ran but finalize_backups() has
                 not yet diffed it; the staged file IS the verbatim original.
      - finalized `<rel>.patch` exists      → reverse-patch it against the current
                 (modified) file to recover the original.

    `rel_path` uses forward slashes, matching the metadata keys."""
    import os
    import json

    backup_dir = os.path.join(root, ".interprex_backups")
    metadata_path = os.path.join(backup_dir, "metadata.json")
    if not os.path.exists(metadata_path):
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception:
        return None

    if rel_path not in metadata:
        return None

    rel_os = rel_path.replace("/", os.sep)
    try:
        # Staged original (pre-finalize) — read verbatim.
        orig_temp = os.path.join(backup_dir, rel_os + ".orig_temp")
        if os.path.exists(orig_temp):
            with open(orig_temp, "rb") as f:
                return f.read()

        # Finalized reverse patch — undo it against the current file.
        patch_file = os.path.join(backup_dir, rel_os + ".patch")
        if os.path.exists(patch_file):
            import sys
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
            from utils.binary_diff import reverse_patch
            target_file = os.path.join(root, rel_os)
            with open(patch_file, "rb") as f:
                patch_bytes = f.read()
            with open(target_file, "rb") as f:
                mod_bytes = f.read()
            return reverse_patch(mod_bytes, patch_bytes, strict=True)
    except Exception:
        return None
    return None


class BaseParser(ABC):
    """Subclass per engine. Each subclass sets `engine` as a class attribute
    to its stable Engine string (e.g. "rpgmaker")."""

    engine: str = ""

    def __init__(self) -> None:
        self._current_root: str = ""
        self._pending_deltas: dict[str, bytes] = {}

    def backup_file(self, root: str, fpath: str) -> None:
        """Stage the TRUE pre-Interprex original for `fpath` before mutation.

        Backups are reverse patches (IDXP in ``utils/binary_diff``):
          1. ``backup_file`` writes ``<rel>.orig_temp`` = true original bytes
          2. inject mutates the live game file
          3. ``finalize_backups`` diffs orig_temp → live into ``<rel>.patch``

        ⚠️ LOAD-BEARING: always stage ``.orig_temp`` for the upcoming write,
        even when a finalized ``.patch`` already exists and live still matches
        orig/mod. The old code early-returned in that case → inject mutated
        live with no pending stage → finalize had nothing to update → restore's
        ``reverse_patch`` failed because live ≠ patch.mod_sha. That is why
        Unity backups "never worked after translate" (DLL identity patch +
        later inject; resources.assets multi-pass raw/font writes).

        Recovery order for the bytes we stage:
          • ``.orig_temp`` already present this session → keep (first writer wins)
          • live == metadata.orig_sha → live IS the original
          • reverse_patch(live, .patch) yields metadata.orig_sha → use that
          • else re-baseline on live (best effort; warn if we lost the original)
        """
        import os
        import sys
        import hashlib
        import json

        if not root or not fpath:
            return

        backup_dir = os.path.join(root, ".interprex_backups")
        try:
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
        except ValueError:
            return

        backup_fpath = os.path.join(backup_dir, rel_path)
        orig_temp_path = backup_fpath + ".orig_temp"
        metadata_path = os.path.join(backup_dir, "metadata.json")

        # Already staged for this inject session — do NOT overwrite. The first
        # backup_file call holds the true original; later calls (font pass,
        # second asset write) must not replace it with already-mutated live.
        if os.path.exists(orig_temp_path):
            return

        if not os.path.exists(fpath):
            return

        try:
            with open(fpath, "rb") as f:
                live_bytes = f.read()
        except Exception:
            return
        live_sha = hashlib.sha256(live_bytes).hexdigest()

        true_orig: bytes | None = None
        true_orig_sha = ""
        lost_original = False

        info = None
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                info = metadata.get(rel_path)
            except Exception:
                info = None

        if info and info.get("type") != "created":
            orig_sha_meta = info.get("orig_sha256") or ""
            if orig_sha_meta and live_sha == orig_sha_meta:
                # Pristine game file — live is the original.
                true_orig = live_bytes
                true_orig_sha = live_sha
            elif orig_sha_meta:
                # Live is translated (or mid-edit). Recover the recorded original
                # via reverse_patch / leftover stage — NOT by snapshotting live.
                recovered = read_backup_original(root, rel_path)
                if recovered is not None:
                    rec_sha = hashlib.sha256(recovered).hexdigest()
                    if rec_sha == orig_sha_meta:
                        true_orig = recovered
                        true_orig_sha = orig_sha_meta
                if true_orig is None:
                    # Patch is stale/identity and live drifted (classic Unity
                    # failure mode). Cannot invent the pre-Interprex bytes.
                    lost_original = True

        if true_orig is None:
            true_orig = live_bytes
            true_orig_sha = live_sha
            if lost_original:
                print(
                    f"[backup] WARNING: cannot recover pre-Interprex original for "
                    f"{rel_path}; re-baselining on current file. 'Restore' will "
                    f"not yield the vanilla game file for this path — verify "
                    f"game files in Steam/GOG if you need a true original.",
                    file=sys.stderr,
                )

        # Ensure gitignore exists
        gitignore_path = os.path.join(backup_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            try:
                os.makedirs(backup_dir, exist_ok=True)
                with open(gitignore_path, "w", encoding="utf-8") as f:
                    f.write("*\n")
            except Exception:
                pass

        def do_write_temp(payload: bytes) -> None:
            import tempfile
            dir_name = os.path.dirname(orig_temp_path)
            os.makedirs(dir_name, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=dir_name, prefix="tmp_orig_", delete=False
            ) as tf:
                tf.write(payload)
                temp_path = tf.name
            try:
                import time
                delays = [0.1, 0.2, 0.4, 0.8]
                for attempt, delay in enumerate(delays):
                    try:
                        os.replace(temp_path, orig_temp_path)
                        break
                    except PermissionError:
                        if attempt == len(delays) - 1:
                            raise
                        time.sleep(delay)
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise

        try:
            do_write_temp(true_orig)
            # mod_sha="" marks "pending finalize" — restore uses .orig_temp
            # verbatim until finalize writes the reverse patch.
            update_metadata(root, rel_path, true_orig_sha, "", "patch")
        except Exception as e:
            print(f"[backup] failed to stage {rel_path}: {e}", file=sys.stderr)

    def finalize_backups(self, root: str) -> None:
        """After inject: turn every staged ``.orig_temp`` into a reverse ``.patch``.

        Verifies ``reverse_patch(live, patch) == orig`` before dropping the
        staged original, so a bad patch can never silently replace a good stage.
        """
        import os
        import sys
        import hashlib
        
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from utils.binary_diff import make_patch, reverse_patch
        
        backup_dir = os.path.join(root, ".interprex_backups")
        if not os.path.isdir(backup_dir):
            return
            
        # Scan backup_dir for any .orig_temp files to compile into patches
        for dirpath, _, filenames in os.walk(backup_dir):
            for filename in filenames:
                if filename.endswith(".orig_temp"):
                    orig_temp_fpath = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(orig_temp_fpath, backup_dir).replace("\\", "/")
                    if rel_path.endswith(".orig_temp"):
                        rel_path = rel_path[:-10]
                        
                    fpath = os.path.join(root, rel_path)
                    if not os.path.exists(fpath):
                        continue
                        
                    try:
                        with open(orig_temp_fpath, "rb") as f:
                            orig_bytes = f.read()
                        with open(fpath, "rb") as f:
                            mod_bytes = f.read()
                    except Exception:
                        continue
                        
                    try:
                        patch = make_patch(orig_bytes, mod_bytes)
                        # Round-trip check: restore must work against CURRENT live.
                        try:
                            check = reverse_patch(mod_bytes, patch, strict=True)
                            if check != orig_bytes:
                                raise ValueError(
                                    "reverse_patch round-trip mismatch "
                                    f"(orig {len(orig_bytes)}B vs recovered {len(check)}B)"
                                )
                        except Exception as ver_err:
                            print(
                                f"[backup] refuse to finalize {rel_path}: patch does not "
                                f"round-trip ({ver_err}). Keeping .orig_temp so restore "
                                f"still works.",
                                file=sys.stderr,
                            )
                            continue

                        patch_fpath = os.path.join(backup_dir, rel_path + ".patch")
                        
                        import tempfile
                        dir_name = os.path.dirname(patch_fpath)
                        os.makedirs(dir_name, exist_ok=True)
                        with tempfile.NamedTemporaryFile("wb", dir=dir_name, prefix="tmp_patch_", delete=False) as tf:
                            tf.write(patch)
                            temp_path = tf.name
                        try:
                            import time
                            delays = [0.1, 0.2, 0.4, 0.8]
                            for attempt, delay in enumerate(delays):
                                try:
                                    os.replace(temp_path, patch_fpath)
                                    break
                                except PermissionError:
                                    if attempt == len(delays) - 1:
                                        raise
                                    time.sleep(delay)
                        except Exception:
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                            raise
                            
                        orig_sha = hashlib.sha256(orig_bytes).hexdigest()
                        mod_sha = hashlib.sha256(mod_bytes).hexdigest()
                        update_metadata(root, rel_path, orig_sha, mod_sha, "patch")
                        
                        # Remove the temporary original file only after a verified patch.
                        import time
                        delays = [0.1, 0.2, 0.4, 0.8]
                        for attempt, delay in enumerate(delays):
                            try:
                                os.remove(orig_temp_fpath)
                                break
                            except PermissionError:
                                if attempt == len(delays) - 1:
                                    pass
                                time.sleep(delay)
                    except Exception as e:
                        print(f"Error creating patch for {fpath}: {e}", file=sys.stderr)

    def write_patch_replacements(self, root: str, fpath: str, replacements: list[tuple[int, bytes, bytes]]) -> None:
        """Helper to apply a list of replacements (offset, orig_bytes, mod_bytes) to fpath.
        Validates overlaps, sorts replacements, creates backups/patches, and updates the file."""
        import os
        import hashlib
        
        if not replacements:
            return
            
        # 1. Sort replacements by offset
        replacements = sorted(replacements, key=lambda x: x[0])
        
        # 2. Assert no overlaps
        for idx in range(len(replacements) - 1):
            curr_offset, curr_orig, _ = replacements[idx]
            next_offset, _, _ = replacements[idx + 1]
            if curr_offset + len(curr_orig) > next_offset:
                raise ValueError(
                    f"Overlapping replacements detected at offset {curr_offset} "
                    f"and {next_offset}"
                )
                
        # 3. Read original bytes
        with open(fpath, "rb") as f:
            orig_bytes = f.read()
            
        # 4. Backup original file
        self.backup_file(root, fpath)
        
        # 5. Apply replacements to build modified bytes
        buf = bytearray(orig_bytes)
        accumulated_shift = 0
        for offset, orig, mod in replacements:
            target_start = offset + accumulated_shift
            target_end = target_start + len(orig)
            
            if target_end > len(buf):
                raise ValueError("Replacement bounds out of file range")
                
            if buf[target_start:target_end] != orig:
                raise ValueError(f"Original content mismatch at offset {offset}")
                
            buf[target_start:target_end] = mod
            accumulated_shift += len(mod) - len(orig)
            
        mod_bytes = bytes(buf)
        
        # 6. Write new file atomically with retry loop
        import tempfile
        dir_name = os.path.dirname(fpath)
        os.makedirs(dir_name, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=dir_name, prefix="tmp_write_", delete=False) as tf:
            tf.write(mod_bytes)
            temp_path = tf.name
            
        try:
            import time
            delays = [0.1, 0.2, 0.4, 0.8]
            for attempt, delay in enumerate(delays):
                try:
                    os.replace(temp_path, fpath)
                    break
                except PermissionError:
                    if attempt == len(delays) - 1:
                        raise
                    time.sleep(delay)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def engine_prompt_addon(self) -> str:
        """Engine-specific instructions appended to the core system prompt.

        Override in a subclass to add rules the model should follow for THIS
        engine only (control codes, tone, format specifiers, etc.). The returned
        string is appended after the core prompt and before the glossary/payload,
        so it can be a plain paragraph or a bullet list — whatever is clearest.

        Return "" (the default) for engines that need no extra instructions."""
        return ""

    @staticmethod
    @abstractmethod
    def detect(root: str) -> bool:
        """True if this engine's project lives at `root`."""
        raise NotImplementedError

    @abstractmethod
    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        """Read every translatable string out of the project."""
        raise NotImplementedError

    @abstractmethod
    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        """Write {id: translated} back into the files. Returns count written."""
        raise NotImplementedError

    def _mk(self, file: str, path: list[str], original: str, context: str = "") -> TranslationString:
        return TranslationString(
            id=make_id(self.engine, file, path, original),
            original=original,
            context=context,
            file=file,
            path=path,
            engine=self.engine,
        )
