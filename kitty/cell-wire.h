/*
 * cell-wire.h
 * Copyright (C) 2025 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "screen.h"

// The cell wire format carries authoritative screen state from a server to a
// client. It ships cells, not sprites and not escape codes, so the client
// shapes and rasterizes with its own fonts. See SERVER-MODE.md section 4.
//
// Two indices in a cell are per-Screen and unstable, because both pools
// garbage collect and renumber. Neither goes on the wire:
//
//   - ch_or_idx with ch_is_idx set indexes the TextCache. The codepoints go in
//     a per-line side table instead. The reader interns them in its own cache.
//   - hyperlink_id indexes the hyperlink pool. Version 1 sends zero. The pool
//     needs its own transfer, which the attach protocol adds later.
//
// sprite_idx is not sent either. The reader derives it when it shapes.

#define CELL_WIRE_MAGIC 0x4c45434bu // "KCEL" little-endian
#define CELL_WIRE_VERSION 1u

// Header flags. CELL_WIRE_SNAPSHOT says the payload holds every line. A caller
// that asks for a delta still gets one when lines have moved without becoming
// dirty.
#define CELL_WIRE_SNAPSHOT 1u

typedef struct CellWireBuf {
    uint8_t *data;
    size_t used, capacity;
} CellWireBuf;

void cell_wire_buf_free(CellWireBuf *buf);

// Serialize the screen's linebuf into buf. A snapshot holds every line. A
// delta holds only the dirty lines, and marks them clean.
bool cell_wire_serialize(Screen *screen, bool snapshot, CellWireBuf *buf);

// Apply a serialized update to the screen. The screen must have the geometry
// the payload names.
bool cell_wire_apply(Screen *screen, const uint8_t *data, size_t sz);

PyObject *screen_serialize_cells(Screen *screen, PyObject *args);
PyObject *screen_apply_serialized_cells(Screen *screen, PyObject *arg);
