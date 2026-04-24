#!/usr/bin/env python3
"""benchmark.py — rate Ollama on variable-output commands to populate goodLLM/badLLM.

Runs on the HONEYPOT Pi (where Ollama lives). Reads ground_truth.jsonl as the
ref-Pi oracle, fires each variable-output command at Ollama N times, and scores
on (a) return-code match, (b) line-count shape match against the ref, and
(c) latency. Writes per-command results to JSONL and prints a verdict table.

Usage
-----
    # default: classify every variable command in ground_truth
    python3 scalpel/local_llm/benchmark.py

    # override defaults
    python3 scalpel/local_llm/benchmark.py \\
        --ground-truth scalpel/tests/ground_truth.jsonl \\
        --model qwen2.5:1.5b \\
        --runs 3 \\
        --out scalpel/local_llm/bench_results.jsonl

    # benchmark a single command (doesn't need ground_truth)
    python3 scalpel/local_llm/benchmark.py --cmd "ps aux" --cmd "ls /tmp"

Verdicts
--------
    good       — rc matches, line count within tolerance, p95 latency < 800ms
    borderline — mostly correct but slow (800-1500ms) or inconsistent shape
    bad        — wrong rc, wildly wrong length, or p95 latency > 1500ms
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_GROUND_TRUTH = "scalpel/tests/ground_truth.jsonl"
DEFAULT_OUT = "scalpel/local_llm/bench_results.jsonl"

GOOD_LATENCY_MS = 800
BAD_LATENCY_MS = 1500
LINE_COUNT_TOLERANCE = 0.30  # within 30% of ref median line count

SYSTEM_PROMPT = (
    "You are bash running on a Raspberry Pi 5 with 64-bit Raspberry Pi OS "
    "(Debian, aarch64). The current user is root in /root. Respond with ONLY "
    "the exact stdout that bash would print for the command. Do not include "
    "any explanation, prose, markdown, or backticks. If the command produces "
    "no output, respond with an empty string."
)


def load_ground_truth(path: Path) -> dict[str, list[dict]]:
    """Group ground_truth.jsonl records by cmd."""
    by_cmd: dict[str, list[dict]] = collections.defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            by_cmd[rec["cmd"]].append(rec)
    return by_cmd


def is_variable(runs: list[dict]) -> bool:
    """A command is 'variable' if its stdout differs across runs."""
    stdouts = {r["stdout"] for r in runs}
    return len(stdouts) > 1


def ollama_generate(
    cmd: str,
    model: str,
    base_url: str,
    timeout: float = 10.0,
) -> tuple[str, int, float]:
    """Call Ollama /api/generate. Returns (stdout_text, approx_rc, latency_ms).

    The LLM doesn't give us an exit code, so we infer: empty output with no
    error tokens -> rc 0; output starting with 'bash:' or containing
    'command not found'/'No such file' -> rc likely non-zero. This is
    approximate — it's fine for benchmark scoring, not prod routing.
    """
    payload = {
        "model": model,
        "prompt": f"$ {cmd}",
        "system": SYSTEM_PROMPT,
        "stream": False,
        "keep_alive": -1,
        "options": {
            "temperature": 0.2,
            "num_predict": 512,
        },
    }
    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    latency_ms = (time.perf_counter() - t0) * 1000.0
    text = body.get("response", "")
    rc = infer_rc(text)
    return text, rc, latency_ms


def infer_rc(text: str) -> int:
    """Best-effort exit-code inference from LLM output."""
    probe = text.lower()
    error_markers = (
        "command not found",
        "no such file or directory",
        "permission denied",
        "bash:",
        "cannot access",
        "operation not permitted",
    )
    if any(m in probe for m in error_markers):
        return 1
    return 0


def shape_score(
    cand_stdout: str,
    cand_rc: int,
    ref_runs: list[dict],
) -> dict:
    """Compare candidate to the ref-Pi runs. Returns score components."""
    ref_rcs = [r["rc"] for r in ref_runs]
    expected_rc = collections.Counter(ref_rcs).most_common(1)[0][0]
    rc_match = cand_rc == expected_rc

    ref_line_counts = [r["stdout"].count("\n") for r in ref_runs]
    ref_median = statistics.median(ref_line_counts)
    cand_lines = cand_stdout.count("\n")
    if ref_median == 0:
        line_ok = cand_lines == 0
    else:
        line_ok = abs(cand_lines - ref_median) / max(ref_median, 1) <= LINE_COUNT_TOLERANCE

    both_empty = (not cand_stdout.strip()) == (not ref_runs[0]["stdout"].strip())

    return {
        "rc_match": rc_match,
        "line_ok": line_ok,
        "both_empty_match": both_empty,
        "cand_lines": cand_lines,
        "ref_median_lines": ref_median,
        "expected_rc": expected_rc,
        "cand_rc": cand_rc,
    }


def verdict(match_rate: float, p95_ms: float, rc_rate: float) -> str:
    """Final classification per command."""
    if rc_rate < 1.0:
        return "bad"  # any rc mismatch is disqualifying
    if p95_ms > BAD_LATENCY_MS:
        return "bad"
    if match_rate >= 0.8 and p95_ms < GOOD_LATENCY_MS:
        return "good"
    if match_rate >= 0.5 and p95_ms < BAD_LATENCY_MS:
        return "borderline"
    return "bad"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def warm_model(model: str, base_url: str) -> None:
    """Force the model into RAM with keep_alive=-1 before we start timing."""
    print(f"warming {model}…", file=sys.stderr, flush=True)
    try:
        ollama_generate("echo ready", model, base_url, timeout=120.0)
    except Exception as e:
        print(f"  warm-up failed: {e}", file=sys.stderr)
        sys.exit(2)
    print("  warm.", file=sys.stderr, flush=True)


def benchmark_one(
    cmd: str,
    ref_runs: list[dict] | None,
    model: str,
    base_url: str,
    runs: int,
) -> dict:
    """Run one command N times, diff each, aggregate."""
    latencies: list[float] = []
    per_run = []
    rc_matches = 0
    shape_matches = 0

    for i in range(1, runs + 1):
        try:
            text, rc, latency_ms = ollama_generate(cmd, model, base_url)
        except Exception as e:
            per_run.append({"run": i, "error": str(e)})
            continue
        latencies.append(latency_ms)
        score = shape_score(text, rc, ref_runs) if ref_runs else None
        if score:
            if score["rc_match"]:
                rc_matches += 1
            if score["line_ok"]:
                shape_matches += 1
        per_run.append({
            "run": i,
            "latency_ms": round(latency_ms, 1),
            "cand_rc": rc,
            "cand_stdout": text,
            "score": score,
        })

    completed = len([r for r in per_run if "error" not in r])
    rc_rate = rc_matches / completed if completed else 0.0
    match_rate = shape_matches / completed if completed else 0.0
    p50 = percentile(latencies, 0.50)
    p95 = percentile(latencies, 0.95)
    v = verdict(match_rate, p95, rc_rate) if ref_runs else "no-ref"

    return {
        "cmd": cmd,
        "runs": runs,
        "completed": completed,
        "latency_p50_ms": round(p50, 1),
        "latency_p95_ms": round(p95, 1),
        "rc_match_rate": round(rc_rate, 2),
        "shape_match_rate": round(match_rate, 2),
        "verdict": v,
        "per_run": per_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ground-truth", default=DEFAULT_GROUND_TRUTH)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--url", default=DEFAULT_OLLAMA)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument(
        "--cmd",
        action="append",
        default=[],
        help="Benchmark a specific command (repeatable). Bypasses ground_truth filter.",
    )
    ap.add_argument(
        "--include-deterministic",
        action="store_true",
        help="Also benchmark commands whose ref-Pi output was identical across runs.",
    )
    args = ap.parse_args()

    gt_path = Path(args.ground_truth)
    if args.cmd:
        commands = [(c, None) for c in args.cmd]
    else:
        if not gt_path.exists():
            print(f"error: {gt_path} not found. Run capture_truth.py first.", file=sys.stderr)
            return 2
        by_cmd = load_ground_truth(gt_path)
        commands = []
        for cmd, runs in by_cmd.items():
            if args.include_deterministic or is_variable(runs):
                commands.append((cmd, runs))

    if not commands:
        print("nothing to benchmark — all commands were deterministic?", file=sys.stderr)
        return 1

    print(f"benchmarking {len(commands)} commands × {args.runs} runs against {args.model}", file=sys.stderr)
    warm_model(args.model, args.url)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with out_path.open("w", encoding="utf-8") as f:
        for i, (cmd, ref_runs) in enumerate(commands, 1):
            print(f"[{i}/{len(commands)}] {cmd}", file=sys.stderr, flush=True)
            result = benchmark_one(cmd, ref_runs, args.model, args.url, args.runs)
            results.append(result)
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
            f.flush()

    print_summary(results)
    print(f"\nfull results → {out_path}", file=sys.stderr)
    return 0


def print_summary(results: list[dict]) -> None:
    buckets: dict[str, list[dict]] = collections.defaultdict(list)
    for r in results:
        buckets[r["verdict"]].append(r)

    print("\n" + "=" * 78)
    print(f"{'verdict':<12}{'p50ms':>8}{'p95ms':>8}{'rc':>6}{'shape':>8}  cmd")
    print("-" * 78)
    order = ["good", "borderline", "bad", "no-ref"]
    for v in order:
        for r in sorted(buckets.get(v, []), key=lambda x: x["latency_p95_ms"]):
            print(
                f"{r['verdict']:<12}"
                f"{r['latency_p50_ms']:>8.1f}"
                f"{r['latency_p95_ms']:>8.1f}"
                f"{r['rc_match_rate']:>6.2f}"
                f"{r['shape_match_rate']:>8.2f}  "
                f"{r['cmd']}"
            )
    print("=" * 78)
    counts = {v: len(buckets.get(v, [])) for v in order}
    print(f"totals: good={counts['good']}  borderline={counts['borderline']}  bad={counts['bad']}  no-ref={counts['no-ref']}")


if __name__ == "__main__":
    sys.exit(main())
