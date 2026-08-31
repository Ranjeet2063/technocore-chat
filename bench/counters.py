"""Why the lifetime counters shard by writer, and why that width is 8.

Run: uv run python bench/counters.py

`_bump` is called by every write in the service — every message append, every note write, and
the reaper's tally — and it used to take one exclusive flock on `.counters` and do a
read-parse-modify-replace inside it. So writes to unrelated rooms serialised against each
other: on technocore.chat (0.11.1, 6 vCPU, WEB_CONCURRENCY=5, ~316 req/s, ~135 writes/s)
`flock` was 63-81% of a worker's syscall time at ~95 ms mean, sampled eight times across two
REAP_EVERY cycles with no periodic component — per-write contention, not the reaper (#588).

The claim being tested is about CONTENTION, which one process cannot show. So the headline
number here is concurrent throughput across real processes, and the baseline is the previous
implementation itself — `_global_bump` below is byte-for-byte what `_bump` did before this
change, kept here so the "before" column stays reproducible after the change has landed.

Three measurements.

1. Concurrent write throughput — the headline. N processes bumping one store, N = 1, 2, 4, 8.
   The global lock should be near-FLAT in N: adding writers adds queueing, not throughput. A
   run that does not show that flat baseline has not reproduced the bug and its "after" column
   means nothing. The sharded one should climb with N until something else binds — and what
   binds is the directory: every shard is replaced by rename into the same parent, so the
   kernel's lock on that one directory inode is the next constraint down.

2. The read path, which is where a shard's cost lands. `counters` sums every part, and two of
   its callers are stamps on hot read paths — `room_stats`'s topic stamp (the /rooms walk) and
   app's note gauge — so an uncached /rooms request pays for three sums. This is the half that
   picks the width: writers want it wide, /rooms wants it narrow.

3. The write path end to end, at production's concurrency. `_bump` in isolation overstates
   the payoff, and eight processes understate it: production runs WEB_CONCURRENCY worker
   PROCESSES each serving sync handlers from a THREADPOOL, so the queue on the old global
   lock was tens of writers deep, not five. Sharding by pid divides that queue by the number
   of workers — threads inside one worker still share its shard, by design — so this is the
   only measurement that predicts what the deployment sees. It also carries a third column,
   `store.append` with the counter removed entirely, because the honest reading of a fix is
   how much of the available headroom it took.

4. Single-op write latency, uncontended, before and after — the cost of the change to a
   writer that was never waiting for anyone.

Builds its own store in a tempfile directory — never read a real one.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import platform
import shutil
import statistics
import sys
import tempfile
import threading
import time
import timeit
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import orjson  # noqa: E402

import store  # noqa: E402

WORKERS = (1, 2, 4, 8)
WORKERS_LIVE = 5  # WEB_CONCURRENCY in production
APPENDS = 60  # per thread, for the end-to-end section
PER_WORKER = 400  # bumps each, enough that a 1-worker run still lasts long enough to time
WIDTHS = (1, 2, 4, 8, 16, 32, 64, 256)  # 1 is today: the single `.counters` file, unsharded
SHIPPED = store.COUNTER_SHARDS


def _global_bump(root: Path, **deltas: int) -> None:
    """`_bump` as it stood before the change: one exclusive lock on one global file, holding a
    whole read-parse-modify-replace. The baseline every "before" figure below comes from."""
    path = root / store.COUNTERS_FILE
    try:
        with store._locked(path):
            current = store._read_counters(path)
            merged = {key: current[key] + deltas.get(key, 0) for key in store.COUNTER_KEYS}
            store._replace(path, orjson.dumps(merged))
    except OSError:
        pass


def _bump_loop(root: str, count: int, sharded: bool, barrier, out: str) -> None:
    """One worker. Latencies are recorded per op and written out as JSON: a child cannot hand
    a list back through a fork, and a queue would add its own lock to what is being measured.

    The barrier is what makes this a contention benchmark rather than N staggered runs — every
    worker is inside the loop before any of them is timed.
    """
    bump = store._bump if sharded else _global_bump
    path = Path(root)
    barrier.wait()
    start = time.perf_counter()
    laps = []
    for _ in range(count):
        t0 = time.perf_counter()
        bump(path, messages=1)
        laps.append(time.perf_counter() - t0)
    Path(out).write_text(json.dumps({"start": start, "end": time.perf_counter(), "laps": laps}))


def _widen(width: int) -> None:
    """Re-shard the store module in this process. The children are forked after it, so they
    inherit the width under test — there is no knob for this at runtime, deliberately (the
    layout is on-disk, exactly like `_shard`), and a benchmark that could not vary it could
    not show why the shipped number is the shipped number."""
    # Through the module dict: the shipped width is a literal, and a type checker rightly
    # objects to rebinding it — this benchmark is the one caller with a reason to.
    vars(store)["COUNTER_SHARDS"] = width
    store._COUNTER_SHARD_FILES = tuple(f"{store.COUNTERS_FILE}.{i:02x}" for i in range(width))
    store._COUNTER_FILES = (store.COUNTERS_FILE, *store._COUNTER_SHARD_FILES)


def _run(workers: int, sharded: bool, width: int = 0) -> tuple[float, float, float]:
    """Returns bumps/s over the whole run, and p50/p99 of one bump, in milliseconds."""
    if width:
        _widen(width)
    context = multiprocessing.get_context("fork")
    root = Path(tempfile.mkdtemp())
    try:
        barrier = context.Barrier(workers)
        procs = [
            context.Process(
                target=_bump_loop,
                args=(str(root), PER_WORKER, sharded, barrier, str(root / f"o{i}")),
            )
            for i in range(workers)
        ]
        [p.start() for p in procs]
        [p.join(300) for p in procs]
        results = [json.loads((root / f"o{i}").read_text()) for i in range(workers)]
        laps = sorted(lap for r in results for lap in r["laps"])
        elapsed = max(r["end"] for r in results) - min(r["start"] for r in results)
        total = store.counters(root)["messages"]
        assert total == workers * PER_WORKER, f"lost {workers * PER_WORKER - total} bumps"
        return len(laps) / elapsed, laps[len(laps) // 2] * 1e3, laps[int(len(laps) * 0.99)] * 1e3
    finally:
        shutil.rmtree(root, ignore_errors=True)
        _widen(SHIPPED)


def throughput() -> None:
    print(f"\nconcurrent write throughput ({PER_WORKER} bumps per worker, one store)")
    print(f"  {'workers':>7}  {'global lock':>26}  {'sharded by writer':>26}  {'gain':>6}")
    print(f"  {'':>7}  {'bumps/s   p50     p99':>26}  {'bumps/s   p50     p99':>26}")
    for workers in WORKERS:
        before = _run(workers, sharded=False)
        after = _run(workers, sharded=True)
        print(
            f"  {workers:>7}  {before[0]:>9,.0f} {before[1]:>6.2f}ms {before[2]:>6.2f}ms  "
            f"{after[0]:>9,.0f} {after[1]:>6.2f}ms {after[2]:>6.2f}ms  {after[0] / before[0]:>5.2f}x"
        )
    print("  A flat 'global lock' column IS the bug: more writers, no more writes.")


def _populate(root: Path, width: int) -> None:
    """A store whose every shard has been written — the steady state after one restart, and
    the worst case for the reader, since a shard that does not exist is a cheaper `open`."""
    root.mkdir(parents=True, exist_ok=True)
    counts = orjson.dumps(dict.fromkeys(store.COUNTER_KEYS, 1_234_567))
    (root / store.COUNTERS_FILE).write_bytes(counts)
    for i in range(width):
        (root / f"{store.COUNTERS_FILE}.{i:02x}").write_bytes(counts)


def _read_cost(width: int) -> float:
    """Seconds for one `counters()` at `width`, against a store where every shard exists —
    the steady state after one restart, and the reader's worst case."""
    root = Path(tempfile.mkdtemp())
    try:
        _populate(root, 0 if width == 1 else width)
        _widen(0 if width == 1 else width)
        n = 2_000
        return timeit.timeit(lambda r=root: store.counters(r), number=n) / n
    finally:
        shutil.rmtree(root, ignore_errors=True)
        _widen(SHIPPED)


