#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

from kitty.attach import (
    SERVER_PROTOCOL_VERSION,
    Attachments,
    ProtocolError,
    check_protocol_version,
    negotiate_compression,
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
