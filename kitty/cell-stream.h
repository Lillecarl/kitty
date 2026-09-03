/*
 * cell-stream.h
 * Copyright (C) 2025 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "cell-wire.h"

#include <zlib.h>

// One deflate stream carries a whole connection, rather than compressing each
// message on its own. Consecutive frames repeat prompts, redraws and the same
// styling over and over, so the dictionary a stream builds is a large win.
// Z_SYNC_FLUSH ends each message, which is what it exists for.
//
// A consequence, recorded in SERVER-MODE.md section 2: a sender cannot drop an
// already compressed frame, because that desynchronizes the reader. Drop
// before compression instead.
//
// Each message carries its length inside the compressed stream, so the length
// compresses along with everything else.

#define CELL_STREAM_LEVEL 1
// A bulk history transfer of a 400 column, 2000 line buffer is about 26 MB.
#define CELL_STREAM_MAX_MESSAGE (64u * 1024u * 1024u)

typedef struct CellStreamWriter {
    PyObject_HEAD

        z_stream z;
    CellWireBuf out;
    bool inited;
} CellStreamWriter;

typedef struct CellStreamReader {
    PyObject_HEAD

        z_stream z;
    CellWireBuf pending;
    size_t consumed;
    bool inited;
} CellStreamReader;

// Compress one message into writer->out, which accumulates until the caller
// drains it.
bool cell_stream_write(CellStreamWriter *self, const uint8_t *msg, size_t sz);

// Decompress data into the reader's pending buffer.
bool cell_stream_feed(CellStreamReader *self, const uint8_t *data, size_t sz);

// Take the next complete message. Returns false with no error set when the
// pending buffer holds no whole message yet. The pointer stays valid until the
// next feed or next call.
bool cell_stream_next(CellStreamReader *self, const uint8_t **msg, size_t *sz);
