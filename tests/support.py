"""Helpers shared by test modules."""

import os
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_test_database_url() -> str | None:
    """TEST_DATABASE_URL from the environment, falling back to the project's .env."""
    return os.environ.get("TEST_DATABASE_URL") or dotenv_values(PROJECT_ROOT / ".env").get(
        "TEST_DATABASE_URL"
    )
