#!/usr/bin/env python3
"""Measure generated temporary libraries, including optional real watcher runs.

Never targets user folders. --watch includes event delivery, file readiness,
subprocess startup and persistence in its measured arrival-to-completion time.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from org_organizer import Organizer

MODES = ("flatten", "rename", "in-place", "duplicates", "cleanup")


def options(mode):
    return dict(recursive=True, strategy="in-place" if mode == "in-place" else "flatten-root",
                include_hidden=False, normalize="standard", collect_empty_dirs=mode != "cleanup",
                random_names_after_organize=mode in {"rename", "in-place"},
                skip_randomly_renamed=True, detect_duplicates=mode == "duplicates",
                empty_dirs_only=mode == "cleanup")


def populate(root, files, mode):
    for i in range(files):
        parent = root / f"album-{i % 20:03d}" if mode == "in-place" else root
        directory = parent / "Images"
        directory.mkdir(parents=True, exist_ok=True)
        # Already-random names and distinct, equal-sized contents exercise warm
        # steady state without charging an initial rename to arrival latency.
        (directory / f"{i:016d}.jpg").write_bytes(f"image-{i:010d}".encode())


def arrival(root, mode, name):
    parent = root / "album-000" if mode == "in-place" else root
    path = parent / (name if mode == "cleanup" else name + ".jpg")
    if mode == "cleanup":
        path.mkdir()
    else:
        path.write_bytes(b"arrival-" + name.encode())
    return path


def organize(root, mode, changed=None):
    started = time.perf_counter()
    result = Organizer(base=root, dry_run=False, changed_paths=changed, **options(mode)).run()
    return time.perf_counter() - started, result


def engine_benchmark(base, files, repeats, mode):
    root = base / mode
    populate(root, files, mode)
    organize(root, mode)
    measurements = {"full": [], "incremental": []}
    phases = {}
    for i in range(repeats):
        for kind in measurements:
            path = arrival(root, mode, f"arrival-{kind}-{i}")
            elapsed, result = organize(root, mode, [path] if kind == "incremental" else None)
            assert not path.exists(), (mode, kind, result)
            assert result.get("execution") == kind, (mode, kind, result.get("execution"))
            measurements[kind].append(elapsed)
            phases[kind] = result.get("phase_seconds", {})
    full, incremental = (statistics.median(measurements[k]) for k in ("full", "incremental"))
    return {"full_median_seconds": full, "incremental_median_seconds": incremental,
            "speedup": full / incremental, "last_phase_seconds": phases}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def wait_for(predicate, proc, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Watcher exited early: {proc.returncode}")
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise TimeoutError("Temporary watcher did not finish in time")


def watcher_benchmark(base, files, repeats, mode):
    from schedule_config import FolderJob, ScheduleConfig, save_config

    root = base / (mode + "-watch")
    populate(root, files, mode)
    state = base / (mode + "-watch-state")
    config = base / (mode + "-schedule.json")
    job = FolderJob(path=str(root), enabled=True, dry_run_verified=True, **options(mode))
    cfg = ScheduleConfig(schedule_mode="watch", scheduler_enabled=True, notify_on_run=False,
                         watch_poll_seconds=0.05, watch_quiet_seconds=0.15, folders=[job])
    save_config(config, cfg)
    env = dict(os.environ, XDG_CONFIG_HOME=str(base / "watch-config"), XDG_STATE_HOME=str(state),
               FILE_ORG_RUNTIME_ACTIVE="1")
    status_path = state / "file-organization" / "watch-status.json"
    history_path = state / "file-organization" / "history.jsonl"
    daemon = Path(__file__).with_name("schedule_daemon.py")
    timings = []
    executions = []
    with (base / (mode + "-daemon.log")).open("w") as log:
        proc = subprocess.Popen([sys.executable, str(daemon), "--foreground", "--config", str(config)],
                                env=env, stdout=log, stderr=log)
        try:
            def idle():
                status = read_json(status_path)
                folder = status.get("folders", {}).get(str(root), {})
                return status if folder.get("state") == "idle" and folder.get("last_run") else None
            status = wait_for(idle, proc)
            for i in range(repeats):
                previous_run = status["folders"][str(root)].get("last_run")
                started = time.perf_counter()
                path = arrival(root, mode, f"watch-arrival-{i}")
                def completed():
                    current = idle()
                    if current and not path.exists() and current["folders"][str(root)].get("last_run") != previous_run:
                        return current
                    return None
                status = wait_for(completed, proc)
                timings.append(time.perf_counter() - started)
                try:
                    rows = [json.loads(line) for line in history_path.read_text().splitlines()]
                    executions.append(rows[-1].get("execution", "cleanup"))
                except (OSError, ValueError, IndexError):
                    executions.append("unknown")
            return {"backend": status.get("backend"), "arrival_median_seconds": statistics.median(timings),
                    "arrival_seconds": timings, "observed_execution": executions,
                    "quiet_seconds": cfg.watch_quiet_seconds}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=3000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--watch", action="store_true", help="Also measure real isolated daemon arrival latency")
    args = parser.parse_args()
    if args.files < 1 or args.repeats < 1:
        parser.error("--files and --repeats must be positive")
    with tempfile.TemporaryDirectory(prefix="organizer-benchmark-") as temporary:
        base = Path(temporary)
        previous_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(base / "engine-state")
        try:
            results = {"synthetic_files": args.files, "repeats": args.repeats, "modes": {}}
            for mode in args.modes:
                results["modes"][mode] = engine_benchmark(base, args.files, args.repeats, mode)
                if args.watch:
                    results["modes"][mode]["watcher"] = watcher_benchmark(base, args.files, args.repeats, mode)
            print(json.dumps(results, indent=2))
        finally:
            if previous_state is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous_state


if __name__ == "__main__":
    main()
