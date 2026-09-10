"""RSA-PSS request signing for the Kalshi API.

Every private request carries three headers::

    KALSHI-ACCESS-KEY        the API key ID
    KALSHI-ACCESS-TIMESTAMP  current time in MILLISECONDS
    KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS/SHA-256 over timestamp + METHOD + path

Three details are easy to get wrong and each produces an opaque 401:

1. The timestamp is **milliseconds**, not seconds.
2. The signed path is the **full path from the API root** (``/trade-api/v2/...``), with the
   query string stripped and the host excluded.
3. The PSS salt length is **DIGEST_LENGTH**, not the more common ``MAX_LENGTH``.

The private key is read from disk once and never logged, serialised, or copied into an
exception message.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..config import ConfigError, Credentials

__all__ = ["RequestSigner", "load_private_key"]


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    """Load an unencrypted PEM RSA private key.

    Errors deliberately mention the path but never the file's contents.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read private key at {path}: {exc.strerror}") from exc

    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ConfigError(
            f"private key at {path} could not be parsed. Kalshi issues an unencrypted "
            f"PEM RSA key; a passphrase-protected or non-RSA key will not work."
        ) from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConfigError(f"private key at {path} is {type(key).__name__}, expected RSA")
    return key


class RequestSigner:
    """Signs Kalshi API requests. Thread-safe; holds no per-request state."""

    def __init__(self, credentials: Credentials, *, signing_prefix: str = "/trade-api/v2") -> None:
        self._api_key_id = credentials.api_key_id
        self._key = load_private_key(credentials.private_key_path)
        self._signing_prefix = signing_prefix.rstrip("/")

    @staticmethod
    def timestamp_ms() -> str:
        """Current time in milliseconds, as the API expects."""
        return str(int(time.time() * 1000))

    def signing_path(self, path: str) -> str:
        """Normalise a path into the exact string that gets signed.

        Accepts either a bare endpoint path (``/markets``) or a full API path
        (``/trade-api/v2/markets``), strips any query string, and returns the full form. This
        lets callers pass endpoint-relative paths while the signature stays correct.
        """
        without_query = path.split("?", 1)[0]
        if not without_query.startswith("/"):
            without_query = "/" + without_query
        if without_query.startswith(self._signing_prefix):
            return without_query
        return f"{self._signing_prefix}{without_query}"

    def sign(self, timestamp: str, method: str, path: str) -> str:
        """Base64 RSA-PSS/SHA-256 signature over ``timestamp + METHOD + path``."""
        message = f"{timestamp}{method.upper()}{self.signing_path(path)}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, path: str) -> dict[str, str]:
        """The three auth headers for one request."""
        timestamp = self.timestamp_ms()
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": self.sign(timestamp, method, path),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Never render the key or the full key id.
        return f"RequestSigner(api_key_id={self._api_key_id[:8]}...)"
