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
    // Set when lines change position without becoming dirty. Line attributes
    // move with their lines, so a scrolled line arrives clean at its new y.
    // A consumer that ships only dirty lines must send everything instead.
    bool content_moved;
} LineBuf;


LineBuf *alloc_linebuf(unsigned int, unsigned int, TextCache *);
