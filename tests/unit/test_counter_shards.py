"""Run: uv run --group dev python -m pytest tests

Every write in the service — a message append, a note write, the reaper's tally — called
`_bump`, and `_bump` took ONE exclusive flock on `.counters` and did a read-parse-replace
inside it. Writes to unrelated rooms therefore queued behind each other: 63-81% of a
worker's syscall time at ~135 writes/s, ~95 ms mean (#588).

The counters have no name to shard on the way `.seqstate` does, so the axis is the writer:
one file per pid bucket, and `counters` sums them. Four things have to hold — a writer takes
only its own lock, no delta is lost or double-counted (including one written by an old worker
mid-deploy, which still lands in the pre-shard file), the sum never goes backwards under
concurrent writers because two of these counters are cache stamps, and the shard files stay
out of everything that walks the store.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from pathlib import Path

import orjson
import pytest


def _legacy(root: Path, **counts: int) -> None:
    """The pre-shard `.counters`, exactly as builds before this change wrote it — and as an
    old worker still writes it during a rolling restart."""
    import store

    root.mkdir(parents=True, exist_ok=True)
    current = orjson.loads((root / store.COUNTERS_FILE).read_bytes()) if _base(root) else {}
    (root / store.COUNTERS_FILE).write_bytes(
        orjson.dumps({key: current.get(key, 0) + counts.get(key, 0) for key in store.COUNTER_KEYS})
    )


def _base(root: Path) -> bool:
    import store

    return (root / store.COUNTERS_FILE).exists()


def _shard_files(root: Path) -> list[str]:
    import store

    return sorted(p.name for p in root.glob(f"{store.COUNTERS_FILE}.??") if p.is_file())


def _as_pid(monkeypatch, pid: int) -> None:
    """Write as a different worker. The shard is `os.getpid() % COUNTER_SHARDS`, so this is
    the only thing that separates two writers — there is no name to vary."""
    monkeypatch.setattr(os, "getpid", lambda: pid)


def _bump_in_a_child(root: str, count: int) -> None:
    """Top-level so `multiprocessing` can reach it. Real processes, not threads: the pid is
    the shard key, and the lock this test is about is an flock that only separate open file
    descriptions contend for the way production's workers do."""
    import store

    for _ in range(count):
        store._bump(Path(root), messages=1)


def _children(root: Path, workers: int, each: int) -> None:
    context = multiprocessing.get_context("fork")
    procs = [
        context.Process(target=_bump_in_a_child, args=(str(root), each)) for _ in range(workers)
    ]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    assert [p.exitcode for p in procs] == [0] * workers, "a writer died mid-bump"


# --------------------------------------------------------------------------- the lock split


def test_a_writer_bumps_its_own_shard_and_never_the_global_file(tmp_path) -> None:
    """The whole change in one assertion. The file `_bump` takes the lock on is what every
    write in the service serialised behind; if a bump still lands on `.counters` there is one
    lock again, whatever the read path does."""
    import store

    store._bump(tmp_path, messages=1)

    mine = f"{store.COUNTERS_FILE}.{os.getpid() % store.COUNTER_SHARDS:02x}"
    assert _shard_files(tmp_path) == [mine], "the bump did not land in this writer's shard"
    assert not _base(tmp_path), "the global file is still being written"
    assert store.counters(tmp_path)["messages"] == 1


def test_distinct_writers_land_in_distinct_shards(tmp_path, monkeypatch) -> None:
    """Consecutive pids — what a fork of N workers actually produces — must spread one per
    shard. This is why the key is `pid % width` and not a hash of the pid: hashing five
    workers into eight buckets collides about a third of the time."""
    import store

    first = 4096  # a multiple of the width, so the run below starts at shard 00
    for offset in range(store.COUNTER_SHARDS):
        _as_pid(monkeypatch, first + offset)
        store._bump(tmp_path, messages=1)

    assert len(_shard_files(tmp_path)) == store.COUNTER_SHARDS, "workers shared a shard"
    assert store.counters(tmp_path)["messages"] == store.COUNTER_SHARDS


