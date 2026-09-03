/*
 * line-buf.h
 * Copyright (C) 2024 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "line.h"
#include "text-cache.h"

typedef struct {
    PyObject_HEAD

        GPUCell *gpu_cell_buf;
    CPUCell *cpu_cell_buf;
    index_type xnum, ynum, *line_map, *scratch;
    LineAttrs *line_attrs;
    Line *line;
    TextCache *text_cache;
    // Set when the whole buffer stops describing what a consumer last saw, so
    // that nothing short of a full resend will do. Switching between the main
    // and the alternate buffer does it. Scrolling does not: line_map records
    // where the lines went, and prev_line_map recovers the move.
    bool content_moved;
    // line_map as of the last serialization, and whether it holds one. The
    // difference between the two maps is the permutation the lines went
    // through, which a consumer can repeat instead of taking the lines again.
    index_type *prev_line_map;
    bool prev_line_map_valid;
    // Working room for that permutation. Sized with the buffer so that
    // serializing allocates nothing.
    index_type *perm;
    LineAttrs *attrs_scratch;
} LineBuf;


LineBuf *alloc_linebuf(unsigned int, unsigned int, TextCache *);
