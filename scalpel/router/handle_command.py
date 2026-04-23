"""Three-tier command router. See scalpel/CONTEXT.md "Routing Decision Logic".

Entry point: handle_command(command, session_id) -> str

Flow (top-down, first match wins):
  Step 0   compound command with control-flow keywords  -> Tier 3 (whole block)
  Step 0b  simple chain (;, &&, ||)                     -> recurse per piece
  Step 1   Tier 1 lookup table                          -> instant canned output
  Step 4   heuristic default:
             pipe, $(...), backticks, len > 80          -> Tier 3
             otherwise                                  -> Tier 2
  Step 5   Tier 3 unreachable                           -> fall back to Tier 2

Tier 2 (local LLM) and Tier 3 (AWS) clients may not be wired in yet. When they
are missing we raise TierUnavailable so cowrie_hook can let cowrie handle the
command natively instead of emitting nonsense.
"""
from __future__ import annotations

import re

from scalpel.router import lookup_table


class TierUnavailable(Exception):
    """The tier that would answer this command is not wired in yet."""


_CONTROL_FLOW_TOKENS = (" for ", " while ", " if ", " case ", " until ", "function ")
_CHAIN_SPLIT_RE = re.compile(r"(?:;|&&|\|\||\n)")


def handle_command(command: str, session_id: str) -> str:
    cmd = command.strip()
    if not cmd:
        return ""

    if _is_control_flow(cmd):
        return _escalate_with_fallback(cmd, session_id)

    parts = [p.strip() for p in _CHAIN_SPLIT_RE.split(cmd) if p.strip()]
    if len(parts) > 1:
        return "".join(handle_command(p, session_id) for p in parts)

    canned = lookup_table.get(cmd)
    if canned is not None:
        return canned

    if _should_escalate(cmd):
        return _escalate_with_fallback(cmd, session_id)
    return _tier2(cmd, session_id)


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
    return llm.generate(cmd, {"session_id": session_id})


def _tier3(cmd: str, session_id: str) -> str:
    try:
        from scalpel.aws import client as aws  # type: ignore[import-not-found]
    except ImportError as e:
        raise TierUnavailable(f"Tier 3 (AWS) not ready: {e}") from e
    return aws.escalate(cmd, session_history=[])
