/*
 * cell-stream.c
 * Copyright (C) 2025 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#include "cell-stream.h"

#define LENGTH_PREFIX 4u

static PyObject *
zlib_error(const char *op, int ret, const char *msg) {
    PyErr_Format(PyExc_ValueError, "%s failed with zlib error %d: %s", op, ret, msg ? msg : "(none)");
    return NULL;
}

static bool
deflate_into(CellStreamWriter *self, const uint8_t *data, size_t sz, int flush) {
    self->z.next_in = (Bytef *)data;
    self->z.avail_in = (uInt)sz;
    do {
        // deflateBound is for a whole stream, so grow by a chunk and go round
        // again instead. Z_SYNC_FLUSH output is small in steady state.
        if (!cell_wire_buf_reserve(&self->out, 4096)) return false;
        self->z.next_out = self->out.data + self->out.used;
        self->z.avail_out = (uInt)(self->out.capacity - self->out.used);
        const uInt before = self->z.avail_out;
        const int ret = deflate(&self->z, flush);
        if (ret != Z_OK && ret != Z_BUF_ERROR) {
            zlib_error("deflate", ret, self->z.msg);
            return false;
        }
        self->out.used += before - self->z.avail_out;
    } while (self->z.avail_in > 0 || self->z.avail_out == 0);
    return true;
}

bool
cell_stream_write(CellStreamWriter *self, const uint8_t *msg, size_t sz) {
    if (sz > CELL_STREAM_MAX_MESSAGE) {
        PyErr_Format(PyExc_ValueError, "Message of %zu bytes is over the limit", sz);
        return false;
    }
    uint8_t prefix[LENGTH_PREFIX] = {sz & 0xff, (sz >> 8) & 0xff, (sz >> 16) & 0xff, (sz >> 24) & 0xff};
    if (!deflate_into(self, prefix, sizeof(prefix), Z_NO_FLUSH)) return false;
    // Z_SYNC_FLUSH makes everything written so far readable, and keeps the
    // dictionary for the next message.
    return deflate_into(self, msg, sz, Z_SYNC_FLUSH);
}

bool
cell_stream_feed(CellStreamReader *self, const uint8_t *data, size_t sz) {
    if (self->consumed) {
        // Reclaim the front of the buffer before it grows.
        memmove(self->pending.data, self->pending.data + self->consumed, self->pending.used - self->consumed);
        self->pending.used -= self->consumed;
        self->consumed = 0;
    }
    self->z.next_in = (Bytef *)data;
    self->z.avail_in = (uInt)sz;
    do {
        if (!cell_wire_buf_reserve(&self->pending, 4096)) return false;
        self->z.next_out = self->pending.data + self->pending.used;
        self->z.avail_out = (uInt)(self->pending.capacity - self->pending.used);
        const uInt before = self->z.avail_out;
        const int ret = inflate(&self->z, Z_SYNC_FLUSH);
        if (ret != Z_OK && ret != Z_BUF_ERROR && ret != Z_STREAM_END) {
            zlib_error("inflate", ret, self->z.msg);
            return false;
        }
        self->pending.used += before - self->z.avail_out;
        if (ret == Z_STREAM_END) break;
    } while (self->z.avail_in > 0 || self->z.avail_out == 0);
    return true;
}

bool
cell_stream_next(CellStreamReader *self, const uint8_t **msg, size_t *sz) {
    const size_t available = self->pending.used - self->consumed;
    if (available < LENGTH_PREFIX) return false;
    const uint8_t *p = self->pending.data + self->consumed;
    const size_t len = (size_t)p[0] | ((size_t)p[1] << 8) | ((size_t)p[2] << 16) | ((size_t)p[3] << 24);
    if (len > CELL_STREAM_MAX_MESSAGE) {
        PyErr_Format(PyExc_ValueError, "Message of %zu bytes is over the limit", len);
        return false;
    }
    if (available - LENGTH_PREFIX < len) return false;
    *msg = p + LENGTH_PREFIX;
    *sz = len;
    self->consumed += LENGTH_PREFIX + len;
    return true;
}

// Python bindings {{{

static PyObject *
new_writer(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    static const char *kwlist[] = {"level", NULL};
    int level = CELL_STREAM_LEVEL;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|i", (char **)kwlist, &level)) return NULL;
    CellStreamWriter *self = (CellStreamWriter *)type->tp_alloc(type, 0);
    if (!self) return NULL;
    const int ret = deflateInit(&self->z, level);
    if (ret != Z_OK) {
        Py_DECREF(self);
        return zlib_error("deflateInit", ret, NULL);
    }
    self->inited = true;
    return (PyObject *)self;
}

static void
writer_dealloc(CellStreamWriter *self) {
    if (self->inited) deflateEnd(&self->z);
    cell_wire_buf_free(&self->out);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
writer_write(CellStreamWriter *self, PyObject *arg) {
#define write_doc "write(msg) -> the compressed bytes for this message"
    if (!PyBytes_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "Expected a bytes object");
        return NULL;
    }
    self->out.used = 0;
    if (!cell_stream_write(self, (const uint8_t *)PyBytes_AS_STRING(arg), (size_t)PyBytes_GET_SIZE(arg))) return NULL;
    return PyBytes_FromStringAndSize((const char *)self->out.data, (Py_ssize_t)self->out.used);
}

static PyMethodDef writer_methods[] = {
    {"write", (PyCFunction)writer_write, METH_O, write_doc},
    {NULL},
};

PyTypeObject CellStreamWriter_Type = {
    PyVarObject_HEAD_INIT(NULL, 0).tp_name = "fast_data_types.CellStreamWriter",
    .tp_basicsize = sizeof(CellStreamWriter),
    .tp_dealloc = (destructor)writer_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "The sending half of one long lived compressed stream",
    .tp_methods = writer_methods,
    .tp_new = new_writer,
};

static PyObject *
new_reader(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    static const char *kwlist[] = {NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "", (char **)kwlist)) return NULL;
    CellStreamReader *self = (CellStreamReader *)type->tp_alloc(type, 0);
    if (!self) return NULL;
    const int ret = inflateInit(&self->z);
    if (ret != Z_OK) {
        Py_DECREF(self);
        return zlib_error("inflateInit", ret, NULL);
    }
    self->inited = true;
    return (PyObject *)self;
}

static void
reader_dealloc(CellStreamReader *self) {
    if (self->inited) inflateEnd(&self->z);
    cell_wire_buf_free(&self->pending);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
reader_feed(CellStreamReader *self, PyObject *arg) {
#define feed_doc "feed(data) -> the messages this data completes"
    if (!PyBytes_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "Expected a bytes object");
        return NULL;
    }
    if (!cell_stream_feed(self, (const uint8_t *)PyBytes_AS_STRING(arg), (size_t)PyBytes_GET_SIZE(arg))) return NULL;
    RAII_PyObject(ans, PyList_New(0));
    if (!ans) return NULL;
    const uint8_t *msg;
    size_t sz;
    while (cell_stream_next(self, &msg, &sz)) {
        RAII_PyObject(item, PyBytes_FromStringAndSize((const char *)msg, (Py_ssize_t)sz));
        if (!item || PyList_Append(ans, item) != 0) return NULL;
    }
    if (PyErr_Occurred()) return NULL;
    return PyList_AsTuple(ans);
}

static PyMethodDef reader_methods[] = {
    {"feed", (PyCFunction)reader_feed, METH_O, feed_doc},
    {NULL},
};

PyTypeObject CellStreamReader_Type = {
    PyVarObject_HEAD_INIT(NULL, 0).tp_name = "fast_data_types.CellStreamReader",
    .tp_basicsize = sizeof(CellStreamReader),
    .tp_dealloc = (destructor)reader_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "The receiving half of one long lived compressed stream",
    .tp_methods = reader_methods,
    .tp_new = new_reader,
};

INIT_TYPE(CellStreamWriter)
INIT_TYPE(CellStreamReader)
// }}}
