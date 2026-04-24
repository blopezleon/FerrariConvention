#!/usr/bin/env python3
"""merge_to_lookup.py — append deterministic ground-truth entries to
scalpel/router/lookup_table.py.

Workflow:
    1. Run scripts/capture_truth.py against the ref Pi to refresh
       scalpel/tests/ground_truth.jsonl with fresh outputs.
    2. Run this script from the repo root:
           py scalpel/scripts/merge_to_lookup.py
       It scans ground_truth.jsonl, finds commands where all 5 runs
       returned identical (stdout, stderr, rc), filters out commands
       already in LOOKUP and commands we deliberately want to be
       dynamic (see DYNAMIC_SKIP), and appends the rest.
    3. Review the diff (`git diff scalpel/router/lookup_table.py`),
       commit, push, redeploy.

Design notes:
    - Idempotent — re-running produces zero new entries if lookup is
      already up to date.
    - "Deterministic" means all RUNS (default 5) of the same cmd
      produced the exact same stdout, stderr, and exit code.
    - Commands with time-varying output (uptime, date) are naturally
      filtered out because their runs differ. But a handful of commands
      accidentally look deterministic within a 5-run capture window
      (e.g., `ss -tulpn` during a very quiet minute) — those are
      blacklisted below.
    - Commands cowrie already handles correctly as natives (ls, cat,
      date, uptime, free, ps, hostname, whoami, id, w, who) are
      blacklisted too, to avoid intercepting native paths that work
      fine and keep the lookup focused on things cowrie can't do.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

GROUND_TRUTH = Path("scalpel/tests/ground_truth.jsonl")
LOOKUP_PY = Path("scalpel/router/lookup_table.py")

# Commands we DON'T want in the lookup even if they appear deterministic —
# either because cowrie native handles them correctly and we want the
# real-time dynamic behavior, or because they're identity/env-dependent
# and honeypot-vs-refpi user differences would cause false matches.
DYNAMIC_SKIP: frozenset[str] = frozenset({
    # Handled by cowrie native with real clocks / real /proc/meminfo.
    "date", "uptime", "free", "free -m", "free -h",
    "hostname",   # cowrie native returns configured hostname
    "whoami", "id", "w", "who", "users", "last -n 10",
    # Per-process / per-session state — letting cowrie native respond
    # from its actual session context is more coherent than a snapshot.
    "ps", "ps aux", "ps -ef", "top", "top -bn1",
    # ls against fs.pickle — intercepting would break touch persistence.
    "ls", "ls -la", "ls ~", "ls -la ~",
    "env", "printenv", "history", "alias",
    # Dynamic sockets/connections — red team may probe twice.
    "ss -tulpn", "ss -tnp", "ss -tuln", "ss -tulwn",
    "netstat -tulpn", "netstat -rn", "netstat -an",
    # pwd depends on cwd; cowrie tracks this per-session.
    "pwd",
    # echo VAR expands the env, handled natively.
    "echo $USER", "echo $HOME", "echo $PATH", "echo $SHELL",
})


def load_ground_truth(path: Path) -> dict[str, list[dict]]:
    """Group records by command."""
    by_cmd: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            by_cmd[rec["cmd"]].append(rec)
    return by_cmd


def is_deterministic(records: list[dict]) -> bool:
    """True iff all runs returned identical (stdout, stderr, rc)."""
    if len(records) < 2:
        # Slow probes ran once — accept as deterministic on faith.
        return True
    first = (records[0]["stdout"], records[0]["stderr"], records[0]["rc"])
    for r in records[1:]:
        if (r["stdout"], r["stderr"], r["rc"]) != first:
            return False
    return True


def extract_existing_keys(lookup_src: str) -> set[str]:
    """Parse existing LOOKUP dict keys (handles both single- and double-quoted)."""
    keys = set()
    # Matches 4-space-indented: 'key': or "key":
    pattern = re.compile(r'^\s{4}(["\'])((?:\\.|(?!\1).)*?)\1:\s', re.MULTILINE)
    for m in pattern.finditer(lookup_src):
        # Unescape common cases (simple; Python literal rules aren't fully
        # needed here — capture_truth keys are plain ASCII with no escapes)
        keys.add(m.group(2))
    return keys


def main() -> int:
    if not GROUND_TRUTH.exists():
        print(f"error: {GROUND_TRUTH} not found — run capture_truth.py first",
              file=sys.stderr)
        return 1

    by_cmd = load_ground_truth(GROUND_TRUTH)
    lookup_src = LOOKUP_PY.read_text(encoding="utf-8")
    existing = extract_existing_keys(lookup_src)

    new_entries: list[tuple[str, str]] = []
    stats = {"already_in_lookup": 0, "blacklisted": 0, "variable": 0, "added": 0}

    for cmd, records in by_cmd.items():
        if cmd in existing:
            stats["already_in_lookup"] += 1
            continue
        if cmd in DYNAMIC_SKIP:
            stats["blacklisted"] += 1
            continue
        if not is_deterministic(records):
            stats["variable"] += 1
            continue
        value = records[0]["stdout"] + records[0]["stderr"]
        new_entries.append((cmd, value))
        stats["added"] += 1

    # Sort for stable diff — alphabetical by command.
    new_entries.sort(key=lambda x: x[0])

    print(f"ground truth records:       {sum(len(v) for v in by_cmd.values())}")
    print(f"unique commands:            {len(by_cmd)}")
    print(f"  already in lookup:        {stats['already_in_lookup']}")
    print(f"  dynamic-skip blacklisted: {stats['blacklisted']}")
    print(f"  variable (rejected):      {stats['variable']}")
    print(f"  new entries to add:       {stats['added']}")
    print()

    if not new_entries:
        print("lookup table already up to date — nothing to add.")
        return 0

    # Insert before the closing '}' of the LOOKUP dict literal.
    m = re.search(r"^\}\s*\n", lookup_src, re.MULTILINE)
    if not m:
        print("error: could not find closing } of LOOKUP dict", file=sys.stderr)
        return 2

    addition = "".join(f"    {c!r}: {v!r},\n" for c, v in new_entries)
    new_src = lookup_src[:m.start()] + addition + lookup_src[m.start():]
    LOOKUP_PY.write_text(new_src, encoding="utf-8")

    print(f"added {len(new_entries)} entries to {LOOKUP_PY}:")
    for c, _ in new_entries:
        print(f"  {c}")
    print()
    print("review with:  git diff scalpel/router/lookup_table.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
