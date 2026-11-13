"""Streaming, chunk-safe JSON normalizer.

Split large third-party JSON arrays or JSONL streams into individual
records without loading the whole payload into memory.
"""

from .errors import (
    DuplicateKeyError,
    EncodingError,
    InvalidTokenError,
    NestingLimitError,
    RecordTooLargeError,
    StreamParseError,
    TruncatedInputError,
)
from .normalize import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_RECORD_BYTES,
    MAX_SAFE_INTEGER,
    MIN_SAFE_INTEGER,
    compact_json,
    iter_compact_json,
    iter_records,
)

__all__ = [
    "iter_records",
    "iter_compact_json",
    "compact_json",
    "StreamParseError",
    "TruncatedInputError",
    "EncodingError",
    "InvalidTokenError",
    "DuplicateKeyError",
    "RecordTooLargeError",
    "NestingLimitError",
    "DEFAULT_MAX_RECORD_BYTES",
    "DEFAULT_MAX_DEPTH",
    "MAX_SAFE_INTEGER",
    "MIN_SAFE_INTEGER",
]

__version__ = "0.1.0"
