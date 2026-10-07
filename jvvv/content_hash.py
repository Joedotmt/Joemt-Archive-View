"""Catalogue file hashes. Storage/snapshot integrity digests remain SHA-256."""
from __future__ import annotations

import hashlib
import re
from typing import Any

DEFAULT_HASH_ALGORITHM = "sha256"
HASH_LABELS = {"sha256": "SHA-256", "blake3": "BLAKE3", "xxh3_128": "XXH3-128"}
HASH_LENGTHS = {"sha256": 32, "blake3": 32, "xxh3_128": 16}


def normalize_hash_algorithm(value: str) -> str:
    algorithm = str(value).strip().lower()
    if algorithm not in HASH_LENGTHS:
        raise ValueError(f"Unsupported content hash algorithm: {value!r}")
    return algorithm


def new_hasher(algorithm: str = DEFAULT_HASH_ALGORITHM) -> Any:
    algorithm = normalize_hash_algorithm(algorithm)
    if algorithm == "sha256":
        return hashlib.sha256()
    try:
        if algorithm == "blake3":
            from blake3 import blake3
            return blake3(max_threads=1)
        from xxhash import xxh3_128
        return xxh3_128()
    except ImportError as exc:
        raise RuntimeError(
            f"{HASH_LABELS[algorithm]} support is not installed. Install the project's dependencies before scanning."
        ) from exc


def content_hash_hex(digest: bytes | str, algorithm: str = DEFAULT_HASH_ALGORITHM) -> str:
    algorithm = normalize_hash_algorithm(algorithm)
    length = HASH_LENGTHS[algorithm]
    label = HASH_LABELS[algorithm]
    if isinstance(digest, (bytes, bytearray, memoryview)):
        if len(digest) != length:
            raise ValueError(f"A {label} digest must be exactly {length} bytes long, not {len(digest)}.")
        return bytes(digest).hex()
    if isinstance(digest, str):
        if re.fullmatch(r"[0-9a-fA-F]{%d}" % (length * 2), digest):
            return digest.lower()
        raise ValueError(f"A {label} hash must be exactly {length * 2} hexadecimal characters.")
    raise ValueError(f"A {label} hash must be bytes or text, not {type(digest).__name__}.")