def test_writers_in_different_shards_do_not_serialise_on_one_lock(tmp_path, monkeypatch) -> None:
    """The topology, not a millisecond count: under one global lock only one writer can be
    inside the read-modify-replace at a time, so four of them holding it at once is a
    barrier that never releases. Fails by timeout under the old shape, and cannot flake into
    passing on a loaded runner the way a duration comparison can."""
    import store

    writers = 4
    together = threading.Barrier(writers)
    real_replace = store._replace
    failed: list[BaseException] = []
    pids = threading.local()

    def wait_while_holding_the_lock(path, data, fsync=False):
        together.wait(timeout=10)  # inside `_locked`: `_replace` is the last step under it
        return real_replace(path, data, fsync)

    monkeypatch.setattr(store, "_replace", wait_while_holding_the_lock)
    monkeypatch.setattr(os, "getpid", lambda: pids.value)

    def bump(pid):
        pids.value = pid
        try:
            store._bump(tmp_path, messages=1)
        except BaseException as exc:  # noqa: BLE001 - surfaced through `failed`
            failed.append(exc)

    threads = [threading.Thread(target=bump, args=(8192 + i,)) for i in range(writers)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]

    assert not failed, f"writers in distinct shards could not overlap: {failed}"
    assert store.counters(tmp_path)["messages"] == writers


def test_no_delta_is_lost_when_every_worker_bumps_at_once(tmp_path) -> None:
    """The lock is still doing its job. Sharding a counter is only safe while every writer
    that shares a shard still serialises on it — drop the lock and this is a lost-update race
    that a single-process test would never show."""
    import store

    _children(tmp_path, workers=4, each=100)

    assert store.counters(tmp_path)["messages"] == 400
    assert len(_shard_files(tmp_path)) > 1, "premise: the writers did not share one shard"


def test_writers_that_share_a_shard_still_serialise_on_it(tmp_path, monkeypatch) -> None:
    """The lock the split kept. Threads inside one worker ARE one writer — same pid, same
    shard — and Starlette runs sync handlers in a threadpool, so this is the common case, not
    a corner of it. The read-modify-replace releases the GIL at every syscall in it, so
    without the flock these lose each other's counts."""
    import store

    _as_pid(monkeypatch, 900)  # every thread below is this one worker
    threads = [
        threading.Thread(target=lambda: [store._bump(tmp_path, messages=1) for _ in range(50)])
        for _ in range(8)
    ]
    [t.start() for t in threads]
    [t.join(60) for t in threads]

    assert _shard_files(tmp_path) == [f"{store.COUNTERS_FILE}.{900 % store.COUNTER_SHARDS:02x}"]
    assert store.counters(tmp_path)["messages"] == 400, "a bump was lost inside one shard"


# --------------------------------------------------------------------------- the read path


def test_the_sum_spans_the_pre_shard_file_and_every_shard(tmp_path, monkeypatch) -> None:
    """The upgrade path, and it needs no migration: the old file is summed as one more shard
    for as long as it exists. A reader that looked only at the shards would report a store's
    whole history as zero the moment this version booted."""
    import store

    _legacy(tmp_path, messages=1_000, rooms_created=7)
    for pid in (512, 513):
        _as_pid(monkeypatch, pid)
        store._bump(tmp_path, messages=1, notes_written=2)

    counted = store.counters(tmp_path)
    assert counted["messages"] == 1_002, "the pre-shard total was dropped"
    assert (counted["rooms_created"], counted["notes_written"]) == (7, 4)


def test_an_old_workers_bump_during_a_rolling_restart_is_counted_exactly_once(
    tmp_path, monkeypatch
) -> None:
    """Old and new workers run together for the length of a deploy. The old ones keep writing
    `.counters` under the global lock; the new ones write shards. Nothing moves a count
    between the two, so a delta from either side is counted once and never lost."""
    import store

    _as_pid(monkeypatch, 4242)
    store._bump(tmp_path, messages=1)  # a new worker
    _legacy(tmp_path, messages=5)  # an old worker, still on the pre-shard code
    store._bump(tmp_path, messages=1)  # the new worker again, after it

    assert store.counters(tmp_path)["messages"] == 7
    assert orjson.loads((tmp_path / store.COUNTERS_FILE).read_bytes())["messages"] == 5


