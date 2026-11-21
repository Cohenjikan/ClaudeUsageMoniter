"""Run with Python 3.11 on Windows: python dev/verify_jsonl.py

Reads the live corpus, then benchmarks a private full copy under the repository
(needs roughly the corpus size in free space). No transcript contents are logged.
The reference module comes directly from git show HEAD:jsonl_costs.py.
"""
from contextlib import ExitStack, contextmanager
import argparse
import ctypes
from ctypes import wintypes
from dataclasses import fields, is_dataclass
from datetime import datetime
import gc
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
LIVE = Path.home() / ".claude" / "projects"
MIB = 1024 ** 2


def rss():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                    ("peak", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                    ("rest", ctypes.c_size_t * 6)]
    info = Counters()
    info.cb = ctypes.sizeof(info)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(-1), ctypes.byref(info), info.cb):
        raise ctypes.WinError()
    return info.working_set / MIB


def plain(report):
    # Dataclasses from different modules have distinct types. Preserve mapping
    # order as well as values, including binary64 costs and datetime objects.
    if is_dataclass(report):
        return {f.name: plain(getattr(report, f.name)) for f in fields(report)}
    if isinstance(report, dict):
        return [(k, plain(v)) for k, v in report.items()]
    if isinstance(report, float):
        return report.hex()
    if isinstance(report, datetime):
        return report.isoformat(), report.fold
    return report


def worker(pipe, version, directory, db, now):
    if version == "old":
        src = subprocess.check_output(["git", "show", "HEAD:jsonl_costs.py"], cwd=REPO)
        mod = types.ModuleType("baseline_jsonl")
        sys.modules[mod.__name__] = mod
        exec(compile(src, "HEAD:jsonl_costs.py", "exec"), mod.__dict__)
    else:
        import jsonl_costs as mod
        # Exercise the public entry point and restart cache on private fixtures too.
        mod.PROJECTS_ROOT = directory
        mod._database = db
    pipe.send(rss())
    while True:
        cmd = pipe.recv()
        if cmd == "stop":
            return
        n = 0
        cpu, wall = time.process_time(), time.perf_counter()
        while True:
            report = mod.build_report(directory, now)
            n += 1
            if cmd != "idle" or time.process_time() - cpu >= 0.25:
                break
        cpu = (time.process_time() - cpu) / n
        wall = (time.perf_counter() - wall) / n
        gc.collect()
        pipe.send((cpu, wall, rss(), n, plain(report)))


@contextmanager
def reader(version, directory, db, now):
    parent, child = mp.Pipe()
    process = mp.Process(target=worker, args=(child, version, directory, db, now))
    process.start()
    child.close()
    try:
        initial = parent.recv()

        def run(action="build", label=None, samples=None):
            parent.send(action)
            cpu, wall, resident, n, report = parent.recv()
            if label:
                print(f"{label:18} {version:3} CPU={cpu:.7f}s wall={wall:.7f}s "
                      f"RSS={resident:.2f}MiB delta={resident-initial:.2f}MiB n={n}", flush=True)
            if samples is not None:
                samples.append(cpu)
            return report
        yield run
    finally:
        if process.is_alive():
            try:
                parent.send("stop")
            except BrokenPipeError:
                pass
        parent.close()
        process.join()


def equal(a, b, label):
    diff = [k for k in a.keys() | b.keys() if a.get(k) != b.get(k)]
    if diff:
        raise AssertionError(f"{label}: mismatching fields {diff}")
    print(f"{label}: all {len(a)} fields equal, {a['all_time']['turns']} turns", flush=True)


def manifest():
    result = {}
    for path in LIVE.rglob("*.jsonl"):
        try:
            st = path.stat()
            result[path.relative_to(LIVE)] = st.st_size, st.st_mtime_ns
        except FileNotFoundError:
            pass
    if not result:
        raise RuntimeError("No readable real transcripts found")
    return result


def mirror(dest, source):
    # Copy exactly the inventoried prefixes. Active sessions can keep appending.
    for rel, (size, _) in source.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with (LIVE / rel).open("rb") as src, target.open("xb") as out:
            left = size
            while left:
                chunk = src.read(min(left, MIB))
                if not chunk:
                    raise RuntimeError("A source shrank during snapshot")
                out.write(chunk)
                left -= len(chunk)


