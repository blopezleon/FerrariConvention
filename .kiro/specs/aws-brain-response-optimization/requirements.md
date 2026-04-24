# Requirements Document

## Introduction

This feature optimizes the end-to-end latency of the AWS brain (Amazon Bedrock) response path in the Cowrie honeypot. Currently, every unrecognised shell command triggers a synchronous, single-shot Bedrock API call that blocks the attacker's terminal for 1–3 seconds. The optimization introduces response caching, response streaming, connection keep-alive, prompt compression, and observability instrumentation so that the Raspberry Pi running Cowrie delivers Bedrock-generated output to the attacker noticeably faster and more reliably.

## Glossary

- **AWS_Brain**: The Amazon Bedrock service used to generate realistic shell responses for unrecognised commands.
- **BedrockClient**: The singleton class in `src/cowrie/llm/bedrock.py` that wraps the boto3 `bedrock-runtime` client and exposes an async `get_response()` Deferred.
- **Response_Cache**: An in-process, TTL-bounded LRU cache that stores (command, cwd, username, hostname) → response text mappings to avoid redundant Bedrock calls.
- **RPI**: The Raspberry Pi device running the Cowrie honeypot process.
- **Twisted_Reactor**: The single-threaded async event loop provided by the Twisted framework that drives all I/O in Cowrie.
- **Thread_Pool**: The Twisted `deferToThread` thread pool used to run blocking boto3 calls without stalling the Twisted_Reactor.
- **Streaming_Response**: A Bedrock `InvokeModelWithResponseStream` call that yields response tokens incrementally rather than waiting for the full response before returning.
- **Cache_Key**: A normalised string derived from the command text, current working directory, username, and hostname used to look up entries in the Response_Cache.
- **TTL**: Time-to-live; the maximum age in seconds before a Response_Cache entry is considered stale and evicted.
- **Latency_Metric**: A floating-point measurement in milliseconds of the elapsed time from command receipt to first byte written to the attacker's terminal.
- **Fallback_Response**: The static `-bash: <cmd>: command not found` message returned when the AWS_Brain is unavailable or times out.

---

## Requirements

### Requirement 1: Response Caching

**User Story:** As a security researcher operating the honeypot, I want repeated identical commands to be answered instantly from cache, so that common attacker scripts do not incur repeated Bedrock round-trip latency.

#### Acceptance Criteria

1. THE Response_Cache SHALL store mappings from Cache_Key to response text with a configurable TTL.
2. WHEN a command is received and a valid Cache_Key entry exists in the Response_Cache, THE BedrockClient SHALL return the cached response without invoking the AWS_Brain.
3. WHEN a command is received and no valid Cache_Key entry exists in the Response_Cache, THE BedrockClient SHALL invoke the AWS_Brain and store the returned response in the Response_Cache before returning it.
4. WHEN a Response_Cache entry age exceeds the configured TTL, THE Response_Cache SHALL evict that entry so that subsequent lookups trigger a fresh AWS_Brain call.
5. THE Response_Cache SHALL enforce a configurable maximum entry count and SHALL evict the least-recently-used entry when the limit is reached.
6. WHERE the `[bedrock]` config section sets `cache_enabled = false`, THE BedrockClient SHALL bypass the Response_Cache entirely and always call the AWS_Brain.
7. THE Cache_Key SHALL be derived by normalising the command string (stripping leading/trailing whitespace and collapsing internal whitespace runs to a single space) combined with the cwd, username, and hostname fields.
8. FOR ALL Cache_Key values k, storing a response r then immediately retrieving with k SHALL return r (round-trip property).

---

### Requirement 2: Request Timeout and Fallback

**User Story:** As a security researcher, I want Bedrock calls that take too long to be abandoned gracefully, so that slow AWS responses do not freeze the attacker's terminal indefinitely.

#### Acceptance Criteria

1. THE BedrockClient SHALL apply a configurable per-call timeout (in seconds) to every AWS_Brain invocation.
2. WHEN an AWS_Brain call duration exceeds the configured timeout, THE BedrockClient SHALL cancel the in-flight call and return the Fallback_Response to the caller.
3. IF the AWS_Brain returns an error response (HTTP 4xx/5xx or a boto3 exception), THEN THE BedrockClient SHALL log the error at WARNING level and return the Fallback_Response.
4. IF the AWS_Brain is invoked and the boto3 client raises a `ThrottlingException`, THEN THE BedrockClient SHALL apply an exponential back-off retry with a configurable maximum retry count before returning the Fallback_Response.
5. THE Fallback_Response SHALL be the string `-bash: <cmd>: command not found` where `<cmd>` is the first token of the original command.
6. WHERE `[bedrock]` config sets `timeout_seconds = 0`, THE BedrockClient SHALL apply no timeout to AWS_Brain calls.

---

### Requirement 3: Connection Keep-Alive and Client Reuse

**User Story:** As a security researcher, I want the boto3 HTTP connection to Bedrock to be reused across calls, so that TCP and TLS handshake overhead is not paid on every command.

#### Acceptance Criteria

