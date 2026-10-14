"""Interprex Autonomous Supervisor & Translation Runner.

Engine-agnostic CLI and live-supervision agent for Interprex.
Allows the AI agent (and developers) to execute translations through the EXACT
same core engine used by the GUI application (TranslationScheduler + Parsers + Inject),
monitor workers, TPM sliding windows, HTTP 500/503/429 errors, quality, and syntax tags
in real-time across ANY supported game engine (Ren'Py, RPG Maker, Unity, Godot, Wolf, etc.).

Usage:
    # Run full translation with live supervision using saved app settings:
    python python-core/supervisor.py run --game "C:/path/to/game"

    # Run with overrides (e.g. adjust batch size or model):
    python python-core/supervisor.py run --game "C:/path/to/game" --batch-size 30 --threads 2 --model "gemma-4-31b-it"

    # Check status of a game project:
    python python-core/supervisor.py status --game "C:/path/to/game"

    # Run engine lint / validation check:
    python python-core/supervisor.py lint --game "C:/path/to/game"

    # Re-inject current project.json translations into game files:
    python python-core/supervisor.py inject --game "C:/path/to/game"
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure python-core is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Reconfigure stdout/stderr for clean UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from main import TranslateReq, TItem
from parsers import detect_engine, get_parser
from scheduler import TranslationScheduler


def parse_app_settings() -> Dict[str, Any]:
    """Read user preferences and API keys from the application's LocalStorage LevelDB."""
    db_dir = os.path.expanduser(
        r"~\AppData\Local\com.alexandr.interprex\EBWebView\Default\Local Storage\leveldb"
    )
    if not os.path.exists(db_dir):
        return {}

    settings: Dict[str, Any] = {}
    pattern = re.compile(rb"interprex\.([a-zA-Z0-9_\.]+)[^\x01]*\x01([^\x01]+)")
    known_providers = ["gemini", "openai", "openrouter", "nvidia", "custom", "anthropic", "deepseek", "ollama", "lmstudio", "kaggle"]
    provider_keys_arrays: Dict[str, List[str]] = {}

    db_files = glob.glob(os.path.join(db_dir, "*.ldb")) + glob.glob(os.path.join(db_dir, "*.log"))
    db_files.sort(key=lambda p: os.path.getmtime(p))
    for f in db_files:
        try:
            with open(f, "rb") as fp:
                raw = fp.read()
            for m in pattern.finditer(raw):
                k = m.group(1).decode("latin1", errors="ignore")
                v_bytes = m.group(2)
                v = v_bytes.decode("utf-8", errors="ignore").strip()
                settings[k] = v

            # Specially match arrays of keys: interprex.providerApiKey.<provider>...[ "..." ]
            for p in known_providers:
                arr_pattern = re.compile(rb"interprex\.providerApiKey\." + p.encode("ascii") + rb".*?(\[[^\]]+\])")
                for am in arr_pattern.finditer(raw):
                    try:
                        arr = json.loads(am.group(1).decode("utf-8"))
                        if isinstance(arr, list):
                            clean_arr = [k.strip() for k in arr if isinstance(k, str) and k.strip()]
                            if clean_arr:
                                provider_keys_arrays[p] = clean_arr
                    except Exception:
                        pass
        except Exception:
            continue

    settings["_provider_keys_arrays"] = provider_keys_arrays
    return settings


def resolve_provider_config(settings: Dict[str, Any], provider_override: Optional[str] = None) -> Dict[str, Any]:
    """Resolve active provider, model, API keys, threads, delay, and TPM."""
    provider = provider_override or settings.get("provider", "gemini")
    target_lang = settings.get("targetLang", "Russian")
    model = settings.get(f"providerModel.{provider}", "")
    threads = int(settings.get(f"providerThreads.{provider}", "1") or 1)
    rpm = float(settings.get(f"providerRpm.{provider}", "0") or 0)
    if provider == "gemini" and model and f"providerRpm.gemini.{model}" in settings:
        rpm = float(settings.get(f"providerRpm.gemini.{model}", "0") or 0)
    delay_seconds = (60.0 / rpm) if rpm > 0 else 0.0
    tpm_raw = float(settings.get(f"providerTpm.{provider}", "0") or 0)
    if provider == "gemini" and model and f"providerTpm.gemini.{model}" in settings:
        tpm_raw = float(settings.get(f"providerTpm.gemini.{model}", "0") or 0)
    # App stores TPM in thousands (e.g. 16 means 16,000)
    tpm_limit = int(tpm_raw * 1000) if 0 < tpm_raw < 10000 else int(tpm_raw)
    batch_size = int(settings.get("maxBatchSize", "30") or 30)

    # API keys extraction
    provider_arrays = settings.get("_provider_keys_arrays", {})
    api_keys: List[str] = provider_arrays.get(provider, [])

    if not api_keys:
        keys_raw = settings.get(f"providerApiKey.{provider}", "")
        if keys_raw:
            try:
                parsed = json.loads(keys_raw)
                if isinstance(parsed, list):
                    api_keys = [k.strip() for k in parsed if isinstance(k, str) and k.strip()]
                elif isinstance(parsed, str) and parsed.strip():
                    api_keys = [parsed.strip()]
            except Exception:
                if keys_raw.strip():
                    api_keys = [keys_raw.strip()]

    # Fallback to single providerApiKey
    if not api_keys and settings.get("providerApiKey"):
        k = settings.get("providerApiKey", "").strip()
        if k:
            api_keys = [k]

    return {
        "provider": provider,
        "target_lang": target_lang,
        "model": model,
        "threads": threads,
        "delay_seconds": delay_seconds,
        "tpm_limit": tpm_limit,
        "batch_size": batch_size,
        "api_keys": api_keys,
    }


