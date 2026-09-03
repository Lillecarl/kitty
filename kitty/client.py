#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

"""The display half of a kitty running in server mode.

The server holds the terminals, the programs and the scrollback. This kitty
holds the display: it shapes text with its own fonts, draws with its own GPU,
and resolves its own keybindings. It owns none of the state it shows.

A client is an ordinary kitty in every other way. Its windows carry real
Screens, and the normal render pass draws them. Only two things differ: nothing
is spawned, so a window has no child, and the cells arrive from a socket
instead of from a parser.
"""

import json
import socket
import struct
from typing import TYPE_CHECKING, Any

from .attach import CHANNEL_PREAMBLE, MSG_CELLS, MSG_EVENT, SERVER_PROTOCOL_VERSION, SUPPORTED_COMPRESSION, ProtocolError
from .constants import appname
from .fast_data_types import CellStreamReader, CellStreamWriter
from .utils import log_error
from .window import Window

if TYPE_CHECKING:
    from .boss import Boss

RC_PREFIX = b'\x1bP@kitty-cmd'
RC_TERMINATOR = b'\x1b\\'
# Long enough that a busy server still answers, short enough that a wrong
# address fails while the user is still looking at the terminal.
HANDSHAKE_TIMEOUT = 10.0


class StubChild:
    """Stands in for the process a client window does not have.

    The programs run on the server. This window shows one and owns none of it,
    so every answer here is empty rather than wrong. A caller that wants the
    real thing has to ask the server.
    """

    def __init__(self, title: str = appname) -> None:
        self.argv = [title]
        self.cmdline = [title]
        self.cwd = ''
        self.current_cwd = ''
        self.foreground_cwd = ''
        self.environ: dict[str, str] = {}
        self.final_env: dict[str, str] = {}
        self.foreground_environ: dict[str, str] = {}
        self.foreground_cmdline = [title]
        self.foreground_processes: list[Any] = []
        self.pid: int | None = None
        self.pid_for_cwd: int | None = None
        self.is_remote = True

    def mark_terminal_ready(self) -> None:
        pass

    def reset_termios_state(self) -> None:
        pass

    def send_signal_for_key(self, key_num: bytes) -> bool:
        # The server's terminal owns the line discipline, so it raises signals.
        return False

    def cmdline_of_pid(self, pid: int) -> list[str]:
        return []


def rc_command(name: str, payload: Any = None) -> bytes:
    """One remote control command, framed the way a socket peer expects it."""
    from .remote_control import create_basic_command, encode_send

    return encode_send(create_basic_command(name, payload))


def read_rc_response(sock: socket.socket) -> dict[str, Any]:
    buf = b''
    while RC_TERMINATOR not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProtocolError('The server closed the connection during the handshake')
        buf += chunk
    body = buf.split(RC_TERMINATOR, 1)[0]
    if not body.startswith(RC_PREFIX):
        raise ProtocolError(f'The server did not answer with a remote control response: {body[:64]!r}')
    ans = json.loads(body[len(RC_PREFIX) :])
    if not isinstance(ans, dict):
        raise ProtocolError('The server sent a malformed response')
    return ans


class ClientWindow(Window):
    """A window that shows a server's screen and owns none of it."""

    server_window_id: int = 0
    client_title: str = ''

    @property
    def title(self) -> str:
        return self.override_title or self.client_title or self.child_title

    def send_key_sequence(self, *keys: Any, synthesize_release_events: bool = True) -> None:
        # Not encoded here. The keyboard protocol flags and the cursor key mode
        # live on the server's Screen, so the server encodes and this side only
        # reports. Releases go too, and usually encode to nothing.
        from .boss import get_boss

        boss = get_boss()
        for key in keys:
            boss.send_key_to_server(key, self)

    def write_to_child(self, data: str | bytes | memoryview) -> None:
        # A paste, a send-text, or anything else that is already text. The
        # program that receives it runs on the server.
        from .attach import text_event
        from .boss import get_boss

        if not data:
            return
        client = get_boss().client
        if client is not None:
            if isinstance(data, (bytes, memoryview)):
                data = bytes(data).decode('utf-8', 'replace')
            client.send(text_event(self.server_window_id, data))

    def resize_child(self, current_pty_size: tuple[int, int, int, int]) -> bool:
        # There is no pty on this side. The server owns it, and hears about a
        # resize as a viewport, in cells it decides for itself.
        self.last_reported_pty_size = current_pty_size
        self.child_is_launched = True
        return False


