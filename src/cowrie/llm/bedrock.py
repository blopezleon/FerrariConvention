# ABOUTME: Amazon Bedrock LLM client for Cowrie honeypot.
# ABOUTME: Uses boto3 with standard AWS credential chain (quickstart via ~/.aws/credentials).

from __future__ import annotations

import collections
import concurrent.futures
import configparser
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from twisted.internet import defer, threads
from twisted.python import log

from cowrie.core.config import CowrieConfig

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


@dataclass
class CacheEntry:
    value: str
    inserted_at: float  # time.monotonic() timestamp


class ResponseCache:
    """TTL-bounded LRU cache backed by collections.OrderedDict."""

    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._store: collections.OrderedDict[str, CacheEntry] = collections.OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() - entry.inserted_at > self._ttl_seconds:
            # Lazy eviction of stale entry
            del self._store[key]
            self._evictions += 1
            self._misses += 1
            return None
        # Move to end (most recently used)
        self._store.move_to_end(key)
        self._hits += 1
        return entry.value

    def put(self, key: str, value: str) -> None:
        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = CacheEntry(value=value, inserted_at=time.monotonic())
        else:
            self._store[key] = CacheEntry(value=value, inserted_at=time.monotonic())
            if len(self._store) > self._max_entries:
                # Evict least recently used (first item)
                self._store.popitem(last=False)
                self._evictions += 1

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }


def build_cache_key(command: str, cwd: str, username: str, hostname: str) -> str:
    """Normalise the four-tuple and return a SHA-256 hex digest cache key."""
    normalised_command = " ".join(command.split())
    raw = f"{normalised_command}|{cwd}|{username}|{hostname}"
    return hashlib.sha256(raw.encode()).hexdigest()


class LatencyStats:
    """Thread-safe accumulator for per-call latency and outcome metrics."""

    _VALID_OUTCOMES = frozenset({"hit", "miss", "timeout", "error"})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_calls = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._timeouts = 0
        self._errors = 0
        self._total_latency_ms = 0.0

    def record(self, outcome: str, latency_ms: float) -> None:
        """Record a single call outcome and its latency in milliseconds."""
        with self._lock:
            self._total_calls += 1
            self._total_latency_ms += latency_ms
            if outcome == "hit":
                self._cache_hits += 1
            elif outcome == "miss":
                self._cache_misses += 1
            elif outcome == "timeout":
                self._timeouts += 1
            elif outcome == "error":
                self._errors += 1

    def snapshot(self) -> dict[str, int | float]:
        """Return a point-in-time snapshot of all counters."""
        with self._lock:
            mean = (
                self._total_latency_ms / self._total_calls
                if self._total_calls > 0
                else 0.0
            )
            return {
                "total_calls": self._total_calls,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "timeouts": self._timeouts,
                "errors": self._errors,
                "mean_latency_ms": mean,
            }


def _fallback_response(command: str) -> str:
    """Return a bash-style 'command not found' message for the given command."""
    tokens = command.split()
    first_token = tokens[0] if tokens else command
    return f"-bash: {first_token}: command not found"


# System prompt that instructs the model to behave like a Linux shell
_SYSTEM_PROMPT = (
    "You are a Linux bash shell running on a server. "
    "When given a command, respond ONLY with the exact terminal output that command would produce. "
    "Do not explain, do not add commentary, do not use markdown. "
    "If the command is not found, respond exactly as bash would: "
    "'-bash: <command>: command not found'. "
    "Keep responses concise and realistic."
)