def test_a_summed_counter_never_goes_backwards_under_concurrent_writers(tmp_path) -> None:
    """`room_stats` and app's note gauge key caches on these values by equality, so a sum that
    dipped would serve an entry built before the writes in between. `_bump` is allowed to LOSE
    a delta — it says so — but that is a different property from one that decreases: parts
    only grow and nothing is ever moved between them, so a later sum is never smaller.
    """
    import store

    _legacy(tmp_path, messages=10)
    context = multiprocessing.get_context("fork")
    procs = [context.Process(target=_bump_in_a_child, args=(str(tmp_path), 60)) for _ in range(4)]
    [p.start() for p in procs]
    samples = []
    while any(p.is_alive() for p in procs) or len(samples) < 50:
        samples.append(store.counters(tmp_path)["messages"])
        if len(samples) > 5_000:  # a hung writer must fail the test, not hang the suite
            break
    [p.join(60) for p in procs]

    assert samples == sorted(samples), "a sampled counter went backwards"
    assert store.counters(tmp_path)["messages"] == 250


def test_a_corrupt_shard_costs_only_its_own_counts(tmp_path, monkeypatch) -> None:
    """Same promise the single file made — counters are diagnostics, never authority — but
    now one shard's worth of it. Garbage in one file must not take down the read path or be
    read as a huge or negative total, and must not hide the shards either side of it."""
    import store

    for pid in (256, 257, 258):
        _as_pid(monkeypatch, pid)
        store._bump(tmp_path, messages=1)
    victim = tmp_path / f"{store.COUNTERS_FILE}.{256 % store.COUNTER_SHARDS:02x}"

    for junk in (b"[]", b"{", b'{"messages": "many"}', b'{"messages": -5}', b'"messages"'):
        victim.write_bytes(junk)
        assert store.counters(tmp_path)["messages"] == 2, junk
        assert store.counters(tmp_path) == {
            **dict.fromkeys(store.COUNTER_KEYS, 0),
            "messages": 2,
        }, junk


def test_a_bump_to_an_unwritable_shard_never_fails_the_write_it_follows(
    tmp_path, monkeypatch
) -> None:
    """Best effort, exactly as before: the caller's append has already landed by the time this
    runs. A shard that cannot be written costs that delta and nothing else — the other shards
    still answer."""
    import store

    _as_pid(monkeypatch, 32)
    store._bump(tmp_path, messages=1)
    blocked = tmp_path / f"{store.COUNTERS_FILE}.{33 % store.COUNTER_SHARDS:02x}"
    blocked.mkdir()  # a directory where the shard file goes: every write to it fails

    _as_pid(monkeypatch, 33)
    store._bump(tmp_path, messages=1)  # must not raise

    assert store.counters(tmp_path)["messages"] == 1
    assert blocked.is_dir()


# --------------------------------------------------------------------------- isolation


def test_shards_are_never_walked_counted_reaped_or_nameable(tmp_path, monkeypatch) -> None:
    """They sit at the root beside `.seqstate.??` and `.usage`, so the room caps, the listings
    and the bucket pruning must not see them — and no caller can name one: this is a
    world-writable service, and the change adds no reachable surface."""
    import store

    store._write_record(tmp_path, "live", "bot", "hi")
    real = os.getpid()
    for pid in range(64, 64 + store.COUNTER_SHARDS):
        _as_pid(monkeypatch, pid)
        store._bump(tmp_path, messages=1)
    _as_pid(monkeypatch, real)  # the reaper below runs as this process, like a real worker
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)

    assert len(_shard_files(tmp_path)) > 1, "premise: the store is sharded"
    assert store._count_rooms(tmp_path)[0] == 1, "shards counted against the room cap"
    assert store.list_rooms(tmp_path) == ["live"], "shards listed as rooms"
    assert [e.name for e in store._walk(tmp_path / "rooms", ".jsonl")] == ["live.jsonl"]
    for name in _shard_files(tmp_path):
        with pytest.raises(store.StoreError):
            store.valid_name(name)
