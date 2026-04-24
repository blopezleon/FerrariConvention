"""Three-tier command router. See scalpel/CONTEXT.md "Routing Decision Logic".

Entry point: handle_command(command, session_id) -> str

Flow (top-down, first match wins):
  Step 0   compound command with control-flow keywords  -> Tier 3 (whole block)
  Step 0b  simple chain (;, &&, ||)                     -> recurse per piece
  Step 1   Tier 1 lookup table                          -> instant canned output
  Step 2   GOODLLM allowlist                            -> Tier 2 (local LLM)
  Step 3   heuristic escalation:
             pipe, $(...), backticks, len > 80          -> Tier 3 (+ Tier 2 fb)
  Step 4   default: raise TierUnavailable               -> cowrie native handles

Contract: raising TierUnavailable signals cowrie_hook to return None, which
tells shell/honeypot.py to fall through to cowrie's native command dispatch.
That is the correct path for `ls`, `cat`, `touch`, etc. — anything cowrie
implements natively and walks the fs.pickle for.
"""
from __future__ import annotations

import re

from scalpel.router import lookup_table


class TierUnavailable(Exception):
    """The tier that would answer this command is not wired in yet, OR the
    router deliberately defers to cowrie native (see Step 4)."""


_CONTROL_FLOW_TOKENS = (" for ", " while ", " if ", " case ", " until ", "function ")
_CHAIN_SPLIT_RE = re.compile(r"(?:;|&&|\|\||\n)")

# Step 2: commands whose output should be *generated fresh per call* by the
# local LLM. These are commands whose ref-Pi value is inherently time-varying
# (uptime, date), load-varying (free, uptime), or space-varying (df) — a
# static lookup would betray the honeypot on a second call. Kept deliberately
# tight: only commands Ollama reliably fakes well at 1.5B params. Expand as
# scalpel/local_llm/benchmark.py produces classifier results.
GOODLLM: frozenset[str] = frozenset(
    {
        # Populated by scalpel/local_llm/benchmark.py once it runs Ollama
        # against every command in ground_truth.jsonl and classifies which
        # outputs stay close to ref Pi. Empty today because:
        #   - free, uptime, date, ps, hostname, w, who, id, whoami → cowrie
        #     native (fast, real-time, no LLM needed)
        #   - df -h/-i → static lookup (cowrie has no `df`; values barely
        #     change between red-team probes)
        #   - everything else uses cowrie's `command not found` or a heuristic
        #     escalation to Tier 3 (AWS, not yet built)
    }
)


def handle_command(command: str, session_id: str) -> str:
    cmd = command.strip()
    if not cmd:
        return ""

    if _is_control_flow(cmd):
        return _escalate_with_fallback(cmd, session_id)

    parts = [p.strip() for p in _CHAIN_SPLIT_RE.split(cmd) if p.strip()]
    if len(parts) > 1:
        return "".join(handle_command(p, session_id) for p in parts)

    # Step 1: Tier 1 static lookup
    canned = lookup_table.get(cmd)
    if canned is not None:
        return canned

    # Step 2: GOODLLM allowlist -> Tier 2 (local Ollama)
    if cmd in GOODLLM:
        return _tier2(cmd, session_id)

    # Step 3
    if _should_escalate(cmd):
        return _escalate_with_fallback(cmd, session_id)

    # Step 4: defer to cowrie native (ls, cat, touch, mkdir, cd, pwd, cp,
    # rm, echo, etc.) — cowrie has real implementations that walk fs.pickle.
    raise TierUnavailable(f"no tier claims {cmd!r}; defer to cowrie native")


def _is_control_flow(cmd: str) -> bool:
    padded = f" {cmd} "
    return any(tok in padded for tok in _CONTROL_FLOW_TOKENS)


def _should_escalate(cmd: str) -> bool:
    if "|" in cmd:
        return True
    if "$(" in cmd or "`" in cmd:
        return True
    if len(cmd) > 80:
        return True
    return False


def _escalate_with_fallback(cmd: str, session_id: str) -> str:
    try:
        return _tier3(cmd, session_id)
    except TierUnavailable:
        return _tier2(cmd, session_id)


def _tier2(cmd: str, session_id: str) -> str:
    try:
        from scalpel.local_llm import client as llm  # type: ignore[import-not-found]
    except ImportError as e:
        raise TierUnavailable(f"Tier 2 (local LLM) not ready: {e}") from e
    try:
        return llm.generate(cmd, {"session_id": session_id})
    except llm.OllamaError as e:
        # Network/decode failure. Fall through to cowrie native rather than
        # returning garbage or hanging — a missing `uptime` is less of a
        # tell than a `uptime` that takes 30 seconds or emits JSON errors.
        raise TierUnavailable(f"Tier 2 (Ollama) failed: {e}") from e


def _tier3(cmd: str, session_id: str) -> str:
    try:
        from scalpel.aws import client as aws  # type: ignore[import-not-found]
    except ImportError as e:
        raise TierUnavailable(f"Tier 3 (AWS) not ready: {e}") from e
    try:
        return aws.escalate(cmd, session_history=[])
    except aws.BedrockError as e:
        # boto3 missing, credentials missing/expired, network blip, Bedrock
        # throttling, etc. Fall through to Tier 2 fallback via
        # _escalate_with_fallback, then to cowrie native if Tier 2 is also
        # down. A silent fallback is less conspicuous than a visible error.
        raise TierUnavailable(f"Tier 3 (Bedrock) failed: {e}") from e
