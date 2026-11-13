"""Public streaming API.

Two top-level layouts are supported:

* ``array``    - one JSON array whose elements are the records.
* ``sequence`` - JSONL style: one JSON object per logical line, with
                 arbitrary JSON whitespace between records.

``auto`` (the default) picks the layout from the first non-whitespace byte.
"""

import json

from ._parser import _Parser
from ._reader import ByteStream
from .errors import InvalidTokenError, StreamParseError, TruncatedInputError

__all__ = [
    "iter_records",
    "iter_compact_json",
    "compact_json",
    "StreamParseError",
    "TruncatedInputError",
    "InvalidTokenError",
]

DEFAULT_MAX_RECORD_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_DEPTH = 100
# Inclusive safe-integer range, matching Number.isSafeInteger in JS.
MAX_SAFE_INTEGER = 2 ** 53 - 1
MIN_SAFE_INTEGER = -(2 ** 53) + 1


def _chunk_reader(source):
    if hasattr(source, "read"):
        return source.read

    # ``iter`` on an iterator returns the same object; on a list it returns a
    # fresh restartable iterator, which would silently replay old chunks.
    iterator = source if hasattr(source, "__next__") else iter(source)

    def read_chunk():
        for chunk in iterator:
            if chunk:
                return chunk
        return b""

    return read_chunk


def iter_records(
    source,
    layout="auto",
    max_record_bytes=DEFAULT_MAX_RECORD_BYTES,
    max_depth=DEFAULT_MAX_DEPTH,
):
    """Yield parsed records as Python objects.

    ``source`` is a binary file-like object or an iterable of ``bytes``
    chunks (``str`` chunks are accepted and encoded as UTF-8).  The
    generator stops permanently at the first error: once an exception is
    raised no further records are yielded.
    """
    if layout not in ("auto", "array", "sequence"):
        raise ValueError("layout must be 'auto', 'array' or 'sequence'")

    reader = ByteStream(_chunk_reader(source))
    parser = _Parser(
        reader,
        max_record_bytes=max_record_bytes,
        max_depth=max_depth,
        max_integer=MAX_SAFE_INTEGER,
        min_integer=MIN_SAFE_INTEGER,
    )

    started = False
    array_mode = layout == "array"

    while True:
        if not started:
            first = parser.skip_whitespace()
            if first < 0:
                # Empty stream is an error: there is nothing to normalize.
                raise TruncatedInputError(
                    "input contained no records", *parser._position()
                )
            started = True
            if layout == "auto":
                array_mode = first == 0x5B  # '['
            if array_mode:
                if layout == "array" and first != 0x5B:
                    parser._error(
                        InvalidTokenError,
                        "layout='array' requires a top-level array",
                    )
                reader.read_byte()  # consume opening '['

        parser.begin_record()

        if array_mode:
            token = parser.skip_whitespace()
            if token == 0x5D:  # closing ']'
                reader.read_byte()
                _expect_eof(parser)
                return
            value = parser.parse_value(0)
            yield value
            reader.release_consumed()
            delimiter = parser.skip_whitespace()
            if delimiter == 0x5D:  # ']'
                reader.read_byte()
                _expect_eof(parser)
                return
            if delimiter != 0x2C:  # ','
                if delimiter < 0:
                    parser._truncation_error(
                        "unexpected end of stream inside top-level array"
                    )
                parser._error(
                    InvalidTokenError,
                    "expected ',' or ']' between array elements",
                )
            reader.read_byte()
        else:
            value = parser.parse_value(0)
            yield value
            reader.release_consumed()
            token = parser.skip_whitespace()
            if token < 0:
                return


def _expect_eof(parser):
    trailing = parser.skip_whitespace()
    if trailing >= 0:
        parser._error(
            InvalidTokenError,
            "trailing data after end of top-level value",
        )


def compact_json(record):
    """Serialize one record with stable key order and no whitespace."""
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def iter_compact_json(source, **kwargs):
    """Yield one compact JSON ``str`` per successfully parsed record."""
    for record in iter_records(source, **kwargs):
        yield compact_json(record)
