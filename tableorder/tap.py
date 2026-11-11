"""
tap — shared helpers for the Table Agent Protocol (draft "table-agent/0.1", see PROTOCOL.md).

Used by both sides: the shop agent (order_mock) and the customer's bot (tableorder/native.py).
Needs:  pip install cryptography
"""
import base64, hashlib, json, os, time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PROTOCOL = "table-agent/0.1"
MAX_SKEW = 120  # seconds allowed between a signed request's timestamp and the receiver's clock


class TapError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj):
    """Bytes that get signed: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(obj):
    return "sha256:" + b64u(hashlib.sha256(obj if isinstance(obj, bytes) else canonical(obj)).digest())


# ── keys ──────────────────────────────────────────────────────────────────────

class Key:
    def __init__(self, private=None, public=None):
        self.private = private
        self.public = public or private.public_key()
        raw = self.public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_b64 = b64u(raw)
        self.kid = "ed25519:" + b64u(hashlib.sha256(raw).digest())[:16]

    @classmethod
    def generate(cls):
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path):
        path = Path(path)
        if path.exists():
            return cls(serialization.load_pem_private_key(path.read_bytes(), password=None))
        k = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(k.private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return k

    @classmethod
    def from_public(cls, public_b64):
        return cls(public=Ed25519PublicKey.from_public_bytes(unb64u(public_b64)))

    def describe(self):
        return {"alg": "Ed25519", "kid": self.kid, "public": self.public_b64}

    def sign_bytes(self, data):
        return b64u(self.private.sign(data))

    def verify_bytes(self, data, sig):
        try:
            self.public.verify(unb64u(sig), data)
            return True
        except (InvalidSignature, ValueError):
            return False


# ── signed envelopes ──────────────────────────────────────────────────────────

def sign(key, payload):
    payload = {"protocol": PROTOCOL, "iat": int(time.time()), **payload}
    return {"payload": payload, "sig": {"alg": "Ed25519", "kid": key.kid, "value": key.sign_bytes(canonical(payload))}}


def verify(envelope, key, expect_type=None):
    """Return the payload if the envelope is signed by `key` (and of the expected type)."""
    try:
        payload, sig = envelope["payload"], envelope["sig"]
    except (KeyError, TypeError):
        raise TapError("bad_envelope", "Expected {payload, sig}")
    if sig.get("kid") != key.kid:
        raise TapError("wrong_key", f"Signed by {sig.get('kid')}, expected {key.kid}", 401)
    if not key.verify_bytes(canonical(payload), sig.get("value", "")):
        raise TapError("bad_signature", "Signature does not verify", 401)
    if payload.get("protocol") != PROTOCOL:
        raise TapError("protocol", f"Unsupported protocol {payload.get('protocol')}")
    if expect_type and payload.get("type") != expect_type:
        raise TapError("wrong_type", f"Expected {expect_type}, got {payload.get('type')}")
    return payload


# ── request signatures (every call after the grant) ───────────────────────────

def request_sig_base(method, path, ts, nonce, body_bytes, token):
    return canonical({"m": method.upper(), "p": path, "ts": int(ts), "n": nonce,
                      "b": digest(body_bytes or b""), "t": token})


def sign_request(key, method, path, body_bytes, token):
    ts, nonce = int(time.time()), b64u(os.urandom(9))
    return {"Authorization": f"TAP {token}", "X-TAP-Ts": str(ts), "X-TAP-Nonce": nonce,
            "X-TAP-Sig": key.sign_bytes(request_sig_base(method, path, ts, nonce, body_bytes, token))}


def fingerprint_words(kid):
    """Short human-checkable form of a key id, e.g. for 'is this the shop you're sitting in?'."""
    h = hashlib.sha256(kid.encode()).digest()
    return "-".join(f"{b:02X}" for b in h[:4])
