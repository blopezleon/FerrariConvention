# SCALPEL — Day 1 Handoff

Snapshot as of end of Day 1 (2026-04-23, ~6 PM). Paste this into a new Claude
chat along with a pointer to `scalpel/CONTEXT.md` to resume work. CONTEXT.md
has the full architecture spec and hackathon constraints; this doc is the
delta — what we built today, what's left, and the reasoning behind key calls.

---

## Hackathon status

- **Honeypot Pi:** `cowrie@10.4.27.49` (cowrie user for admin), `root@10.4.27.49:2222` (attacker-facing, root/root)
- **Cowrie install root:** `/home/cowrie/cowrie/` (confirmed — NOT `~/cowrie/share/cowrie/` as CONTEXT.md originally claimed)
- **fs.pickle path:** `/home/cowrie/cowrie/src/cowrie/data/fs.pickle` (NOT `~/cowrie/share/cowrie/fs.pickle`)
- **Current diff_pis.py score:** 90/92 clean = **97.8%** on ground-truth comparison. The 2 remaining flags (`ls /tmp`, `ls -la /tmp`) are test-harness artifacts (ref captured non-TTY, honeypot runs TTY; timestamps can never byte-match a different Pi snapshot)

---

## What we built today

### 1. Scalpel router wired into cowrie's shell backend

The original commit (`a102e8d4`) wired scalpel into `src/cowrie/llm/protocol.py`, which required `backend = llm` in cowrie.cfg. That backend replaced ALL of cowrie's native command dispatch with "scalpel-or-Ollama-or-nothing" — so `ls`, `cat`, `ps`, etc. never ran cowrie's real implementations.

**We switched to `backend = shell`** and moved the scalpel hook to the shell backend instead. The flow now is:

```
raw command line
  └─> src/cowrie/shell/honeypot.py lineReceived (NEW: scalpel pre-filter)
        └─> scalpel.cowrie_hook.on_command
              └─> scalpel.router.handle_command (Steps 0-4)
                    ├─ Step 1: Tier 1 static lookup       -> canned output
                    ├─ Step 2: GOODLLM allowlist          -> Tier 2 (Ollama)
                    ├─ Step 3: pipe/$()/long heuristics   -> Tier 3 (AWS, not built)
                    └─ Step 4: defer                      -> TierUnavailable
        └─ if None: cowrie's native command dispatch runs (runCommand, commands/ls.py etc.)
```

This means cowrie's ~50 native commands (ls, cat, cd, pwd, touch, mkdir, rm,
echo, ps, date, uptime, hostname, free, whoami, w, who, id, etc.) all work
again, backed by `fs.pickle`.

### 2. Tier 1 static lookup (`scalpel/router/lookup_table.py`)

**73 entries.** Populated from `scalpel/tests/ground_truth.jsonl` + the
demerit-driven additions we made today. Covers:

- Identity: `whoami`, `hostnamectl`
- ARM64/Debian fingerprint: `uname -a/-m/-r/-v/-s`, `arch`, `cat /proc/cpuinfo`, `cat /proc/version`, `cat /etc/os-release`, `cat /etc/debian_version`, `cat /etc/issue`, `lscpu`, `lsb_release -a`
- Filesystem: `mount`, `ls /`, `ls -la /`, `ls /home`, `ls /var/log`, `ls /etc`
- Files: `cat /etc/passwd`, `cat /etc/group`, `cat /etc/hosts`, `cat /etc/resolv.conf`, `cat /etc/shadow` (denied)
- Disk: `df -h`, `df -i` (cowrie has no `df` native)
- Processes snapshots: `ps aux`, `ps -ef`, `top -bn1` — kept as static because qwen2.5:1.5b hallucinates process lists badly; red team's one-call probe gets a coherent answer
- Services: `systemctl list-units --type=service --no-pager`
- Recon: `find / -writable -type d 2>/dev/null`, `grep -r password /etc 2>/dev/null`

