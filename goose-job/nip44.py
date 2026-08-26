"""NIP-44 v2 encrypt/decrypt. Used for Buzz observer frames (kind 24200)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct

from coincurve import PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

_SALT = b"nip44-v2"
_MIN_PLAINTEXT = 1
_MAX_PLAINTEXT = 65_535
_VERSION = 2


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def conversation_key(secret: bytes, recipient_pubkey_hex: str) -> bytes:
    # Raw ECDH x-coordinate. coincurve.ecdh() hashes the point (libsecp256k1
    # default) which does not match NIP-44.
    pub = PublicKey(b"\x02" + bytes.fromhex(recipient_pubkey_hex))
    shared_x = pub.multiply(secret).format(compressed=True)[1:]
    return _hkdf_extract(_SALT, shared_x)


def _message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    okm = _hkdf_expand(conv_key, nonce, 76)
    return okm[:32], okm[32:44], okm[44:76]


def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << ((unpadded_len - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (((unpadded_len - 1) // chunk) + 1)


def _pad(plaintext: str) -> bytes:
    unpadded = plaintext.encode("utf-8")
    n = len(unpadded)
    if n < _MIN_PLAINTEXT or n > _MAX_PLAINTEXT:
        raise ValueError("plaintext length out of range")
    padded_len = calc_padded_len(n)
    return struct.pack(">H", n) + unpadded + (b"\x00" * (padded_len - n))


def _unpad(padded: bytes) -> str:
    if len(padded) < 2:
        raise ValueError("padded payload too short")
    n = struct.unpack(">H", padded[:2])[0]
    if n < _MIN_PLAINTEXT or n > _MAX_PLAINTEXT or 2 + n > len(padded):
        raise ValueError("invalid pad length")
    body = padded[2 : 2 + n]
    if padded[2 + n :] != b"\x00" * (len(padded) - 2 - n):
        raise ValueError("invalid padding")
    return body.decode("utf-8")


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    nonce16 = (0).to_bytes(4, "little") + nonce12
    encryptor = Cipher(algorithms.ChaCha20(key, nonce16), mode=None).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _mac(hmac_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()


def encrypt(plaintext: str, conv_key: bytes, nonce: bytes | None = None) -> str:
    nonce = nonce if nonce is not None else os.urandom(32)
    if len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    ciphertext = _chacha20(chacha_key, chacha_nonce, _pad(plaintext))
    payload = bytes([_VERSION]) + nonce + ciphertext + _mac(hmac_key, nonce, ciphertext)
    return base64.b64encode(payload).decode("ascii")


def decrypt(payload_b64: str, conv_key: bytes) -> str:
    raw = base64.b64decode(payload_b64)
    if len(raw) < 1 + 32 + 32 + 32 or raw[0] != _VERSION:
        raise ValueError("invalid nip44 payload")
    nonce = raw[1:33]
    mac = raw[-32:]
    ciphertext = raw[33:-32]
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    if not hmac.compare_digest(mac, _mac(hmac_key, nonce, ciphertext)):
        raise ValueError("invalid mac")
    return _unpad(_chacha20(chacha_key, chacha_nonce, ciphertext))
