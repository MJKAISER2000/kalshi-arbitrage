"""Request signing tests.

Signing failures produce an opaque 401 with no hint as to which of the three easy mistakes
was made, so each is pinned here: millisecond timestamps, the full path from the API root with
the query string stripped, and PSS DIGEST_LENGTH salt.
"""

import base64
import time
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_arb.api.auth import RequestSigner, load_private_key
from kalshi_arb.config import ConfigError, Credentials


@pytest.fixture(scope="module")
def key_pair(tmp_path_factory) -> tuple[Path, rsa.RSAPrivateKey]:
    """A throwaway RSA key. Never a real credential, even in tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("keys") / "test.key"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path, key


@pytest.fixture
def signer(key_pair) -> RequestSigner:
    path, _ = key_pair
    return RequestSigner(Credentials(api_key_id="test-key-id", private_key_path=path))


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


def test_loads_pem_key(key_pair):
    path, _ = key_pair
    assert isinstance(load_private_key(path), rsa.RSAPrivateKey)


def test_missing_key_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read private key"):
        load_private_key(tmp_path / "absent.key")


def test_malformed_key_raises_without_leaking_contents(tmp_path):
    path = tmp_path / "bad.key"
    path.write_text("-----BEGIN PRIVATE KEY-----\nsupersecretgarbage\n-----END PRIVATE KEY-----")
    with pytest.raises(ConfigError) as exc:
        load_private_key(path)
    assert "supersecretgarbage" not in str(exc.value)


def test_repr_does_not_leak_key_material(signer):
    text = repr(signer)
    assert "PRIVATE" not in text
    assert "test-key-id" not in text  # only a truncated prefix appears


# ---------------------------------------------------------------------------
# Signing path normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("/markets", "/trade-api/v2/markets"),
        ("markets", "/trade-api/v2/markets"),
        ("/trade-api/v2/markets", "/trade-api/v2/markets"),
        ("/portfolio/orders?limit=5", "/trade-api/v2/portfolio/orders"),
        ("/trade-api/v2/portfolio/balance?a=1&b=2", "/trade-api/v2/portfolio/balance"),
    ],
)
def test_signing_path_normalisation(signer, given, expected):
    """Query strings are stripped and the API-root prefix is always present."""
    assert signer.signing_path(given) == expected


def verify(public_key, signature_b64: str, message: str) -> None:
    """Verify a signature against an expected message.

    RSA-PSS salts randomly, so two signatures over the same message differ byte-for-byte.
    Equality comparison would always fail; verification is the only meaningful assertion.
    """
    public_key.verify(
        base64.b64decode(signature_b64),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_query_string_is_excluded_from_the_signed_message(signer, key_pair):
    """Signing the query string is a common cause of intermittent 401s on paginated calls."""
    _, key = key_pair
    ts = "1700000000000"
    expected = f"{ts}GET/trade-api/v2/markets"
    verify(key.public_key(), signer.sign(ts, "GET", "/markets?limit=5&cursor=abc"), expected)
    verify(key.public_key(), signer.sign(ts, "GET", "/markets"), expected)


# ---------------------------------------------------------------------------
# Signature correctness
# ---------------------------------------------------------------------------


def test_signature_verifies_with_digest_length_salt(signer, key_pair):
    """The salt length must be DIGEST_LENGTH; MAX_LENGTH is rejected by Kalshi."""
    _, key = key_pair
    ts, method, path = "1700000000000", "GET", "/portfolio/balance"
    signature = base64.b64decode(signer.sign(ts, method, path))
    message = f"{ts}GET/trade-api/v2/portfolio/balance".encode()

    key.public_key().verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_method_is_uppercased(signer, key_pair):
    _, key = key_pair
    ts = "1700000000000"
    verify(key.public_key(), signer.sign(ts, "get", "/markets"), f"{ts}GET/trade-api/v2/markets")


def test_a_signature_does_not_verify_against_a_different_path(signer, key_pair):
    """A signature must be bound to its exact path, or it could be replayed elsewhere."""
    _, key = key_pair
    ts = "1700000000000"
    sig = signer.sign(ts, "GET", "/markets")
    with pytest.raises(InvalidSignature):
        verify(key.public_key(), sig, f"{ts}GET/trade-api/v2/events")


def test_a_signature_does_not_verify_against_a_different_method(signer, key_pair):
    _, key = key_pair
    ts = "1700000000000"
    sig = signer.sign(ts, "GET", "/markets")
    with pytest.raises(InvalidSignature):
        verify(key.public_key(), sig, f"{ts}POST/trade-api/v2/markets")


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_headers_carry_all_three_fields(signer):
    headers = signer.headers("GET", "/portfolio/balance")
    assert set(headers) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"


def test_timestamp_is_milliseconds_not_seconds(signer):
    """Seconds would be ~1000x too small and rejected as a stale request."""
    ts = int(signer.timestamp_ms())
    now_s = time.time()
    assert abs(ts / 1000 - now_s) < 5
    assert ts > 1_000_000_000_000  # 13 digits: milliseconds since epoch


def test_signature_is_base64(signer):
    sig = signer.headers("GET", "/markets")["KALSHI-ACCESS-SIGNATURE"]
    assert base64.b64decode(sig)  # round-trips
    assert "\n" not in sig