def width_choice() -> None:
    """The whole trade-off in one table, and the only place the shipped number is argued.

    A shard buys write concurrency up to the number of concurrent WRITERS — there are
    WEB_CONCURRENCY of those, five in production — and past that it buys nothing, because
    there is no sixth process to keep the sixth lock busy. Every shard costs the READER
    unconditionally: `counters` sums them all, and three sums are on one uncached /rooms
    request (`_rooms_stamp`, `room_stats`'s topic stamp, app's note gauge). One is paid per
    write, the other per read, and this service reads far more than it writes.
    """
    print("\nwidth — writes want it wide, /rooms wants it narrow")
    print(f"  {'width':>9}  {'8 writers':>18}  {'counters()':>12}  {'3x (one /rooms)':>17}")
    for width in WIDTHS:
        rate = _run(8, sharded=True, width=width)[0] if width > 1 else _run(8, sharded=False)[0]
        per = _read_cost(width)
        label = f"{width} (today)" if width == 1 else f"{width}{' <-' if width == SHIPPED else ''}"
        print(f"  {label:>9}  {rate:>11,.0f} b/s  {per * 1e6:>9.1f} us  {per * 3e6:>14.1f} us")
    print(f"  Shipped width: {SHIPPED} — the write column has stopped climbing well before it")
    print("  (8 writers, and production runs 5), and the read column is still cheap there.")


