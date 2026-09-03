#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

import struct

from .base import BaseTest, parse_bytes

# Layout constants from kitty/cell-wire.c. An update with no records is exactly
# a header.
HEADER_SIZE = 18
CELL_SIZE = 24


def side_table_entries(data):
    """Walk a payload and return its (y, x, codepoints) side table entries."""
    magic, version, flags, columns, lines, cx, cy, records = struct.unpack_from('<IBBHHHHI', data)
    assert magic == 0x4C45434B and version == 1
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
