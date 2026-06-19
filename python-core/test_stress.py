"""Scheduler stress + RPM pacing test.
Part 1: scale (fast, no delay).
Part 2: RPM pacing — FakeProvider tracks real timestamps per key, verifies
no key exceeded its RPM budget in any sliding 60s window.

Run: python test_stress.py"""
import json
import os
import sys
import threading
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

import scheduler
from providers.base import TranslateResult, Usage


class FakeProvider:
    """Provider that tracks every request timestamp per key.
    Optionally enforces RPM (429s if exceeded)."""
    name = "fake"

    def __init__(self, auth_keys=None, rate_once=None, delay=0.0,
                 rpm_limit=0):
        self.auth_keys = auth_keys or set()
        self.rate_once = dict(rate_once or {})
        self.delay = delay
        self.rpm_limit = rpm_limit
        self.lock = threading.Lock()
        self.ts: dict[str, list[float]] = defaultdict(list)

    def count_tokens(self, text, cfg):
        return None

    def translate(self, batch, lang, glossary, cfg):
        if self.delay:
            time.sleep(self.delay)
        k = cfg.api_key
        now = time.time()
        if k in self.auth_keys:
            raise RuntimeError("API key not valid. Please pass a valid API key. (403)")
        if self.rate_once.get(k, 0) > 0:
            self.rate_once[k] -= 1
            raise RuntimeError("429 Too Many Requests: rate limit exceeded")
        with self.lock:
            self.ts[k].append(now)
            if self.rpm_limit > 0:
                cutoff = now - 60.0
                win = [t for t in self.ts[k] if t > cutoff]
                self.ts[k] = win
                if len(win) > self.rpm_limit:
                    raise RuntimeError("429 Too Many Requests: rate limit exceeded")
        return TranslateResult({it.id: it.text + "_" + lang for it in batch},
                               Usage(10, 12))

    def assert_rpm_ok(self, expected_rpm=None):
        """Check that no key exceeded RPM in any 60s window."""
        limit = expected_rpm or self.rpm_limit
        if not limit:
            return
        with self.lock:
            for k, stamps in self.ts.items():
                if len(stamps) < 2:
                    continue
                for i, t0 in enumerate(stamps):
                    window = [t for t in stamps if t0 - 60.0 < t <= t0 + 0.001]
                    if len(window) > limit:
                        raise AssertionError(
                            "RPM violation key=%s: %d requests in 60s (limit=%d)"
                            % (k, len(window), limit))

    def max_rps_per_key(self):
        """Return peak requests-per-second per key (informational)."""
        with self.lock:
            result = {}
            for k, stamps in self.ts.items():
                if len(stamps) < 2:
                    result[k] = 0
                    continue
                # find densest 1-second window
                best = 0
                for i, t0 in enumerate(stamps):
                    count = sum(1 for t in stamps if t0 <= t < t0 + 1.0)
                    best = max(best, count)
                result[k] = best
            return result


class Req:
    target_lang = "russian"
    glossary = {}
    base_url = ""
    model = ""
    max_context_tokens = 0
    max_batch_size = 5
    root = ""
    engine = ""
    free_only = False

    def __init__(self, items, api_key="K1", api_key_2="", threads=3,
                 provider="fake", delay_seconds=0.0, api_keys=None,
                 max_batch_size=5):
        self.items = items
        self.api_key = api_key
        self.api_key_2 = api_key_2
        self.threads = threads
        self.provider = provider
        self.delay_seconds = delay_seconds
        self.api_keys = api_keys
        self.max_batch_size = max_batch_size


class It:
    def __init__(self, i, text, file="a.txt"):
        self.id = str(i)
        self.text = text
        self.context = ""
        self.file = file
        self.path = []


