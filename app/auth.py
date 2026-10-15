"""API-key authentication dependency."""
import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException, status
from dotenv import load_dotenv

load_dotenv()


def _expected_key() -> Optional[str]:
    value = os.getenv("API_KEY")
    if value is None or value.strip() == "":
        return None
    return value.strip()


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = _expected_key()
    # If no key is configured, authentication is disabled (development convenience).
    if expected is None:
        return
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
