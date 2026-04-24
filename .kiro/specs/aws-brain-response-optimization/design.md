# Design Document: AWS Brain Response Optimization

## Overview

This feature reduces the end-to-end latency of the Amazon Bedrock response path in Cowrie's LLM backend mode. The current `BedrockClient` in `src/cowrie/llm/bedrock.py` makes a synchronous, single-shot `Converse` API call for every unrecognised command, blocking the attacker's terminal for 1–3 seconds. On a Raspberry Pi with limited CPU and a variable-latency AWS connection, this is perceptible and degrades the honeypot's realism.

The optimization introduces seven complementary improvements:

1. **Response caching** — TTL-bounded LRU cache avoids redundant Bedrock calls for repeated commands
2. **Request timeout and fallback** — per-call deadline with graceful degradation to a static response
3. **Connection keep-alive** — boto3 client singleton with a sized urllib3 connection pool
4. **Prompt compression** — single-turn prompts with configurable length caps reduce token count
5. **Streaming response delivery** — `InvokeModelWithResponseStream` writes tokens to the terminal as they arrive
6. **Latency observability** — per-call timing metrics and a `get_stats()` summary function
7. **Configuration schema** — all tuning knobs exposed in `[bedrock]` section of `cowrie.cfg`

All changes are confined to `src/cowrie/llm/bedrock.py` and the `[bedrock]` config section. No other modules require modification.

---

## Architecture

### Current Call Path

```
attacker keystroke
  → HoneyPotInteractiveProtocol.lineReceived()
  → BedrockClient.get_response()          # returns Deferred
    → threads.deferToThread(_call_bedrock) # blocking boto3 call in thread pool
      → boto3 client.converse()           # ~1-3 s round trip
    → Deferred fires with response text
  → terminal.write(response)
  → _show_prompt()
```

### Optimized Call Path

```
attacker keystroke
  → HoneyPotInteractiveProtocol.lineReceived()
  → BedrockClient.get_response()
    → _build_cache_key(command, cwd, username, hostname)
    → ResponseCache.get(key)              # O(1) LRU lookup
      [HIT]  → return cached text immediately (< 1 ms)
      [MISS] → threads.deferToThread(_call_bedrock_with_timeout)
                 → boto3 client.converse() OR invoke_with_response_stream()
                 → ResponseCache.put(key, text)
                 → record latency metric
    → Deferred fires with response text
  → terminal.write(response)  [or incremental chunks if streaming]
  → _show_prompt()
```

### Component Diagram

```mermaid
graph TD
    A[HoneyPotInteractiveProtocol] -->|get_response()| B[BedrockClient]
    B --> C{ResponseCache}
    C -->|HIT| D[Return cached text]
    C -->|MISS| E[Thread Pool]
    E --> F{streaming?}
    F -->|false| G[boto3 Converse API]
    F -->|true| H[boto3 InvokeModelWithResponseStream]
    G --> I[ResponseCache.put]
    H --> J[Incremental terminal.write]
    I --> K[LatencyStats.record]
    J --> K
    B --> L[get_stats()]
    L --> M[StatsDict]
```

---

## Components and Interfaces

### `ResponseCache`

A standalone class wrapping Python's `functools.lru_cache` pattern using an `OrderedDict` for explicit TTL + LRU eviction. Chosen over `cachetools` to avoid adding a new dependency.

```python
class ResponseCache:
    def __init__(self, max_entries: int, ttl_seconds: int) -> None: ...
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, value: str) -> None: ...
    def invalidate(self, key: str) -> None: ...
    def clear(self) -> None: ...
    def stats(self) -> dict[str, int]: ...  # hits, misses, evictions
```

Internally stores `(value, inserted_at_monotonic)` tuples. `get()` checks `time.monotonic() - inserted_at > ttl_seconds` and treats stale entries as misses, evicting them lazily. LRU eviction on `put()` when `len > max_entries`.

### `CacheKey`

A module-level function, not a class:

```python
def build_cache_key(command: str, cwd: str, username: str, hostname: str) -> str:
    """Normalise and hash the four-tuple into a cache key string."""
```

Normalisation: `" ".join(command.split())` (strips leading/trailing whitespace, collapses internal runs). Then concatenates with `|` separator and returns a hex digest of the SHA-256 hash for a fixed-length key.

### `LatencyStats`

A lightweight stats accumulator, thread-safe via a `threading.Lock`:

```python
class LatencyStats:
    def record(self, outcome: str, latency_ms: float) -> None: ...
    def snapshot(self) -> dict[str, int | float]: ...
    # outcome values: "hit", "miss", "timeout", "error"
```

### Updated `BedrockClient`

The existing singleton class gains:

- `_cache: ResponseCache` — initialised in `__init__` when `cache_enabled=True`
- `_stats: LatencyStats` — always initialised
- `_call_bedrock_with_timeout()` — wraps `_call_bedrock()` with `signal.alarm` / `concurrent.futures.wait` timeout
- `_call_bedrock_streaming()` — uses `invoke_model_with_response_stream` and yields chunks
- `get_stats()` — module-level function delegating to the singleton's `_stats`