class Client:
    """One attachment, from the display side.

    Two connections to the same address, as the server expects: a short lived
    one that carries the handshake, and a long lived one that carries the
    frames.
    """

    def __init__(self, boss: 'Boss', address: str) -> None:
        self.boss = boss
        self.address = address
        self.hello: dict[str, Any] = {}
        self.channel: socket.socket | None = None
        self.reader: CellStreamReader | None = None
        self.writer: CellStreamWriter | None = None
        self.pending = b''
        # Server window id to the window showing it.
        self.windows: dict[int, int] = {}
        self.timer_id = 0
        self.viewport: dict[str, int] = {}
        self.os_window_id = 0

    # Connecting {{{

    def connect_socket(self) -> socket.socket:
        if not self.address.startswith('unix:'):
            raise ProtocolError(f'Only unix: addresses are supported, not {self.address}')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(HANDSHAKE_TIMEOUT)
        sock.connect(self.address[len('unix:') :])
        return sock

    def attach(self, metrics: dict[str, int]) -> dict[str, Any]:
        """Say hello, and describe the display the server is drawing for.

        The metrics have to be real, so this runs after the OS window exists
        and the fonts are loaded. The server has no display and lays its
        windows out for whatever it is told here.
        """
        sock = self.connect_socket()
        try:
            payload = {
                'protocol_version': SERVER_PROTOCOL_VERSION,
                'compression': list(SUPPORTED_COMPRESSION),
                'client': f'{appname} {metrics["width"]}x{metrics["height"]}',
                **metrics,
            }
            sock.sendall(rc_command('attach', payload))
            response = read_rc_response(sock)
        finally:
            sock.close()
        if not response.get('ok'):
            raise ProtocolError(str(response.get('error') or 'The server refused the attachment'))
        data = response.get('data')
        self.hello = json.loads(data) if isinstance(data, str) else (data or {})
        return self.hello

    def open_channel(self) -> None:
        """Take the second connection, the one the frames come down."""
        sock = self.connect_socket()
        sock.sendall(CHANNEL_PREAMBLE + b'%d\n' % int(self.hello['attachment_id']))
        buf = b''
        while b'\n' not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProtocolError('The server closed the data channel')
            buf += chunk
        line, rest = buf.split(b'\n', 1)
        if not line.startswith(b'OK'):
            raise ProtocolError(f'The server refused the data channel: {line.decode("utf-8", "replace")}')
        compressed = self.hello.get('compression') == 'zlib'
        self.reader = CellStreamReader() if compressed else None
        self.writer = CellStreamWriter() if compressed else None
        self.pending = rest
        sock.settimeout(0)
        self.channel = sock

    # }}}

    def close(self) -> None:
        if self.channel is not None:
            self.channel.close()
            self.channel = None

    # Windows {{{

    def client_metrics(self, os_window_id: int) -> dict[str, int]:
        """The grid this client can actually draw, in the server's terms."""
        from .fast_data_types import get_os_window_size

        m = get_os_window_size(os_window_id) or {}
        return {
            'cell_width': int(m.get('cell_width', 0)),
            'cell_height': int(m.get('cell_height', 0)),
            'width': int(m.get('width', 0)),
            'height': int(m.get('height', 0)),
            'dpi_x': int(m.get('xdpi', 0)),
            'dpi_y': int(m.get('ydpi', 0)),
        }

    def window_for(self, server_window_id: int) -> 'Window | None':
        """The window showing a server window, made on first sight of it."""
        wid = self.windows.get(server_window_id)
        if wid is not None:
            window = self.boss.window_id_map.get(wid)
            if window is not None:
                return window
            del self.windows[server_window_id]
        tab = self.boss.active_tab
        if tab is None:
            return None
        window = ClientWindow(tab, StubChild(), self.boss.args)
        window.server_window_id = server_window_id
        tab._add_window(window)
        self.boss.add_client_window(window)
        self.windows[server_window_id] = window.id
        return window

    def grid_of(self, payload: bytes) -> tuple[int, int]:
        """The grid a payload describes, read out of its header."""
        return tuple(struct.unpack_from('<HH', payload, 6))  # type: ignore[return-value]

    def note_titles(self, os_windows: Any) -> None:
        for osw in os_windows or ():
            for w in osw.get('windows', ()):
                window = self.boss.window_id_map.get(self.windows.get(w.get('id', 0), 0))
                if window is not None and w.get('title'):
                    window.client_title = str(w['title'])
                    window.title_updated()

    # }}}

    # Taking the frames {{{

    def read(self) -> tuple[bytes, ...]:
        """Whatever the server has said since the last look."""
        if self.channel is None:
            return ()
        chunks = []
        while True:
            try:
                data = self.channel.recv(65536)
            except BlockingIOError:
                break
            except OSError as err:
                log_error(f'The connection to the kitty server failed: {err}')
                self.close()
                return ()
            if not data:
                log_error('The kitty server closed the connection')
                self.close()
                break
            chunks.append(data)
        data = self.pending + b''.join(chunks)
        self.pending = b''
        if not data:
            return ()
        if self.reader is not None:
            return tuple(bytes(x) for x in self.reader.feed(data))
        messages = []
        while len(data) >= 4:
            (length,) = struct.unpack_from('<I', data)
            if len(data) < 4 + length:
                break
            messages.append(data[4 : 4 + length])
            data = data[4 + length :]
        self.pending = data
        return tuple(messages)

    def apply(self, message: bytes) -> None:
        if not message:
            return
        if message[0] == MSG_CELLS:
            server_window_id = struct.unpack_from('<I', message, 1)[0]
            payload = message[5:]
            window = self.window_for(server_window_id)
            if window is None:
                return
            # The payload only fits the grid it was made for. The server lays
            # out for this client, so a mismatch is the moment before that
            # settles, not a disagreement.
            columns, lines = self.grid_of(payload)
            if (window.screen.columns, window.screen.lines) != (columns, lines):
                window.screen.resize(lines, columns)
            try:
                window.screen.apply_serialized_cells(payload)
            except ValueError as err:
                log_error(f'Dropped a frame from the kitty server: {err}')
        elif message[0] == MSG_EVENT:
            self.event(json.loads(message[1:]))

    def event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        name = event.get('event')
        if name == 'os_windows':
            self.note_titles(event.get('os_windows'))
        elif name == 'superseded':
            log_error(f'Another client took this session: {event.get("client")}')
            self.close()

    def send_viewport(self, os_window_id: int) -> None:
        """Tell the server the grid this client can now draw.

        The client owns its window, so a resize here is authoritative. The
        server relayouts for it and the next frames arrive in the new shape.
        """
        metrics = self.client_metrics(os_window_id)
        if not metrics['width'] or not metrics['height']:
            return
        if metrics == self.viewport:
            return
        self.viewport = metrics
        try:
            sock = self.connect_socket()
        except OSError as err:
            log_error(f'Could not tell the kitty server about a resize: {err}')
            return
        try:
            sock.sendall(rc_command('set-client-viewport', {'match': 'all', 'self': False, **metrics}))
            read_rc_response(sock)
        except Exception as err:
            log_error(f'The kitty server refused the new viewport: {err}')
        finally:
            sock.close()

    def send(self, message: bytes) -> None:
        """One event, up the same channel the frames come down."""
        if self.channel is None:
            return
        try:
            self.channel.sendall(bytes(self.writer.write(message)) if self.writer is not None else struct.pack('<I', len(message)) + message)
        except OSError as err:
            log_error(f'Could not send to the kitty server: {err}')
            self.close()

    def pump(self, timer_id: int | None = None) -> None:
        # A resize of this window is the client's to declare, so look before
        # reading. send_viewport does nothing when nothing changed.
        if self.os_window_id:
            self.send_viewport(self.os_window_id)
        for message in self.read():
            self.apply(message)

    # }}}
