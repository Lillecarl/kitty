#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

import struct

from kitty.fast_data_types import CellStreamReader, CellStreamWriter

from .base import BaseTest, parse_bytes

# Layout constants from kitty/cell-wire.c. An update with no records is exactly
# a header.
HEADER_SIZE = 19
CELL_SIZE = 24


def visual_state(data):
    """The visual state byte out of a payload header."""
    return struct.unpack_from('<B', data, 4 + 1 + 1 + 2 + 2 + 2 + 2)[0]


def side_table_entries(data):
    """Walk a payload and return its (y, x, codepoints) side table entries."""
    magic, version, flags, columns, lines, cx, cy, visual, records = struct.unpack_from('<IBBHHHHBI', data)
    assert magic == 0x4C45434B and version == 2
    ans, off = [], HEADER_SIZE
    for _ in range(records):
        y = struct.unpack_from('<H', data, off)[0]
        off += 3 + columns * CELL_SIZE
        count = struct.unpack_from('<H', data, off)[0]
        off += 2
        for _ in range(count):
            x, num = struct.unpack_from('<HB', data, off)
            off += 3
            ans.append((y, x, struct.unpack_from(f'<{num}I', data, off)))
            off += 4 * num
    assert off == len(data), f'{off} bytes walked, payload is {len(data)}'
    return ans


class TestCellWire(BaseTest):
    def compare_screens(self, src, dest):
        self.ae(src.columns, dest.columns)
        self.ae(src.lines, dest.lines)
        for y in range(src.lines):
            a = [dict(c) for c in src.cpu_cells(y)]
            b = [dict(c) for c in dest.cpu_cells(y)]
            # Version 1 does not carry hyperlink ids, because the pool
            # renumbers them and the client has no pool.
            for cells in (a, b):
                for c in cells:
                    c.pop('hyperlink')
            self.assertEqual(a, b, f'cpu cells differ on line {y}')
            self.assertEqual(src.line(y).as_ansi(), dest.line(y).as_ansi(), f'styles differ on line {y}')
        self.ae((src.cursor.x, src.cursor.y), (dest.cursor.x, dest.cursor.y))

    def roundtrip(self, cols=20, lines=6, feed=()):
        src = self.create_screen(cols=cols, lines=lines)
        dest = self.create_screen(cols=cols, lines=lines)
        for data in feed:
            parse_bytes(src, data.encode())
        dest.apply_serialized_cells(src.serialize_cells(True))
        self.compare_screens(src, dest)
        return src, dest

    def test_snapshot_roundtrip(self):
        self.roundtrip(feed=('hello\r\nworld',))

    def test_styles_survive(self):
        src, dest = self.roundtrip(
            feed=(
                '\x1b[1;3;4;31;48;5;22mstyled\x1b[m plain\r\n',
                '\x1b[38;2;10;20;30mtruecolor\x1b[m\r\n',
                '\x1b[9;53mstrike overline\x1b[m',
            )
        )
        self.assertIn('\x1b[', src.line(0).as_ansi())

    def test_multi_codepoint_cells_use_the_side_table(self):
        # These cells hold a TextCache index, which is per screen and unstable.
        # The codepoints travel in the side table instead.
        src, dest = self.roundtrip(
            feed=(
                'áễ \U0001f469‍\U0001f4bb x\r\n',
                'ココニチハ wide',
            )
        )
        self.assertIn('\U0001f469‍\U0001f4bb', str(dest.line(0)))
        # Each side gets to number its own cache.
        self.assertGreater(dest.text_cache_count(), 0)

    def test_hyperlinks_do_not_break_the_wire(self):
        src, dest = self.roundtrip(feed=('\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\ after',))
        self.assertIsNone(dest.cpu_cells(0, 0)['hyperlink'])

    def test_delta_carries_only_dirty_lines(self):
        src = self.create_screen(cols=20, lines=6)
        dest = self.create_screen(cols=20, lines=6)
        parse_bytes(src, b'first\r\nsecond\r\nthird')
        dest.apply_serialized_cells(src.serialize_cells(True))
        self.compare_screens(src, dest)

        # Nothing changed, so the next delta is a bare header.
        self.ae(len(src.serialize_cells()), HEADER_SIZE)

        parse_bytes(src, b'\r\n\x1b[32mfourth')
        delta = src.serialize_cells()
        self.assertGreater(len(delta), HEADER_SIZE)
        dest.apply_serialized_cells(delta)
        self.compare_screens(src, dest)

        # The serializer marks the lines it sent clean, so a repeat is empty.
        self.ae(len(src.serialize_cells()), HEADER_SIZE)

    def test_visual_state_travels_with_the_frame(self):
        # A curses application hides the cursor, redraws, then shows it again.
        # So cursor visibility is per frame state, like the cursor position,
        # and it travels in the header rather than beside it.
        src = self.create_screen(cols=20, lines=6)
        dest = self.create_screen(cols=20, lines=6)
        self.assertTrue(src.cursor_visible)

        # Hide the cursor, make it a blinking beam, invert the screen.
        parse_bytes(src, b'\x1b[?25l\x1b[5 q\x1b[?5h')
        payload = src.serialize_cells(True)
        dest.apply_serialized_cells(payload)
        self.assertFalse(dest.cursor_visible)
        self.ae(dest.cursor.shape, src.cursor.shape)
        self.ae(dest.cursor.blink, src.cursor.blink)
        # Reverse video has no Python getter, so assert the bit directly. A
        # comparison alone would pass if the sender dropped it consistently.
        self.assertTrue(visual_state(payload) & 0x04)
        # And compare the whole byte, which covers every bit in both directions.
        self.ae(visual_state(dest.serialize_cells(True)), visual_state(payload))

        # And back again, on a delta rather than a snapshot.
        parse_bytes(src, b'\x1b[?25h\x1b[2 q\x1b[?5l')
        payload = src.serialize_cells()
        dest.apply_serialized_cells(payload)
        self.assertFalse(visual_state(payload) & 0x04)
        self.assertTrue(dest.cursor_visible)
        self.ae(dest.cursor.shape, src.cursor.shape)
        self.ae(dest.cursor.blink, src.cursor.blink)
        self.ae(visual_state(dest.serialize_cells(True)), visual_state(payload))

    def test_scrolling_content_reaches_the_client(self):
        # Lines that scroll up keep their attributes, so they arrive clean at
        # their new position. Dirtiness alone would leave the client stale.
        src = self.create_screen(cols=20, lines=6)
        dest = self.create_screen(cols=20, lines=6)
        parse_bytes(src, '\r\n'.join(f'line {i}' for i in range(6)).encode())
        dest.apply_serialized_cells(src.serialize_cells(True))
        self.compare_screens(src, dest)

        parse_bytes(src, b'\r\nseventh')
        dest.apply_serialized_cells(src.serialize_cells())
        self.compare_screens(src, dest)

    def test_moved_lines_reach_the_client(self):
        # Insert, delete and the alternate screen all move lines without
        # dirtying them.
        src = self.create_screen(cols=20, lines=6)
        dest = self.create_screen(cols=20, lines=6)
        parse_bytes(src, '\r\n'.join(f'line {i}' for i in range(6)).encode())
        dest.apply_serialized_cells(src.serialize_cells(True))
        self.compare_screens(src, dest)

        for data in (b'\x1b[H\x1b[2L', b'\x1b[H\x1b[1M', b'\x1b[?1049h', b'\x1b[?1049l'):
            parse_bytes(src, data)
            dest.apply_serialized_cells(src.serialize_cells())
            self.compare_screens(src, dest)

    def test_pending_wrap_cursor_survives(self):
        # A full line leaves the cursor one past the last column, waiting for
        # the wrap the next character triggers.
        src = self.create_screen(cols=20, lines=6)
        dest = self.create_screen(cols=20, lines=6)
        parse_bytes(src, b'x' * 20)
        self.ae(src.cursor.x, 20)
        dest.apply_serialized_cells(src.serialize_cells(True))
        self.compare_screens(src, dest)

    def test_geometry_mismatch_is_refused(self):
        src = self.create_screen(cols=20, lines=6)
        dest = self.create_screen(cols=10, lines=6)
        with self.assertRaises(ValueError):
            dest.apply_serialized_cells(src.serialize_cells(True))

    def test_bad_payloads_are_refused(self):
        dest = self.create_screen(cols=20, lines=6)
        with self.assertRaises(ValueError):
            dest.apply_serialized_cells(b'not a payload at all')
        with self.assertRaises(ValueError):
            dest.apply_serialized_cells(b'')
        src = self.create_screen(cols=20, lines=6)
        parse_bytes(src, b'hello')
        data = src.serialize_cells(True)
        with self.assertRaises(ValueError):
            dest.apply_serialized_cells(data[:-4])