class BedrockClient:
    """
    Calls Amazon Bedrock (Converse API) to generate realistic shell responses.

    Authentication uses the standard boto3 credential chain:
      1. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
      2. ~/.aws/credentials  (quickstart / aws configure)
      3. IAM instance profile (EC2/ECS/Lambda)

    Config section [bedrock] in cowrie.cfg:
      model_id   - Bedrock model ID (default: amazon.nova-micro-v1:0)
      region     - AWS region (default: us-east-1)
      max_tokens - Max tokens in response (default: 300)
      temperature - 0.0-1.0 (default: 0.3)
      debug      - Log requests/responses (default: false)
    """

    _instance: BedrockClient | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        if not HAS_BOTO3:
            raise ImportError(
                "boto3 is required for Bedrock support. "
                "Install it with: pip install boto3"
            )

        self.model_id = CowrieConfig.get(
            "bedrock", "model_id", fallback="amazon.nova-micro-v1:0"
        )
        self.region = CowrieConfig.get("bedrock", "region", fallback="us-east-1")
        self.max_tokens = CowrieConfig.getint("bedrock", "max_tokens", fallback=300)
        self.temperature = CowrieConfig.getfloat("bedrock", "temperature", fallback=0.3)
        self.debug = CowrieConfig.getboolean("bedrock", "debug", fallback=False)

        # --- New config keys (Requirement 7.1) ---
        try:
            self.cache_enabled = CowrieConfig.getboolean(
                "bedrock", "cache_enabled", fallback=True
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid cache_enabled; using default True")
            self.cache_enabled = True

        try:
            self.cache_ttl_seconds = CowrieConfig.getint(
                "bedrock", "cache_ttl_seconds", fallback=3600
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid cache_ttl_seconds; using default 3600")
            self.cache_ttl_seconds = 3600

        try:
            self.cache_max_entries = CowrieConfig.getint(
                "bedrock", "cache_max_entries", fallback=1000
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid cache_max_entries; using default 1000")
            self.cache_max_entries = 1000

        try:
            self.timeout_seconds = CowrieConfig.getfloat(
                "bedrock", "timeout_seconds", fallback=10.0
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid timeout_seconds; using default 10.0")
            self.timeout_seconds = 10.0

        try:
            self.max_retries = CowrieConfig.getint(
                "bedrock", "max_retries", fallback=2
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid max_retries; using default 2")
            self.max_retries = 2

        try:
            self.max_pool_connections = CowrieConfig.getint(
                "bedrock", "max_pool_connections", fallback=10
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid max_pool_connections; using default 10")
            self.max_pool_connections = 10

        try:
            self.streaming = CowrieConfig.getboolean(
                "bedrock", "streaming", fallback=False
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid streaming; using default False")
            self.streaming = False

        try:
            self.max_prompt_chars = CowrieConfig.getint(
                "bedrock", "max_prompt_chars", fallback=500
            )
        except (ValueError, TypeError, configparser.Error):
            log.msg("WARNING: invalid max_prompt_chars; using default 500")
            self.max_prompt_chars = 500

        # Validate positive-only constraints (Requirement 7.4)
        if self.cache_ttl_seconds <= 0:
            raise ValueError(
                f"cache_ttl_seconds must be > 0, got {self.cache_ttl_seconds}"
            )
        if self.cache_max_entries <= 0:
            raise ValueError(
                f"cache_max_entries must be > 0, got {self.cache_max_entries}"
            )

        # Initialise cache and stats (Requirements 1.1, 6.1)
        self._cache: ResponseCache | None = (
            ResponseCache(
                max_entries=self.cache_max_entries,
                ttl_seconds=self.cache_ttl_seconds,
            )
            if self.cache_enabled
            else None
        )
        self._stats: LatencyStats = LatencyStats()

        boto_config = Config(max_pool_connections=self.max_pool_connections)
        self._client = boto3.client(
            "bedrock-runtime", region_name=self.region, config=boto_config
        )
        log.msg(
            f"BedrockClient initialised: model={self.model_id} region={self.region}"
        )

    @classmethod
    def get_instance(cls) -> BedrockClient:
        """Return a shared singleton so we reuse the boto3 client."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _build_prompt(self, command: str, hostname: str, username: str, cwd: str) -> dict[str, Any]:
        """
        Construct a single-turn prompt payload for the Bedrock Converse API.
        Truncates the system prompt at a sentence boundary when the total
        character count would exceed self.max_prompt_chars.
        """
        user_message = f"[{username}@{hostname} {cwd}]$ {command}"

        # Reserve space for the user message; truncate system prompt to fit
        available = self.max_prompt_chars - len(user_message)
        if available <= 0:
            # User message alone already at/over limit — use empty system prompt
            system_text = ""
        elif len(_SYSTEM_PROMPT) > available:
            # Truncate at the last sentence boundary (.) within the budget
            truncated = _SYSTEM_PROMPT[:available]
            dot_pos = truncated.rfind(".")
            if dot_pos != -1:
                system_text = truncated[: dot_pos + 1]
            else:
                system_text = truncated
        else:
            system_text = _SYSTEM_PROMPT

        return {
            "modelId": self.model_id,
            "system": [{"text": system_text}],
            "messages": [{"role": "user", "content": [{"text": user_message}]}],
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }

    def _call_bedrock_once(self, command: str, hostname: str, username: str, cwd: str) -> str:
        """
        Raw single blocking call to Bedrock Converse API.
        Runs in a thread pool so it doesn't block the Twisted reactor.
        """
        request = self._build_prompt(command, hostname, username, cwd)

        if self.debug:
            log.msg(f"Bedrock request: {json.dumps(request, indent=2)}")

        response = self._client.converse(**request)

        if self.debug:
            log.msg(f"Bedrock response: {json.dumps(response, indent=2, default=str)}")

        output_message = response["output"]["message"]
        text = "".join(
            block["text"]
            for block in output_message["content"]
            if "text" in block
        )
        return text.strip()

    def _call_bedrock(self, command: str, hostname: str, username: str, cwd: str) -> str:
        """
        Blocking call to Bedrock with exponential back-off retry on ThrottlingException.
        Returns fallback response on unrecoverable errors.
        """
        for attempt in range(self.max_retries + 1):
            try:
                return self._call_bedrock_once(command, hostname, username, cwd)
            except ClientError as err:
                code = err.response["Error"]["Code"]
                if code == "ThrottlingException":
                    if attempt < self.max_retries:
                        sleep_secs = (2 ** attempt) * 0.5
                        time.sleep(sleep_secs)
                        continue
                    # Exhausted retries
                    return _fallback_response(command)
                else:
                    log.msg(f"WARNING: Bedrock ClientError for '{command}': {code}")
                    return _fallback_response(command)
            except EndpointConnectionError as err:
                log.msg(f"WARNING: Bedrock EndpointConnectionError for '{command}': {err}")
                self._client = None
                return _fallback_response(command)
            except BotoCoreError as err:
                log.msg(f"WARNING: Bedrock BotoCoreError for '{command}': {err}")
                return _fallback_response(command)
        return _fallback_response(command)

    def _call_bedrock_with_timeout(self, command: str, hostname: str, username: str, cwd: str) -> str:
        """
        Wraps _call_bedrock() with an optional per-call timeout.
        When timeout_seconds == 0, calls _call_bedrock() directly.
        On timeout, records outcome and returns fallback response.
        """
        if self.timeout_seconds == 0:
            return self._call_bedrock(command, hostname, username, cwd)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._call_bedrock, command, hostname, username, cwd)
            try:
                return future.result(timeout=self.timeout_seconds)
            except concurrent.futures.TimeoutError:
                self._stats.record("timeout", self.timeout_seconds * 1000)
                return _fallback_response(command)

    def _call_bedrock_streaming(self, command: str, hostname: str, username: str, cwd: str) -> str:
        """
        Blocking call to Bedrock using the converse_stream API.
        Collects all text chunks and returns the assembled response string.
        On mid-stream error, appends fallback response to already-received chunks.
        Runs in a thread pool so it doesn't block the Twisted reactor.
        """
        request = self._build_prompt(command, hostname, username, cwd)

        # converse_stream uses the same payload structure as converse
        converse_kwargs = {
            "modelId": request["modelId"],
            "system": request["system"],
            "messages": request["messages"],
            "inferenceConfig": request["inferenceConfig"],
        }

        if self.debug:
            log.msg(f"Bedrock streaming request: {json.dumps(converse_kwargs, indent=2)}")

        chunks: list[str] = []
        try:
            response = self._client.converse_stream(**converse_kwargs)
            for event in response["stream"]:
                text = event.get("contentBlockDelta", {}).get("delta", {}).get("text", "")
                if text:
                    chunks.append(text)
                if event.get("messageStop"):
                    break
        except Exception as err:
            log.msg(f"WARNING: Bedrock streaming error for '{command}': {err}")
            chunks.append(_fallback_response(command))

        result = "".join(chunks).strip()

        if self.debug:
            log.msg(f"Bedrock streaming response: {result!r}")

        return result

    def _get_response_sync(
        self, command: str, hostname: str, username: str, cwd: str
    ) -> str:
        """
        Synchronous implementation of get_response(), runs in the thread pool.
        Handles cache lookup, Bedrock call, cache store, stats recording, and logging.
        """
        t0 = time.monotonic()
        key = build_cache_key(command, cwd, username, hostname)

        # Cache hit path (Requirement 1.2)
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                latency_ms = (time.monotonic() - t0) * 1000
                outcome = "hit"
                self._stats.record(outcome, latency_ms)
                log.msg(
                    f"BedrockClient key={key[:12]} outcome={outcome} latency_ms={latency_ms:.2f}"
                )
                if self.debug:
                    log.msg(f"BedrockClient cache hit response: {cached!r}")
                return cached

        # Cache miss — call Bedrock (Requirement 1.3)
        if self.streaming:
            result = self._call_bedrock_streaming(command, hostname, username, cwd)
        else:
            result = self._call_bedrock_with_timeout(command, hostname, username, cwd)

        latency_ms = (time.monotonic() - t0) * 1000
        outcome = "miss"

        # Store in cache (Requirement 1.3)
        if self._cache is not None:
            self._cache.put(key, result)

        self._stats.record(outcome, latency_ms)
        log.msg(
            f"BedrockClient key={key[:12]} outcome={outcome} latency_ms={latency_ms:.2f}"
        )
        if self.debug:
            log.msg(f"BedrockClient miss response: {result!r}")

        return result

    def get_response(
        self, command: str, hostname: str = "svr04", username: str = "root", cwd: str = "~"
    ) -> defer.Deferred:
        """
        Async wrapper — returns a Deferred that fires with the response string.
        Falls back to empty string on any error.
        Delegates to _get_response_sync() in the thread pool.
        """
        d = threads.deferToThread(
            self._get_response_sync, command, hostname, username, cwd
        )
        d.addErrback(self._on_error, command)
        return d

    def _on_error(self, err: Any, command: str) -> str:
        if HAS_BOTO3 and err.check(ClientError):
            code = err.value.response["Error"]["Code"]
            log.err(f"Bedrock ClientError for '{command}': {code}")
        elif HAS_BOTO3 and err.check(BotoCoreError):
            log.err(f"Bedrock BotoCoreError for '{command}': {err.value}")
        else:
            log.err(f"Bedrock unexpected error for '{command}': {err.value}")
        return ""


def get_stats() -> dict[str, int | float]:
    """Return lifetime stats from the singleton BedrockClient instance."""
    return BedrockClient.get_instance()._stats.snapshot()
