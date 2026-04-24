# Implementation Plan: AWS Brain Response Optimization

## Overview

Incrementally refactor `src/cowrie/llm/bedrock.py` to add response caching, timeout/fallback, connection keep-alive, prompt compression, streaming delivery, latency observability, and a validated configuration schema. All changes are confined to that single file plus a new test file. Each task builds on the previous one and ends with the components wired together.

## Tasks

- [ ] 1. Add `CacheEntry` dataclass, `ResponseCache` class, and `build_cache_key()` function
  - Add `CacheEntry` dataclass with `value: str` and `inserted_at: float` fields
  - Implement `ResponseCache.__init__(max_entries, ttl_seconds)` using `collections.OrderedDict`
  - Implement `ResponseCache.get(key)` with TTL check via `time.monotonic()` and lazy eviction of stale entries
  - Implement `ResponseCache.put(key, value)` with LRU eviction when `len > max_entries`
  - Implement `ResponseCache.invalidate(key)`, `ResponseCache.clear()`, and `ResponseCache.stats()` returning `{"hits": int, "misses": int, "evictions": int}`
  - Implement `build_cache_key(command, cwd, username, hostname)` normalising with `" ".join(command.split())`, concatenating with `|`, and returning a SHA-256 hex digest
  - _Requirements: 1.1, 1.4, 1.5, 1.7, 1.8_

  - [ ]* 1.1 Write property test for cache round-trip (Property 1)
    - **Property 1: Cache round-trip**
    - **Validates: Requirements 1.8**
    - Use `@given(st.text(), st.text())` for key and value; assert `cache.get(k) == r` immediately after `cache.put(k, r)`

  - [ ]* 1.2 Write property test for LRU eviction on capacity overflow (Property 4)
    - **Property 4: LRU eviction on capacity overflow**
    - **Validates: Requirements 1.5**
    - Use `@given(st.integers(min_value=1, max_value=50))` for N; insert N+1 distinct keys and assert the first-inserted key is absent

  - [ ]* 1.3 Write property test for cache key normalisation (Property 5)
    - **Property 5: Cache key normalisation is idempotent**
    - **Validates: Requirements 1.7**
    - Use `@given(st.text())` with injected leading/trailing/internal whitespace; assert key equals key of normalised form

- [ ] 2. Add `LatencyStats` class
  - Implement `LatencyStats` with a `threading.Lock` protecting internal counters
  - Implement `LatencyStats.record(outcome: str, latency_ms: float)` accepting outcomes `"hit"`, `"miss"`, `"timeout"`, `"error"`
  - Implement `LatencyStats.snapshot()` returning `{"total_calls": int, "cache_hits": int, "cache_misses": int, "timeouts": int, "errors": int, "mean_latency_ms": float}`
  - Ensure `snapshot()` returns all-zero dict before any `record()` calls
  - _Requirements: 6.1, 6.4, 6.5_

  - [ ]* 2.1 Write property test for latency always recorded (Property 10)
    - **Property 10: Latency is always recorded**
    - **Validates: Requirements 6.1, 6.4**
    - Use `@given(st.integers(min_value=1, max_value=20))` for call count; assert `total_calls` increments by exactly 1 per `record()` call and `latency_ms` is non-negative

  - [ ]* 2.2 Write property test for get_stats() schema completeness (Property 11)
    - **Property 11: get_stats() schema is always complete**
    - **Validates: Requirements 6.4, 6.5**
    - Use `@given(st.lists(st.sampled_from(["hit","miss","timeout","error"])))` for outcome sequences; assert snapshot always contains exactly the six required keys with non-negative values

