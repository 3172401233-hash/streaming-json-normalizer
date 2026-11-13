import io
import json
import unittest

from streaming_json_normalizer import (
    DuplicateKeyError,
    EncodingError,
    InvalidTokenError,
    NestingLimitError,
    RecordTooLargeError,
    StreamParseError,
    TruncatedInputError,
    compact_json,
    iter_compact_json,
    iter_records,
)


def chunked(data, size):
    """Split bytes into fixed-size chunks, including size=1."""
    return (data[i : i + size] for i in range(0, len(data), size))


def collect(source, **kwargs):
    return list(iter_records(source, **kwargs))


class ByteChunkTests(unittest.TestCase):
    def test_record_split_across_every_chunk_size(self):
        records = [
            {"id": 1, "name": "alice", "tags": ["a", "b"], "ok": True},
            {"id": 2, "name": "bob", "tags": [], "ok": None, "ratio": 1.5},
        ]
        payload = json.dumps(records, ensure_ascii=False).encode("utf-8")
        for size in range(1, 8):
            with self.subTest(chunk_size=size):
                self.assertEqual(
                    collect(iter(chunked(payload, size))), records
                )

    def test_file_like_source(self):
        payload = b'[{"a": 1}, {"a": 2}]'
        self.assertEqual(collect(io.BytesIO(payload)), [{"a": 1}, {"a": 2}])

    def test_multibyte_utf8_split_at_every_byte(self):
        text = "漢字🪐é—\u00e9"
        record = {"msg": text, "nested": [text, {"k": text}]}
        payload = json.dumps([record], ensure_ascii=False).encode("utf-8")
        for size in range(1, 7):
            with self.subTest(chunk_size=size):
                result = collect(iter(chunked(payload, size)))
                self.assertEqual(result, [record])

    def test_escapes_split_across_chunks(self):
        record = {
            "quote": 'he said "hi"\nnext\tend',
            "backslash": "a\\b",
            "unicode": "snowman \u2603 and rocket \U0001f680",
        }
        payload = json.dumps([record], ensure_ascii=True).encode("ascii")
        for size in range(1, 9):
            with self.subTest(chunk_size=size):
                self.assertEqual(
                    collect(iter(chunked(payload, size))), [record]
                )

    def test_surrogate_pair_via_unicode_escapes(self):
        payload = b'["\\uD83D\\uDE80"]'
        self.assertEqual(collect(iter(chunked(payload, 1))), ["\U0001F680"])

    def test_lone_surrogates_are_rejected(self):
        # High surrogate without a following low surrogate.
        payload = b'["\\uD83D"]'
        with self.assertRaises(InvalidTokenError):
            collect(iter(chunked(payload, 1)))
        # Lone low surrogate.
        payload = b'["\\uDE80"]'
        with self.assertRaises(InvalidTokenError):
            collect(iter(chunked(payload, 1)))
        # High surrogate followed by something that is not a surrogate pair.
        payload = b'["\\uD83D\\u0041"]'
        with self.assertRaises(InvalidTokenError):
            collect(iter(chunked(payload, 1)))


class LayoutTests(unittest.TestCase):
    def test_top_level_array_auto_detected(self):
        payload = b' [ {"a": 1} , {"b": 2} ] '
        self.assertEqual(
            collect(iter(chunked(payload, 2))), [{"a": 1}, {"b": 2}]
        )

    def test_jsonl_sequence_auto_detected(self):
        payload = b'{"a": 1}\n{"b": 2}\r\n  {"c": 3}'
        self.assertEqual(
            collect(iter(chunked(payload, 3))),
            [{"a": 1}, {"b": 2}, {"c": 3}],
        )

    def test_explicit_array_layout_rejects_object_stream(self):
        with self.assertRaises(InvalidTokenError):
            collect(iter([b'{"a": 1}']), layout="array")

    def test_explicit_sequence_layout(self):
        payload = b'{"a": 1}{"a": 2}'
        self.assertEqual(
            collect(iter(chunked(payload, 2)), layout="sequence"),
            [{"a": 1}, {"a": 2}],
        )

    def test_empty_array_yields_no_records(self):
        self.assertEqual(collect(iter([b"[  ]"])), [])

    def test_empty_stream_is_an_error(self):
        with self.assertRaises(TruncatedInputError):
            collect(iter([b"   "]))

    def test_trailing_data_rejected(self):
        with self.assertRaises(InvalidTokenError):
            collect(iter([b'{"a": 1} garbage']))
        with self.assertRaises(InvalidTokenError):
            collect(iter([b'[{"a": 1}] garbage']))