Public interface remains unchanged: `get_response(command, hostname, username, cwd) -> Deferred[str]`.

---

## Data Models

### Cache Entry

```python
@dataclass
class CacheEntry:
    value: str
    inserted_at: float  # time.monotonic() timestamp
```

### Stats Snapshot

```python
# Returned by get_stats()
{
    "total_calls": int,
    "cache_hits": int,
    "cache_misses": int,
    "timeouts": int,
    "errors": int,
    "mean_latency_ms": float,
}
```

### Config Keys (new additions to `[bedrock]` section)

| Key | Type | Default | Validation |
|-----|------|---------|------------|
| `cache_enabled` | bool | `true` | — |
| `cache_ttl_seconds` | int | `3600` | > 0 |
| `cache_max_entries` | int | `1000` | > 0 |
| `timeout_seconds` | float | `10.0` | ≥ 0 |
| `max_retries` | int | `2` | ≥ 0 |
| `max_pool_connections` | int | `10` | ≥ 1 |
| `streaming` | bool | `false` | — |
| `max_prompt_chars` | int | `500` | > 0 |

### Log Message Format

```
BedrockClient key=<sha256_hex[:12]> outcome=<hit|miss|timeout|error> latency_ms=<float>
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Cache round-trip

*For any* cache key `k` and response string `r`, storing `r` under `k` in the `ResponseCache` and then immediately retrieving with `k` SHALL return `r`.

**Validates: Requirements 1.8**

---

### Property 2: Cache hit bypasses Bedrock

*For any* (command, cwd, username, hostname) tuple whose cache key is already present in the `ResponseCache`, calling `get_response()` SHALL return the cached value without invoking the boto3 Bedrock client.

**Validates: Requirements 1.2**

---

### Property 3: Cache miss stores result

*For any* (command, cwd, username, hostname) tuple whose cache key is absent from the `ResponseCache`, calling `get_response()` SHALL invoke the boto3 Bedrock client exactly once and store the returned response in the cache before returning it.

**Validates: Requirements 1.3**

---

### Property 4: LRU eviction on capacity overflow

*For any* `ResponseCache` with `max_entries = N` and `N+1` distinct cache keys inserted in order, the entry that was least recently used SHALL be absent from the cache after the `(N+1)`th insertion.

**Validates: Requirements 1.5**

---

### Property 5: Cache key normalisation is idempotent

*For any* command string `s`, the cache key produced from `s` with arbitrary leading, trailing, or internal whitespace variations SHALL be identical to the cache key produced from the whitespace-normalised form of `s` (i.e., `" ".join(s.split())`).

**Validates: Requirements 1.7**

---

### Property 6: Fallback response format

*For any* command string, when the Bedrock call fails (timeout, error, or throttling exhaustion), the returned string SHALL match the pattern `-bash: <first_token>: command not found` where `<first_token>` is the first whitespace-delimited token of the original command.

**Validates: Requirements 2.2, 2.3, 2.5**

---

### Property 7: Error handling always returns fallback

*For any* boto3 exception type raised during a Bedrock call (ClientError, BotoCoreError, EndpointConnectionError, ThrottlingException, etc.), `get_response()` SHALL return the Fallback_Response string rather than propagating the exception to the caller.

**Validates: Requirements 2.3, 2.4, 3.4**

---

### Property 8: Singleton client identity

*For any* sequence of `N ≥ 2` calls to `BedrockClient.get_instance()`, all returned references SHALL point to the same object (i.e., `id(instance_i) == id(instance_j)` for all `i, j`).

**Validates: Requirements 3.1, 3.3**

---

### Property 9: Prompt length never exceeds configured limit

*For any* command string of arbitrary length, the total character count of the prompt payload sent to the Bedrock API SHALL be less than or equal to the configured `max_prompt_chars` value.

**Validates: Requirements 4.5**

---

### Property 10: Latency is always recorded

*For any* `get_response()` call (hit, miss, timeout, or error), a non-negative `latency_ms` value SHALL be recorded in `LatencyStats` and the `total_calls` counter SHALL increment by exactly 1.

**Validates: Requirements 6.1, 6.4**

---

### Property 11: get_stats() schema is always complete

*For any* sequence of `get_response()` calls, `get_stats()` SHALL return a dict containing exactly the keys `total_calls`, `cache_hits`, `cache_misses`, `timeouts`, `errors`, and `mean_latency_ms`, with all integer fields ≥ 0 and `mean_latency_ms` ≥ 0.0.

**Validates: Requirements 6.4, 6.5**

---

### Property 12: Missing config keys never raise exceptions

*For any* subset of the eight new `[bedrock]` config keys being absent from `cowrie.cfg`, `BedrockClient.__init__()` SHALL complete without raising an exception and SHALL use the documented default value for each absent key.

**Validates: Requirements 7.2**

---

### Property 13: Invalid config values fall back to defaults

*For any* config key that accepts a numeric type (int or float), providing a non-numeric string value SHALL cause `BedrockClient.__init__()` to log a WARNING and use the documented default value rather than raising an exception.

**Validates: Requirements 7.3**

---

### Property 14: Non-positive cache config raises ValueError

*For any* value `v ≤ 0` assigned to `cache_ttl_seconds` or `cache_max_entries`, `BedrockClient.__init__()` SHALL raise a `ValueError` with a descriptive message.

**Validates: Requirements 7.4**

---

## Error Handling

### Timeout

The blocking `_call_bedrock()` function runs in a `deferToThread` thread. Timeout is implemented using `concurrent.futures.Future` with `future.result(timeout=timeout_seconds)`. On `concurrent.futures.TimeoutError`, the thread is abandoned (boto3 does not support cancellation), the outcome is recorded as `"timeout"`, and `Fallback_Response` is returned.

When `timeout_seconds = 0`, no timeout wrapper is applied and the raw `deferToThread` is used directly.

### ThrottlingException Retry

Retry logic uses a simple loop inside `_call_bedrock()` (still in the thread pool):

```
attempt 0: call Bedrock
  ThrottlingException → sleep 2^0 * base_delay, attempt 1
  ThrottlingException → sleep 2^1 * base_delay, attempt 2
  ThrottlingException → return Fallback_Response
