#!/usr/bin/env python3
"""capture_truth.py — record (stdout, stderr, rc, latency) for each probe.

Intended to run ON the reference Pi. Streamed over SSH so nothing lands on
the Pi's disk and nothing contaminates /tmp:

    ssh pi@<ref_pi> 'python3 -' < scalpel/scripts/capture_truth.py \\
        > scalpel/tests/ground_truth.jsonl

Output is JSONL on stdout, one record per (command, run). Progress on stderr.
Environment knobs: RUNS (default 5), TIMEOUT seconds (default 10).
"""
import json
import os
import subprocess
import sys
import time

RUNS = int(os.environ.get("RUNS", "5"))
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))


def login_path() -> str:
    """Extract PATH from a login shell without capturing profile.d banners.

    `bash -lc` sources /etc/profile + /etc/profile.d/* + ~/.profile so PATH
    matches interactive-SSH reality (includes /usr/sbin → arp, ip, stat…).
    But profile.d scripts like sshpwd.sh print to stdout, which would
    contaminate every probe if we ran `bash -lc` for every command.

    Solution: run bash -lc once, sentinel-wrap the PATH echo, grep the
    sentinel, and reuse that PATH for plain `bash -c` probe runs.
    """
    proc = subprocess.run(
        ["bash", "-lc", "printf '\\n__PATH__=%s\\n' \"$PATH\""],
        capture_output=True,
        text=True,
        timeout=10,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__PATH__="):
            return line[len("__PATH__="):]
    # Fallback: Debian root login PATH. Shouldn't happen on a healthy Pi.
    return "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


PROBE_ENV = os.environ.copy()
PROBE_ENV["PATH"] = login_path()

# Regular probes: run RUNS times each so we can distinguish deterministic
# output (Tier 1 lookup material) from variable output (Tier 2 LLM).
PROBES = [
    # identity / shell
    "whoami", "id", "pwd", "hostname", "hostnamectl",
    "echo $USER", "echo $HOME", "echo $SHELL", "echo $PATH",

    # ARM64/Debian fingerprint surface
    "uname -a", "uname -m", "uname -r", "uname -s", "uname -v", "arch",
    "cat /proc/cpuinfo", "cat /proc/version", "cat /proc/meminfo",
    "cat /etc/os-release", "cat /etc/debian_version", "cat /etc/issue",
    "lscpu", "lsb_release -a", "uptime", "date",

    # memory / disk
    "free -m", "free -h", "df -h", "df -i", "mount",

    # processes
    "ps aux", "ps -ef", "top -bn1", "w", "who", "users", "last -n 10",

    # filesystem listing
    "ls", "ls -la", "ls /", "ls -la /",
    "ls /home", "ls /root", "ls -la /root",
    "ls /tmp", "ls -la /tmp", "ls /var/log", "ls /etc",

    # common file contents (some should fail — we want the exact error text)
    "cat /etc/passwd", "cat /etc/group", "cat /etc/hosts",
    "cat /etc/resolv.conf", "cat /etc/shadow",
    "stat /etc/passwd", "stat /root",

    # environment / history
    "env", "printenv", "history", "alias",

    # networking
    "ip a", "ip addr show", "ip route", "ip link",
    "arp -a", "arp", "route", "route -n",
    "ss -tulpn", "ss -tnp", "ss -tulwn", "ss -tuln",
    "netstat -tulpn", "netstat -rn", "netstat -an",
    "hostname -I",

    # privilege recon
    "sudo -n -l", "sudo -l", "cat /etc/sudoers",
    "ls -la /etc/cron.d", "ls -la /etc/crontab", "ls -la /etc/cron.daily",
    "crontab -l", "cat /root/.bash_history",
    "ls -la /root/.ssh", "cat /root/.ssh/authorized_keys",

    # persistence recon
    "systemctl list-units --type=service --no-pager",
    "systemctl list-timers --no-pager",
    "ls /etc/systemd/system/", "cat /etc/rc.local",

    # package / tool inventory
    "dpkg -l",
    "which python", "which python3", "which python2", "which perl",
    "which ruby", "which gcc", "which make", "which curl", "which wget",
    "which nc", "which ncat", "which nmap", "which tcpdump", "which strace",
    "command -v bash", "bash --version",
    "nc -h", "nc",

    # SSH surface
    "ls /etc/ssh/", "cat /etc/ssh/sshd_config",

    # error-path probes — honeypot must fail IDENTICALLY
    "nmap", "ncat", "nonexistentcmd12345",
    "cat /root/nothere", "ls /nothere", "cd /nothere",

    # misc
    "ls -la ~", "file /bin/bash", "readlink -f /bin/sh",

    # --- additions (deterministic, red-team-probed, not in lookup yet) ---

    # pi-specific fingerprint surface
    "cat /etc/machine-id", "cat /etc/timezone", "cat /etc/rpi-issue",
    "cat /boot/config.txt", "cat /boot/cmdline.txt",
    "cat /boot/firmware/config.txt", "cat /boot/firmware/cmdline.txt",
    "ls /boot", "ls -la /boot", "ls /boot/firmware",

    # uname variants the red team scripts often collect together
    "uname", "uname -o", "uname -p", "uname -i", "uname -n",

    # kernel / hardware inventory
    "lsmod", "lsblk", "lspci", "lsusb",
    "cat /proc/cmdline", "cat /proc/mounts", "cat /proc/modules",

    # locale / timezone
    "locale", "timedatectl", "readlink /etc/localtime",

    # apt / package surface
    "cat /etc/apt/sources.list", "ls /etc/apt/sources.list.d/",
    "apt-cache policy", "apt --version", "dpkg --version",

    # ssh / services
    "systemctl is-active ssh", "systemctl is-enabled ssh",
    "systemctl status ssh --no-pager", "cat /etc/ssh/sshd_config.d/",
    "ls /etc/ssh",

    # common file-tree reconnaissance
    "ls /var", "ls -la /var", "ls /opt", "ls -la /opt",
    "ls /usr", "ls /usr/bin", "ls /usr/sbin", "ls /usr/local",
    "ls /media", "ls /mnt",

    # file metadata on canonical binaries / configs
    "stat /bin/bash", "stat /bin/sh", "stat /usr/bin/python3",
    "stat /etc/os-release", "stat /etc/hostname",
    "file /bin/sh", "file /usr/bin/python3", "file /etc/passwd",

    # individual env vars (printenv KEY form the red team uses
    # to isolate specific values)
    "printenv HOME", "printenv USER", "printenv PATH",
    "printenv SHELL", "printenv LANG",

    # common tool lookups we missed
    "which python3", "which sh", "which ls", "which cat",
    "which sudo", "which apt", "which apt-get", "which docker",
    "which git", "which vim", "which nano",
    "type ls", "type cd", "type echo",

    # pi/arm-specific binaries
    "which vcgencmd", "vcgencmd version",

    # misc red-team favorites
    "getent passwd root", "getent group root", "groups",
    "cat /etc/login.defs", "cat /etc/host.conf", "cat /etc/fstab",
    "cat /etc/bash.bashrc", "cat /etc/profile",

    # pi-user discovery — Pi OS always has a pi account, red team
    # enumerates it reflexively
    "getent passwd pi", "groups pi", "finger pi",
    "ls /home/pi", "ls -la /home/pi", "ls -la /home",
    "cat /home/pi/.bashrc", "cat /home/pi/.bash_history",
    "cat /home/pi/.profile", "ls -la /home/pi/.ssh",
    "cat /home/pi/.ssh/authorized_keys",

    # network config (legacy but still probed)
    "cat /etc/network/interfaces", "cat /etc/dhcpcd.conf",
    "cat /etc/NetworkManager/NetworkManager.conf",
    "ls /etc/NetworkManager/system-connections/",

    # firewall surface
    "iptables -L -n", "iptables -t nat -L -n", "iptables -L -v",
    "ufw status", "nft list ruleset",

    # persistence detail — individual cron files
    "cat /etc/crontab",
    "ls /etc/cron.hourly", "ls /etc/cron.weekly", "ls /etc/cron.monthly",
    "ls /etc/cron.yearly", "ls /var/spool/cron/crontabs",
    "ls /etc/init.d",

    # container / VM detection — always run by any serious red teamer
    "systemd-detect-virt", "systemd-detect-virt -c", "systemd-detect-virt -v",
    "cat /proc/1/cgroup", "cat /proc/1/sched",
    "ls -la /.dockerenv", "ls -la /run/.containerenv",

    # log hunting — these are dynamic but red team still runs them
    "ls -la /var/log", "tail -20 /var/log/syslog",
    "tail -20 /var/log/auth.log", "ls /var/log/apt",
    "cat /var/log/dpkg.log",

    # defense tool detection
    "which osqueryd", "which falco", "which auditctl",
    "which clamscan", "which rkhunter", "which chkrootkit",
    "systemctl status auditd --no-pager",

    # bash startup / profile surface
    "ls /etc/profile.d", "cat /etc/skel/.bashrc",
    "cat /etc/skel/.profile", "cat ~/.bashrc", "cat ~/.profile",
]

# Slow probes: run once — output is generally stable and full repeats
# would burn minutes on `find /` style commands.
SLOW_PROBES = [
    "find / -perm -4000 -type f 2>/dev/null",
    "find / -writable -type d 2>/dev/null",
    "getcap -r / 2>/dev/null",
    "grep -r password /etc 2>/dev/null",
    "apt list --installed 2>/dev/null",
    "du -sh /home/*",
    "find / -name '*.sql' 2>/dev/null",
    # Classic red-team credential / key sweeps. All slow on a full fs walk.
    "find / -name id_rsa 2>/dev/null",
    "find / -name id_rsa.pub 2>/dev/null",
    "find / -name authorized_keys 2>/dev/null",
    "find / -name '*.pem' 2>/dev/null",
    "find / -name '*.key' 2>/dev/null",
    "find / -name 'known_hosts' 2>/dev/null",
    "find / -name '.git' -type d 2>/dev/null",
    "find / -name 'wp-config.php' 2>/dev/null",
    "find / -name 'credentials*' 2>/dev/null",
    "find /opt -type f 2>/dev/null",
    "find /home -type f 2>/dev/null",
    "find /tmp /var/tmp /dev/shm -type f 2>/dev/null",
]


def run_probe(cmd: str, run: int) -> None:
    t0 = time.perf_counter_ns()
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            timeout=TIMEOUT,
            text=True,
            errors="replace",
            env=PROBE_ENV,
        )
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = (e.stderr or "") + f"\n[timeout after {TIMEOUT}s]"
        rc = 124
    t1 = time.perf_counter_ns()

    record = {
        "cmd": cmd,
        "run": run,
        "rc": rc,
        "ns": t1 - t0,
        "stdout": stdout,
        "stderr": stderr,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)


def main() -> None:
    total = len(PROBES) * RUNS + len(SLOW_PROBES)
    i = 0
    for cmd in PROBES:
        for run in range(1, RUNS + 1):
            i += 1
            print(f"[{i}/{total}] {cmd} (run {run})", file=sys.stderr, flush=True)
            run_probe(cmd, run)
    for cmd in SLOW_PROBES:
        i += 1
        print(f"[{i}/{total}] {cmd} (slow, 1 run)", file=sys.stderr, flush=True)
        run_probe(cmd, 1)
    print(f"done — {total} records emitted", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
