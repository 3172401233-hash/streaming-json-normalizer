"""Byte-oriented stream reader with incremental UTF-8 decoding.

The reader owns a growing-then-compacted byte buffer.  Every position it
reports is an absolute byte offset over the concatenated input chunks, so
errors can be located even after earlier bytes were released.
"""

from .errors import EncodingError, TruncatedInputError


class ByteStream:
    def __init__(self, read_chunk):
        self._read_chunk = read_chunk
        self._buffer = bytearray()
        # Offset of _buffer[0] within the full concatenated stream.
        self._base = 0
        # Line/column of _buffer[0] within the full concatenated stream.
        self._base_line = 1
        self._base_column = 1
        self._view_start = 0
        # True only once the source returned an empty chunk on the latest
        # refill attempt; reset whenever a non-empty chunk arrives.
        self._eof = False

    @property
    def eof(self):
        return self._eof

    def absolute_offset(self, view_index=0):
        return self._base + self._view_start + view_index

    def _compact(self):
        if self._view_start:
            for byte in self._buffer[: self._view_start]:
                if byte == 0x0A:
                    self._base_line += 1
                    self._base_column = 1
                else:
                    self._base_column += 1
            del self._buffer[: self._view_start]
            self._base += self._view_start
            self._view_start = 0

    def ensure(self, count):
        """Make at least ``count`` bytes available, refilling as needed.

        Empty chunks are transparently skipped.  The result is ``False``
        only when the buffer cannot be filled because the source is
        currently exhausted.
        """
        while len(self._buffer) - self._view_start < count:
            if not self._refill_one():
                return False
        return len(self._buffer) - self._view_start >= count

    def _refill_one(self):
        """Pull one non-empty chunk, skipping any empty chunks.

        Returns ``False`` only when the underlying source is exhausted.
        """
        self._compact()
        while True:
            chunk = self._read_chunk()
            if not chunk:
                self._eof = True
                return False
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if chunk:
                self._buffer.extend(chunk)
                self._eof = False
                return True

    def read_byte(self):
        if self.buffered_bytes() == 0:
            if not self._refill_one():
                return -1
        value = self._buffer[self._view_start]
        self._view_start += 1
        return value

    def peek_byte(self):
        if self.buffered_bytes() == 0:
            if not self._refill_one():
                return -1
        return self._buffer[self._view_start]

    def read_char(self):
        """Read one decoded Unicode character.

        Returns ``-1`` at clean EOF.  A multi-byte sequence split across
        chunks is assembled here, so callers never see partial characters.
        """
        first = self.read_byte()
        if first < 0:
            return -1
        if first < 0x80:
            return first

        if first < 0xC2:
            # Continuation byte (0x80-0xBF) or overlong 2-byte start
            # (0xC0/0xC1) can never open a valid sequence.
            raise EncodingError(
                "invalid UTF-8 lead byte 0x%02X" % first,
                *self._position(-1),
            )

        if first < 0xE0:
            length, min_codepoint = 2, 0x80
        elif first < 0xF0:
            length, min_codepoint = 3, 0x800
        elif first < 0xF5:
            length, min_codepoint = 4, 0x10000
        else:
            raise EncodingError(
                "invalid UTF-8 lead byte 0x%02X" % first,
                *self._position(-1),
            )

        start = self.absolute_offset(-1)
        if not self.ensure(length - 1):
            eof = self.absolute_offset() + self.buffered_bytes()
            line, column = self._line_column(eof)
            raise TruncatedInputError(
                "truncated UTF-8 sequence at end of stream",
                eof,
                line,
                column,
            )

        lead_mask = {2: 0x1F, 3: 0x0F, 4: 0x07}[length]
        codepoint = first & lead_mask
        for index in range(1, length):
            byte = self._buffer[self._view_start]
            if byte < 0x80 or byte > 0xBF:
                raise EncodingError(
                    "invalid UTF-8 continuation byte 0x%02X" % byte,
                    *self._position(0),
                )
            codepoint = (codepoint << 6) | (byte & 0x3F)
            self._view_start += 1

        if codepoint < min_codepoint:
            raise EncodingError(
                "overlong UTF-8 encoding of U+%04X" % codepoint,
                *self._position(-length),
            )
        if 0xD800 <= codepoint <= 0xDFFF:
            raise EncodingError(
                "UTF-8 encodes surrogate U+%04X" % codepoint,
                *self._position(-length),
            )
        if codepoint > 0x10FFFF:
            raise EncodingError(
                "UTF-8 codepoint U+%X exceeds U+10FFFF" % codepoint,
                *self._position(-length),
            )
        return codepoint

    def _position(self, relative):
        offset = self.absolute_offset(relative)
        return (offset,) + self._line_column(offset)

    def _line_column(self, offset):
        line = self._base_line
        column = self._base_column
        start = self._view_start + (offset - self.absolute_offset())
        for byte in self._buffer[:start]:
            if byte == 0x0A:
                line += 1
                column = 1
            else:
                column += 1
        return line, column

    def release_consumed(self):
        """Drop every byte the parser has already consumed."""
        self._compact()

    def buffered_bytes(self):
        return len(self._buffer) - self._view_start
