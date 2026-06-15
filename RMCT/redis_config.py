"""Shared Redis connection options for Channels and collab consumers (Windows-friendly timeouts)."""
from __future__ import annotations

import os


def redis_socket_timeout() -> float:
    return float(os.getenv("REDIS_SOCKET_TIMEOUT", "30"))


def redis_connect_timeout() -> float:
    return float(os.getenv("REDIS_CONNECT_TIMEOUT", "10"))


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


def channels_redis_hosts() -> list:
    """channels_redis host entries with explicit socket timeouts."""
    return [
        {
            "address": redis_url(),
            "socket_timeout": redis_socket_timeout(),
            "socket_connect_timeout": redis_connect_timeout(),
        }
    ]


def async_redis_kwargs() -> dict:
    return {
        "decode_responses": True,
        "socket_timeout": redis_socket_timeout(),
        "socket_connect_timeout": redis_connect_timeout(),
        "health_check_interval": 30,
        "retry_on_timeout": True,
    }
