"""Tier 2 — local Ollama client.

Contract (locked at kickoff, see scalpel/CONTEXT.md):
    generate(command: str, session_state: dict) -> str

Returns LLM-generated bash output. Raises OllamaError on network/decode
failures; handle_command._tier2 catches that and re-raises as
TierUnavailable so the router falls through to cowrie native instead of
emitting nonsense.

Implementation notes:
  - Uses Ollama's native /api/generate endpoint (simpler than /v1 chat).
  - stream=False so we get one JSON blob back (no streaming reassembly).
  - keep_alive=-1 on every call pins the model in RAM — avoids the 30-40s
    reload penalty the red team would latency-fingerprint. Requires Ollama
    service to either have OLLAMA_KEEP_ALIVE=-1 set system-wide OR this
    client to always pass it (we do both belt-and-suspenders).
  - stdlib urllib only — no new deps to manage on the Pi.
  - Blocks the twisted reactor for up to TIMEOUT_S. Acceptable for the
    single-red-team hackathon model; concurrent sessions would need
    deferToThread.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:1.5b"
TIMEOUT_S = 3.0

# The system prompt is the lever between "plausible bash output" and
# "LLM rambling about what the command does". Short + imperative beats long
# + polite with a small model like qwen2.5:1.5b.
_SYSTEM = (
    "You are the shell on a Raspberry Pi 5 running 64-bit Raspberry Pi OS "
    "(Debian 13 trixie, aarch64). Output ONLY the raw terminal output of "
    "the given command — no explanations, no markdown, no code fences, no "
    "trailing commentary. If the command produces no output (touch, cd, "
    "export, etc.) reply with an empty string. Use values that would be "
    "plausible on a system that booted about 4 hours ago with ~16 GB RAM "
    "and a small number of active services. The hostname is 'raspberrypi'."
)


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns something unparseable."""


def generate(command: str, session_state: dict) -> str:
    """Synchronous HTTP call to Ollama. Returns stdout-style output string."""
    prompt = f"{_SYSTEM}\n\nCommand: {command}\nOutput:"
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": -1,
        "options": {
            "temperature": 0.2,
            "num_predict": 500,
        },
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
        result = json.loads(body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OllamaError(f"Ollama unreachable: {e!r}") from e
    except json.JSONDecodeError as e:
        raise OllamaError(f"Ollama returned non-JSON: {e!r}") from e

    text = result.get("response", "")
    if not isinstance(text, str):
        raise OllamaError(f"Ollama 'response' not a string: {text!r}")

    # Strip the markdown code fences small models sometimes emit even when
    # told not to. Then ensure a single trailing newline: bash output
    # always ends with \n, and cowrie's terminal writes it verbatim.
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    return text + "\n" if text else ""
