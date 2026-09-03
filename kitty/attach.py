#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

"""Client attachment state for a kitty running in server mode.

A server owns the terminals; a client owns the display. The client attaches
over the remote control socket, which SSH is expected to carry, and the two
agree on versions and compression before any cell data moves.
"""

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

    def attach(self, peer_id: int, client: str, compression: str) -> tuple[Attachment, int]:
        """Take over the session. Returns the new attachment and who it kicked."""
        kicked = self.current.id if self.current is not None else 0
        self.counter += 1
        self.current = Attachment(self.counter, peer_id, client, compression)
        return self.current, kicked

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
