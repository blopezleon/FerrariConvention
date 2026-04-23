"""Cowrie -> scalpel bridge. Cowrie's command intercept calls on_command().

Contract:
  - Return a str  -> replace cowrie's native output with this.
  - Return None   -> let cowrie handle the command natively (its default fs +
                    builtins). Used when no tier can answer, e.g. during early
                    rollout when Tier 2/3 clients aren't wired in yet.
"""
from __future__ import annotations

from scalpel.router.handle_command import TierUnavailable, handle_command


def on_command(command: str, session_id: str) -> str | None:
    try:
        return handle_command(command, session_id)
    except TierUnavailable:
        return None
