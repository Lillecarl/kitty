#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

"""Client attachment state for a kitty running in server mode.

A server owns the terminals; a client owns the display. The client attaches
over the remote control socket, which SSH is expected to carry, and the two
agree on versions and compression before any cell data moves.
"""

import json
import struct
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from .boss import Boss

# The version of the kitty server protocol as a whole. It moves independently
# of kitty's own version, like RC_ENCRYPTION_PROTOCOL_VERSION does.
SERVER_PROTOCOL_VERSION = 1

# In the order the server prefers them. A client names every method it supports
# and the server picks the best of them that it knows, so adding zstd later
# needs no new handshake and no change on an old client.
SUPPORTED_COMPRESSION = ('zlib', 'none')


class ProtocolError(ValueError):
    pass


class Attachment(NamedTuple):
    id: int
    peer_id: int
    client: str
    compression: str
    # How the client presents a window. An OS window created after the client
    # attached has to be laid out for it too, so the metrics outlive the
    # handshake that carried them.
    metrics: dict[str, float]


class Attachments:
    """Who is attached. One at a time, but the protocol does not assume it.

    Every attachment gets an id, and losing the session is an explicit event
    rather than a closed socket, so serving several read-only clients later
    needs no change to the shape of the protocol.

    An attachment outlives the connection that made it, because the handshake
    runs over a short lived remote control connection. The data channel gives
    the attachment a real lifetime.
    """

    def __init__(self) -> None:
        self.counter = 0
        self.current: Attachment | None = None
        self.channel: 'DataChannel | None' = None

    def attach(self, peer_id: int, client: str, compression: str, metrics: dict[str, float] | None = None) -> tuple[Attachment, int]:
        """Take over the session. Returns the new attachment and who it kicked."""
        kicked = self.current.id if self.current is not None else 0
        if self.channel is not None:
            # Say why, then go. A closed socket cannot explain itself.
            try:
                self.channel.send(event_message({'event': 'superseded', 'client': client}))
            except Exception:
                pass
            self.channel = None
        self.counter += 1
        self.current = Attachment(self.counter, peer_id, client, compression, metrics or {})
        return self.current, kicked

    def open_channel(self, attachment_id: int, peer_id: int) -> 'DataChannel':
        """Give an attachment its data channel. Raises if it was superseded."""
        self.channel = DataChannel(self.get(attachment_id), peer_id)
        return self.channel

    def channel_for_peer(self, peer_id: int) -> 'DataChannel | None':
        if self.channel is not None and self.channel.peer_id == peer_id:
            return self.channel
        return None

    def peer_died(self, peer_id: int) -> None:
        if self.channel is not None and self.channel.peer_id == peer_id:
            self.channel = None

    def get(self, attachment_id: int) -> Attachment:
        """Look up an attachment, saying why it is gone if it is.

        Ids only ever go up, so anything below the counter existed once and was
        superseded. That needs no record of past attachments.
        """
        if self.current is not None and self.current.id == attachment_id:
            return self.current
        if 0 < attachment_id <= self.counter:
            raise ProtocolError('Another client attached to this session')
        raise ProtocolError(f'No attachment with id {attachment_id}')


attachments = Attachments()


def negotiate_compression(client_supports: Any) -> str:
    # A client that names nothing gets zlib. Compression is part of the
    # protocol, not an extra, so silence is not a request for a raw stream.
    # A client that wants one asks for "none".
    if not client_supports:
        return SUPPORTED_COMPRESSION[0]
    if isinstance(client_supports, str):
        client_supports = (client_supports,)
    for method in SUPPORTED_COMPRESSION:
        if method in client_supports:
            return method
    raise ProtocolError(
        'This kitty server supports the compression methods: {}, the client offered: {}'.format(
            ', '.join(SUPPORTED_COMPRESSION), ', '.join(map(str, client_supports))
        )
    )


def check_protocol_version(client_version: Any) -> None:
    if not isinstance(client_version, int):
        raise ProtocolError('The client did not report a protocol version')
    if client_version == SERVER_PROTOCOL_VERSION:
        return
    side = 'client' if client_version < SERVER_PROTOCOL_VERSION else 'server'
    raise ProtocolError(
        f'The client speaks version {client_version} of the kitty server protocol and this server speaks {SERVER_PROTOCOL_VERSION}. Update the {side}.'
    )


