"""Test with REAL delay to simulate cloud provider latency.
This is where the ramp-down bug would actually bite."""
import json
import math
import os
import sys
import threading
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

import scheduler
from providers.base import TranslateResult, Usage

_orig_claim = scheduler.TranslationScheduler.claim_batch
_metrics = []
_mlock = threading.Lock()


def _patched_claim(self, worker_idx, cal, worker_key):
    result = _orig_claim(self, worker_idx, cal, worker_key)
    kind = result[0]
    with self.cond:
        rem = self._remaining_locked()
        done = len(self.result)
        in_fl = self.in_flight
        free_w = sum(
            1 for i in range(self.worker_count)
            if i not in self.in_flight_workers
            and self.keys_to_use[i // self.threads] not in self.dead_keys
        )
        b_est = max(1, math.ceil(rem / max(1.0, self.avg_batch_items)))
        rank = self._effective_rank_locked(worker_idx)
    with _mlock:
        _metrics.append({
            "w": worker_idx, "k": kind[0], "rem": rem, "done": done,
            "inf": in_fl, "free": free_w, "b": b_est, "r": rank,
            "ab": round(self.avg_batch_items, 2),
        })
    return result


scheduler.TranslationScheduler.claim_batch = _patched_claim


class SlowProvider:
    """Simulates a real cloud API with latency + batching."""
    name = "slow"

    def __init__(self, latency=0.1, batch_time_per_item=0.005):
        self.latency = latency
        self.batch_time = batch_time_per_item
        self.lock = threading.Lock()
        self.ts = defaultdict(list)
        self.rpm_limit = 0

    def set_rpm(self, rpm):
        self.rpm_limit = rpm

    def count_tokens(self, text, cfg):
        return None

    def translate(self, batch, lang, glossary, cfg):
        # Simulate real API latency
        time.sleep(self.latency + len(batch) * self.batch_time)

        # Real RPM enforcement (optional)
        if self.rpm_limit > 0:
            k = cfg.api_key
            now = time.time()
            with self.lock:
                self.ts[k].append(now)
                cutoff = now - 60.0
                self.ts[k] = [t for t in self.ts[k] if t > cutoff]
                if len(self.ts[k]) > self.rpm_limit:
                    raise RuntimeError("429 Too Many Requests")

        return TranslateResult({it.id: it.text + "_" + lang for it in batch},
                               Usage(len(batch) * 20, len(batch) * 10))


class Req:
    target_lang = "russian"; glossary = {}; base_url = ""; model = ""
    max_context_tokens = 0; max_batch_size = 5; root = ""; engine = ""
    free_only = False
    def __init__(self, items, threads=1, api_keys=None, delay_seconds=0.0,
                 max_batch_size=5):
        self.items = items; self.api_key = "K0"; self.api_key_2 = ""
        self.threads = threads; self.provider = "slow"
        self.delay_seconds = delay_seconds; self.api_keys = api_keys
        self.max_batch_size = max_batch_size


class It:
    def __init__(self, i, text, file="a.txt"):
        self.id = str(i); self.text = text; self.context = ""
        self.file = file; self.path = []


def analyze(label, metrics, n_items):
    if not metrics:
        print("  %s: no data" % label)
        return

    rest_work = [m for m in metrics if m["k"] == "r" and m["rem"] > 0]
    claim = [m for m in metrics if m["k"] == "c"]
    done_vals = [m["done"] for m in metrics]
    max_done = max(done_vals) if done_vals else 0

    # Find stall points: periods where done didn't increase
    # Group by 1-second buckets
    t0 = metrics[0]["done"]  # not time, just use done as proxy
    buckets = defaultdict(lambda: {"claims": 0, "rests": 0, "max_done": 0})
    for i, m in enumerate(metrics):
        b = i // max(1, len(metrics) // 20)
        buckets[b]["claims" if m["k"] == "c" else "rests"] += 1
        buckets[b]["max_done"] = max(buckets[b]["max_done"], m["done"])

    print("  %s: %d claims, %d rests_with_work, max_done=%d/%d" % (
        label, len(claim), len(rest_work), max_done, n_items))

    # Print bucket table
    print("    %5s %6s %5s %5s %5s" % ("bucket", "done", "claims", "rests", "free"))
    for b in sorted(buckets.keys()):
        d = buckets[b]
        # Get representative free_workers from this bucket
        bucket_metrics = metrics[b * max(1, len(metrics)//20):
                                 (b+1) * max(1, len(metrics)//20)]
        avg_free = sum(m["free"] for m in bucket_metrics) / max(1, len(bucket_metrics))
        print("    %5d %6d %5d %5d %5.1f" % (
            b, d["max_done"], d["claims"], d["rests"], avg_free))


def main():
    saved = (scheduler._RETRY_BACKOFF_FIRST, scheduler._RETRY_BACKOFF_REST,
             scheduler.get_provider)
    scheduler._RETRY_BACKOFF_FIRST = 0
    scheduler._RETRY_BACKOFF_REST = 0

    try:
        N = 5000  # smaller N so test finishes
        N_UNIQUE = 2000

        items = [It(i, "s%d" % (i % N_UNIQUE), file="sc_%03d.rpy" % (i % 200))
                 for i in range(N)]

        # Test 1: slow provider, 2 keys, 10 threads each, no delay
        print("=== SlowProvider(100ms), 2 keys, 10t ===")
        _metrics.clear()
        prov = SlowProvider(latency=0.1)
        req = Req(items, threads=10, api_keys=["K0", "K1"])
        scheduler.get_provider = lambda n: prov
        sched = scheduler.TranslationScheduler(req, should_pause=lambda: False)
        t0 = time.time()
        out = None
        for line in sched.stream():
            evt = json.loads(line)
            if evt["type"] == "done":
                out = evt
        elapsed = time.time() - t0
        n = len(out["translations"]) if out else 0
        analyze("100ms/2k/20t", _metrics, N)
        print("    total: %d/%d  %.1fs" % (n, N, elapsed))

        # Test 2: slow provider, 2 keys, 10 threads, delay_seconds matching RPM
        print("\n=== SlowProvider(100ms) + delay=12s, 2 keys, 10t, RPM=50 ===")
        _metrics.clear()
        prov = SlowProvider(latency=0.1)
        prov.set_rpm(50)
        delay = 10 * 60 / 50  # 12s
        req = Req(items, threads=10, api_keys=["K0", "K1"], delay_seconds=delay)
        scheduler.get_provider = lambda n: prov
        sched = scheduler.TranslationScheduler(req, should_pause=lambda: False)
        t0 = time.time()
        out = None
        for line in sched.stream():
            evt = json.loads(line)
            if evt["type"] == "done":
                out = evt
        elapsed = time.time() - t0
        n = len(out["translations"]) if out else 0
        analyze("100ms+12s/2k/20t", _metrics, N)
        print("    total: %d/%d  %.1fs" % (n, N, elapsed))

        # Test 3: Very slow provider (simulates slow model), 5 keys
        print("\n=== SlowProvider(500ms), 5 keys, 4t each ===")
        _metrics.clear()
        prov = SlowProvider(latency=0.5)
        req = Req(items, threads=4, api_keys=["K0","K1","K2","K3","K4"])
        scheduler.get_provider = lambda n: prov
        sched = scheduler.TranslationScheduler(req, should_pause=lambda: False)
        t0 = time.time()
        out = None
        for line in sched.stream():
            evt = json.loads(line)
            if evt["type"] == "done":
                out = evt
        elapsed = time.time() - t0
        n = len(out["translations"]) if out else 0
        analyze("500ms/2k/20t", _metrics, N)
        print("    total: %d/%d  %.1fs" % (n, N, elapsed))

        # Test 4: Simulate user's exact numbers (scaled down)
        # 65k -> 6500, 30k unique -> 3000, 200 files, 2 keys, 10t
        print("\n=== User scenario (6500/3000uni/200files/2k/10t/100ms) ===")
        _metrics.clear()
        items_user = [It(i, "u%d" % (i % 3000), file="sc_%03d.rpy" % (i % 200))
                      for i in range(6500)]
        prov = SlowProvider(latency=0.1)
        req = Req(items_user, threads=10, api_keys=["K0", "K1"])
        scheduler.get_provider = lambda n: prov
        sched = scheduler.TranslationScheduler(req, should_pause=lambda: False)
        t0 = time.time()
        out = None
        for line in sched.stream():
            evt = json.loads(line)
            if evt["type"] == "done":
                out = evt
        elapsed = time.time() - t0
        n = len(out["translations"]) if out else 0
        analyze("user/6500", _metrics, 6500)
        print("    total: %d/%d  %.1fs" % (n, 6500, elapsed))

    finally:
        (scheduler._RETRY_BACKOFF_FIRST, scheduler._RETRY_BACKOFF_REST,
             scheduler.get_provider) = saved
        scheduler.TranslationScheduler.claim_batch = _orig_claim


if __name__ == "__main__":
    main()