1. THE BedrockClient SHALL reuse a single boto3 `bedrock-runtime` client instance across all calls within a process lifetime (singleton pattern already present; this requirement formalises it).
2. THE BedrockClient SHALL configure the underlying urllib3 connection pool with a configurable `max_pool_connections` value so that concurrent Thread_Pool workers share persistent connections.
3. WHEN the Cowrie process starts, THE BedrockClient SHALL initialise the boto3 client exactly once and SHALL NOT create additional clients for subsequent requests.
4. IF the boto3 client raises a connection-level error (e.g., `EndpointConnectionError`), THEN THE BedrockClient SHALL log the error, reset the internal client instance, and return the Fallback_Response so that the next call attempts a fresh connection.

---

### Requirement 4: Prompt Compression

**User Story:** As a security researcher, I want the system prompt and conversation context sent to Bedrock to be as concise as possible, so that token count is minimised and inference latency is reduced.

#### Acceptance Criteria

1. THE BedrockClient SHALL use a single-turn prompt format (no conversation history) for the shell-backend Bedrock integration, sending only the system prompt and the current command.
2. THE BedrockClient SHALL limit the system prompt to a configurable maximum character length, truncating at a sentence boundary if the configured limit is exceeded.
3. WHEN `max_tokens` is set in `[bedrock]`, THE BedrockClient SHALL pass that value to the AWS_Brain inference configuration so that the model stops generating after the specified token count.
4. THE BedrockClient SHALL set `temperature` to the configured value (default 0.3) to reduce sampling overhead and produce more deterministic, shorter responses.
5. FOR ALL valid command strings, the prompt character count sent to the AWS_Brain SHALL be less than or equal to the configured `max_prompt_chars` limit.

---

### Requirement 5: Incremental (Streaming) Response Delivery

**User Story:** As a security researcher, I want the honeypot to begin writing Bedrock output to the attacker's terminal as tokens arrive, so that the attacker perceives a faster response even when total generation time is unchanged.

#### Acceptance Criteria

1. WHERE `[bedrock]` config sets `streaming = true`, THE BedrockClient SHALL invoke the AWS_Brain using the `InvokeModelWithResponseStream` API instead of the `Converse` API.
2. WHEN streaming is enabled and the AWS_Brain returns the first token chunk, THE BedrockClient SHALL write that chunk to the attacker's terminal within 50 ms of receiving it from the AWS_Brain.
3. WHEN streaming is enabled and the AWS_Brain stream ends, THE BedrockClient SHALL flush any remaining buffered output and display the shell prompt.
4. IF streaming is enabled and the AWS_Brain stream raises an error mid-stream, THEN THE BedrockClient SHALL write any already-received output, append the Fallback_Response for the remainder, and display the shell prompt.
5. WHERE `[bedrock]` config sets `streaming = false` (default), THE BedrockClient SHALL use the existing `Converse` API and deliver the complete response at once.

---

### Requirement 6: Latency Observability

**User Story:** As a security researcher, I want per-call latency metrics to be logged, so that I can measure the impact of optimizations and identify regressions.

#### Acceptance Criteria

1. THE BedrockClient SHALL record the elapsed time in milliseconds from the moment a `get_response()` call is dispatched to the Thread_Pool until the response is returned to the Twisted_Reactor.
2. WHEN a Bedrock call completes (success or fallback), THE BedrockClient SHALL emit a Twisted log message containing the Cache_Key hash, the outcome (`hit`, `miss`, `timeout`, `error`), and the Latency_Metric.
3. WHERE `[bedrock]` config sets `debug = true`, THE BedrockClient SHALL additionally log the full request payload and response payload alongside the Latency_Metric.
4. THE BedrockClient SHALL expose a module-level `get_stats()` function that returns a dict containing `total_calls`, `cache_hits`, `cache_misses`, `timeouts`, `errors`, and `mean_latency_ms` computed over the process lifetime.
5. WHEN `get_stats()` is called before any Bedrock calls have been made, THE BedrockClient SHALL return a dict with all numeric fields set to 0 and `mean_latency_ms` set to 0.0.

---

### Requirement 7: Configuration Schema

**User Story:** As a security researcher deploying Cowrie, I want all optimization parameters to be controllable from `cowrie.cfg` without code changes, so that I can tune the system for my specific hardware and network conditions.

#### Acceptance Criteria

1. THE BedrockClient SHALL read the following new keys from the `[bedrock]` config section with the specified defaults:
   - `cache_enabled` (boolean, default: `true`)
   - `cache_ttl_seconds` (integer, default: `3600`)
   - `cache_max_entries` (integer, default: `1000`)
   - `timeout_seconds` (float, default: `10.0`)
   - `max_retries` (integer, default: `2`)
   - `max_pool_connections` (integer, default: `10`)
   - `streaming` (boolean, default: `false`)
   - `max_prompt_chars` (integer, default: `500`)
2. IF a config key is absent from `cowrie.cfg`, THEN THE BedrockClient SHALL use the documented default value without raising an exception.
3. IF a config key is present but contains an invalid value (e.g., a non-integer for `cache_max_entries`), THEN THE BedrockClient SHALL log a WARNING and fall back to the documented default value.
4. THE BedrockClient SHALL validate that `cache_ttl_seconds` is greater than 0 and that `cache_max_entries` is greater than 0 at initialisation time; IF either value is invalid, THEN THE BedrockClient SHALL raise a `ValueError` with a descriptive message.
