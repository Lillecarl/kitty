#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

"""End to end test of a client attached to a kitty running in server mode.

The client here has no display and does not render. It attaches, takes the
frames, and rebuilds the screens, which is the whole of what a real client adds
on top of a renderer.
"""

import json
import os
import socket
import struct
import subprocess
import tempfile
import time

from kitty.attach import CHANNEL_PREAMBLE, MSG_CELLS, MSG_EVENT, key_event, text_event
from kitty.constants import kitty_exe
from kitty.fast_data_types import GLFW_FKEY_ENTER, CellStreamReader, CellStreamWriter, Screen

from .base import BaseTest, Callbacks

CELL_WIDTH, CELL_HEIGHT = 8, 16
VIEWPORT_WIDTH, VIEWPORT_HEIGHT = 800, 480
# Generous: these wait on a real process, a real shell and a real socket.
STARTUP_TIMEOUT = 30
IDLE_TIMEOUT = 10


class FakeClient:
    """The client half of the protocol, with no rendering."""

    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.reader = CellStreamReader()
        self.writer = CellStreamWriter()
        self.screens = {}
        self.events = []
        self.geometry = {}
        self.sock = None

    def rc(self, *args):
        out = subprocess.run([kitty_exe(), '@', '--to', f'unix:{self.sock_path}', *args], capture_output=True, text=True, timeout=STARTUP_TIMEOUT)
        if out.returncode:
            raise AssertionError(f'kitty @ {" ".join(args)} failed: {out.stderr.strip()}')
        return out.stdout.strip()

    def attach(self, client='test'):
        hello = json.loads(
            self.rc(
                'attach',
                '--client',
                client,
                '--cell-width',
                str(CELL_WIDTH),
                '--cell-height',
                str(CELL_HEIGHT),
                '--width',
                str(VIEWPORT_WIDTH),
                '--height',
                str(VIEWPORT_HEIGHT),
            )
        )
        self.note_geometry(hello['os_windows'])
        return hello

    def note_geometry(self, os_windows):
        for w in os_windows:
            self.geometry[w['id']] = w

    def open_channel(self, attachment_id):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(STARTUP_TIMEOUT)
        self.sock.connect(self.sock_path)
        self.sock.sendall(CHANNEL_PREAMBLE + b'%d\n' % attachment_id)
        buf = b''
        while b'\n' not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError('The server closed the data channel')
            buf += chunk
        line, rest = buf.split(b'\n', 1)
        self.pending = rest
        return line

    def send(self, message):
        self.sock.sendall(bytes(self.writer.write(message)))

    def screen_for(self, window_id):
        screen = self.screens.get(window_id)
        if screen is None:
            # A real client would use its own fonts here. Any window will do,
            # because every OS window a client is attached to has its metrics.
            geom = next(iter(self.geometry.values()))
            cols = geom['width'] // geom['cell_width']
            lines = geom['height'] // geom['cell_height']
            c = Callbacks()
            screen = Screen(c, lines, cols, 100, geom['cell_width'], geom['cell_height'], 0, c)
            c.color_profile = screen.color_profile
            self.screens[window_id] = screen
        return screen

    def pump(self, seconds=1.0):
        """Take whatever the server has to say for a while."""
        self.sock.settimeout(0.1)
        end = time.monotonic() + seconds
        frames = 0
        while time.monotonic() < end:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            data, self.pending = self.pending + data, b''
            for message in self.reader.feed(data):
                message = bytes(message)
                if message[0] == MSG_CELLS:
                    window_id = struct.unpack_from('<I', message, 1)[0]
                    self.screen_for(window_id).apply_serialized_cells(message[5:])
                    frames += 1
                elif message[0] == MSG_EVENT:
                    event = json.loads(message[1:])
                    self.events.append(event)
                    if event.get('event') == 'os_windows':
                        self.note_geometry(event['os_windows'])
        return frames

    def wait_for(self, predicate, timeout=IDLE_TIMEOUT):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.3)
            if predicate():
                return True
        return False

    def text(self):
        return '\n'.join(str(s.line(y)) for s in self.screens.values() for y in range(s.lines))

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class TestServerMode(BaseTest):
    def setUp(self):
        super().setUp()
        self.tdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tdir, 'srv')
        self.server = subprocess.Popen(
            [kitty_exe(), '--server', '-o', 'allow_remote_control=yes', '--listen-on', f'unix:{self.sock_path}'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        end = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < end and not os.path.exists(self.sock_path):
            time.sleep(0.05)
        if not os.path.exists(self.sock_path):
            self.server.kill()
            self.skipTest('The kitty server did not create its socket')

    def tearDown(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=STARTUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.server.kill()
        import shutil

        shutil.rmtree(self.tdir, ignore_errors=True)
        super().tearDown()

    def test_a_client_mirrors_the_session(self):
        client = FakeClient(self.sock_path)
        self.addCleanup(client.close)
        hello = client.attach('laptop')
        self.ae(hello['superseded'], 0)
        self.ae(hello['compression'], 'zlib')
        # The client's own metrics decide the grid, since the server has none.
        window = hello['os_windows'][0]
        self.ae((window['cell_width'], window['cell_height']), (CELL_WIDTH, CELL_HEIGHT))

        self.ae(client.open_channel(hello['attachment_id']), b'OK')
        self.assertTrue(client.wait_for(lambda: bool(client.screens)), 'No frames arrived')
        self.ae(next(iter(client.screens.values())).columns, VIEWPORT_WIDTH // CELL_WIDTH)

        marker = 'MIRRORED-BY-THE-CLIENT'
        client.rc('send-text', '--match', 'all', f'printf {marker}\r')
        self.assertTrue(client.wait_for(lambda: marker in client.text()), f'The client never saw {marker}:\n{client.text()}')

        # A window opened after the attach flows without a reattach, and is
        # laid out for the client rather than for a display the server lacks.
        client.rc('launch', '--type', 'os-window')
        self.assertTrue(client.wait_for(lambda: len(client.screens) > 1), 'The second OS Window never arrived')
        self.assertTrue(any(e.get('event') == 'os_windows' for e in client.events), 'No OS Window event arrived')

        # The client types, and the shell it never runs answers. Text goes
        # through as it is; the Enter key does not, because only the server
        # knows how this terminal wants a key encoded.
        # The marker only appears once the shell runs the command, never in the
        # echo of it, so this fails if Enter does not arrive.
        typed = 'TYPED-BY-THE-CLIENT'
        window_id = min(client.screens)
        client.send(text_event(window_id, "printf 'TYPED%sCLIENT' -BY-THE-"))
        client.send(key_event(window_id, GLFW_FKEY_ENTER))
        self.assertTrue(client.wait_for(lambda: typed in client.text()), f'The typing never reached the shell:\n{client.text()}')

        # Another client takes the session. This one is told why, rather than
        # just losing its socket.
        client.events.clear()
        client.rc('attach', '--client', 'desktop', '--cell-width', '10', '--cell-height', '20')
        self.assertTrue(
            client.wait_for(lambda: any(e.get('event') == 'superseded' for e in client.events)),
            f'The superseded event never arrived: {client.events}',
        )
