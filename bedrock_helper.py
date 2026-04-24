# bedrock_helper.py
# Simple reusable Bedrock client. Import this from any file in the project.
#
# Usage:
#   from bedrock_helper import ask_bedrock
#   response = ask_bedrock("what is the capital of France?")
#   print(response)
#
# Credentials are read from environment variables automatically:
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_DEFAULT_REGION
#
# Refresh credentials by updating .envrc and running: source .envrc

from __future__ import annotations

import os
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Default model — cheap and fast
DEFAULT_MODEL = "amazon.nova-micro-v1:0"
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def get_client(region: str = DEFAULT_REGION):
    """Return a boto3 bedrock-runtime client using env var credentials."""
    return boto3.client("bedrock-runtime", region_name=region)


def ask_bedrock(
    prompt: str,
    system_prompt: str = "You are a helpful assistant.",
    model_id: str = DEFAULT_MODEL,
    max_tokens: int = 500,
    temperature: float = 0.7,
    region: str = DEFAULT_REGION,
) -> str:
    """
    Send a prompt to Bedrock and return the response text.

    Args:
        prompt:        The user message to send.
        system_prompt: Instructions for how the model should behave.
        model_id:      Bedrock model ID to use.
        max_tokens:    Max tokens in the response.
        temperature:   0.0-1.0, lower = more deterministic.
        region:        AWS region.

    Returns:
        The model's response as a string, or an error message.
    """
    try:
        client = get_client(region)
        response = client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return response["output"]["message"]["content"][0]["text"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        return f"[Bedrock ClientError] {code}: {msg}"
    except BotoCoreError as e:
        return f"[Bedrock BotoCoreError] {e}"
    except Exception as e:
        return f"[Bedrock Error] {e}"


if __name__ == "__main__":
    # Quick test — run with: python3 bedrock_helper.py
    print("Testing Bedrock connection...")
    result = ask_bedrock("Say hello in one sentence.")
    print(f"Response: {result}")
