/*
 * cell-wire.c
 * Copyright (C) 2025 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#include "cell-wire.h"

#include "lineops.h"

#define HEADER_SIZE CELL_WIRE_HEADER_SIZE
#define CELL_SIZE (6u * 4u)

// Bit layout of the packed CPUCell flags word. 31 bits are in use.
#define F_IS_IDX_SHIFT 0u
#define F_WRAPPED_SHIFT 1u
#define F_MULTICELL_SHIFT 2u
#define F_NATURAL_WIDTH_SHIFT 3u
#define F_SCALE_SHIFT 4u
#define F_SUBSCALE_N_SHIFT 7u
#define F_SUBSCALE_D_SHIFT 11u
#define F_X_SHIFT 15u
#define F_Y_SHIFT 21u
#define F_WIDTH_SHIFT 24u
#define F_VALIGN_SHIFT 27u
#define F_HALIGN_SHIFT 29u

#define MASK(bits) ((1u << (bits)) - 1u)

void
cell_wire_buf_free(CellWireBuf *buf) {
    free(buf->data);
    buf->data = NULL;
    buf->used = 0;
    buf->capacity = 0;
}

bool
cell_wire_buf_reserve(CellWireBuf *buf, size_t extra) {
    if (buf->capacity - buf->used >= extra) return true;
    size_t needed = buf->used + extra;
    size_t cap = buf->capacity ? buf->capacity : 4096;
    while (cap < needed) cap *= 2;
    uint8_t *d = realloc(buf->data, cap);
    if (!d) {
        PyErr_NoMemory();
        return false;
    }
    buf->data = d;
    buf->capacity = cap;
    return true;
}

// All integers go on the wire little-endian, written byte by byte. The C
// bitfield layout of CPUCell is implementation defined, so no struct is ever
// blitted. See SERVER-MODE.md section 3b.
static void
write_u8(CellWireBuf *buf, uint8_t val) {
    buf->data[buf->used++] = val;
}

static void
write_u16(CellWireBuf *buf, uint16_t val) {
    buf->data[buf->used++] = val & 0xff;
    buf->data[buf->used++] = (val >> 8) & 0xff;
}

static void
write_u32(CellWireBuf *buf, uint32_t val) {
    buf->data[buf->used++] = val & 0xff;
    buf->data[buf->used++] = (val >> 8) & 0xff;
    buf->data[buf->used++] = (val >> 16) & 0xff;
    buf->data[buf->used++] = (val >> 24) & 0xff;
}

typedef struct Reader {
    const uint8_t *data;
    size_t pos, sz;
} Reader;

static bool
have(Reader *r, size_t n) {
    if (r->sz - r->pos < n) {
        PyErr_SetString(PyExc_ValueError, "Truncated cell wire payload");
        return false;
    }
    return true;
}

static uint8_t
read_u8(Reader *r) {
    return r->data[r->pos++];
}

static uint16_t
read_u16(Reader *r) {
    uint16_t ans = (uint16_t)r->data[r->pos] | (uint16_t)((uint16_t)r->data[r->pos + 1] << 8);
    r->pos += 2;
    return ans;
}

static uint32_t
read_u32(Reader *r) {
    uint32_t ans =
        (uint32_t)r->data[r->pos] | ((uint32_t)r->data[r->pos + 1] << 8) | ((uint32_t)r->data[r->pos + 2] << 16) | ((uint32_t)r->data[r->pos + 3] << 24);
    r->pos += 4;
    return ans;
}

static uint32_t
pack_flags(const CPUCell *c) {
    uint32_t f = 0;
    if (c->ch_is_idx) f |= 1u << F_IS_IDX_SHIFT;
    if (c->next_char_was_wrapped) f |= 1u << F_WRAPPED_SHIFT;
    if (c->is_multicell) f |= 1u << F_MULTICELL_SHIFT;
    if (c->natural_width) f |= 1u << F_NATURAL_WIDTH_SHIFT;
    f |= (c->scale & MASK(SCALE_BITS)) << F_SCALE_SHIFT;
    f |= (c->subscale_n & MASK(SUBSCALE_BITS)) << F_SUBSCALE_N_SHIFT;
    f |= (c->subscale_d & MASK(SUBSCALE_BITS)) << F_SUBSCALE_D_SHIFT;
    f |= (c->x & MASK(WIDTH_BITS + SCALE_BITS)) << F_X_SHIFT;
    f |= (c->y & MASK(SCALE_BITS)) << F_Y_SHIFT;
    f |= (c->width & MASK(WIDTH_BITS)) << F_WIDTH_SHIFT;
    f |= (c->valign & MASK(VALIGN_BITS)) << F_VALIGN_SHIFT;
    f |= (c->halign & MASK(HALIGN_BITS)) << F_HALIGN_SHIFT;
    return f;
}

static void
unpack_flags(CPUCell *c, uint32_t f) {
    c->next_char_was_wrapped = (f >> F_WRAPPED_SHIFT) & 1u;
    c->is_multicell = (f >> F_MULTICELL_SHIFT) & 1u;
    c->natural_width = (f >> F_NATURAL_WIDTH_SHIFT) & 1u;
    c->scale = (f >> F_SCALE_SHIFT) & MASK(SCALE_BITS);
    c->subscale_n = (f >> F_SUBSCALE_N_SHIFT) & MASK(SUBSCALE_BITS);
    c->subscale_d = (f >> F_SUBSCALE_D_SHIFT) & MASK(SUBSCALE_BITS);
    c->x = (f >> F_X_SHIFT) & MASK(WIDTH_BITS + SCALE_BITS);
    c->y = (f >> F_Y_SHIFT) & MASK(SCALE_BITS);
    c->width = (f >> F_WIDTH_SHIFT) & MASK(WIDTH_BITS);
    c->valign = (f >> F_VALIGN_SHIFT) & MASK(VALIGN_BITS);
    c->halign = (f >> F_HALIGN_SHIFT) & MASK(HALIGN_BITS);
}

static bool
serialize_line(Screen *screen, index_type y, const Line *line, CellWireBuf *buf, ListOfChars *lc) {
    const index_type columns = line->xnum;
    if (!cell_wire_buf_reserve(buf, 2u + 1u + columns * CELL_SIZE + 2u)) return false;
    write_u16(buf, (uint16_t)y);
    LineAttrs attrs = line->attrs;
    attrs.has_dirty_text = false; // dirtiness is server bookkeeping
    write_u8(buf, attrs.val);
    size_t side_table_count = 0;
    const size_t side_count_pos = buf->used + columns * CELL_SIZE;
    for (index_type x = 0; x < columns; x++) {
        const CPUCell *c = line->cpu_cells + x;
        const GPUCell *g = line->gpu_cells + x;
        write_u32(buf, c->ch_is_idx ? 0u : c->ch_or_idx);
        write_u32(buf, pack_flags(c));
        write_u32(buf, g->fg);
        write_u32(buf, g->bg);
        write_u32(buf, g->decoration_fg);
        write_u32(buf, g->attrs.val);
    }
    write_u16(buf, 0); // patched below, once the count is known
    for (index_type x = 0; x < columns; x++) {
        const CPUCell *c = line->cpu_cells + x;
        if (!c->ch_is_idx) continue;
        tc_chars_at_index(screen->text_cache, c->ch_or_idx, lc);
        if (!cell_wire_buf_reserve(buf, 2u + 1u + lc->count * 4u)) return false;
        write_u16(buf, (uint16_t)x);
        write_u8(buf, (uint8_t)lc->count);
        for (size_t i = 0; i < lc->count; i++) write_u32(buf, lc->chars[i]);
        side_table_count++;
    }
    buf->data[side_count_pos] = side_table_count & 0xff;
    buf->data[side_count_pos + 1] = (side_table_count >> 8) & 0xff;
    return true;
}

bool
cell_wire_serialize(Screen *screen, bool snapshot, CellWireBuf *buf) {
    // Line attributes travel with their lines, so a line that scrolls up
    // arrives clean at its new position. Dirtiness alone would leave the
    // reader stale, so a buffer that moved sends everything. Consecutive
    // frames of scrolling output repeat almost verbatim, which is what the
    // stream compressor is for.
    const bool all_lines = snapshot || screen->linebuf->content_moved;
    buf->used = 0;
    if (!cell_wire_buf_reserve(buf, HEADER_SIZE)) return false;
    write_u32(buf, CELL_WIRE_MAGIC);
    write_u8(buf, CELL_WIRE_VERSION);
    write_u8(buf, all_lines ? CELL_WIRE_SNAPSHOT : 0u);
    write_u16(buf, (uint16_t)screen->columns);
    write_u16(buf, (uint16_t)screen->lines);
    write_u16(buf, (uint16_t)screen->cursor->x);
    write_u16(buf, (uint16_t)screen->cursor->y);
    const size_t record_count_pos = buf->used;
    write_u32(buf, 0); // patched below
    RAII_ListOfChars(lc);
    uint32_t records = 0;
    Line line = {.text_cache = screen->text_cache};
    for (index_type y = 0; y < screen->lines; y++) {
        linebuf_init_line_at(screen->linebuf, y, &line);
        if (!all_lines && !line.attrs.has_dirty_text) continue;
        if (!serialize_line(screen, y, &line, buf, &lc)) return false;
        linebuf_mark_line_clean(screen->linebuf, y);
        records++;
    }
    // Nothing else consumes dirtiness in server mode, because render() does
    // not run. So the serializer owns the reset, or every update resends the
    // whole screen.
    screen->is_dirty = false;
    screen->history_line_added_count = 0;
    screen->linebuf->content_moved = false;
    buf->data[record_count_pos] = records & 0xff;
    buf->data[record_count_pos + 1] = (records >> 8) & 0xff;
    buf->data[record_count_pos + 2] = (records >> 16) & 0xff;
    buf->data[record_count_pos + 3] = (records >> 24) & 0xff;
    return true;
}

bool
cell_wire_apply(Screen *screen, const uint8_t *data, size_t sz) {
    Reader r = {.data = data, .sz = sz};
    if (!have(&r, HEADER_SIZE)) return false;
    if (read_u32(&r) != CELL_WIRE_MAGIC) {
        PyErr_SetString(PyExc_ValueError, "Not a cell wire payload");
        return false;
    }
    const uint8_t version = read_u8(&r);
    if (version != CELL_WIRE_VERSION) {
        PyErr_Format(PyExc_ValueError, "Unsupported cell wire version: %u", version);
        return false;
    }
    read_u8(&r); // flags: a delta and a snapshot apply the same way
    const uint16_t columns = read_u16(&r), lines = read_u16(&r);
    if (columns != screen->columns || lines != screen->lines) {
        PyErr_Format(PyExc_ValueError, "Payload is for a %ux%u screen, this screen is %ux%u", columns, lines, screen->columns, screen->lines);
        return false;
    }
    const uint16_t cursor_x = read_u16(&r), cursor_y = read_u16(&r);
    const uint32_t records = read_u32(&r);
    RAII_ListOfChars(lc);
    Line line = {.text_cache = screen->text_cache};
    for (uint32_t rec = 0; rec < records; rec++) {
        if (!have(&r, 2u + 1u)) return false;
        const uint16_t y = read_u16(&r);
        const LineAttrs attrs = {.val = read_u8(&r)};
        if (y >= screen->lines) {
            PyErr_Format(PyExc_ValueError, "Line %u is out of range", y);
            return false;
        }
        if (!have(&r, columns * CELL_SIZE + 2u)) return false;
        linebuf_init_line_at(screen->linebuf, y, &line);
        for (index_type x = 0; x < columns; x++) {
            CPUCell *c = line.cpu_cells + x;
            GPUCell *g = line.gpu_cells + x;
            zero_at_ptr(c);
            zero_at_ptr(g);
            const uint32_t ch = read_u32(&r);
            const uint32_t flags = read_u32(&r);
            unpack_flags(c, flags);
            c->ch_or_idx = ch;
            c->ch_is_idx = false;
            g->fg = read_u32(&r);
            g->bg = read_u32(&r);
            g->decoration_fg = read_u32(&r);
            g->attrs.val = read_u32(&r);
        }
        const uint16_t side_count = read_u16(&r);
        for (uint16_t i = 0; i < side_count; i++) {
            if (!have(&r, 2u + 1u)) return false;
            const uint16_t x = read_u16(&r);
            const uint8_t num = read_u8(&r);
            if (x >= columns || num > MAX_NUM_CODEPOINTS_PER_CELL) {
                PyErr_SetString(PyExc_ValueError, "Invalid side table entry");
                return false;
            }
            if (!have(&r, num * 4u)) return false;
            ensure_space_for_chars(&lc, num);
            lc.count = num;
            for (uint8_t j = 0; j < num; j++) lc.chars[j] = read_u32(&r);
            // Intern into this screen's cache, which numbers independently of
            // the sender's.
            CPUCell *c = line.cpu_cells + x;
            c->ch_or_idx = tc_get_or_insert_chars(screen->text_cache, &lc);
            c->ch_is_idx = true;
        }
        screen->linebuf->line_attrs[y] = attrs;
        linebuf_mark_line_dirty(screen->linebuf, y);
    }
    // x may be one past the last column: that is the pending wrap state, which
    // holds after a line fills and before the next character wraps it.
    screen->cursor->x = MIN(cursor_x, screen->columns);
    screen->cursor->y = MIN(cursor_y, screen->lines - 1);
    screen->is_dirty = true;
    return true;
}

PyObject *
screen_serialize_cells(Screen *screen, PyObject *args) {
    int snapshot = 0;
    if (!PyArg_ParseTuple(args, "|p", &snapshot)) return NULL;
    CellWireBuf buf = {0};
    if (!cell_wire_serialize(screen, (bool)snapshot, &buf)) {
        cell_wire_buf_free(&buf);
        return NULL;
    }
    PyObject *ans = PyBytes_FromStringAndSize((const char *)buf.data, (Py_ssize_t)buf.used);
    cell_wire_buf_free(&buf);
    return ans;
}

PyObject *
screen_apply_serialized_cells(Screen *screen, PyObject *arg) {
    if (!PyBytes_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "Expected a bytes object");
        return NULL;
    }
    if (!cell_wire_apply(screen, (const uint8_t *)PyBytes_AS_STRING(arg), (size_t)PyBytes_GET_SIZE(arg))) return NULL;
    Py_RETURN_NONE;
}
