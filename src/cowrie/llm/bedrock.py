# ABOUTME: Amazon Bedrock LLM client for Cowrie honeypot.
# ABOUTME: Uses boto3 with standard AWS credential chain (quickstart via ~/.aws/credentials).

from __future__ import annotations

import json
import threading
from typing import Any

from twisted.internet import defer, threads
from twisted.python import log

from cowrie.core.config import CowrieConfig

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


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

        self._client = boto3.client("bedrock-runtime", region_name=self.region)
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

    def _call_bedrock(self, command: str, hostname: str, username: str, cwd: str) -> str:
        """
        Blocking call to Bedrock Converse API.
        Runs in a thread pool so it doesn't block the Twisted reactor.
        """
        user_message = (
            f"[{username}@{hostname} {cwd}]$ {command}"
        )

        request: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": _SYSTEM_PROMPT}],
            "messages": [{"role": "user", "content": [{"text": user_message}]}],
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }

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

    def get_response(
        self, command: str, hostname: str = "svr04", username: str = "root", cwd: str = "~"
    ) -> defer.Deferred:
        """
        Async wrapper — returns a Deferred that fires with the response string.
        Falls back to empty string on any error.
        """
        d = threads.deferToThread(
            self._call_bedrock, command, hostname, username, cwd
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