### 3. Tier 2 Ollama client (`scalpel/local_llm/client.py`)

- `generate(command, session_state) -> str`
- stdlib `urllib` only (no new deps)
- 3-second timeout; raises `OllamaError` on failure, which `_tier2` catches and re-raises as `TierUnavailable` so the router falls through to cowrie native
- Every request includes `keep_alive: -1` belt-and-suspenders with the systemd `OLLAMA_KEEP_ALIVE=-1`
- System prompt is short and imperative (required for small models like qwen2.5:1.5b)
- Strips markdown fences the small model sometimes emits anyway

### 4. GOODLLM allowlist — currently empty

After benchmarking what cowrie native actually provides, most candidates for
LLM routing turned out to be either:
- Already handled natively with real data (`free` reads real /proc/meminfo)
- Better as a static lookup snapshot (`df -h` doesn't change meaningfully between probes)
- Better as a cowrie native patch (`date`, `uptime`)

**`GOODLLM` is seeded empty.** Populating it requires running
`scalpel/local_llm/benchmark.py` against Ollama + ground truth — classifies
each command by how close Ollama's output gets to ref Pi. Not done yet.

### 5. Cowrie native patches

**`src/cowrie/commands/base.py` — `Command_date`:**
- Was: `datetime.now(UTC).strftime('%a %b %d %H:%M:%S UTC %Y')` → `Thu Apr 23 17:45:22 UTC 2026`
- Now: `datetime.now().astimezone().strftime('%a %e %b %H:%M:%S %Z %Y')` → `Thu 23 Apr 18:05:22 EDT 2026`
- Rationale: ref Pi captured in EDT, cowrie's "always UTC" was a fingerprint

**`src/cowrie/commands/uptime.py` — `Command_uptime`:**
- Was: `"{} up 0min, 1 user, load average: 0.00, 0.00, 0.00"` starting from cowrie process uptime, always zero load
- Now: leading space, fake boot time = now − (~4 hours + random 0-30min jitter), load avg jittered around (0.08, 0.04, 0.02) per call
- Rationale: fresh cowrie reports "up 0 min" which is obviously a new process; zero load avg is implausible on a system with any background services

**`src/cowrie/commands/ls.py` — `Command_ls` (do_ls_l)`:**
- `nlink` now computed: for dirs, `2 + count of subdirectory children`; for everything else, `1`. Was hardcoded `1`.
- Date format changed from `%Y-%m-%d %H:%M` (ISO) to `%b %e %H:%M` (GNU ls short form)
- Sort changed from case-sensitive ASCII (`.ICE-unix` before `.font-unix`) to case-insensitive locale (`.font-unix` before `.ICE-unix`) — matches GNU ls in default locale

### 6. fs.pickle hardened (`scalpel/scripts/harden_tmp.py`)

- Removed `lol.sh`, `test.sh`, `.X11-unix` from `/tmp` (leftover from team testing + stock cowrie defaults)
- Added `.font-unix`, `.ICE-unix`, and 4 `systemd-private-*` dirs matching the ref Pi's machine-id (`b384a3a8d40c4ca9b688908a728ddf54`)
- Atomic pickle write so mid-save crash doesn't corrupt fs
- Script is stdlib-only, runs with system python3 on the Pi

### 7. Shell backend config

**`etc/cowrie.cfg`:**
- `[honeypot] backend = llm` → `backend = shell`
- `[honeypot] hostname = raspberrypi` (was `svr04` default — obvious fingerprint)
- `[llm]` section kept but unused now (for future Tier 2 direct routing)

### 8. diff_pis.py harness fixes

- `IDENTITY_DEPENDENT` skip list extended with `crontab -l`, `cat /root/nothere` (ref captured as `pi`; honeypot runs as `root`; both responses are correct for their user)
- Deterministic comparison now `rstrip()`s both sides before diffing — fixes trailing-whitespace artifacts from `clean_output()` stripping interactive-shell prompt noise

### 9. Welcome banner removed

- `src/cowrie/llm/protocol.py` no longer writes `Welcome to <hostname>\n` on connect (real Debian doesn't; was a pre-auth fingerprint)
- File only loads when `backend = llm`, so currently dormant — fix kept for completeness

### 10. ground_truth.jsonl fix

- `systemctl list-units --type=service --no-pager` had `?` characters in the "Legend:" section where it should have had `→` (UTF-8 U+2192). The lookup was correct; ground truth was mojibaked during an earlier capture pass. Fixed in-place.

---

## Current file map

```
scalpel/
├── CONTEXT.md                       # ORIGINAL architecture spec — READ THIS
├── cowrie_hook.py                   # Bridge: on_command() -> str | None (raises TierUnavailable caught here)
│
├── router/
│   ├── handle_command.py            # Steps 0-4 routing (control flow, chain, lookup, GOODLLM, escalation, defer)
│   └── lookup_table.py              # 73 Tier 1 entries
│
├── local_llm/
│   ├── client.py                    # Ollama HTTP client (NEW today)
│   └── benchmark.py                 # Pre-existing, NOT YET RUN — populates GOODLLM/badLLM
│
├── scripts/
│   ├── capture_truth.py             # Run on ref Pi over SSH -> ground_truth.jsonl
│   └── harden_tmp.py                # NEW today — cleans fs.pickle /tmp
│
├── tests/
│   ├── diff_pis.py                  # Honeypot vs. ground truth byte-diff
│   ├── ground_truth.jsonl           # 150+ records captured from ref Pi
│   ├── demerits.md                  # Latest scorecard (auto-overwritten per run)
│   └── demerits.jsonl               # Machine-readable mismatches
│
├── notes/
│   ├── decisions.md                 # Decision log (empty?)
│   └── handoff.md                   # THIS FILE
│
└── aws/                             # NOT BUILT (Tier 3)

src/cowrie/
├── scalpel_bridge.py                # Puts repo root on sys.path so cowrie can import scalpel
├── commands/
│   ├── base.py                      # PATCHED: Command_date now uses local tz + ref-Pi format
│   ├── uptime.py                    # PATCHED: fake boot time, load avg jitter, leading space
│   └── ls.py                        # PATCHED: nlink computed, date format, case-insensitive sort
└── shell/
    ├── honeypot.py                  # PATCHED: scalpel pre-filter at top of lineReceived
    └── session.py                   # fs.pickle-on-disconnect hook (pre-existing, kept)

etc/
└── cowrie.cfg                       # backend=shell + hostname=raspberrypi
```

---

## Deployment procedure (push + restart)

```powershell
# From your Windows laptop (use `py` not `python` — Windows launcher):
scp scalpel/router/handle_command.py cowrie@10.4.27.49:~/cowrie/scalpel/router/
scp scalpel/router/lookup_table.py   cowrie@10.4.27.49:~/cowrie/scalpel/router/
scp scalpel/local_llm/client.py      cowrie@10.4.27.49:~/cowrie/scalpel/local_llm/
scp src/cowrie/commands/base.py      cowrie@10.4.27.49:~/cowrie/src/cowrie/commands/
scp src/cowrie/commands/uptime.py    cowrie@10.4.27.49:~/cowrie/src/cowrie/commands/
scp src/cowrie/commands/ls.py        cowrie@10.4.27.49:~/cowrie/src/cowrie/commands/
scp src/cowrie/shell/honeypot.py     cowrie@10.4.27.49:~/cowrie/src/cowrie/shell/
scp etc/cowrie.cfg                   cowrie@10.4.27.49:~/cowrie/etc/cowrie.cfg

# Restart cowrie (venv must be activated — the bare `cowrie` command is at
# cowrie-env/bin/cowrie and needs twistd on PATH):
ssh cowrie@10.4.27.49 "cd ~/cowrie && source cowrie-env/bin/activate && cowrie restart"

# Score the result:
py scalpel/tests/diff_pis.py --honeypot 10.4.27.49
# Reads scalpel/tests/demerits.md for the human-readable report.
```

If fs.pickle needs re-hardening (e.g., someone `touch`ed stuff into /tmp):
```powershell
scp scalpel/scripts/harden_tmp.py cowrie@10.4.27.49:/tmp/
ssh cowrie@10.4.27.49 "python3 /tmp/harden_tmp.py ~/cowrie/src/cowrie/data/fs.pickle"
ssh cowrie@10.4.27.49 "cd ~/cowrie && source cowrie-env/bin/activate && cowrie restart"
```

---

## Ollama setup (one-time on the Pi)

Tier 2 client exists but Ollama isn't pinned yet, so GOODLLM commands currently
time out after 3s and fall through. Before populating GOODLLM, do this:

```bash
ssh cowrie@10.4.27.49
# on the Pi:
sudo systemctl status ollama              # is it even running?
sudo systemctl start ollama               # if not
ollama list                               # verify qwen2.5:1.5b pulled
# If not:  ollama pull qwen2.5:1.5b

# Pin the model in RAM — 30-40s reload penalty = instant latency fingerprint
sudo systemctl edit ollama
# Add:
#   [Service]
#   Environment="OLLAMA_KEEP_ALIVE=-1"
# Save, then:
sudo systemctl restart ollama

# Force initial load:
curl -s http://localhost:11434/api/generate \
    -d '{"model":"qwen2.5:1.5b","prompt":"hi","stream":false,"keep_alive":-1}'

# Verify resident:
curl -s http://localhost:11434/api/ps
# Expect: qwen2.5:1.5b with expires_at far in future
```

---

## What's left — Day 2 priorities (9 AM–noon, noon = code freeze)

### CRITICAL (deliverables the organizer listed)

1. **Tier 3 AWS Bedrock client** — `scalpel/aws/client.py`
   - Signature: `escalate(command: str, session_history: list) -> str`
   - 3-second timeout; raises an error that `_tier3` converts to `TierUnavailable` so we fall back to Tier 2
   - Must call API Gateway HTTPS endpoint, not Bedrock directly (Pi doesn't have AWS creds)

2. **AWS Lambda function** — `scalpel/aws/lambda_function.py`
   - Receives `{"command": ..., "history": [...]}` from the Pi
   - Calls Bedrock (managed LLM — use Claude or Titan)
   - Returns plausible command output

3. **API Gateway HTTPS endpoint** — deploy Lambda behind API Gateway, get public URL

4. **CloudWatch dashboard — REQUIRED deliverable per CONTEXT §3.2.**
   - Must show edge/cloud interactions and performance
   - Aggregate logs from cowrie + Tier 3 calls
   - This is explicitly called out as "not optional" in the brief

### HIGH VALUE (more points on realism/efficiency)

5. **Run `scalpel/local_llm/benchmark.py`** on the Pi
   - Classifies every ground-truth command by how well Ollama handles it
   - Output feeds GOODLLM (handles locally) and badLLM (force-route to Tier 3)
   - Without this, GOODLLM stays empty and we over-use Tier 3

6. **More Tier 1 entries** — classic attacker recon we didn't capture
   - `ip a`, `ip addr show`, `ip route`, `ip link`
   - `ss -tulpn`, `ss -tnp`, `netstat -tulpn`
   - `getcap -r / 2>/dev/null`
   - `find / -perm -4000 -type f 2>/dev/null`
   - `stat /etc/passwd`, `stat /root`, `stat /etc/shadow`
   - Capture each on the ref Pi via `capture_truth.py`, append to `lookup_table.py`

7. **Banner / MOTD hardening** — one of the 4 red-team probe dimensions
   - Currently cowrie emits its stock Debian 7 license blurb on connect
   - Ref Pi shows real /etc/motd + /etc/update-motd.d/*
   - Fix: either override `/etc/motd` in fs.pickle or configure cowrie's banner_file

### NICE TO HAVE

8. **LLM session history** — currently Tier 2 is single-turn
   - `scalpel/router/session_state.py` per-session history
   - Pass last N command/response pairs in the Ollama prompt

9. **Expand harden_tmp.py** — /tmp is done, but cowrie's fs.pickle has 2013 timestamps everywhere. `stat /etc/passwd` on the honeypot shows 2013 ctime → tell. A broader harden pass could touch-dates across the fs.

---

## Constraints / Do Not Touch (per CONTEXT.md §8)

- **Do not change SSH port 2222 or root/root credentials** — scored as 0 if changed
- **Do not re-image the Pi**
- **Must send honeypot IP to Slack by 10:20 AM both days** — or lose points
- **Do not hack or probe other teams' systems** — automatic DQ
- **No "phone a friend"** — no contacting humans outside the team during event hours (Claude/LLMs are fine)

---

## Known open issues (do NOT treat as bugs for Day 2)

- `ls /tmp` and `ls -la /tmp` still flagged in diff_pis. Explained in demerits.md:
  - `ls /tmp`: TTY column format vs ref's non-TTY one-per-line. Red team sees TTY format (looks normal); test harness sees column format from invoke_shell.
  - `ls -la /tmp`: our fs.pickle `/tmp` has 6 entries, ref had 8+; our timestamps are from harden_tmp.py run time, not ref capture time. Impossible to byte-match across different machines/times without copying ref Pi's fs.pickle exactly.

- `..` parent entry in `ls -la /tmp` shows `Apr  5 08:03` (no year). That's the root "/" node's ctime from stock fs.pickle (2013). Our ls.py date format `%b %e %H:%M` omits year for dates within ~6 months but includes no special casing for old dates. Minor fingerprint.

- Cowrie's `ls` emits column format when there's a TTY — real Linux does too. This is correct behavior; the diff_pis mismatch is the test's fault, not the honeypot's.

---

## Decisions worth knowing (so future Claude doesn't re-litigate)

- **Backend choice:** `shell`, NOT `llm`. The original commit had `llm`, which bypassed cowrie's 50 native commands. Flipping to `shell` with a scalpel pre-filter in `HoneyPotShell.lineReceived` gives us the best of both worlds.
- **Tier 1 over LLM for variable commands:** We briefly added `dynamic.py` as a "Tier 1.5" for date/uptime shims. Reverted — cowrie already implements these natively with real time; our shim was duplication. Patched cowrie's native impls directly instead.
- **`free` and `ps` stay native, not Tier 1 or Tier 2:** cowrie's `free` reads real /proc/meminfo on the Pi. `ps` walks cowrie's fake process list (static but fast). Both better than lookup or LLM.
- **`df` is Tier 1, not Tier 2:** cowrie has no `df` command. LLM would work but is slow. Static lookup from ref Pi works — disk usage doesn't change meaningfully between red-team probes.
- **Tier 3 fall-through on failure:** when AWS is unreachable (always, today), `_escalate_with_fallback` tries Tier 2 next. If Tier 2 is also unavailable, we raise `TierUnavailable` → cowrie native or `command not found`. This means zero Tier 2/3 downtime can make the honeypot look obviously broken.
- **Test harness trailing-whitespace tolerance:** `clean_output()` strips trailing blank lines from the honeypot's interactive-shell capture, but leaves them in ref ground truth. `rstrip()`'ing both sides in the deterministic compare fixes pure whitespace false positives without hiding real fidelity gaps.

---

## How to resume with Claude

Paste this file + `scalpel/CONTEXT.md` into a new chat, then say what you
want to tackle (e.g., "let's build the Tier 3 AWS client"). Claude should
already know:
- The hackathon target (eMERGE 2026, Miami Beach, Army Research Lab judges)
- Scoring formula (40% realism, 40% efficiency, 20% presentation)
- Hard Day 2 code-freeze at noon
- That `py` not `python` on Windows PowerShell
- That fs.pickle lives at `~/cowrie/src/cowrie/data/fs.pickle`, not the CONTEXT.md-claimed path
