#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

from typing import TYPE_CHECKING

from kitty.attach import SERVER_PROTOCOL_VERSION

from .base import (
    ArgsType,
    Boss,
    PayloadGetType,
    PayloadType,
    RCOptions,
    RemoteCommand,
    RemoteControlErrorWithoutTraceback,
    ResponseType,
    Window,
)

if TYPE_CHECKING:
    from kitty.cli_stub import AttachRCOptions as CLIOptions


class Attach(RemoteCommand):
    protocol_spec = __doc__ = """
    protocol_version/int: Version of the kitty server protocol the client speaks
    compression/list.str: Compression methods the client supports, best first
    client/str: Human readable description of the client, for diagnostics
    cell_width/int: Width of a cell in pixels, as the client measures it
    cell_height/int: Height of a cell in pixels, as the client measures it
    width/int: Width of the client viewport in pixels
    height/int: Height of the client viewport in pixels
    dpi_x/float: Horizontal logical DPI
    dpi_y/float: Vertical logical DPI
    """

    short_desc = 'Attach to a kitty running in server mode'
    desc = (
        'Perform the handshake that gives a client the session. The two sides agree on a protocol'
        ' version and a compression method, and the client reports how it presents windows, since'
        ' a server has no display and no fonts of its own.\n\nThe exchange is deliberately'
        ' uncompressed, because it is where compression itself is agreed. Cell data flows'
        ' afterwards.\n\nOne client holds a session at a time. Attaching takes it, and the client'
        ' that held it is told it was superseded rather than simply disconnected.'
    )
    options_spec = f"""\
--protocol-version
default={SERVER_PROTOCOL_VERSION}
type=int
Version of the kitty server protocol the client speaks. Defaults to the version this kitty
was built with, which is what a matching client sends.


--compression
type=list
A compression method the client supports. Repeat the option to offer several; the server
picks the best of them that it knows. Defaults to zlib. Use "none" on its own to ask for an
uncompressed stream.


--client
default=
Human readable description of the client, shown in diagnostics.


--cell-width
default=0
type=int
Width of a single cell in pixels, as measured by the client.


--cell-height
default=0
type=int
Height of a single cell in pixels, as measured by the client.


--width
default=0
type=int
Width of the client viewport in pixels.


--height
default=0
type=int
Height of the client viewport in pixels.


--dpi-x
default=0
type=float
Horizontal logical DPI of the client display.


--dpi-y
default=0
type=float
Vertical logical DPI of the client display.
"""

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        return {
            'protocol_version': opts.protocol_version or SERVER_PROTOCOL_VERSION,
            'compression': list(opts.compression or ('zlib',)),
            'client': opts.client,
            'cell_width': opts.cell_width,
            'cell_height': opts.cell_height,
            'width': opts.width,
            'height': opts.height,
            'dpi_x': opts.dpi_x,
            'dpi_y': opts.dpi_y,
        }

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        from kitty.attach import (
            ProtocolError,
            apply_client_viewport,
            attachments,
            check_protocol_version,
            negotiate_compression,
            os_window_geometry,
        )
        from kitty.constants import is_server_mode, version
        from kitty.fast_data_types import CELL_WIRE_VERSION

        if not is_server_mode():
            raise RemoteControlErrorWithoutTraceback('This kitty is not running in server mode, so it has no session to hand to a client')
        try:
            check_protocol_version(payload_get('protocol_version'))
            compression = negotiate_compression(payload_get('compression'))
            for os_window_id in tuple(boss.os_window_map):
                apply_client_viewport(
                    boss,
                    os_window_id,
                    cell_width=payload_get('cell_width'),
                    cell_height=payload_get('cell_height'),
                    width=payload_get('width'),
                    height=payload_get('height'),
                    dpi_x=payload_get('dpi_x'),
                    dpi_y=payload_get('dpi_y'),
                )
        except ProtocolError as err:
            raise RemoteControlErrorWithoutTraceback(str(err)) from err
        metrics = {k: payload_get(k) for k in ('cell_width', 'cell_height', 'width', 'height', 'dpi_x', 'dpi_y') if payload_get(k)}
        attachment, superseded = attachments.attach(int(payload_get('peer_id') or 0), str(payload_get('client') or ''), compression, metrics)
        return {
            'attachment_id': attachment.id,
            'protocol_version': SERVER_PROTOCOL_VERSION,
            'cell_wire_version': CELL_WIRE_VERSION,
            'compression': compression,
            'kitty_version': list(version),
            'superseded': superseded,
            'os_windows': [os_window_geometry(boss, i) for i in tuple(boss.os_window_map)],
        }


attach = Attach()