def apply_client_viewport(
    boss: 'Boss',
    os_window_id: int,
    cell_width: int = 0,
    cell_height: int = 0,
    width: int = 0,
    height: int = 0,
    dpi_x: float = 0,
    dpi_y: float = 0,
) -> None:
    """Lay a server side OS window out for the display the client actually has.

    Zero means leave unchanged, so a caller can send only what it knows.
    """
    from .fast_data_types import get_os_window_size, set_os_window_cell_size

    if (cell_width and cell_width < 1) or (cell_height and cell_height < 1):
        raise ProtocolError('Cell width and height must be positive')
    metrics = get_os_window_size(os_window_id)
    if metrics is None:
        raise ProtocolError(f'The OS Window {os_window_id} does not exist')
    if cell_width or cell_height:
        set_os_window_cell_size(os_window_id, cell_width or metrics['cell_width'], cell_height or metrics['cell_height'], dpi_x, dpi_y)
    if width or height:
        boss.resize_os_window(os_window_id, width=width, height=height, unit='pixels', incremental=False, metrics=metrics)
    # Relayout unconditionally. A viewport that happens to match the current one
    # produces no resize event, so a cell size change under it would otherwise
    # never reach the layout.
    tm = boss.os_window_map.get(os_window_id)
    if tm is not None:
        tm.resize()


def os_window_geometry(boss: 'Boss', os_window_id: int) -> dict[str, Any]:
    from .fast_data_types import get_os_window_size

    metrics = get_os_window_size(os_window_id) or {}
    tm = boss.os_window_map.get(os_window_id)
    return {
        'id': os_window_id,
        'width': metrics.get('width', 0),
        'height': metrics.get('height', 0),
        'cell_width': metrics.get('cell_width', 0),
        'cell_height': metrics.get('cell_height', 0),
        'windows': [w.id for tab in (tm or ()) for w in tab],
    }


# The data channel {{{

# A client opens its data channel with this line, uncompressed, naming the
# attachment the handshake gave it. Remote control connections open with
# \x1bP@kitty-cmd, so one socket carries both.
CHANNEL_PREAMBLE = b'KITTY-STREAM '

# Message types inside the stream. The wire format itself says nothing about
# which window a frame belongs to, so the envelope does.
MSG_CELLS = 1
MSG_EVENT = 2

# Stop serializing when this much is already queued for the client. Cell
# updates are idempotent, so the dirty state of each screen is the queue: skip
# a tick and the next one sends the coalesced result.
WRITE_HIGH_WATER = 1024 * 1024


def frame_message(window_id: int, payload: bytes) -> bytes:
    return bytes((MSG_CELLS,)) + struct.pack('<I', window_id) + payload


def event_message(event: Any) -> bytes:
    return bytes((MSG_EVENT,)) + json.dumps(event).encode('utf-8')


def parse_preamble(data: bytes) -> int:
    """Read the attachment id out of a data channel preamble."""
    if not data.startswith(CHANNEL_PREAMBLE):
        raise ProtocolError('Not a kitty data channel preamble')
    line = data[len(CHANNEL_PREAMBLE) :].split(b'\n', 1)[0]
    try:
        return int(line)
    except ValueError:
        raise ProtocolError(f'Invalid attachment id: {line!r}') from None


class DataChannel:
    """The frames for one attachment, on their own connection.

    Its writer is a single stream for the life of the connection, so a frame
    can never be dropped after compression without desynchronizing the reader.
    Frames are dropped before that, by not serializing at all.
    """

    def __init__(self, attachment: Attachment, peer_id: int) -> None:
        from .fast_data_types import CellStreamReader, CellStreamWriter

        self.attachment = attachment
        self.peer_id = peer_id
        self.compressed = attachment.compression == 'zlib'
        self.writer = CellStreamWriter() if self.compressed else None
        self.reader = CellStreamReader() if self.compressed else None
        # Windows this client has a full picture of. Anything else gets a
        # snapshot before it gets a delta.
        self.known: set[int] = set()
        # The OS window layout the client has been told about, so a change can
        # be noticed and sent.
        self.geometry: list[dict[str, Any]] = []
        self.os_windows: set[int] = set()

    def encode(self, message: bytes) -> bytes:
        if self.writer is None:
            return struct.pack('<I', len(message)) + message
        return bytes(self.writer.write(message))

    def send(self, message: bytes) -> int:
        from .fast_data_types import push_data_to_peer

        return int(push_data_to_peer(self.peer_id, self.encode(message)))

    def queued(self) -> int:
        from .fast_data_types import push_data_to_peer

        return int(push_data_to_peer(self.peer_id, b''))

    def receive(self, data: bytes) -> tuple[Any, ...]:
        if self.reader is None:
            return ()
        return tuple(self.reader.feed(data))