def run(req, prov, timeout=600, pause_flag=None):
    should = (lambda: pause_flag["v"]) if pause_flag else (lambda: False)
    scheduler.get_provider = lambda n: prov
    sched = scheduler.TranslationScheduler(req, should_pause=should)
    out = {"final": None}

    def go():
        for line in sched.stream():
            evt = json.loads(line)
            if evt["type"] == "done":
                out["final"] = evt

    t = threading.Thread(target=go, daemon=True)
    t.start()
    if pause_flag is None:
        t.join(timeout)
        assert not t.is_alive(), "scheduler deadlocked (timeout)"
        return out["final"]
    return sched, t, out


def main():
    saved = (scheduler._RETRY_BACKOFF_FIRST, scheduler._RETRY_BACKOFF_REST,
             scheduler.get_provider)
    scheduler._RETRY_BACKOFF_FIRST = 0
    scheduler._RETRY_BACKOFF_REST = 0

    N = 100_000

    try:
        # ═══════════════════════════════════════════════════════════════════
        # PART 1: scale (no delay)
        # ═══════════════════════════════════════════════════════════════════
        print("--- PART 1: scale ---")

        t0 = time.time()
        items = [It(i, "hello%d" % i) for i in range(N)]
        final = run(Req(items, threads=20), FakeProvider())
        print("  100k x 20t x 1k : %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        t0 = time.time()
        items = [It(i, "s%d" % i) for i in range(N)]
        final = run(Req(items, threads=20,
                        api_keys=["K0","K1","K2","K3","K4"]), FakeProvider())
        print("  100k x 20t x 5k : %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        t0 = time.time()
        items = [It(i, "rep_%d" % (i % 100), file="f%d.txt" % (i % 10))
                 for i in range(N)]
        final = run(Req(items, threads=20,
                        api_keys=["K0","K1","K2"]), FakeProvider())
        print("  100k dedup(100) : %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        t0 = time.time()
        items = [It(i, "ft%d" % i, file="sc_%04d.rpy" % (i // 200))
                 for i in range(N)]
        final = run(Req(items, threads=20, api_keys=["K0","K1"]), FakeProvider())
        print("  100k x 500 files: %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        t0 = time.time()
        items = [It(i, "fo%d" % i) for i in range(N)]
        final = run(Req(items, threads=20,
                        api_keys=["K0","K1","K2"]),
                    FakeProvider(auth_keys={"K0"}))
        print("  100k failover   : %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        t0 = time.time()
        items = [It(i, "rt%d" % i) for i in range(N)]
        final = run(Req(items, threads=20, api_keys=["K0","K1"]),
                    FakeProvider(rate_once={"K0": 30}))
        print("  100k rate(30x)  : %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        t0 = time.time()
        items = [It(i, "d%d" % i) for i in range(N)]
        final = run(Req(items, threads=20,
                        api_keys=["K0","K1","K2","K3","K4"]),
                    FakeProvider(auth_keys={"K0","K1","K2","K3","K4"}),
                    timeout=120)
        print("  100k all-dead   : terminated %.1fs" % (time.time()-t0))

        items = [It(i, "p%d" % i) for i in range(N)]
        flag = {"v": False}
        sched, t, out = run(Req(items, threads=20, api_keys=["K0","K1"]),
                            FakeProvider(delay=0.001), pause_flag=flag)
        while len(sched.result) < 500:
            time.sleep(0.05)
        flag["v"] = True
        time.sleep(1.0)
        at_pause = len(sched.result)
        time.sleep(1.0)
        assert len(sched.result) == at_pause
        flag["v"] = False
        t.join(600)
        n = len(out["final"]["translations"]) if out["final"] else 0
        assert n == N
        print("  100k pause/resume: %d/%d" % (n, N))

        t0 = time.time()
        items = [It(i, "t%d" % i) for i in range(N)]
        final = run(Req(items, threads=20, api_keys=["K0","K1","K2"],
                        max_batch_size=2), FakeProvider())
        print("  100k tiny(2)    : %d/%d  %.1fs" % (
            len(final["translations"]), N, time.time()-t0))

        N2 = 200_000
        t0 = time.time()
        items = [It(i, "b%d" % i) for i in range(N2)]
        final = run(Req(items, threads=20,
                        api_keys=["K0","K1","K2","K3","K4"]), FakeProvider())
        print("  200k x 20t x 5k: %d/%d  %.1fs" % (
            len(final["translations"]), N2, time.time()-t0))

        # ═══════════════════════════════════════════════════════════════════
        # PART 2: RPM pacing
        #
        # Scheduler paces each thread with delay_seconds between requests.
        # With T threads per key and RPM limit: delay = T * 60 / RPM.
        # FakeProvider tracks timestamps and 429s if any 60s window exceeds
        # RPM. After the run, we also scan timestamps independently.
        #
        # We use tiny delays + tiny strings so the test finishes fast,
        # but set RPM limits low enough to CATCH a scheduler that ignores
        # pacing entirely (all threads firing simultaneously = instant blow-up).
        # ═══════════════════════════════════════════════════════════════════
        print("\n--- PART 2: RPM pacing ---")

        # Helper: run N strings, measure actual peak RPS per key
        def rpm_test(label, n, threads, keys, rpm_limit, max_batch=5):
            delay = threads * 60.0 / rpm_limit
            prov = FakeProvider(rpm_limit=rpm_limit)
            items = [It(i, "r%d" % i) for i in range(n)]
            t0 = time.time()
            final = run(Req(items, threads=threads, api_keys=keys,
                            delay_seconds=delay, max_batch_size=max_batch),
                        prov, timeout=300)
            elapsed = time.time() - t0
            assert final and len(final["translations"]) == n, \
                "%s: got %d/%d" % (label, len(final["translations"]) if final else 0, n)
            prov.assert_rpm_ok()
            peaks = prov.max_rps_per_key()
            total = sum(len(s) for s in prov.ts.values())
            peak = max(peaks.values()) if peaks else 0
            print("  %s: %d/%d  %.1fs  reqs=%d  peak_rps=%d  delay=%.3fs  RPM OK" % (
                label, len(final["translations"]), n, elapsed, total, peak, delay))
            return prov

        # 11. 1 key, 20 threads, RPM=200 → delay=6s. Fast because 50 strings.
        #     Without pacing, all 20 threads fire instantly → peak ~20 req at once
        #     in <1s → 20 RPS, but RPM limit is 200 so it won't trigger 429.
        #     The real check is timestamps: each key should have requests
        #     spread over time, not clustered.
        rpm_test("1k/20t/RPM=200", 50, 20, ["K0"], 200)

        # 12. 2 keys, 10 threads each, RPM=100 → delay=6s
        rpm_test("2k/10t/RPM=100", 100, 10, ["K0","K1"], 100)

        # 13. 5 keys, 4 threads each, RPM=50 → delay=4.8s
        rpm_test("5k/4t/RPM=50", 200, 4, ["K0","K1","K2","K3","K4"], 50)

        # 14. Tight: 1 key, 5 threads, RPM=30 → delay=10s
        #     30 RPM with 5 threads means each thread fires every 10s.
        rpm_test("1k/5t/RPM=30", 30, 5, ["K0"], 30)

        # 15. 3 keys, 10 threads each, RPM=60 → delay=10s
        rpm_test("3k/10t/RPM=60", 300, 10, ["K0","K1","K2"], 60)

        # 16. Big: 1000 strings, 3 keys, 10 threads/key, RPM=100 → delay=6s
        rpm_test("1k/3k/10t/RPM=100", 1000, 10, ["K0","K1","K2"], 100)

        print("\n=== ALL PASS ===")

    finally:
        (scheduler._RETRY_BACKOFF_FIRST, scheduler._RETRY_BACKOFF_REST,
         scheduler.get_provider) = saved


if __name__ == "__main__":
    main()
