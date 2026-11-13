"""Exception types raised by the streaming JSON normalizer."""


class StreamParseError(ValueError):
    """Base error carrying the absolute byte position where parsing failed.

    Positions are measured against the concatenation of every byte chunk
    supplied to the parser, not the current chunk.
    """

    def __init__(self, message, offset, line, column):
        self.message = message
        self.offset = offset
        self.line = line
        self.column = column
        super().__init__(
            "%s (byte offset %d, line %d, column %d)"
            % (message, offset, line, column)
        )


class TruncatedInputError(StreamParseError):
    """Raised when the stream ends in the middle of a value or token."""


class EncodingError(StreamParseError):
    """Raised on an invalid or truncated UTF-8 byte sequence."""


class InvalidTokenError(StreamParseError):
    """Raised on malformed JSON syntax or an illegal escape sequence."""


class DuplicateKeyError(StreamParseError):
    """Raised when one object contains the same key more than once."""


class RecordTooLargeError(StreamParseError):
    """Raised when one record exceeds the configured byte budget."""


class NestingLimitError(StreamParseError):
    """Raised when a value is nested deeper than the configured depth."""