def append(path, data):
    with path.open("ab") as out:
        out.write(data)


def record(i, cost=5):
    return json.dumps({"message": {"id": f"jsonl-benchmark-{i}", "model": "opus-4",
                       "usage": {"output_tokens": cost}}, "sessionId": "jsonl-benchmark",
                       "timestamp": "2026-11-21T00:00:00Z"}).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-cache", action="store_true",
                        help="keep the verified live index next to the app if none exists")
    args = parser.parse_args()
    now = datetime.now().astimezone()
    source = manifest()
    print(f"Python {sys.version.split()[0]}, {len(source)} real files, "
          f"{sum(v[0] for v in source.values())} bytes", flush=True)
    with tempfile.TemporaryDirectory(prefix=".jsonl-verify-", dir=REPO) as temp:
        work = Path(temp)
        with reader("old", LIVE, None, now) as old, reader("new", LIVE, work/"live.db", now) as new:
            old(label="live cold")
            new(label="live cold")
            for _ in range(10):
                before = manifest()
                a, b = old(), new()
                if before == manifest():
                    equal(a, b, "stable live window")
                    break
            else:
                raise RuntimeError("Could not obtain a stable comparison window on live data")
        if args.seed_cache:
            try:
                os.link(work / "live.db", REPO / "jsonl_index.sqlite3")
                print("Seeded the app index from the verified real-data build.", flush=True)
            except FileExistsError:
                print("An app index already exists.", flush=True)

        tree = work / "snapshot"
        mirror(tree, source)
        target_size = 10 * MIB
        target = tree / max((p for p, (n, _) in source.items() if n <= target_size - 2),
                            key=lambda p: source[p][0])
        append(target, b"\n" + b" " * (target_size - target.stat().st_size - 2) + b"\n")
        db = work / "snapshot.db"
        with ExitStack() as stack:
            old, new = [stack.enter_context(reader(v, tree, db, now)) for v in ("old", "new")]
            equal(old(label="snapshot cold"), new(label="snapshot cold"), "snapshot cold")
            equal(old("idle", "snapshot idle"), new("idle", "snapshot idle"), "snapshot idle")
            # Windows process CPU time is quantized. Time only the append
            # refresh, returning both indexes to the same 10 MiB base each time.
            samples = [], []
            for i in range(16):
                if i:
                    with target.open("r+b") as out:
                        out.truncate(target_size)
                    old()
                    new()
                append(target, b"".join(record(j) + b"\n" for j in range(50)))
                a, b = old(samples=samples[0]), new(samples=samples[1])
                if a != b:
                    equal(a, b, f"append repeat {i}")
            equal(a, b, "append 50 (16 passes)")
            for version, values in zip(("old", "new"), samples):
                print(f"10MiB + 50 lines {version} CPU={sum(values)/len(values):.7f}s "
                      f"n={len(values)}", flush=True)
            with reader("new", tree, db, now) as restart:
                equal(old(), restart(label="persisted restart"), "restart + uncheckpointed append")
            print(f"disk cache={db.stat().st_size / MIB:.2f}MiB", flush=True)

        sample = work / "simulation"
        sample.mkdir()
        cases = [p for p, (n, _) in source.items() if 100_000 < n < MIB][:3]
        for i, rel in enumerate(cases):
            shutil.copyfile(tree / rel, sample / f"real-{i}.jsonl")
        target = sample / "real-0.jsonl"
        with reader("old", sample, None, now) as old, reader("new", sample, work/"sim.db", now) as new:
            def check(label):
                equal(old(), new(), label)
            check("copied real files")
            append(target, b"\n" + record(100) + b"\n")
            check("complete append")
            line = record(101)
            append(target, line[:30])
            check("partial JSON")
            append(target, line[30:])
            check("valid JSON at EOF")
            append(target, b"\n")
            check("completed newline")
            early = sample / "000-first.jsonl"
            early.write_bytes(record(100, 999) + b"\n")
            check("earlier duplicate")
            early.unlink()
            check("delete winner")
            target.write_bytes(record(102) + b"\n")
            check("truncate")
            target.write_bytes(record(103, 8000) + b"\n" + record(104) + b"\n")
            check("larger rewrite")
            target.unlink()
            check("delete transcript")


if __name__ == "__main__":
    main()
