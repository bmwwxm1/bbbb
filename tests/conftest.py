"""Shared fixtures for all tests."""

from __future__ import annotations

import os

# Set required env vars BEFORE any bot imports
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("ADMIN_CHAT_ID", "123456")
os.environ.setdefault("MRKT_AUTH_TOKEN", "test_token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")
os.environ.setdefault("SHADOW_MODE", "true")