class NumberTests(unittest.TestCase):
    def test_safe_integers_become_int(self):
        boundary = 2 ** 53
        payload = (
            b"[%d, %d, %d, %d, 0, -0, 123]"
            % (
                boundary - 1,
                boundary,
                -boundary,
                -boundary - 1,
            )
        )
        result = collect(iter(chunked(payload, 2)))
        values = result
        self.assertEqual(
            values,
            [
                boundary - 1,
                str(boundary),
                str(-boundary),
                str(-boundary - 1),
                0,
                0,
                123,
            ],
        )
        self.assertIsInstance(values[0], int)
        self.assertIsInstance(values[1], str)
        self.assertIsInstance(values[2], str)
        self.assertIsInstance(values[3], str)

    def test_huge_integer_preserves_exact_spelling(self):
        huge = "123456789012345678901234567890"
        payload = ('[{"n": %s}]' % huge).encode()
        result = collect(iter(chunked(payload, 1)))
        self.assertEqual(result, [{"n": huge}])

    def test_floats_and_exponents_are_validated(self):
        payload = b'[{"a": 1.25, "b": -0.5, "c": 1e3, "d": 1.2E-4}]'
        result = collect(iter(chunked(payload, 3)))[0]
        self.assertEqual(result, {"a": 1.25, "b": -0.5, "c": 1000.0, "d": 1.2e-4})

    def test_malformed_numbers_rejected(self):
        bad_payloads = [
            b"[01]",
            b"[1.]",
            b"[1e]",
            b"[1e+]",
            b"[-x]",
            b"[1.2.3]",
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(StreamParseError):
                    collect(iter(chunked(payload, 1)))


class DuplicateKeyTests(unittest.TestCase):
    def test_duplicate_key_raises(self):
        payload = b'[{"a": 1, "a": 2}]'
        with self.assertRaises(DuplicateKeyError) as ctx:
            collect(iter(chunked(payload, 2)))
        self.assertIn("a", str(ctx.exception))

    def test_duplicate_key_nested(self):
        payload = b'[{"outer": {"k": 1, "k": 2}}]'
        with self.assertRaises(DuplicateKeyError):
            collect(iter(chunked(payload, 1)))


class LimitTests(unittest.TestCase):
    def test_nesting_depth_limit(self):
        payload = b"[" + b"[" * 6 + b"1" + b"]" * 7
        with self.assertRaises(NestingLimitError) as ctx:
            collect(iter(chunked(payload, 2)), max_depth=5)
        self.assertGreaterEqual(ctx.exception.offset, 0)

    def test_nesting_within_limit_ok(self):
        payload = b'[[{"a": [1, 2]}]]'
        result = collect(iter([payload]), max_depth=5)
        self.assertEqual(result, [[{"a": [1, 2]}]])

    def test_object_depth_counts(self):
        payload = b'[{"a": {"b": {"c": 1}}}]'
        with self.assertRaises(NestingLimitError):
            collect(iter([payload]), max_depth=2)

    def test_record_too_large(self):
        record = b'{"pad": "%s"}' % (b"x" * 64)
        with self.assertRaises(RecordTooLargeError) as ctx:
            collect(iter(chunked(record, 7)), max_record_bytes=32)
        self.assertGreaterEqual(ctx.exception.offset, 0)

    def test_small_records_in_one_large_buffer_not_flagged(self):
        payload = b"[" + b",".join(b'{"i":%d}' % i for i in range(50)) + b"]"
        result = collect(io.BytesIO(payload), max_record_bytes=16)
        self.assertEqual(len(result), 50)

    def test_limit_boundary_inclusive(self):
        record = b'{"a": 1}'
        self.assertEqual(
            collect(io.BytesIO(b"[" + record + b"]"), max_record_bytes=len(record)),
            [{"a": 1}],
        )
        with self.assertRaises(RecordTooLargeError):
            collect(
                io.BytesIO(b"[" + record + b"]"),
                max_record_bytes=len(record) - 1,
            )

    def test_sequence_layout_counts_top_level_container_depth(self):
        with self.assertRaises(NestingLimitError):
            collect(
                io.BytesIO(b"[[1]]"),
                layout="sequence",
                max_depth=1,
            )


class StreamRobustnessTests(unittest.TestCase):
    def test_empty_chunks_are_skipped(self):
        source = iter(
            [b'[{"a":', b"", b"1},", b"", b'{"b": 2}]', b""]
        )
        self.assertEqual(collect(source), [{"a": 1}, {"b": 2}])

    def test_string_chunks_accepted(self):
        source = iter(['[{"a": 1', '}, {"b": 2}]'])
        self.assertEqual(collect(source), [{"a": 1}, {"b": 2}])

    def test_generator_terminates_after_error(self):
        good = b'{"a": 1}\n'
        bad = b'{"a": broken}\n'
        trailing = b'{"a": 9}\n'
        gen = iter_records(iter(chunked(good + bad + trailing, 4)))
        self.assertEqual(next(gen), {"a": 1})
        with self.assertRaises(StreamParseError):
            next(gen)
        # The generator is exhausted; no later record can surface.
        with self.assertRaises(StopIteration):
            next(gen)


class ErrorReportingTests(unittest.TestCase):
    def test_truncated_input_reports_position(self):
        payload = b'[{"a": "unterminated'
        with self.assertRaises(TruncatedInputError) as ctx:
            collect(iter(chunked(payload, 3)))
        # EOF is reported at the absolute end of the stream.
        self.assertEqual(ctx.exception.offset, len(payload))

    def test_truncated_multibyte_character(self):
        # U+4E2D in UTF-8 is E4 B8 AD; cut after the first two bytes.
        payload = b'["\xe4\xb8'
        with self.assertRaises(TruncatedInputError) as ctx:
            collect(iter([payload]))
        self.assertEqual(ctx.exception.offset, len(payload))

    def test_invalid_utf8_reports_position(self):
        payload = b'["ok\xff"]'
        with self.assertRaises(EncodingError) as ctx:
            collect(iter(chunked(payload, 1)))
        self.assertEqual(ctx.exception.offset, payload.index(b"\xff"))

    def test_utf8_encoded_surrogate_rejected(self):
        # ED A0 80 is the UTF-8 encoding of U+D800 (forbidden).
        payload = b'["\xed\xa0\x80"]'
        with self.assertRaises(EncodingError):
            collect(iter(chunked(payload, 1)))

    def test_illegal_token_reports_absolute_offset(self):
        payload = b'[{"a": 1}, {"b": ?}]'
        with self.assertRaises(InvalidTokenError) as ctx:
            collect(iter(chunked(payload, 4)))
        self.assertEqual(ctx.exception.offset, payload.index(b"?"))

    def test_line_and_column_tracking(self):
        payload = b'{\n  "a": 1,\n  "b": bad\n}'
        with self.assertRaises(InvalidTokenError) as ctx:
            collect(iter(chunked(payload, 3)), layout="sequence")
        self.assertEqual(ctx.exception.line, 3)
        self.assertEqual(ctx.exception.column, 8)

    def test_no_records_emitted_after_error(self):
        good_before = b'{"a": 1}\n{"a": 2}\n'
        bad = b'{"a": broken}\n{"a": 3}\n'
        gen = iter_records(iter(chunked(good_before + bad, 5)))
        self.assertEqual(next(gen), {"a": 1})
        self.assertEqual(next(gen), {"a": 2})
        with self.assertRaises(StreamParseError):
            next(gen)
        with self.assertRaises(StopIteration):
            next(gen)

    def test_unexpected_end_inside_array(self):
        with self.assertRaises(TruncatedInputError):
            collect(iter([b'[{"a": 1}, {"a":']))

    def test_unescaped_control_character_rejected(self):
        with self.assertRaises(InvalidTokenError):
            collect(iter([b'["line\nbreak"]']))


class NormalizationTests(unittest.TestCase):
    def test_compact_json_stable_key_order(self):
        record = {"z": 1, "a": 2, "m": [1, 2]}
        self.assertEqual(
            compact_json(record), '{"z":1,"a":2,"m":[1,2]}'
        )

    def test_compact_output_roundtrip(self):
        payload = (
            b'[{"z": 1, "a": {"y": 2, "b": 3}}, '
            b'{"big": 9007199254740993}]'
        )
        lines = list(iter_compact_json(iter(chunked(payload, 5))))
        self.assertEqual(
            lines,
            [
                '{"z":1,"a":{"y":2,"b":3}}',
                '{"big":"9007199254740993"}',
            ],
        )
        for line in lines:
            json.loads(line)

    def test_non_ascii_emitted_directly(self):
        self.assertEqual(compact_json({"k": "漢"}), '{"k":"漢"}')


if __name__ == "__main__":
    unittest.main()
