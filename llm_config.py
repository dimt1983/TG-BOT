"""Shared LLM endpoint configuration for BOT_TG."""
from __future__ import annotations

import os


def anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("PROXYAPI_KEY", "")


def anthropic_base_url() -> str:
    return os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")


def anthropic_messages_url() -> str:
    base = anthropic_base_url()
    return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"


def openai_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("PROXYAPI_KEY", "")


def openai_base_url() -> str:
    return os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def google_api_key() -> str:
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("PROXYAPI_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")


def google_base_url() -> str:
    return os.environ.get("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
