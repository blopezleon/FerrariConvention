#!/usr/bin/env python3
"""harden_tmp.py — clean up the /tmp dir in cowrie's fs.pickle so `ls /tmp`
looks like a fresh Raspberry Pi OS instead of a hacked practice box.

Problem: the stock fs.pickle ships with a 2013-dated /tmp containing
.ICE-unix and .X11-unix. Our teammates' testing left lol.sh and test.sh
in there. Both are honeypot tells to the red team.

Fix: wipe /tmp's contents and repopulate with ref-Pi-style entries —
.font-unix, .ICE-unix, and four systemd-private-* dirs.

This script is idempotent and atomic. Run it on the Pi:

    scp scalpel/scripts/harden_tmp.py cowrie@<pi>:/tmp/
    ssh cowrie@<pi> "cd ~/cowrie && python3 /tmp/harden_tmp.py share/cowrie/fs.pickle"

Then restart cowrie so the running process reloads:

    ssh cowrie@<pi> "cd ~/cowrie && source cowrie-env/bin/activate && cowrie restart"

The script prints what it changed. If the pickle format moves or the /tmp
node is missing, it fails loudly rather than silently corrupting the FS.
"""
from __future__ import annotations

import os
import pickle
import sys
import time

# Node layout from src/cowrie/scripts/fsctl.py:
#   [name, type, uid, gid, size, mode, ctime, contents, target, realfile]
A_NAME, A_TYPE, A_UID, A_GID, A_SIZE, A_MODE, A_CTIME, A_CONTENTS = range(8)
T_DIR = 1

# Perms: drwxrwxrwt = 0o41777 ; drwx------ = 0o40700 (type_bits | perm_bits)
# Cowrie stores type+mode combined, matching stat(2) st_mode. T_DIR => 0o040000.
MODE_TMP = 0o041777          # drwxrwxrwt (for .font-unix / .ICE-unix)
MODE_SYSTEMD_PRIV = 0o040700  # drwx------ (for systemd-private-*)


def find_tmp(fs):
    """Walk the fs root node to /tmp. Return the /tmp node (raises if missing)."""
    for child in fs[A_CONTENTS]:
        if child[A_NAME] == "tmp":
            return child
    raise SystemExit("fs.pickle has no /tmp node — aborting, pickle may be corrupt")


def make_dir(name: str, mode: int, size: int, ctime: float) -> list:
    """Mint a directory node matching cowrie's list-shape."""
    return [
        name,       # A_NAME
        T_DIR,      # A_TYPE
        0,          # A_UID   (root)
        0,          # A_GID   (root)
        size,       # A_SIZE  (ref Pi shows 40 for unix-socket dirs, 60 for systemd-private)
        mode,       # A_MODE
        ctime,      # A_CTIME
        [],         # A_CONTENTS (empty dirs)
        None,       # A_TARGET
        None,       # A_REALFILE
    ]


def harden(fs_path: str) -> None:
    with open(fs_path, "rb") as f:
        fs = pickle.load(f)

    tmp = find_tmp(fs)
    before = sorted(child[A_NAME] for child in tmp[A_CONTENTS])
    print(f"/tmp before: {before}")

    # Use a single ctime close to the ref-Pi capture so timestamps look
    # coherent with recent-boot activity. Not byte-perfect (cowrie's ls
    # renders dates as ISO anyway) but plausible.
    now = time.time()

    # Red-team-plausible /tmp content. The systemd-private-* hash in the
    # middle is the machine-id from hostnamectl (see lookup_table.py); keeping
    # it consistent means `cat /etc/machine-id` and these dir names line up.
    MACHINE_ID = "b384a3a8d40c4ca9b688908a728ddf54"
    new_contents = [
        make_dir(".font-unix", MODE_TMP, 40, now),
        make_dir(".ICE-unix", MODE_TMP, 40, now),
        make_dir(f"systemd-private-{MACHINE_ID}-bluetooth.service-5iy5tq",
                 MODE_SYSTEMD_PRIV, 60, now),
        make_dir(f"systemd-private-{MACHINE_ID}-polkit.service-mWUU9f",
                 MODE_SYSTEMD_PRIV, 60, now),
        make_dir(f"systemd-private-{MACHINE_ID}-systemd-hostnamed.service-hPDFoW",
                 MODE_SYSTEMD_PRIV, 60, now),
        make_dir(f"systemd-private-{MACHINE_ID}-systemd-logind.service-pHDZ3U",
                 MODE_SYSTEMD_PRIV, 60, now),
    ]
    tmp[A_CONTENTS] = new_contents
    # Also bump /tmp's own ctime so `ls -la /` shows a recent /tmp entry.
    tmp[A_CTIME] = now
    tmp[A_MODE] = MODE_TMP  # drwxrwxrwt, matching ref Pi

    after = sorted(child[A_NAME] for child in tmp[A_CONTENTS])
    print(f"/tmp after:  {after}")

    # Atomic write so a mid-save crash can't corrupt fs.pickle.
    tmp_path = fs_path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(fs, f)
    os.replace(tmp_path, fs_path)
    print(f"wrote {fs_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: harden_tmp.py <fs.pickle path>")
    harden(sys.argv[1])
