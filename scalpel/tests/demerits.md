# Honeypot Demerit Report

**Honeypot:** `root@10.4.27.49:2222`  
**Ground truth:** `scalpel/tests/ground_truth.jsonl`  
**Generated:** 2026-04-23 17:37:03

## Summary

- **Tested:** 92 commands
- **Clean:** 90
- **Flagged:** 2 (2.2%)
- **Skipped (identity-dependent):** 26 — captured as `pi` user on ref Pi but red team logs into cowrie as `root`, so these mismatches are test-setup artifacts, not real fidelity gaps.

### Findings by type

| Count | Type |
|-------|------|
| 2 | output mismatch |

## Flagged commands

### `ls /tmp` — deterministic

- output mismatch

**Expected:**
```
systemd-private-b384a3a8d40c4ca9b688908a728ddf54-bluetooth.service-5iy5tq
systemd-private-b384a3a8d40c4ca9b688908a728ddf54-polkit.service-mWUU9f
systemd-private-b384a3a8d40c4ca9b688908a728ddf54-systemd-hostnamed.service-hPDFoW
systemd-private-b384a3a8d40c4ca9b688908a728ddf54-systemd-logind.service-pHDZ3U
```

**Got:**
```

systemd-private-b384a3a8d40c4ca9b688908a728ddf54-bluetooth.service-5iy5tq         systemd-private-b384a3a8d40c4ca9b688908a728ddf54-polkit.service-mWUU9f            systemd-private-b384a3a8d40c4ca9b688908a728ddf54-systemd-hostnamed.service-hPDFoW systemd-private-b384a3a8d40c4ca9b688908a728ddf54-systemd-logind.service-pHDZ3U    
```

---

### `ls -la /tmp` — deterministic

- output mismatch

**Expected:**
```
total 4
drwxrwxrwt 10 root root  200 Apr 23 12:21 .
drwxr-xr-x 18 root root 4096 Apr 12 20:06 ..
drwxrwxrwt  2 root root   40 Apr 22 15:15 .font-unix
drwxrwxrwt  2 root root   40 Apr 22 15:15 .ICE-unix
drwx------  3 root root   60 Apr 22 15:15 systemd-private-b384a3a8d40c4ca9b688908a728ddf54-bluetooth.service-5iy5tq
drwx------  3 root root   60 Apr 23 11:23 systemd-private-b384a3a8d40c4ca9b688908a... [+380B]```

**Got:**
```
drwxrwxrwt 8 root root 4096 Apr 23 17:22 .
drwxr-xr-x 22 root root 4096 Apr  5 08:03 ..
drwxrwxrwt 2 root root   40 Apr 23 17:22 .font-unix
drwxrwxrwt 2 root root   40 Apr 23 17:22 .ICE-unix
drwx------ 2 root root   60 Apr 23 17:22 systemd-private-b384a3a8d40c4ca9b688908a728ddf54-bluetooth.service-5iy5tq
drwx------ 2 root root   60 Apr 23 17:22 systemd-private-b384a3a8d40c4ca9b688908a728ddf54-polk... [+261B]```

---