class TestCellStream(BaseTest):
    def test_messages_round_trip(self):
        w, r = CellStreamWriter(), CellStreamReader()
        msgs = [b'', b'a', b'hello world', bytes(range(256)) * 40]
        for m in msgs:
            self.ae(r.feed(w.write(m)), (m,))

    def test_framing_survives_any_chunking(self):
        # A socket hands over whatever it has, so a message boundary and a read
        # boundary have nothing to do with each other.
        w, r = CellStreamWriter(), CellStreamReader()
        msgs = [f'message number {i}'.encode() * (i + 1) for i in range(8)]
        data = b''.join(w.write(m) for m in msgs)
        got = []
        for i in range(len(data)):
            got.extend(r.feed(data[i : i + 1]))
        self.ae(got, msgs)

    def test_the_dictionary_is_reused_across_messages(self):
        # One stream for the whole connection is the point: a repeated frame
        # costs almost nothing after the first.
        w = CellStreamWriter()
        payload = b'prompt> some output line\n' * 40
        sizes = [len(w.write(payload)) for _ in range(4)]
        # 1000 bytes of repetitive text costs about 48 the first time and under
        # 20 after that.
        self.assertLess(sizes[1] * 2, sizes[0])
        self.assertLess(sizes[-1], sizes[1] + 1)

    def test_screen_updates_travel_over_the_stream(self):
        src = self.create_screen(cols=80, lines=24)
        dest = self.create_screen(cols=80, lines=24)
        w, r = CellStreamWriter(), CellStreamReader()

        parse_bytes(src, b'\x1b[32mhello\x1b[m from the server')
        snapshot = src.serialize_cells(True)
        for msg in r.feed(w.write(snapshot)):
            dest.apply_serialized_cells(msg)
        self.ae(str(src.line(0)).rstrip(), str(dest.line(0)).rstrip())

        # A blank 80x24 screen is the easiest thing there is to compress.
        self.assertLess(len(w.write(snapshot)) * 100, len(snapshot))

        for i in range(30):
            parse_bytes(src, f'\r\nline {i}'.encode())
            for msg in r.feed(w.write(src.serialize_cells())):
                dest.apply_serialized_cells(msg)
        for y in range(src.lines):
            self.ae(str(src.line(y)), str(dest.line(y)))