```

`base_delay = 0.5 s`. Maximum sleep before giving up: `(2^max_retries - 1) * base_delay`.

### Connection Reset

On `EndpointConnectionError`, `BedrockClient._client` is set to `None` and a new client is created on the next call via `_get_or_create_client()`. This ensures a stale connection pool does not permanently block the honeypot.

### Streaming Mid-Stream Error

If `invoke_model_with_response_stream` raises after yielding some chunks, the already-written chunks are left on the terminal and the Fallback_Response suffix is appended. The prompt is always displayed regardless of error state.

---

## Testing Strategy

### Unit Tests (`src/cowrie/test/test_bedrock_optimization.py`)

Use `unittest.TestCase` consistent with the existing test suite. Mock boto3 using `unittest.mock.patch`.

**Example-based tests:**
- TTL expiry: store entry, advance mock time past TTL, verify cache miss
- `cache_enabled=false`: two identical calls both invoke mocked Bedrock
- Timeout=0: verify no timeout wrapper applied
- ThrottlingException retry: mock raises N times then succeeds, verify call count
- Streaming API selection: `streaming=true` calls `invoke_model_with_response_stream`
- `get_stats()` initial state: all zeros before any calls
- Debug logging: `debug=true` logs request/response payloads
- Config defaults: each key absent → correct default used
- `cache_ttl_seconds=0` → `ValueError` raised

**Property-based tests** use [Hypothesis](https://hypothesis.readthedocs.io/) (already compatible with Python 3.10+, no new mandatory dependency — add to `requirements.txt` under a `[test]` extra):

Each property test runs a minimum of 100 examples (Hypothesis default is 100; set `@settings(max_examples=100)`).

Tag format in comments: `# Feature: aws-brain-response-optimization, Property N: <property_text>`

| Property | Test method | Hypothesis strategy |
|----------|-------------|---------------------|
| 1 (cache round-trip) | `test_prop_cache_roundtrip` | `st.text(), st.text()` |
| 2 (hit bypasses Bedrock) | `test_prop_cache_hit_no_bedrock_call` | `st.text(), st.text(), st.text(), st.text()` |
| 3 (miss stores result) | `test_prop_cache_miss_stores_result` | same |
| 4 (LRU eviction) | `test_prop_lru_eviction` | `st.integers(min_value=1, max_value=50)` |
| 5 (key normalisation) | `test_prop_cache_key_normalisation` | `st.text()` with whitespace injection |
| 6 (fallback format) | `test_prop_fallback_format` | `st.text(min_size=1)` |
| 7 (errors return fallback) | `test_prop_errors_return_fallback` | `st.sampled_from([ClientError, BotoCoreError, ...])` |
| 8 (singleton identity) | `test_prop_singleton_identity` | `st.integers(min_value=2, max_value=20)` |
| 9 (prompt length) | `test_prop_prompt_length_bounded` | `st.text(), st.integers(min_value=50, max_value=2000)` |
| 10 (latency recorded) | `test_prop_latency_always_recorded` | `st.integers(min_value=1, max_value=20)` |
| 11 (stats schema) | `test_prop_stats_schema_complete` | `st.lists(st.sampled_from(["hit","miss","timeout","error"]))` |
| 12 (missing config) | `test_prop_missing_config_no_exception` | `st.sets(st.sampled_from(CONFIG_KEYS))` |
| 13 (invalid config) | `test_prop_invalid_config_uses_default` | `st.sampled_from(NUMERIC_KEYS), st.text()` |
| 14 (non-positive raises) | `test_prop_nonpositive_cache_config_raises` | `st.integers(max_value=0)` |

### Integration Tests

Not automated in CI (require live AWS credentials). Document in `docs/LLM.rst`:
- End-to-end latency measurement with `streaming=true` on a real Bedrock endpoint
- Verify CloudWatch / boto3 metrics under sustained load

### Running Tests

```bash
# All unit tests including new property tests
coverage run -m unittest discover src --verbose

# Property tests only (faster feedback loop)
python -m pytest src/cowrie/test/test_bedrock_optimization.py -v
```
