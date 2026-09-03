#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

import json
import struct

from kitty.attach import (
    CHANNEL_PREAMBLE,
    MSG_CELLS,
    MSG_EVENT,
    SERVER_PROTOCOL_VERSION,
    Attachments,
    ProtocolError,
    check_protocol_version,
    event_message,
    frame_message,
    negotiate_compression,
    parse_preamble,
)

from .base import BaseTest


class TestAttach(BaseTest):
    def test_protocol_versions_name_the_side_to_update(self):
        check_protocol_version(SERVER_PROTOCOL_VERSION)
        with self.assertRaises(ProtocolError) as cm:
            check_protocol_version(SERVER_PROTOCOL_VERSION - 1)
        self.assertIn('Update the client', str(cm.exception))
        with self.assertRaises(ProtocolError) as cm:
            check_protocol_version(SERVER_PROTOCOL_VERSION + 1)
        self.assertIn('Update the server', str(cm.exception))
        for bad in (None, 'one', 1.0):
            with self.assertRaises(ProtocolError):
                check_protocol_version(bad)

    def test_compression_negotiation(self):
        self.ae(negotiate_compression(['zlib']), 'zlib')
        self.ae(negotiate_compression(['none']), 'none')
        self.ae(negotiate_compression('zlib'), 'zlib')
        # The server picks the best method it knows of the ones offered, so the
        # order the client lists them in does not matter.
        self.ae(negotiate_compression(['none', 'zlib']), 'zlib')
        self.ae(negotiate_compression(['zlib', 'none']), 'zlib')
        # Silence is not a request for a raw stream.
        self.ae(negotiate_compression([]), 'zlib')
        with self.assertRaises(ProtocolError) as cm:
            negotiate_compression(['brotli'])
        self.assertIn('brotli', str(cm.exception))
        self.assertIn('zlib', str(cm.exception))

    def test_attaching_supersedes_the_incumbent(self):
        a = Attachments()
        first, kicked = a.attach(peer_id=7, client='laptop', compression='zlib')
        self.ae((first.id, kicked), (1, 0))
        self.ae(a.get(1), first)

        second, kicked = a.attach(peer_id=8, client='desktop', compression='none')
        self.ae((second.id, kicked), (2, 1))
        self.ae(a.get(2), second)
        self.ae(second.client, 'desktop')

        # The kicked client is told why, rather than just failing to be found.
        with self.assertRaises(ProtocolError) as cm:
            a.get(1)
        self.assertIn('Another client attached', str(cm.exception))
        with self.assertRaises(ProtocolError) as cm:
            a.get(99)
        self.assertIn('No attachment', str(cm.exception))

    def test_channel_preamble(self):
        self.ae(parse_preamble(CHANNEL_PREAMBLE + b'7\n'), 7)
        self.ae(parse_preamble(CHANNEL_PREAMBLE + b'7\nleftovers'), 7)
        for bad in (b'GET / HTTP/1.1\r\n', CHANNEL_PREAMBLE + b'x\n'):
            with self.assertRaises(ProtocolError):
                parse_preamble(bad)

    def test_message_envelopes(self):
        # The cell format says nothing about which window a frame is for, so
        # the envelope does.
        m = frame_message(9, b'payload')
        self.ae(m[0], MSG_CELLS)
        self.ae(struct.unpack_from('<I', m, 1)[0], 9)
        self.ae(m[5:], b'payload')
        e = event_message({'event': 'superseded'})
        self.ae(e[0], MSG_EVENT)
        self.ae(json.loads(e[1:]), {'event': 'superseded'})

    def test_a_channel_round_trips_its_own_messages(self):
        from kitty.fast_data_types import CellStreamReader

        a = Attachments()
        attachment, _ = a.attach(peer_id=3, client='laptop', compression='zlib')
        channel = a.open_channel(attachment.id, 3)
        reader = CellStreamReader()
        sent = [frame_message(1, b'first'), event_message({'event': 'os_windows'}), frame_message(2, b'second')]
        wire = b''.join(channel.encode(m) for m in sent)
        # Feed it in awkward pieces: a socket splits where it likes.
        got = []
        for i in range(0, len(wire), 3):
            got.extend(bytes(x) for x in reader.feed(wire[i : i + 3]))
        self.ae(got, sent)

        # Opening a channel for a superseded attachment is refused.
        a.attach(peer_id=4, client='desktop', compression='zlib')
        with self.assertRaises(ProtocolError):
            a.open_channel(attachment.id, 3)