- [ ] 3. Extend `BedrockClient.__init__()` with new config keys and validation
  - Read all eight new `[bedrock]` config keys using `CowrieConfig` with `fallback=` defaults: `cache_enabled`, `cache_ttl_seconds`, `cache_max_entries`, `timeout_seconds`, `max_retries`, `max_pool_connections`, `streaming`, `max_prompt_chars`
  - Wrap each numeric read in a try/except; log WARNING and use default on `ValueError`/`TypeError`
  - Raise `ValueError` with descriptive message if `cache_ttl_seconds <= 0` or `cache_max_entries <= 0`
  - Initialise `self._cache: ResponseCache | None` (None when `cache_enabled=False`)
  - Initialise `self._stats: LatencyStats` unconditionally
  - Configure boto3 client with `botocore.config.Config(max_pool_connections=max_pool_connections)`
  - _Requirements: 3.2, 7.1, 7.2, 7.3, 7.4_

  - [ ]* 3.1 Write property test for missing config keys never raise exceptions (Property 12)
    - **Property 12: Missing config keys never raise exceptions**
    - **Validates: Requirements 7.2**
    - Use `@given(st.sets(st.sampled_from(CONFIG_KEYS)))` for absent key subsets; mock `CowrieConfig` to raise `NoSectionError`/`NoOptionError` for absent keys and assert `__init__` completes without exception

  - [ ]* 3.2 Write property test for invalid config values fall back to defaults (Property 13)
    - **Property 13: Invalid config values fall back to defaults**
    - **Validates: Requirements 7.3**
    - Use `@given(st.sampled_from(NUMERIC_KEYS), st.text())` for key and non-numeric string; assert `__init__` completes and uses documented default

  - [ ]* 3.3 Write property test for non-positive cache config raises ValueError (Property 14)
    - **Property 14: Non-positive cache config raises ValueError**
    - **Validates: Requirements 7.4**
    - Use `@given(st.integers(max_value=0))` for `cache_ttl_seconds` and `cache_max_entries`; assert `ValueError` is raised

- [ ] 4. Implement `_build_prompt()` with length capping and single-turn format
  - Implement `_build_prompt(command, hostname, username, cwd)` that constructs a single-turn prompt (no conversation history)
  - Truncate the system prompt at a sentence boundary when it exceeds `max_prompt_chars`; ensure total prompt character count ≤ `max_prompt_chars`
  - Pass configured `temperature` and `max_tokens` values to the inference config dict
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ]* 4.1 Write property test for prompt length never exceeds configured limit (Property 9)
    - **Property 9: Prompt length never exceeds configured limit**
    - **Validates: Requirements 4.5**
    - Use `@given(st.text(), st.integers(min_value=50, max_value=2000))` for command and limit; assert `len(_build_prompt(...)) <= max_prompt_chars`

- [ ] 5. Implement `_call_bedrock_with_timeout()` with retry and fallback
  - Implement `_call_bedrock_with_timeout()` wrapping the existing `_call_bedrock()` using `concurrent.futures.Future` with `future.result(timeout=timeout_seconds)`
  - When `timeout_seconds == 0`, call `_call_bedrock()` directly with no timeout wrapper
  - On `concurrent.futures.TimeoutError`, record outcome `"timeout"` in `_stats` and return `Fallback_Response`
  - Implement exponential back-off retry loop inside `_call_bedrock()` for `ThrottlingException`: up to `max_retries` attempts with `sleep(2**attempt * 0.5)`; return `Fallback_Response` after exhaustion
  - On `EndpointConnectionError`, set `self._client = None` and return `Fallback_Response`
  - On any other boto3 exception (`ClientError`, `BotoCoreError`), log WARNING and return `Fallback_Response`
  - `Fallback_Response` = `-bash: <first_token>: command not found`
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.4_

  - [ ]* 5.1 Write property test for fallback response format (Property 6)
    - **Property 6: Fallback response format**
    - **Validates: Requirements 2.2, 2.3, 2.5**
    - Use `@given(st.text(min_size=1))` for command strings; assert fallback matches `-bash: <first_token>: command not found`

  - [ ]* 5.2 Write property test for errors always return fallback (Property 7)
    - **Property 7: Error handling always returns fallback**
    - **Validates: Requirements 2.3, 2.4, 3.4**
    - Use `@given(st.sampled_from([ClientError, BotoCoreError, EndpointConnectionError, ThrottlingException]))` for exception types; mock boto3 to raise each and assert `get_response()` returns `Fallback_Response`

