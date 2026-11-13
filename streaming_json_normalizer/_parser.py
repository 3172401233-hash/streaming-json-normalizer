"""Recursive-descent JSON parser operating on a chunked byte stream."""

from .errors import (
    DuplicateKeyError,
    InvalidTokenError,
    NestingLimitError,
    RecordTooLargeError,
    TruncatedInputError,
)

_WHITESPACE = frozenset(b" \t\n\r")
_SIMPLE_ESCAPES = {
    0x22: 0x22,  # "
    0x5C: 0x5C,  # backslash
    0x2F: 0x2F,  # /
    0x62: 0x08,  # b
    0x66: 0x0C,  # f
    0x6E: 0x0A,  # n
    0x72: 0x0D,  # r
    0x74: 0x09,  # t
}
_LITERALS = {
    b"true": True,
    b"false": False,
    b"null": None,
}


class _Parser:
    def __init__(
        self,
        reader,
        max_record_bytes,
        max_depth,
        max_integer,
        min_integer,
    ):
        self._reader = reader
        self._max_record_bytes = max_record_bytes
        self._max_depth = max_depth
        self._max_integer = max_integer
        self._min_integer = min_integer

    def _position(self, relative=0):
        offset = self._reader.absolute_offset(relative)
        line, column = self._reader._line_column(offset)
        return offset, line, column

    def _eof_position(self):
        offset = self._reader.absolute_offset() + self._reader.buffered_bytes()
        line, column = self._reader._line_column(offset)
        return offset, line, column

    def _error(self, exc_type, message, relative=0):
        raise exc_type(message, *self._position(relative))

    def _truncation_error(self, message):
        raise TruncatedInputError(message, *self._eof_position())

    def _check_record_size(self):
        reader = self._reader
        consumed = reader.absolute_offset() - self._record_start
        buffered = reader.buffered_bytes()
        # Bytes already consumed for this record, plus any buffer growth
        # caused by refills that belong to the in-flight record.  Preloaded
        # bytes for later records are excluded by the baseline.
        size = consumed + max(0, buffered - self._record_buffered_base)
        if size > self._max_record_bytes:
            self._error(
                RecordTooLargeError,
                "record exceeds limit of %d bytes" % self._max_record_bytes,
            )

    def begin_record(self):
        self._record_start = self._reader.absolute_offset()
        self._record_buffered_base = self._reader.buffered_bytes()

    def skip_whitespace(self):
        while True:
            byte = self._reader.peek_byte()
            if byte not in _WHITESPACE:
                return byte
            self._reader.read_byte()

    def parse_value(self, depth):
        value = self._parse_value(depth)
        self._check_record_size()
        return value

    def _parse_value(self, depth):
        self._check_record_size()
        if depth > self._max_depth:
            self._error(
                NestingLimitError,
                "nesting depth %d exceeds limit of %d"
                % (depth, self._max_depth),
            )
        byte = self.skip_whitespace()
        if byte < 0:
            self._truncation_error(
                "unexpected end of stream while expecting a value"
            )
        if byte == 0x7B:  # {
            return self._parse_object(depth)
        if byte == 0x5B:  # [
            return self._parse_array(depth)
        if byte == 0x22:  # "
            return self._parse_string()
        if byte == 0x74 or byte == 0x66 or byte == 0x6E:  # t/f/n
            return self._parse_literal(byte)
        if byte == 0x2D or 0x30 <= byte <= 0x39:  # - or digit
            return self._parse_number()
        self._error(
            InvalidTokenError,
            "unexpected token byte 0x%02X" % byte,
        )

    def _parse_literal(self, first):
        for token, value in _LITERALS.items():
            if token[0] != first:
                continue
            self._reader.read_byte()  # consume the first byte
            for expected in token[1:]:
                byte = self._reader.read_byte()
                if byte < 0:
                    self._truncation_error(
                        "unexpected end of stream inside literal %r"
                        % token.decode("ascii")
                    )
                if byte != expected:
                    self._error(
                        InvalidTokenError,
                        "invalid literal; expected %r"
                        % token.decode("ascii"),
                        -1,
                    )
            return value
        self._error(InvalidTokenError, "invalid literal")

    def _parse_number(self):
        is_integer = True
        raw = bytearray()

        def next_byte():
            self._check_record_size()
            byte = self._reader.read_byte()
            if byte < 0:
                self._truncation_error(
                    "unexpected end of stream inside number"
                )
            raw.append(byte)
            return byte

        first = next_byte()
        negative = first == 0x2D  # '-'
        if negative:
            first = next_byte()
        if not 0x30 <= first <= 0x39:
            self._error(
                InvalidTokenError,
                "number must start with a digit",
                -1,
            )
        if first == 0x30:  # leading zero: no more integer digits
            pass
        else:
            while True:
                digit = self._reader.peek_byte()
                if not 0x30 <= digit <= 0x39:
                    break
                raw.append(self._reader.read_byte())

        if self._reader.peek_byte() == 0x2E:  # '.'
            is_integer = False
            raw.append(self._reader.read_byte())
            fraction = next_byte()
            if not 0x30 <= fraction <= 0x39:
                self._error(
                    InvalidTokenError,
                    "decimal point must be followed by a digit",
                    -1,
                )
            while 0x30 <= self._reader.peek_byte() <= 0x39:
                raw.append(self._reader.read_byte())

        if self._reader.peek_byte() in (0x65, 0x45):  # e/E
            is_integer = False
            raw.append(self._reader.read_byte())
            sign = self._reader.peek_byte()
            if sign == 0x2B or sign == 0x2D:
                raw.append(self._reader.read_byte())
            exponent = next_byte()
            if not 0x30 <= exponent <= 0x39:
                self._error(
                    InvalidTokenError,
                    "exponent must contain a digit",
                    -1,
                )
            while 0x30 <= self._reader.peek_byte() <= 0x39:
                raw.append(self._reader.read_byte())

        self._check_record_size()
        if not is_integer:
            return float(bytes(raw))

        digits = raw.decode("ascii")
        value = int(digits)
        if value > self._max_integer or value < self._min_integer:
            # Preserve the exact spelling, including a leading minus sign.
            return digits
        return value

    def _parse_string(self):
        # Consume the opening quote.
        self._reader.read_byte()
        chunks = []
        buffer = self._reader

        while True:
            self._check_record_size()
            char = buffer.read_char()
            if char < 0:
                self._truncation_error(
                    "unexpected end of stream inside string"
                )
            if char == 0x22:  # closing quote
                return "".join(chunks)
            if char < 0x20:
                self._error(
                    InvalidTokenError,
                    "unescaped control character U+%04X in string" % char,
                    -1,
                )
            if char != 0x5C:  # backslash
                chunks.append(chr(char))
                continue

            escape = buffer.read_byte()
            if escape < 0:
                self._truncation_error(
                    "unexpected end of stream after escape backslash"
                )
            if escape == 0x75:  # \uXXXX
                codepoint = self._read_hex4()
                if 0xD800 <= codepoint <= 0xDBFF:
                    slash = buffer.read_byte()
                    u_mark = buffer.read_byte()
                    if slash < 0 or u_mark < 0:
                        self._truncation_error(
                            "unexpected end of stream in surrogate pair"
                        )
                    if slash != 0x5C or u_mark != 0x75:
                        self._error(
                            InvalidTokenError,
                            "high surrogate must be followed by a low surrogate",
                            -2,
                        )
                    low = self._read_hex4()
                    if not 0xDC00 <= low <= 0xDFFF:
                        self._error(
                            InvalidTokenError,
                            "expected low surrogate, got U+%04X" % low,
                            -6,
                        )
                    codepoint = (
                        0x10000
                        + ((codepoint - 0xD800) << 10)
                        + (low - 0xDC00)
                    )
                elif 0xDC00 <= codepoint <= 0xDFFF:
                    self._error(
                        InvalidTokenError,
                        "lone low surrogate U+%04X" % codepoint,
                        -6,
                    )
                chunks.append(chr(codepoint))
                continue

            mapped = _SIMPLE_ESCAPES.get(escape)
            if mapped is None:
                self._error(
                    InvalidTokenError,
                    "invalid escape sequence \\%s" % chr(escape),
                    -1,
                )
            chunks.append(chr(mapped))

    def _read_hex4(self):
        codepoint = 0
        for _ in range(4):
            byte = self._reader.read_byte()
            if byte < 0:
                self._truncation_error(
                    "unexpected end of stream in \\u escape"
                )
            if 0x30 <= byte <= 0x39:
                nibble = byte - 0x30
            elif 0x41 <= byte <= 0x46:
                nibble = byte - 0x41 + 10
            elif 0x61 <= byte <= 0x66:
                nibble = byte - 0x61 + 10
            else:
                self._error(
                    InvalidTokenError,
                    "invalid hex digit in \\u escape",
                    -1,
                )
            codepoint = (codepoint << 4) | nibble
        return codepoint

    def _parse_object(self, depth):
        # ``depth`` counts how many containers wrap this value.  The outer
        # array of an ``array`` layout is consumed by the driver and does
        # not count toward a record's depth.
        if depth > self._max_depth:
            self._error(
                NestingLimitError,
                "nesting depth %d exceeds limit of %d"
                % (depth, self._max_depth),
            )
        self._reader.read_byte()  # '{'
        result = {}
        byte = self.skip_whitespace()
        if byte == 0x7D:  # empty object
            self._reader.read_byte()
            return result

        while True:
            self._check_record_size()
            byte = self.skip_whitespace()
            if byte != 0x22:
                if byte < 0:
                    self._truncation_error(
                        "unexpected end of stream inside object"
                    )
                self._error(
                    InvalidTokenError,
                    "object key must be a string",
                )
            key = self._parse_string()
            if key in result:
                self._error(
                    DuplicateKeyError,
                    "duplicate object key %r" % key,
                )
            byte = self.skip_whitespace()
            if byte != 0x3A:  # ':'
                if byte < 0:
                    self._truncation_error(
                        "unexpected end of stream; expected ':'"
                    )
                self._error(InvalidTokenError, "expected ':' after object key")
            self._reader.read_byte()
            result[key] = self.parse_value(depth + 1)

            byte = self.skip_whitespace()
            if byte == 0x7D:  # '}'
                self._reader.read_byte()
                return result
            if byte != 0x2C:  # ','
                if byte < 0:
                    self._truncation_error(
                        "unexpected end of stream inside object"
                    )
                self._error(
                    InvalidTokenError,
                    "expected ',' or '}' in object",
                )
            self._reader.read_byte()

    def _parse_array(self, depth):
        if depth > self._max_depth:
            self._error(
                NestingLimitError,
                "nesting depth %d exceeds limit of %d"
                % (depth, self._max_depth),
            )
        self._reader.read_byte()  # '['
        result = []
        byte = self.skip_whitespace()
        if byte == 0x5D:  # empty array
            self._reader.read_byte()
            return result

        while True:
            self._check_record_size()
            result.append(self.parse_value(depth + 1))
            byte = self.skip_whitespace()
            if byte == 0x5D:  # ']'
                self._reader.read_byte()
                return result
            if byte != 0x2C:  # ','
                if byte < 0:
                    self._truncation_error(
                        "unexpected end of stream inside array"
                    )
                self._error(
                    InvalidTokenError,
                    "expected ',' or ']' in array",
                )
            self._reader.read_byte()
