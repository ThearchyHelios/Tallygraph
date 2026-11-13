"""Compare SQLite golden bytes across runtimes without changing stored artifacts."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


def baseline_file_hash(path: Path, *, writer_version: int) -> str:
    data = bytearray(path.read_bytes())
    assert data[:16] == b"SQLite format 3\x00"
    major, minor, patch = sqlite3.sqlite_version_info
    # Offset 96 is the SQLite library version that last wrote the file. All other
    # bytes, including page layout, schema and data, remain part of the assertion.
    assert int.from_bytes(data[96:100], "big") == major * 1_000_000 + minor * 1_000 + patch
    data[96:100] = writer_version.to_bytes(4, "big")
    return hashlib.sha256(data).hexdigest()
