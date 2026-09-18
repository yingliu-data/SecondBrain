"""P0-5 — the API_SECRET_KEY strength guard.

The bug this prevents: docker-compose substitutes an empty string for an unset
variable, so a missing .env produced API_SECRET_KEY="" -- and HMAC keyed on b""
authenticates any request that signs with the same empty key.
"""
import pytest

from app.config import MIN_SECRET_LEN, require_strong_secret


@pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
def test_empty_or_blank_rejected(bad):
    with pytest.raises(RuntimeError, match="empty or unset"):
        require_strong_secret(bad)


def test_short_key_rejected():
    with pytest.raises(RuntimeError, match=f"minimum is {MIN_SECRET_LEN}"):
        require_strong_secret("a" * (MIN_SECRET_LEN - 1))


def test_error_message_tells_you_how_to_fix_it():
    with pytest.raises(RuntimeError, match="openssl rand -hex 32"):
        require_strong_secret("too-short")


def test_sufficient_key_passes_through_unchanged():
    key = "0123456789abcdef" * 4
    assert require_strong_secret(key) == key


def test_boundary_is_inclusive():
    assert require_strong_secret("a" * MIN_SECRET_LEN)