def _append_proc(root: str, mode: str, threads: int, barrier, out: str) -> None:
    """One worker process: `threads` threads, each appending to its own room, so nothing here
    contends on a room lock and the counter is the only thing shared. `mode` is patched after
    the fork, which is why this benchmark can compare implementations at all."""
    # Through the module dict, like `_widen`: rebinding a module function is exactly what a
    # type checker should object to, and swapping the implementation is what this measures.
    if mode == "global":
        vars(store)["_bump"] = _global_bump
    elif mode == "none":
        vars(store)["_bump"] = lambda *a, **k: None
    path = Path(root)
    laps: list[float] = []
    guard = threading.Lock()

    def one(tid: int) -> None:
        room = f"r{os.getpid() % 9973:04d}x{tid}"
        store.append(path, room, "bot", "warm")  # the create is outside the timed section
        mine = []
        barrier.wait()
        for i in range(APPENDS):
            t0 = time.perf_counter()
            store.append(path, room, "bot", f"message number {i} with a little padding")
            mine.append(time.perf_counter() - t0)
        with guard:
            laps.extend(mine)

    workers = [threading.Thread(target=one, args=(i,)) for i in range(threads)]
    start = time.perf_counter()
    [t.start() for t in workers]
    [t.join(600) for t in workers]
    Path(out).write_text(json.dumps({"start": start, "end": time.perf_counter(), "laps": laps}))


def _append_run(procs: int, threads: int, mode: str) -> tuple[float, float, float]:
    context = multiprocessing.get_context("fork")
    root = Path(tempfile.mkdtemp())
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Both throttles satisfied up front: neither the reap pass nor the snapshot is under
        # test here, and one of them landing inside a run is worth more than every lock in it.
        (root / ".reaped").touch()
        (root / store.SNAPSHOTS_FILE).touch()
        barrier = context.Barrier(procs * threads)
        running = [
            context.Process(
                target=_append_proc, args=(str(root), mode, threads, barrier, str(root / f"a{i}"))
            )
            for i in range(procs)
        ]
        [p.start() for p in running]
        [p.join(600) for p in running]
        assert all(p.exitcode == 0 for p in running), [p.exitcode for p in running]
        results = [json.loads((root / f"a{i}").read_text()) for i in range(procs)]
        laps = sorted(lap for r in results for lap in r["laps"])
        elapsed = max(r["end"] for r in results) - min(r["start"] for r in results)
        return len(laps) / elapsed, laps[len(laps) // 2] * 1e3, laps[int(len(laps) * 0.99)] * 1e3
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_path() -> None:
    """`store.append` end to end at WORKERS_LIVE processes x T threads — the shape production
    actually has, and the only one where the lock split shows up in a whole write.

    Read the third column before quoting the second: the counter's own read-modify-replace is
    the larger cost, and splitting its lock does not touch it. What this change buys is the
    queue in front of it, which is what #588 measured (63-81% of syscall time in `flock`).
    """
    print(f"\nstore.append() end to end, {WORKERS_LIVE} processes x T threads, own room each")
    print(
        f"  {'threads':>7}  {'global lock':>26}  {'sharded by writer':>26}  {'no counter at all':>26}"
    )
    for threads in (4, 8, 16):
        row = {m: _append_run(WORKERS_LIVE, threads, m) for m in ("global", "sharded", "none")}
        print(
            f"  {threads:>7}  "
            + "  ".join(
                f"{row[m][0]:>6,.0f}/s p50 {row[m][1]:>6.2f} p99 {row[m][2]:>7.2f}ms"
                for m in ("global", "sharded", "none")
            )
        )
    print("  Eight processes with no threads show almost none of this: the lock is only the")
    print("  constraint once the queue on it is tens of writers deep, which threads are how")
    print("  this service gets. Traffic concentrated in ONE room shows none of it either —")
    print("  those writers already serialise on that room's own flock.")


def single_write() -> None:
    """The uncontended cost, for completeness: one writer, nobody to wait for."""
    print("\nsingle-op write latency, uncontended")
    for label, bump in (("global lock (before)", _global_bump), ("sharded (after)", store._bump)):
        root = Path(tempfile.mkdtemp())
        try:
            root.mkdir(parents=True, exist_ok=True)
            bump(root, messages=1)  # warm: the lock file and the shard both exist
            one = partial(bump, root, messages=1)
            laps = [timeit.timeit(one, number=1) for _ in range(2_000)]
            laps.sort()
            print(
                f"  {label:<22} p50 {statistics.median(laps) * 1e6:>8.1f} us"
                f"   p99 {laps[int(len(laps) * 0.99)] * 1e6:>8.1f} us"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    assert store._COUNTER_FILES[0] == store.COUNTERS_FILE, (
        "the benchmark and the store disagree about the layout"
    )
    print(
        f"{platform.platform()}\n{os.cpu_count()} logical CPUs, "
        f"python {platform.python_version()}, store shard width {store.COUNTER_SHARDS}"
    )
    throughput()
    write_path()
    width_choice()
    single_write()
