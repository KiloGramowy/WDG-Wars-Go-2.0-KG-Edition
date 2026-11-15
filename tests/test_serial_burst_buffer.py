"""Burst/fragment RX must retain complete records and an untouched tail."""
import random
import unittest

from watchdogs.serial_manager import SerialLineBuffer


class SerialBurstBufferTests(unittest.TestCase):
    def test_large_burst_preserves_order_and_partial_next_record(self):
        lines = [f'@SOLO {{"seq":{seq},"name":"Żółw 🐷"}}' for seq in range(4096)]
        buffer = SerialLineBuffer()
        payload = ("\r\n".join(lines) + "\r\npartial 🐷").encode("utf-8")
        self.assertEqual(buffer.feed(payload), lines)
        self.assertEqual(buffer.feed(b" tail\r\n"), ["partial 🐷 tail"])
        self.assertEqual(buffer.feed(b""), [])

    def test_arbitrary_byte_boundaries_keep_utf8_and_empty_line_behavior(self):
        text = "  first  \r\n\nŻółw 🐷\n\t\r\nlast\ntrailing"
        raw = text.encode("utf-8")
        for seed in range(20):
            with self.subTest(seed=seed):
                randomizer = random.Random(seed)
                buffer = SerialLineBuffer()
                output, offset = [], 0
                while offset < len(raw):
                    count = randomizer.randrange(1, 9)
                    output.extend(buffer.feed(raw[offset:offset + count]))
                    offset += count
                self.assertEqual(output, ["first", "Żółw 🐷", "last"])
                self.assertEqual(buffer.feed(b"\n"), ["trailing"])

    def test_invalid_utf8_replacement_does_not_discard_following_record(self):
        buffer = SerialLineBuffer()
        self.assertEqual(buffer.feed(b"bad\xff\nnext\n"), ["bad\ufffd", "next"])


if __name__ == "__main__":
    unittest.main()
