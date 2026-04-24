# ABOUTME: Property-based and unit tests for AWS Brain Response Optimization.
# ABOUTME: Tests for ResponseCache, build_cache_key, LatencyStats, and BedrockClient.

from __future__ import annotations

import configparser
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from cowrie.llm.bedrock import CacheEntry, LatencyStats, ResponseCache, _fallback_response, build_cache_key

# Module-level constants for config key property tests
CONFIG_KEYS = [
    "cache_enabled",
    "cache_ttl_seconds",
    "cache_max_entries",
    "timeout_seconds",
    "max_retries",
    "max_pool_connections",
    "streaming",
    "max_prompt_chars",
]

NUMERIC_KEYS = [
    "cache_ttl_seconds",
    "cache_max_entries",
    "max_retries",
    "max_pool_connections",
    "max_prompt_chars",
]


class TestResponseCacheProperties(unittest.TestCase):
    """Property-based tests for ResponseCache."""

    # Feature: aws-brain-response-optimization, Property 1: Cache round-trip
    @given(st.text(), st.text())
    @settings(max_examples=100)
    def test_prop_cache_roundtrip(self, key: str, value: str) -> None:
        """Validates: Requirements 1.8

        For any cache key k and response string r, storing r under k and
        immediately retrieving with k SHALL return r.
        """
        cache = ResponseCache(max_entries=100, ttl_seconds=3600)
        cache.put(key, value)
        result = cache.get(key)
        self.assertEqual(result, value)

    # Feature: aws-brain-response-optimization, Property 4: LRU eviction on capacity overflow
    @given(st.integers(min_value=1, max_value=50))
    @settings(max_examples=100)
    def test_prop_lru_eviction(self, n: int) -> None:
        """Validates: Requirements 1.5

        For any ResponseCache with max_entries=N and N+1 distinct keys inserted
        in order, the entry that was least recently used SHALL be absent after
        the (N+1)th insertion.
        """
        cache = ResponseCache(max_entries=n, ttl_seconds=3600)
        keys = [f"key_{i}" for i in range(n + 1)]
        for k in keys:
            cache.put(k, f"value_{k}")
        # The first key inserted (LRU) should have been evicted
        self.assertIsNone(cache.get(keys[0]))
        # The most recently inserted key should still be present
        self.assertEqual(cache.get(keys[-1]), f"value_{keys[-1]}")

    # Feature: aws-brain-response-optimization, Property 5: Cache key normalisation is idempotent
    @given(st.text())
    @settings(max_examples=100)
    def test_prop_cache_key_normalisation(self, command: str) -> None:
        """Validates: Requirements 1.7

        For any command string s, the cache key produced from s with arbitrary
        leading, trailing, or internal whitespace variations SHALL be identical
        to the cache key produced from the whitespace-normalised form of s.
        """
        cwd = "/home/user"
        username = "root"
        hostname = "svr04"

        normalised = " ".join(command.split())

        # Key from original command
        key_original = build_cache_key(command, cwd, username, hostname)
        # Key from normalised command
        key_normalised = build_cache_key(normalised, cwd, username, hostname)

        self.assertEqual(key_original, key_normalised)


class TestCacheEntry(unittest.TestCase):
    """Unit tests for CacheEntry dataclass."""

    def test_cache_entry_fields(self) -> None:
        entry = CacheEntry(value="hello", inserted_at=1234.5)
        self.assertEqual(entry.value, "hello")
        self.assertEqual(entry.inserted_at, 1234.5)


