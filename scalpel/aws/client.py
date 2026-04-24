"""Tier 3 — AWS Bedrock via bedrock_helper.

Contract (locked at kickoff, see scalpel/CONTEXT.md):
    escalate(command: str, session_history: list) -> str

Returns Bedrock-generated bash output. Raises TierUnavailable on any
failure — bedrock_helper.ask_bedrock does NOT raise on error (it returns
"[Bedrock <type>] ..." strings), so we detect those and convert.
handle_command._tier3 catches TierUnavailable and tries Tier 2 next;
if that also fails, cowrie native handles the command.

Implementation notes:
  - Uses the simple sync bedrock_helper (not the async cowrie-integrated
    src/cowrie/llm/bedrock.py). Scalpel's architecture is sync — called
    from shell/honeypot.py lineReceived, expected to return str.
  - Blocks the twisted reactor for up to ~3s while Bedrock responds.
    Acceptable for the single-red-team hackathon model; with concurrent
    sessions we'd want deferToThread.
  - Credentials via env vars (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    AWS_SESSION_TOKEN, AWS_DEFAULT_REGION) — must be set on the Pi.
  - boto3 lazily imported so scalpel loads cleanly on dev machines that
    don't have boto3 installed; ImportError becomes TierUnavailable.
"""
from __future__ import annotations


class BedrockError(RuntimeError):
    """Raised when Bedrock is unreachable, returns an error string, or boto3
    is not installed. handle_command._tier3 catches and re-raises as
    TierUnavailable so the router's fallback chain kicks in."""


_SYSTEM = (
    "You are the shell on a Raspberry Pi 5 running 64-bit Raspberry Pi OS "
    "(Debian 13 trixie, aarch64). The hostname is 'raspberrypi'. The user "
    "is 'root'. Respond with ONLY the raw terminal output of the given "
    "command — no explanations, no markdown, no code fences, no trailing "
    "commentary. If the command produces no output (touch, cd, export, "
    "etc.) reply with an empty string. If the command would not exist on "
    "a fresh Raspberry Pi OS install, respond with: "
    "'-bash: <command>: command not found'. Keep output concise and "
    "realistic. Do not invent processes, users, or data beyond what would "
    "plausibly exist on a system uptime of ~4 hours with ~16 GB RAM."
)


def escalate(command: str, session_history: list) -> str:
    """Tier 3 — delegate to Bedrock. Raises BedrockError on any failure."""
    # bedrock_helper.py lives at the repo root (where teammate put it);
    # scalpel_bridge.py puts repo root on sys.path at cowrie import time,
    # so a bare `import bedrock_helper` works inside the cowrie process.
    try:
        from bedrock_helper import ask_bedrock  # type: ignore[import-not-found]
    except ImportError as e:
        raise BedrockError(f"boto3/bedrock_helper unavailable: {e}") from e

    # Build a minimal conversation-style prompt. session_history is a list;
    # we accept either list[str] ("cmd1", "cmd2") or list[tuple[str,str]]
    # ((cmd, response) pairs) and coerce. Only the last few entries matter
    # for context; longer histories waste tokens.
    history_lines = []
    for entry in (session_history or [])[-5:]:
        if isinstance(entry, tuple) and len(entry) == 2:
            cmd, resp = entry
            history_lines.append(f"$ {cmd}\n{resp.rstrip()}")
        elif isinstance(entry, str):
            history_lines.append(f"$ {entry}")
    history_text = "\n".join(history_lines)

    if history_text:
        prompt = f"{history_text}\n$ {command}"
    else:
        prompt = f"$ {command}"

    result = ask_bedrock(
        prompt=prompt,
        system_prompt=_SYSTEM,
        max_tokens=500,
        temperature=0.2,
    )

    # bedrock_helper returns error strings with a "[Bedrock" prefix on
    # ClientError, BotoCoreError, or unexpected exceptions. Detect and
    # convert so our router fallback chain works.
    if result.startswith("[Bedrock"):
        raise BedrockError(result)

    # Match the Tier 2 contract: always end with a newline unless the
    # response is genuinely empty (e.g. a successful `touch` or `cd`).
    text = result.rstrip()
    return text + "\n" if text else ""