def find_or_extract_strings(root: str, engine: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load existing project.json and extract_cache.json, or extract fresh strings via engine parser."""
    proj_dir = os.path.join(root, "Interprex")
    os.makedirs(proj_dir, exist_ok=True)
    proj_path = os.path.join(proj_dir, "project.json")
    cache_path = os.path.join(proj_dir, "extract_cache.json")

    parser = get_parser(engine)
    items_meta: Dict[str, Dict[str, Any]] = {}

    # Check extract cache
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            for item in cdata.get("strings", []):
                items_meta[item["id"]] = item
        except Exception:
            pass

    # Load or initialize project.json
    project_data: Dict[str, Any] = {
        "version": 1,
        "engine": engine,
        "root": root.replace("\\", "/"),
        "strings": {},
        "glossary": {},
    }

    if os.path.exists(proj_path):
        try:
            with open(proj_path, "r", encoding="utf-8") as f:
                project_data = json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load {proj_path}: {e}")

    # If project strings empty or cache missing, extract via parser
    if not project_data.get("strings") or not items_meta:
        print(f"[INFO] Extracting strings for engine '{engine}' from '{root}'...")
        extracted = parser.extract(root)
        for s in extracted:
            d = s.to_dict()
            items_meta[d["id"]] = d
            if d["id"] not in project_data["strings"]:
                project_data["strings"][d["id"]] = {
                    "original": d["original"],
                    "translated": "",
                    "approved": False,
                }

        # Save cache
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"strings": list(items_meta.values())}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # Save project.json if fresh
    if not os.path.exists(proj_path):
        _atomic_save_json(proj_path, project_data)

    # Ensure items_meta has entries for all strings in project_data
    items_list: List[Dict[str, Any]] = []
    for sid, sdata in project_data.get("strings", {}).items():
        meta = items_meta.get(sid, {})
        items_list.append({
            "id": sid,
            "text": sdata.get("original", ""),
            "original": sdata.get("original", ""),
            "translated": sdata.get("translated", ""),
            "context": meta.get("context", ""),
            "file": meta.get("file", "unknown"),
            "path": meta.get("path", []),
            "approved": sdata.get("approved", False),
        })

    return project_data, items_list


def _atomic_save_json(path: str, data: Any):
    """Save JSON atomically to avoid corruption on unexpected termination."""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        temp_name = tf.name
    os.replace(temp_name, path)


def run_translation(
    game_path: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_keys: Optional[List[str]] = None,
    batch_size: Optional[int] = None,
    threads: Optional[int] = None,
    tpm_limit: Optional[int] = None,
    delay_seconds: Optional[float] = None,
    target_lang: Optional[str] = None,
    force_all: bool = False,
    auto_inject: bool = True,
):
    """Run translation pipeline end-to-end with real-time supervision."""
    root = os.path.abspath(game_path)
    print("=" * 70)
    print(f"INTERPREX SUPERVISOR: TRANSLATION PIPELINE")
    print(f"Target Game: {root}")
    print("=" * 70)

    # 1. Engine Detection
    engine = detect_engine(root)
    if not engine or engine == "unknown":
        print(f"[ERROR] Could not detect game engine for path: {root}")
        return False
    print(f"[1/5] Engine Detected: '{engine.upper()}'")

    # 2. Config & Keys Resolution
    app_settings = parse_app_settings()
    config = resolve_provider_config(app_settings, provider_override=provider)

    active_provider = provider or config["provider"]
    active_model = model or config["model"]
    active_keys = api_keys or config["api_keys"]
    active_threads = threads or config["threads"]
    active_batch_size = batch_size or config["batch_size"]
    active_tpm = tpm_limit if tpm_limit is not None else config["tpm_limit"]
    active_delay = delay_seconds if delay_seconds is not None else config["delay_seconds"]
    active_lang = target_lang or config["target_lang"]

    if not active_keys and active_provider in ("gemini", "openrouter", "nvidia"):
        print(f"[ERROR] No API keys found for provider '{active_provider}'.")
        print("Please provide --key or configure keys in Interprex.")
        return False

    print(f"[2/5] Configuration:")
    print(f"      - Provider:     {active_provider}")
    print(f"      - Model:        {active_model or '(default)'}")
    print(f"      - Keys Count:   {len(active_keys)}")
    print(f"      - Workers/Key:  {active_threads} (Total workers: {active_threads * max(1, len(active_keys))})")
    print(f"      - Batch Size:   {active_batch_size} strings")
    print(f"      - TPM Limit:    {active_tpm:,} tokens/min per key" if active_tpm else "      - TPM Limit:    Off")
    print(f"      - Delay (RPM):  {active_delay:.1f}s" if active_delay else "      - Delay (RPM):  Off")
    print(f"      - Target Lang:  {active_lang}")

    # 3. Load Project & Filter Work
    project_data, items_list = find_or_extract_strings(root, engine)
    total_strings = len(items_list)

    if force_all:
        untranslated = items_list
    else:
        untranslated = [
            it for it in items_list
            if not it.get("translated") or it.get("translated") == it.get("original")
        ]

    already_done = total_strings - len(untranslated)
    print(f"[3/5] Strings Inventory:")
    print(f"      - Total:        {total_strings}")
    print(f"      - Translated:   {already_done} ({already_done * 100 / max(1, total_strings):.1f}%)")
    print(f"      - Pending:      {len(untranslated)}")

    if not untranslated:
        print("\n[INFO] All strings are already translated!")
        if auto_inject:
            _inject_and_validate(root, engine, project_data, active_lang)
        return True

    # 4. Prepare Scheduler Request
    req_items = [
        TItem(
            id=it["id"],
            text=it["text"],
            context=it["context"],
            file=it["file"],
            path=it["path"],
        )
        for it in untranslated
    ]

    req = TranslateReq(
        provider=active_provider,
        items=req_items,
        target_lang=active_lang,
        glossary=project_data.get("glossary", {}),
        api_keys=active_keys,
        model=active_model,
        max_batch_size=active_batch_size,
        root=root,
        engine=engine,
        threads=active_threads,
        delay_seconds=active_delay,
        tpm_limit=active_tpm,
        timeout_seconds=200.0,
    )

    print(f"\n[4/5] Launching Translation Scheduler ({len(req_items)} strings in flight)...")
    print("-" * 70)

    sched = TranslationScheduler(req, should_pause=lambda: False)
    proj_path = os.path.join(root, "Interprex", "project.json")

    start_time = time.time()
    batch_count = 0
    errors_logged = 0

    try:
        for raw_line in sched.stream():
            line = raw_line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue

            etype = evt.get("type")
            if etype == "progress":
                phase = evt.get("phase", "")
                w_idx = evt.get("worker_idx", 0)
                done = evt.get("done", 0)
                tot = evt.get("total", len(req_items))
                batches = evt.get("batches", 0)

                # Format TPM ledger if present
                tpm_str = ""
                if "tpm_used" in evt and "tpm_limit" in evt:
                    tpm_str = f" | TPM: {evt['tpm_used']:,}/{evt['tpm_limit']:,}"

                # Live status snapshot dictionary
                live_snapshot = {
                    "timestamp": time.time(),
                    "total": total_strings,
                    "done": already_done + done,
                    "pct": round((already_done + done) * 100 / max(1, total_strings), 1),
                    "batches_completed": batch_count,
                    "errors_logged": errors_logged,
                    "phase": phase,
                    "worker_idx": w_idx,
                }
                live_file = os.path.join(root, "Interprex", "supervisor_live.json")
                try:
                    with open(live_file, "w", encoding="utf-8") as lf:
                        json.dump(live_snapshot, lf)
                except Exception:
                    pass

                # Batch completed
                if evt.get("translations"):
                    batch_count += 1
                    new_trans = evt["translations"]
                    # Update project data in memory
                    for sid, trans_text in new_trans.items():
                        if sid in project_data.get("strings", {}):
                            project_data["strings"][sid]["translated"] = trans_text

                    # Atomic save on every batch
                    _atomic_save_json(proj_path, project_data)

                    # Sample first translation for live quality inspection
                    sample_id = next(iter(new_trans))
                    orig_sample = project_data["strings"][sample_id]["original"]
                    trans_sample = new_trans[sample_id]

                    pct = (already_done + done) * 100 / max(1, total_strings)
                    print(f"\n[SUCCESS] Batch #{batch_count} landed ({len(new_trans)} strings) | Total: {already_done + done}/{total_strings} ({pct:.1f}%){tpm_str}", flush=True)
                    print(f"   [ORIG] {orig_sample[:80] + ('...' if len(orig_sample) > 80 else '')}", flush=True)
                    print(f"   [TRAN] {trans_sample[:80] + ('...' if len(trans_sample) > 80 else '')}", flush=True)

                    # Check for bracket/tag preservation
                    if ("{" in orig_sample and "{" not in trans_sample) or ("[" in orig_sample and "[" not in trans_sample):
                        print(f"   [WARN] Potential tag loss detected in batch #{batch_count}!", flush=True)

                elif phase == "translating_batch":
                    bsize = evt.get("batch_size", 0)
                    bnum = evt.get("batch_num", 0)
                    try_i = evt.get("try_i", 1)
                    try_str = f" (attempt #{try_i})" if try_i > 1 else ""
                    print(f"   [WORKER {w_idx}] Sending batch #{bnum} ({bsize} strings){try_str}...{tpm_str}", flush=True)

                elif phase == "waiting_tpm":
                    wait_s = evt.get("wait_left", 0)
                    print(f"   [WAIT-TPM] Worker {w_idx} waiting TPM slot ({wait_s:.0f}s left){tpm_str}", flush=True)

                elif phase == "waiting_delay":
                    wait_s = evt.get("wait_left", 0)
                    print(f"   [WAIT-RPM] Worker {w_idx} pacing delay ({wait_s:.1f}s left)", flush=True)

                elif phase == "waiting_retry":
                    wait_s = evt.get("wait_left", 0)
                    err_msg = evt.get("last_error", "Unknown error")
                    errors_logged += 1
                    print(f"   [RETRY] Worker {w_idx} attempt #{evt.get('try_i', 1)} failed: {err_msg} (waiting {wait_s:.0f}s)", flush=True)

                elif phase == "batch_error":
                    err_msg = evt.get("last_error", "Unknown error")
                    errors_logged += 1
                    print(f"   [ERROR] Worker {w_idx}: {err_msg}", flush=True)

            elif etype == "done":
                elapsed = time.time() - start_time
                usage = evt.get("usage", {})
                print("\n" + "-" * 70)
                print(f"[DONE] Translation stream finished in {elapsed:.1f}s!")
                print(f"       Prompt tokens:     {usage.get('prompt_tokens', 0):,}")
                print(f"       Completion tokens: {usage.get('completion_tokens', 0):,}")
                print(f"       Errors / Retries:  {errors_logged}")

                # Save final project state
                _atomic_save_json(proj_path, project_data)
                break

    except KeyboardInterrupt:
        print("\n[INTERRUPT] Translation paused by user. Progress saved safely to project.json.")
        return False

    # 5. Inject & Validate
    if auto_inject:
        _inject_and_validate(root, engine, project_data, active_lang)

    return True


def _inject_and_validate(root: str, engine: str, project_data: Dict[str, Any], target_lang: str):
    """Inject translations into game files and run engine lint."""
    print("\n[5/5] Injecting translations into game files...")
    parser = get_parser(engine)

    translations = {
        sid: sdata.get("translated", "")
        for sid, sdata in project_data.get("strings", {}).items()
        if sdata.get("translated")
    }

    try:
        written = parser.inject(root, translations, target_lang, [])
        if hasattr(parser, "finalize_backups"):
            parser.finalize_backups(root)
        print(f"[INJECT] Written {written} files successfully.")
    except Exception as e:
        print(f"[ERROR] Failed to inject translations: {e}")
        return

    # Engine-specific validation
    if engine == "renpy":
        print("[LINT] Running Ren'Py engine lint check...")
        try:
            from parsers.renpy import lint_with_engine
            report = lint_with_engine(root, target_lang)
            ours = report.get("ours", [])
            print(f"[LINT] Verdict: {report.get('verdict', 'unknown')} | Actionable errors: {len(ours)}")
            if ours:
                for err in ours:
                    print(f"   [ERROR] {err.get('file', '')}:{err.get('line', '')} - {err.get('message', '')}")
            else:
                print("[LINT] PASS! 0 translation errors found by the game engine.")
        except Exception as e:
            print(f"[WARN] Ren'Py lint check skipped: {e}")


def show_status(game_path: str):
    """Display current translation status for a game."""
    root = os.path.abspath(game_path)
    engine = detect_engine(root)
    proj_path = os.path.join(root, "Interprex", "project.json")

    print("=" * 60)
    print(f"INTERPREX GAME STATUS")
    print(f"Path:   {root}")
    print(f"Engine: {engine}")
    print("=" * 60)

    if not os.path.exists(proj_path):
        print("No project.json found. Game has not been extracted yet.")
        return

    with open(proj_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    strings = data.get("strings", {})
    total = len(strings)
    translated = sum(1 for v in strings.values() if v.get("translated") and v.get("translated") != v.get("original"))
    pending = total - translated

    pct = translated * 100 / max(1, total)
    print(f"Total Strings:      {total}")
    print(f"Translated:         {translated} ({pct:.1f}%)")
    print(f"Pending:            {pending}")

    # Inspect last 3 translations
    trans_samples = [
        (v.get("original", ""), v.get("translated", ""))
        for v in strings.values()
        if v.get("translated") and v.get("translated") != v.get("original")
    ][-3:]

    if trans_samples:
        print("\nRecent Translations:")
        for orig, tran in trans_samples:
            print(f"  • ORIG: {orig[:60]}")
            print(f"    TRAN: {tran[:60]}")


def main():
    parser = argparse.ArgumentParser(description="Interprex Autonomous Supervisor")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Run command
    run_p = subparsers.add_parser("run", help="Run translation pipeline with live supervision")
    run_p.add_argument("--game", required=True, help="Path to game root folder")
    run_p.add_argument("--provider", help="LLM Provider (gemini, openrouter, nvidia, custom, ollama)")
    run_p.add_argument("--model", help="Model name (e.g. gemma-4-31b-it, gemini-2.5-flash)")
    run_p.add_argument("--key", action="append", dest="api_keys", help="API key(s) (can specify multiple)")
    run_p.add_argument("--batch-size", type=int, help="Batch size in strings")
    run_p.add_argument("--threads", type=int, help="Worker threads per key")
    run_p.add_argument("--tpm", type=int, help="TPM limit per key (e.g. 16000)")
    run_p.add_argument("--delay", type=float, help="Pacing delay in seconds")
    run_p.add_argument("--lang", help="Target language (default: Russian)")
    run_p.add_argument("--force-all", action="store_true", help="Re-translate all strings")
    run_p.add_argument("--no-inject", action="store_true", help="Skip injection step")

    # Status command
    stat_p = subparsers.add_parser("status", help="Show project translation status")
    stat_p.add_argument("--game", required=True, help="Path to game root folder")

    # Lint command
    lint_p = subparsers.add_parser("lint", help="Run engine lint / validation check")
    lint_p.add_argument("--game", required=True, help="Path to game root folder")
    lint_p.add_argument("--lang", default="Russian", help="Target language")

    # Inject command
    inj_p = subparsers.add_parser("inject", help="Re-inject translations into game files")
    inj_p.add_argument("--game", required=True, help="Path to game root folder")
    inj_p.add_argument("--lang", default="Russian", help="Target language")

    args = parser.parse_args()

    if args.command == "run":
        run_translation(
            game_path=args.game,
            provider=args.provider,
            model=args.model,
            api_keys=args.api_keys,
            batch_size=args.batch_size,
            threads=args.threads,
            tpm_limit=args.tpm,
            delay_seconds=args.delay,
            target_lang=args.lang,
            force_all=args.force_all,
            auto_inject=not args.no_inject,
        )
    elif args.command == "status":
        show_status(args.game)
    elif args.command == "lint":
        root = os.path.abspath(args.game)
        engine = detect_engine(root)
        if engine == "renpy":
            from parsers.renpy import lint_with_engine
            rep = lint_with_engine(root, args.lang)
            print(f"Lint verdict: {rep.get('verdict')} | Ours: {len(rep.get('ours', []))}")
        else:
            print(f"Engine '{engine}' does not require engine-level linting.")
    elif args.command == "inject":
        root = os.path.abspath(args.game)
        engine = detect_engine(root)
        proj_data, _ = find_or_extract_strings(root, engine)
        _inject_and_validate(root, engine, proj_data, args.lang)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
