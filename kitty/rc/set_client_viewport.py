#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

from typing import TYPE_CHECKING

from .base import (
    MATCH_WINDOW_OPTION,
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
    from kitty.cli_stub import SetClientViewportRCOptions as CLIOptions


class SetClientViewport(RemoteCommand):
    protocol_spec = __doc__ = """
    match/str: Which OS Window to apply the metrics to
    self/bool: Boolean indicating whether to use the window the command is run in
    cell_width/int: Width of a cell in pixels
    cell_height/int: Height of a cell in pixels
    width/int: Width of the viewport in pixels
    height/int: Height of the viewport in pixels
    dpi_x/float: Horizontal logical DPI
    dpi_y/float: Vertical logical DPI
    """

    short_desc = 'Report the attached client display metrics to a server'
    desc = (
        'Tell a kitty running in server mode how the attached client presents its windows.'
        ' A server has no display and no fonts of its own, so it cannot know how large a cell'
        ' is. The client measures a cell with its own fonts and reports it here, together with'
        ' the size of the area it has available.\n\nThe server uses the two to lay out its'
        ' windows, resize the grid and notify the programs running in it. This is the handshake'
        ' a client performs when it attaches.'
    )
    options_spec = (
        MATCH_WINDOW_OPTION
        + """\n
--cell-width
default=0
type=int
Width of a single cell in pixels, as measured by the client. Zero leaves it unchanged.


--cell-height
default=0
type=int
Height of a single cell in pixels, as measured by the client. Zero leaves it unchanged.


--width
default=0
type=int
Width of the client viewport in pixels. Zero leaves it unchanged.


--height
default=0
type=int
Height of the client viewport in pixels. Zero leaves it unchanged.


--dpi-x
default=0
type=float
Horizontal logical DPI of the client display. Zero leaves it unchanged.


--dpi-y
default=0
type=float
Vertical logical DPI of the client display. Zero leaves it unchanged.


--self
type=bool-set
Apply to the OS Window this command is run in, rather than the active one.
"""
    )

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        return {
            'match': opts.match,
            'self': opts.self,
            'cell_width': opts.cell_width,
            'cell_height': opts.cell_height,
            'width': opts.width,
            'height': opts.height,
            'dpi_x': opts.dpi_x,
            'dpi_y': opts.dpi_y,
        }

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        from kitty.attach import ProtocolError, apply_client_viewport
        from kitty.constants import is_server_mode

        if not is_server_mode():
            raise RemoteControlErrorWithoutTraceback(
                'This kitty is not running in server mode, so it renders with its own fonts and derives the cell size from them'
            )
        windows = self.windows_for_match_payload(boss, window, payload_get)
        for os_window_id in {w.os_window_id for w in windows if w}:
            try:
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
        return None


set_client_viewport = SetClientViewport()