- [ ] 6. Implement `_call_bedrock_streaming()` for incremental response delivery
  - Implement `_call_bedrock_streaming(prompt_payload)` calling `invoke_model_with_response_stream` and yielding text chunks as they arrive
  - Wire streaming path into `get_response()` when `self._streaming == True`
  - On mid-stream error, yield already-received chunks then append `Fallback_Response` suffix
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

- [ ] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Wire cache, stats, and logging into `get_response()`
  - In `get_response()`, call `build_cache_key()` and check `self._cache.get(key)` when cache is enabled; return immediately on hit, recording outcome `"hit"` and latency in `_stats`
  - On cache miss, call `_call_bedrock_with_timeout()` (or streaming variant), store result in cache, record outcome `"miss"` and latency in `_stats`
  - Emit Twisted log message: `BedrockClient key=<sha256[:12]> outcome=<outcome> latency_ms=<float>` after every call
  - When `debug=True`, additionally log full request and response payloads
  - _Requirements: 1.2, 1.3, 1.6, 6.1, 6.2, 6.3_

  - [ ]* 8.1 Write property test for cache hit bypasses Bedrock (Property 2)
    - **Property 2: Cache hit bypasses Bedrock**
    - **Validates: Requirements 1.2**
    - Use `@given(st.text(), st.text(), st.text(), st.text())` for (command, cwd, username, hostname); pre-populate cache and assert boto3 mock is never called

  - [ ]* 8.2 Write property test for cache miss stores result (Property 3)
    - **Property 3: Cache miss stores result**
    - **Validates: Requirements 1.3**
    - Use `@given(st.text(), st.text(), st.text(), st.text())`; assert boto3 mock called exactly once and result is retrievable from cache afterwards

- [ ] 9. Add module-level `get_stats()` function and verify singleton identity
  - Add module-level `get_stats()` delegating to `BedrockClient.get_instance()._stats.snapshot()`
  - Verify `get_instance()` returns the same object across multiple calls (singleton enforcement already present; add guard if missing)
  - _Requirements: 3.1, 3.3, 6.4, 6.5_

  - [ ]* 9.1 Write property test for singleton client identity (Property 8)
    - **Property 8: Singleton client identity**
    - **Validates: Requirements 3.1, 3.3**
    - Use `@given(st.integers(min_value=2, max_value=20))` for N; assert all N `get_instance()` calls return the same `id()`

- [ ] 10. Create test file `src/cowrie/test/test_bedrock_optimization.py`
  - Create the test file with `unittest.TestCase` base class consistent with the existing test suite
  - Add `hypothesis` to `requirements.txt` under a `[test]` extra (or as a dev dependency)
  - Implement all example-based unit tests: TTL expiry, `cache_enabled=false`, `timeout=0`, ThrottlingException retry count, streaming API selection, `get_stats()` initial state, debug logging, config defaults, `cache_ttl_seconds=0` raises `ValueError`
  - Implement all 14 property-based tests using `@given` / `@settings(max_examples=100)` with the tag comment format `# Feature: aws-brain-response-optimization, Property N: <property_text>`
  - Mock boto3 throughout using `unittest.mock.patch`
  - _Requirements: 1.1–1.8, 2.1–2.6, 3.1–3.4, 4.1–4.5, 5.1–5.5, 6.1–6.5, 7.1–7.4_

- [ ] 11. Update `etc/cowrie.cfg.dist` with new `[bedrock]` config keys
  - Add all eight new keys to the `[bedrock]` section in `etc/cowrie.cfg.dist` with their documented defaults and inline comments explaining each knob
  - _Requirements: 7.1_

- [ ] 12. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- All implementation is confined to `src/cowrie/llm/bedrock.py` and `src/cowrie/test/test_bedrock_optimization.py`
- Property tests use Hypothesis `@settings(max_examples=100)` and tag comments for traceability
- Checkpoints at tasks 7 and 12 validate incremental progress before wiring and final integration