class TestResponseCacheUnit(unittest.TestCase):
    """Unit tests for ResponseCache."""

    def test_get_miss_returns_none(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        self.assertIsNone(cache.get("nonexistent"))

    def test_put_and_get(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        cache.put("k", "v")
        self.assertEqual(cache.get("k"), "v")

    def test_invalidate(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        cache.put("k", "v")
        cache.invalidate("k")
        self.assertIsNone(cache.get("k"))

    def test_invalidate_nonexistent_is_noop(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        cache.invalidate("missing")  # should not raise

    def test_clear(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.clear()
        self.assertIsNone(cache.get("a"))
        self.assertIsNone(cache.get("b"))

    def test_stats_initial(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        s = cache.stats()
        self.assertEqual(s, {"hits": 0, "misses": 0, "evictions": 0})

    def test_stats_after_operations(self) -> None:
        cache = ResponseCache(max_entries=2, ttl_seconds=3600)
        cache.get("missing")  # miss
        cache.put("a", "1")
        cache.get("a")  # hit
        cache.put("b", "2")
        cache.put("c", "3")  # evicts "a"
        s = cache.stats()
        self.assertEqual(s["hits"], 1)
        self.assertEqual(s["misses"], 1)
        self.assertEqual(s["evictions"], 1)

    def test_ttl_expiry(self) -> None:
        import time

        cache = ResponseCache(max_entries=10, ttl_seconds=0)
        cache.put("k", "v")
        # With ttl=0, any elapsed time > 0 should expire the entry
        time.sleep(0.01)
        self.assertIsNone(cache.get("k"))

    def test_overwrite_existing_key(self) -> None:
        cache = ResponseCache(max_entries=10, ttl_seconds=3600)
        cache.put("k", "v1")
        cache.put("k", "v2")
        self.assertEqual(cache.get("k"), "v2")


class TestBuildCacheKey(unittest.TestCase):
    """Unit tests for build_cache_key()."""

    def test_returns_hex_string(self) -> None:
        key = build_cache_key("ls", "/", "root", "host")
        # SHA-256 hex digest is 64 chars
        self.assertEqual(len(key), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in key))

    def test_different_inputs_produce_different_keys(self) -> None:
        k1 = build_cache_key("ls", "/", "root", "host")
        k2 = build_cache_key("pwd", "/", "root", "host")
        self.assertNotEqual(k1, k2)

    def test_whitespace_normalisation(self) -> None:
        k1 = build_cache_key("  ls   -la  ", "/", "root", "host")
        k2 = build_cache_key("ls -la", "/", "root", "host")
        self.assertEqual(k1, k2)

    def test_deterministic(self) -> None:
        k1 = build_cache_key("ls", "/", "root", "host")
        k2 = build_cache_key("ls", "/", "root", "host")
        self.assertEqual(k1, k2)


class TestLatencyStatsProperties(unittest.TestCase):
    """Property-based tests for LatencyStats."""

    # Feature: aws-brain-response-optimization, Property 10: Latency is always recorded
    @given(st.integers(min_value=1, max_value=20))
    @settings(max_examples=100)
    def test_prop_latency_always_recorded(self, n: int) -> None:
        """Validates: Requirements 6.1, 6.4

        For any get_response() call (hit, miss, timeout, or error), a
        non-negative latency_ms value SHALL be recorded in LatencyStats and
        the total_calls counter SHALL increment by exactly 1.
        """
        stats = LatencyStats()
        outcomes = ["hit", "miss", "timeout", "error"]
        for i in range(n):
            outcome = outcomes[i % len(outcomes)]
            latency = float(i)
            before = stats.snapshot()["total_calls"]
            stats.record(outcome, latency)
            after = stats.snapshot()["total_calls"]
            self.assertEqual(after - before, 1)
            self.assertGreaterEqual(stats.snapshot()["mean_latency_ms"], 0.0)

    # Feature: aws-brain-response-optimization, Property 11: get_stats() schema is always complete
    @given(st.lists(st.sampled_from(["hit", "miss", "timeout", "error"])))
    @settings(max_examples=100)
    def test_prop_stats_schema_complete(self, outcomes: list) -> None:
        """Validates: Requirements 6.4, 6.5

        For any sequence of get_response() calls, get_stats() SHALL return a
        dict containing exactly the keys total_calls, cache_hits, cache_misses,
        timeouts, errors, and mean_latency_ms, with all integer fields >= 0
        and mean_latency_ms >= 0.0.
        """
        required_keys = {"total_calls", "cache_hits", "cache_misses", "timeouts", "errors", "mean_latency_ms"}
        stats = LatencyStats()
        for i, outcome in enumerate(outcomes):
            stats.record(outcome, float(i))
        snap = stats.snapshot()
        self.assertEqual(set(snap.keys()), required_keys)
        for key in ("total_calls", "cache_hits", "cache_misses", "timeouts", "errors"):
            self.assertGreaterEqual(snap[key], 0)
        self.assertGreaterEqual(snap["mean_latency_ms"], 0.0)


class TestBedrockClientConfigProperties(unittest.TestCase):
    """Property-based tests for BedrockClient.__init__() config handling."""

    def setUp(self) -> None:
        # Reset singleton before each test to avoid cross-test contamination
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def tearDown(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def _make_config_mock(self, absent_keys: frozenset) -> MagicMock:
        """Return a CowrieConfig mock that raises NoSectionError for absent keys."""
        defaults = {
            "cache_enabled": True,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "timeout_seconds": 10.0,
            "max_retries": 2,
            "max_pool_connections": 10,
            "streaming": False,
            "max_prompt_chars": 500,
            # pre-existing keys
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
            "max_tokens": 300,
            "temperature": 0.3,
            "debug": False,
        }

        def _get(section, key, fallback=None):
            if key in absent_keys:
                raise configparser.NoSectionError(section)
            return defaults.get(key, fallback)

        def _getint(section, key, fallback=None):
            if key in absent_keys:
                raise configparser.NoSectionError(section)
            return int(defaults.get(key, fallback))

        def _getfloat(section, key, fallback=None):
            if key in absent_keys:
                raise configparser.NoSectionError(section)
            return float(defaults.get(key, fallback))

        def _getboolean(section, key, fallback=None):
            if key in absent_keys:
                raise configparser.NoSectionError(section)
            return bool(defaults.get(key, fallback))

        mock = MagicMock()
        mock.get.side_effect = _get
        mock.getint.side_effect = _getint
        mock.getfloat.side_effect = _getfloat
        mock.getboolean.side_effect = _getboolean
        return mock

    # Feature: aws-brain-response-optimization, Property 12: Missing config keys never raise exceptions
    @given(st.frozensets(st.sampled_from(CONFIG_KEYS)))
    @settings(max_examples=100)
    def test_prop_missing_config_no_exception(self, absent_keys: frozenset) -> None:
        """Validates: Requirements 7.2

        For any subset of the eight new [bedrock] config keys being absent,
        BedrockClient.__init__() SHALL complete without raising an exception
        and SHALL use the documented default value for each absent key.
        """
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = self._make_config_mock(absent_keys)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            # Should not raise regardless of which keys are absent
            try:
                client = bedrock_mod.BedrockClient()
            except Exception as exc:
                self.fail(
                    f"BedrockClient.__init__() raised {type(exc).__name__} "
                    f"with absent keys {absent_keys}: {exc}"
                )

    # Feature: aws-brain-response-optimization, Property 13: Invalid config values fall back to defaults
    @given(
        st.sampled_from(NUMERIC_KEYS),
        st.text(min_size=1).filter(lambda s: not s.strip().lstrip("-").isdigit()),
    )
    @settings(max_examples=100)
    def test_prop_invalid_config_uses_default(self, bad_key: str, bad_value: str) -> None:
        """Validates: Requirements 7.3

        For any config key that accepts a numeric type, providing a non-numeric
        string value SHALL cause BedrockClient.__init__() to log a WARNING and
        use the documented default value rather than raising an exception.
        """
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        defaults = {
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }

        def _getint_raises_for_bad_key(section, key, fallback=None):
            if key == bad_key:
                raise ValueError(f"invalid literal: {bad_value!r}")
            good = {
                "max_tokens": 300,
                "cache_ttl_seconds": 3600,
                "cache_max_entries": 1000,
                "max_retries": 2,
                "max_pool_connections": 10,
                "max_prompt_chars": 500,
            }
            return good.get(key, fallback)

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = _getint_raises_for_bad_key
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            try:
                client = bedrock_mod.BedrockClient()
            except Exception as exc:
                self.fail(
                    f"BedrockClient.__init__() raised {type(exc).__name__} "
                    f"for invalid {bad_key}={bad_value!r}: {exc}"
                )
            # The attribute should equal the documented default
            self.assertEqual(getattr(client, bad_key), defaults[bad_key])

    # Feature: aws-brain-response-optimization, Property 14: Non-positive cache config raises ValueError
    @given(st.integers(max_value=0))
    @settings(max_examples=100)
    def test_prop_nonpositive_cache_ttl_raises(self, bad_value: int) -> None:
        """Validates: Requirements 7.4

        For any value v <= 0 assigned to cache_ttl_seconds,
        BedrockClient.__init__() SHALL raise a ValueError with a descriptive message.
        """
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: (
            bad_value if k == "cache_ttl_seconds" else {
                "max_tokens": 300,
                "cache_max_entries": 1000,
                "max_retries": 2,
                "max_pool_connections": 10,
                "max_prompt_chars": 500,
            }.get(k, fallback)
        )
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            with self.assertRaises(ValueError):
                bedrock_mod.BedrockClient()

    @given(st.integers(max_value=0))
    @settings(max_examples=100)
    def test_prop_nonpositive_cache_max_entries_raises(self, bad_value: int) -> None:
        """Validates: Requirements 7.4

        For any value v <= 0 assigned to cache_max_entries,
        BedrockClient.__init__() SHALL raise a ValueError with a descriptive message.
        """
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: (
            bad_value if k == "cache_max_entries" else {
                "max_tokens": 300,
                "cache_ttl_seconds": 3600,
                "max_retries": 2,
                "max_pool_connections": 10,
                "max_prompt_chars": 500,
            }.get(k, fallback)
        )
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            with self.assertRaises(ValueError):
                bedrock_mod.BedrockClient()


class TestBedrockClientPromptProperties(unittest.TestCase):
    """Property-based tests for BedrockClient._build_prompt()."""

    def setUp(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def tearDown(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def _make_client(self, max_prompt_chars: int):
        """Create a BedrockClient with the given max_prompt_chars, mocking config and boto3."""
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": max_prompt_chars,
        }.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            client = bedrock_mod.BedrockClient()
        return client

    # Feature: aws-brain-response-optimization, Property 9: Prompt length never exceeds configured limit
    @given(st.text(), st.integers(min_value=50, max_value=2000))
    @settings(max_examples=100)
    def test_prop_prompt_length_bounded(self, command: str, limit: int) -> None:
        """Validates: Requirements 4.5

        For any command string of arbitrary length, the total character count
        of the prompt payload sent to the Bedrock API SHALL be less than or
        equal to the configured max_prompt_chars value.
        """
        client = self._make_client(limit)
        payload = client._build_prompt(command, "svr04", "root", "~")

        # Count actual text content: system prompt text + user message text
        system_text = "".join(item["text"] for item in payload["system"])
        user_text = "".join(
            block["text"]
            for msg in payload["messages"]
            for block in msg["content"]
            if "text" in block
        )
        total_chars = len(system_text) + len(user_text)

        self.assertLessEqual(
            total_chars,
            limit,
            f"Total prompt chars {total_chars} exceeded limit {limit} "
            f"for command of length {len(command)}",
        )


class TestFallbackResponseProperties(unittest.TestCase):
    """Property-based tests for _fallback_response() and error handling."""

    # Feature: aws-brain-response-optimization, Property 6: Fallback response format
    @given(st.text(min_size=1))
    @settings(max_examples=100)
    def test_prop_fallback_format(self, command: str) -> None:
        """Validates: Requirements 2.2, 2.3, 2.5

        For any command string, when the Bedrock call fails, the returned string
        SHALL match the pattern `-bash: <first_token>: command not found` where
        <first_token> is the first whitespace-delimited token of the original command.
        """
        tokens = command.split()
        expected_token = tokens[0] if tokens else command
        expected = f"-bash: {expected_token}: command not found"
        self.assertEqual(_fallback_response(command), expected)

    # Feature: aws-brain-response-optimization, Property 7: Error handling always returns fallback
    @given(st.sampled_from(["ClientError", "BotoCoreError", "EndpointConnectionError"]))
    @settings(max_examples=100)
    def test_prop_errors_return_fallback(self, exc_type_name: str) -> None:
        """Validates: Requirements 2.3, 2.4, 3.4

        For any boto3 exception type raised during a Bedrock call, _call_bedrock()
        SHALL return the fallback response string rather than propagating the exception.
        """
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

        exc_map = {
            "ClientError": ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "test"}}, "Converse"
            ),
            "BotoCoreError": BotoCoreError(),
            "EndpointConnectionError": EndpointConnectionError(
                endpoint_url="https://bedrock.us-east-1.amazonaws.com"
            ),
        }
        exc_to_raise = exc_map[exc_type_name]

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 0,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 0.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        command = "somecommand"
        expected = _fallback_response(command)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_client = MagicMock()
            mock_client.converse.side_effect = exc_to_raise
            mock_boto3.client.return_value = mock_client

            bedrock_mod.BedrockClient._instance = None
            client = bedrock_mod.BedrockClient()
            result = client._call_bedrock(command, "svr04", "root", "~")

        self.assertEqual(result, expected)
        bedrock_mod.BedrockClient._instance = None


class TestBedrockStreamingUnit(unittest.TestCase):
    """Unit tests for streaming API selection in BedrockClient."""

    def setUp(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def tearDown(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def _make_client(self, streaming: bool):
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": streaming,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            client = bedrock_mod.BedrockClient()
        return client

    def test_streaming_true_calls_converse_stream(self) -> None:
        """When streaming=True, _call_bedrock_streaming() uses converse_stream, not converse."""
        client = self._make_client(streaming=True)

        # Set up mock stream response
        mock_events = [
            {"contentBlockDelta": {"delta": {"text": "hello "}}},
            {"contentBlockDelta": {"delta": {"text": "world"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
        mock_stream_response = {"stream": iter(mock_events)}
        client._client = MagicMock()
        client._client.converse_stream.return_value = mock_stream_response

        result = client._call_bedrock_streaming("ls", "svr04", "root", "~")

        client._client.converse_stream.assert_called_once()
        client._client.converse.assert_not_called()
        self.assertEqual(result, "hello world")

    def test_streaming_false_calls_converse(self) -> None:
        """When streaming=False, get_response() uses _call_bedrock_with_timeout (converse), not converse_stream."""
        client = self._make_client(streaming=False)

        mock_response = {
            "output": {
                "message": {
                    "content": [{"text": "file1.txt"}]
                }
            }
        }
        client._client = MagicMock()
        client._client.converse.return_value = mock_response

        result = client._call_bedrock("ls", "svr04", "root", "~")

        client._client.converse.assert_called_once()
        client._client.converse_stream.assert_not_called()
        self.assertEqual(result, "file1.txt")

    def test_streaming_mid_stream_error_appends_fallback(self) -> None:
        """On mid-stream error, already-received chunks are kept and fallback is appended."""
        client = self._make_client(streaming=True)

        def bad_stream():
            yield {"contentBlockDelta": {"delta": {"text": "partial "}}}
            raise RuntimeError("stream broken")

        client._client = MagicMock()
        client._client.converse_stream.return_value = {"stream": bad_stream()}

        result = client._call_bedrock_streaming("badcmd", "svr04", "root", "~")

        # Should contain the partial chunk plus the fallback
        self.assertIn("partial", result)
        self.assertIn("command not found", result)

    def test_streaming_assembles_all_chunks(self) -> None:
        """All text chunks from the stream are concatenated into the final response."""
        client = self._make_client(streaming=True)

        chunks = ["line1\n", "line2\n", "line3"]
        mock_events = [
            {"contentBlockDelta": {"delta": {"text": c}}} for c in chunks
        ] + [{"messageStop": {"stopReason": "end_turn"}}]

        client._client = MagicMock()
        client._client.converse_stream.return_value = {"stream": iter(mock_events)}

        result = client._call_bedrock_streaming("cat file", "svr04", "root", "~")
        self.assertEqual(result, "line1\nline2\nline3")


class TestBedrockSingletonProperties(unittest.TestCase):
    """Property-based tests for BedrockClient singleton identity."""

    def setUp(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def tearDown(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    # Feature: aws-brain-response-optimization, Property 8: Singleton client identity
    @given(st.integers(min_value=2, max_value=20))
    @settings(max_examples=100)
    def test_prop_singleton_identity(self, n: int) -> None:
        """Validates: Requirements 3.1, 3.3

        For any sequence of N >= 2 calls to BedrockClient.get_instance(), all
        returned references SHALL point to the same object (i.e., id(instance_i)
        == id(instance_j) for all i, j).
        """
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            instances = [bedrock_mod.BedrockClient.get_instance() for _ in range(n)]

        first_id = id(instances[0])
        for i, inst in enumerate(instances[1:], start=1):
            self.assertEqual(
                id(inst),
                first_id,
                f"get_instance() call {i} returned a different object (id mismatch)",
            )


class TestBedrockCacheIntegrationProperties(unittest.TestCase):
    """Property-based tests for cache integration in _get_response_sync()."""

    def setUp(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def tearDown(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def _make_client(self):
        """Create a BedrockClient with cache enabled, mocking config and boto3."""
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            client = bedrock_mod.BedrockClient()
        return client

    # Feature: aws-brain-response-optimization, Property 2: Cache hit bypasses Bedrock
    @given(st.text(), st.text(), st.text(), st.text())
    @settings(max_examples=100)
    def test_prop_cache_hit_no_bedrock_call(
        self, command: str, cwd: str, username: str, hostname: str
    ) -> None:
        """Validates: Requirements 1.2

        For any (command, cwd, username, hostname) tuple whose cache key is
        already present in the ResponseCache, calling _get_response_sync()
        SHALL return the cached value without invoking the boto3 Bedrock client.
        """
        from cowrie.llm.bedrock import build_cache_key

        client = self._make_client()
        key = build_cache_key(command, cwd, username, hostname)
        cached_value = "cached response"
        client._cache.put(key, cached_value)

        # Replace the boto3 client with a fresh mock to track calls
        client._client = MagicMock()

        result = client._get_response_sync(command, hostname, username, cwd)

        self.assertEqual(result, cached_value)
        client._client.converse.assert_not_called()
        client._client.converse_stream.assert_not_called()

    # Feature: aws-brain-response-optimization, Property 3: Cache miss stores result
    @given(st.text(), st.text(), st.text(), st.text())
    @settings(max_examples=100)
    def test_prop_cache_miss_stores_result(
        self, command: str, cwd: str, username: str, hostname: str
    ) -> None:
        """Validates: Requirements 1.3

        For any (command, cwd, username, hostname) tuple whose cache key is
        absent from the ResponseCache, calling _get_response_sync() SHALL
        invoke the boto3 Bedrock client exactly once and store the returned
        response in the cache before returning it.
        """
        from cowrie.llm.bedrock import build_cache_key

        client = self._make_client()
        # Ensure cache is empty for this key
        key = build_cache_key(command, cwd, username, hostname)
        client._cache.invalidate(key)

        bedrock_response = "bedrock response"
        mock_converse_response = {
            "output": {
                "message": {
                    "content": [{"text": bedrock_response}]
                }
            }
        }
        client._client = MagicMock()
        client._client.converse.return_value = mock_converse_response

        result = client._get_response_sync(command, hostname, username, cwd)

        # Bedrock was called exactly once
        client._client.converse.assert_called_once()
        # Result is stored in cache
        cached = client._cache.get(key)
        self.assertEqual(cached, result)


class TestBedrockClientUnitExtras(unittest.TestCase):
    """Additional example-based unit tests for BedrockClient."""

    def setUp(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def tearDown(self) -> None:
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

    def _make_client(self, extra_config: dict | None = None):
        """Create a BedrockClient with optional config overrides."""
        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        int_defaults = {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }
        float_defaults = {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }
        bool_defaults = {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }
        str_defaults = {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }

        if extra_config:
            for k, v in extra_config.items():
                if k in int_defaults:
                    int_defaults[k] = v
                elif k in float_defaults:
                    float_defaults[k] = v
                elif k in bool_defaults:
                    bool_defaults[k] = v
                elif k in str_defaults:
                    str_defaults[k] = v

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: str_defaults.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: int_defaults.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: float_defaults.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: bool_defaults.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            client = bedrock_mod.BedrockClient()
        return client

    def test_cache_enabled_false_always_calls_bedrock(self) -> None:
        """When cache_enabled=False, two identical calls both invoke Bedrock."""
        client = self._make_client({"cache_enabled": False, "timeout_seconds": 0.0, "max_retries": 0})
        self.assertIsNone(client._cache)

        mock_response = {
            "output": {"message": {"content": [{"text": "result"}]}}
        }
        client._client = MagicMock()
        client._client.converse.return_value = mock_response

        client._get_response_sync("ls", "svr04", "root", "~")
        client._get_response_sync("ls", "svr04", "root", "~")

        self.assertEqual(client._client.converse.call_count, 2)

    def test_timeout_zero_no_timeout_wrapper(self) -> None:
        """When timeout_seconds=0, _call_bedrock_with_timeout calls _call_bedrock directly."""
        import concurrent.futures
        client = self._make_client({"timeout_seconds": 0.0, "max_retries": 0})
        self.assertEqual(client.timeout_seconds, 0.0)

        mock_response = {
            "output": {"message": {"content": [{"text": "ok"}]}}
        }
        client._client = MagicMock()
        client._client.converse.return_value = mock_response

        # Patch ThreadPoolExecutor to verify it is NOT used when timeout=0
        with patch("cowrie.llm.bedrock.concurrent.futures.ThreadPoolExecutor") as mock_executor:
            result = client._call_bedrock_with_timeout("echo hi", "svr04", "root", "~")

        mock_executor.assert_not_called()
        self.assertEqual(result, "ok")

    def test_throttling_retry_count(self) -> None:
        """ThrottlingException raised N times then succeeds; call count == N+1."""
        from botocore.exceptions import ClientError

        max_retries = 2
        client = self._make_client({"max_retries": max_retries, "timeout_seconds": 0.0})

        throttle_exc = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )
        success_response = {
            "output": {"message": {"content": [{"text": "success"}]}}
        }

        # Raise ThrottlingException max_retries times, then succeed
        side_effects = [throttle_exc] * max_retries + [success_response]
        client._client = MagicMock()
        client._client.converse.side_effect = side_effects

        # Patch time.sleep to avoid actual delays
        with patch("cowrie.llm.bedrock.time.sleep"):
            result = client._call_bedrock("ls", "svr04", "root", "~")

        self.assertEqual(result, "success")
        self.assertEqual(client._client.converse.call_count, max_retries + 1)

    def test_get_stats_initial_state(self) -> None:
        """get_stats() returns all zeros before any calls are made."""
        from cowrie.llm.bedrock import get_stats

        import cowrie.llm.bedrock as bedrock_mod
        bedrock_mod.BedrockClient._instance = None

        config_mock = MagicMock()
        config_mock.get.side_effect = lambda s, k, fallback=None: {
            "model_id": "amazon.nova-micro-v1:0",
            "region": "us-east-1",
        }.get(k, fallback)
        config_mock.getint.side_effect = lambda s, k, fallback=None: {
            "max_tokens": 300,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 1000,
            "max_retries": 2,
            "max_pool_connections": 10,
            "max_prompt_chars": 500,
        }.get(k, fallback)
        config_mock.getfloat.side_effect = lambda s, k, fallback=None: {
            "temperature": 0.3,
            "timeout_seconds": 10.0,
        }.get(k, fallback)
        config_mock.getboolean.side_effect = lambda s, k, fallback=None: {
            "debug": False,
            "cache_enabled": True,
            "streaming": False,
        }.get(k, fallback)

        with patch("cowrie.llm.bedrock.boto3") as mock_boto3, \
             patch("cowrie.llm.bedrock.CowrieConfig", config_mock):
            mock_boto3.client.return_value = MagicMock()
            bedrock_mod.BedrockClient._instance = None
            # Force singleton creation
            bedrock_mod.BedrockClient.get_instance()
            stats = get_stats()

        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["cache_hits"], 0)
        self.assertEqual(stats["cache_misses"], 0)
        self.assertEqual(stats["timeouts"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["mean_latency_ms"], 0.0)

    def test_debug_logging_logs_request_and_response(self) -> None:
        """When debug=True, request and response payloads are logged via log.msg."""
        client = self._make_client({"debug": True, "timeout_seconds": 0.0, "max_retries": 0})
        self.assertTrue(client.debug)

        mock_response = {
            "output": {"message": {"content": [{"text": "debug output"}]}}
        }
        client._client = MagicMock()
        client._client.converse.return_value = mock_response

        logged_messages: list[str] = []

        with patch("cowrie.llm.bedrock.log") as mock_log:
            mock_log.msg.side_effect = lambda msg, *a, **kw: logged_messages.append(str(msg))
            client._call_bedrock_once("ls", "svr04", "root", "~")

        # At least one log message should contain request payload info
        request_logged = any("request" in m.lower() or "modelId" in m or "messages" in m for m in logged_messages)
        response_logged = any("response" in m.lower() for m in logged_messages)
        self.assertTrue(request_logged, f"Expected request log; got: {logged_messages}")
        self.assertTrue(response_logged, f"Expected response log; got: {logged_messages}")


if __name__ == "__main__":
    unittest.main()
