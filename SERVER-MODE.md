# kitty server mode — design notes

Goal: kitty runs on a GPU-less host, owns the PTYs, the VT parser, all screen
and scrollback state, tabs/windows/layouts, and delegates *only presentation*
to a kitty client that attaches and detaches over a socket. tmux-style
persistence, but the client must be kitty, not an arbitrary terminal.

## Settled decisions

- **Server is GPU-less; the client owns all rendering.**
- **Single client.** No concurrent attachments; attaching kicks the incumbent.
- **The client holds the cell grid *and* scrollback**, so scrolling is local and
  nothing is ever re-encoded to escape codes.
- **Multiple OS windows are supported** — one native client window per
  server-side OS window (§3a).
- **Cross-platform attach is supported** (Linux server ↔ macOS client and the
  reverse), which requires explicit packing of `CPUCell` (§3b).
- **Transport is a unix socket, reached over SSH.** Universally available, and
  it means SSH owns identity and encryption — kitty needs no network listener,
  no TLS and no auth layer of its own. The existing `--listen-on unix:` and peer
  plumbing is the whole transport substrate (§1, §6 step 3).
- **Keybindings resolve client-side** (§3b, §5.4).
- **Compression is zlib**, already a hard dependency of kitty, as one long-lived
  stream per connection (§2).
- **Wire format ships cells, not sprites and not escape codes** (§4).
- **Config authority is split.** The client mandates the front end: fonts,
  keybindings, colors and the rest of presentation. The server keeps what only
  it can own — the PTY, shell integration, `scrollback_lines`, and the layout
  config that §5.1 keeps server-side. §5.2 draws the line.
- **The protocol carries versions and negotiates them** (§5.6). Not lockstep.
- **Upstreaming is a goal.** So the protocol follows kitty's own conventions
  and gets a `docs/*-protocol.rst` of its own, and nothing in it is
  tmux-shaped (§1, §4).

## Progress

**Spike 1 is done.** kitty runs headless, with no display and no GL context.

Verified end to end: it starts with no display, `kitty @ ls` reports a window,
text sent over the socket reaches a real shell and comes back through
`kitty @ get-text`, a second OS window opens, and it exits cleanly with no GLFW
or GLAD complaints. That last point is the one that matters, because it proves
VT parsing produces a correct cell grid with no display anywhere.

Measured while doing it, on a bare `Screen` with rendering excluded: ASCII
37.6 MB/s, Unicode 38.5 MB/s, CSI codes 28.6 MB/s, long escape codes
143.7 MB/s, images 138.1 MB/s. The outlier is unique multi-codepoint cells at
0.7 MB/s, which is the TextCache path from §2 and roughly 50x slower than
normal text. Synthetic, but worth knowing. The server does strictly less CPU
work than a normal kitty, because it drops shaping along with rendering.

Two things learned that the plan did not anticipate:

- **The vendored null GLFW platform had real bitrot.** Nothing has built it
  since kitty's GLFW diverged from upstream, so half a dozen platform
  signatures had moved, the drag-and-drop and clipboard entry points had been
  replaced, and a key-name table still used the pre-rename `GLFW_KEY_`
  constants. Two fixes were needed outside the null files, in `internal.h` and
  `input.c`, on branches no display backend compiles.
- **GL is touched in more places than `render()`.** Skipping `initialize_gpu`
  leaves every GL function pointer NULL, which turns each missed site into an
  exact backtrace. The sites found: per-window and per-tab VAO creation, the
  border VAO, the sprite map's `glGetIntegerv` limit query, prerendered
  box-drawing sprites, and `make_os_window_context_current`. Guarding the VAO
  helpers on a negative index covered a whole class at once, rather than
  guarding each caller.

**The geometry half of spike 2 already works.** Layout runs server-side in
pixels, so an external caller driving `resize-os-window --unit pixels` exercises
the exact chain a client will: viewport pixels reflow the layout, resize the
grid, and deliver `SIGWINCH` to the child. Verified headless at 1200x800,
400x300 and 1920x1080, with the shell's own `stty size` matching the grid
`kitty @ ls` reports each time.

That was mostly free, because §5.1's plan — keep layout on the server and feed
it client-supplied pixels — matches what kitty already does. Only two more GL
sites needed gating: the framebuffer size callback sets the GPU viewport, and
the live resize path sets the swap interval.

**Spike 2 is now complete, and the server opens no font files at all.**

`FONTS_DATA_HANDLE` made that cheap. It is a prefix struct: `FontGroup` starts
with `FONTS_DATA_HEAD` and appends the faces, and every consumer outside the
render path reads only the head, meaning cell metrics, logical DPI and the
nominal font size. So a bare head allocation serves them with no font behind it.
Gating `load_fonts_data` itself, rather than its four callers, means no path can
reach fontconfig, FreeType or HarfBuzz. Each OS window gets its own allocation
instead of an interned `FontGroup`, because the handshake writes the client's
metrics into `fcm`, and sharing one would make a handshake on one window
silently move another.

`kitty @ set-client-viewport` carries the handshake: cell size, viewport and
DPI. Verified headless, with the shell's own `stty size` agreeing each time —
8x16 cells in 1200x800 gives 150x50, 20x40 gives 60x20, 8x16 in 1920x1080 gives
240x67. Measured: no font file mapped, fontconfig reads no cache, RSS ~42 MB.
The fontconfig, FreeType and HarfBuzz libraries stay mapped because they are
link-time dependencies of the extension module; nothing calls into them.

Two traps worth recording:

- **A font size change must not clobber the client's metrics**, and must not
  replace the per-window allocation, which would leak it. Server mode records
  the nominal size and keeps both.
- **The relayout after a handshake has to be unconditional.** Setting a viewport
  equal to the current one produces no resize event, because
  `update_os_window_viewport` early-returns on "no change", so a cell size
  change under it would never reach the layout.

`kitty --server` now selects the headless backend directly, so the temporary
KITTY_GLFW_MODULE hook is gone. Unlike `--start-as`=hidden it needs no display
at all, and it pairs with `--listen-on`.

Still open before a real client: `set-client-viewport` is the mechanism for the
handshake, but no attach protocol carries it yet.

**Spike 3 has its first half: the cell wire format exists and round-trips.**

`kitty/cell-wire.c` serializes a screen and reads the result back. There is no
transport yet, on purpose — the format is the part that is hard to change once
a peer exists, so it gets settled and tested alone. `kitty_tests/cell_wire.py`
writes into one `Screen`, applies the payload to a second, and compares them
cell by cell.

The format is 18 bytes of header, then one record per line: the line
attributes, a flat array of 24-byte cells, and a side table. A cell is six
little-endian `uint32`s — the codepoint, the packed `CPUCell` flags, and
`fg`/`bg`/`decoration_fg`/`attrs` from the `GPUCell`. Nothing is blitted,
because C decides bitfield layout for itself (§3b).

Three things do not go on the wire:

- `sprite_idx`, because the client derives it when it shapes (§1). Four bytes
  per cell, free.
- **A TextCache index**, per §2. The codepoints go in the side table, keyed by
  `x`, and the reader interns them with `tc_get_or_insert_chars`. Verified: `á`
  ships as `(0x61, 0x301)` and a ZWJ emoji as `(0x1f469, 0x200d, 0x1f4bb)` in
  both of the cells it covers.
- **A hyperlink id**, which the plan did not anticipate. It has exactly the
  TextCache disease — `remap_hyperlink_ids` (`hyperlink.c:100`) rewrites ids
  across every live cell, and the pool GCs every 8192 adds. Worse, an id means
  nothing without the pool. Version 1 sends zero and the attach protocol adds
  the pool transfer (§7).

Two decisions worth recording.

**Serialization walks the linebuf, not the render traversal.** Step 3 below
originally said to mirror `screen_update_cell_data`. That is wrong now:
`scrolled_by`, the pixel-scroll offset and the overlay line are all *view*
state, and the view belongs to the client. Server-side `scrolled_by` stays 0,
so the traversal degenerates to plain iteration anyway. Walking rows
`0..lines-1` drops the blank-line records, the overlay, and
`FONTS_DATA_HANDLE` from the signature.

**The serializer owns dirtiness.** `render()` returns at the top in server
mode, so nothing else calls `linebuf_mark_line_clean` or resets the screen's
dirty flags. A delta that did not clean up after itself would resend the whole
screen every frame. The test that catches this asserts the *second* delta is a
bare header. `cursor_has_moved` resends are gone with it, because they exist
for ligatures under the cursor, which is shaping, which is client-side.

**Dirtiness alone does not describe a frame, which the plan got wrong.**
`linebuf_index` (`line-buf.c:365`) rotates `line_attrs` together with the
lines, so a clean line that scrolls from y=5 to y=4 arrives clean at y=4.
Normal kitty does not care, because `update_line_data` (`screen.c:4014`) sits
*outside* the dirty check and re-uploads every line by position every frame.
Dirtiness gates shaping there, not transmission. A delta that ships only dirty
lines therefore misses `cat bigfile` entirely — the most common workload there
is.

The fix for v1 is a `content_moved` flag on `LineBuf`, set wherever lines
change position without becoming dirty: the four rotation functions, the
switch to the alternate screen, and rewrap. The serializer sends every line
when it sees the flag, then clears it. Conservative and correct, and cheap on
the wire, because consecutive frames of scrolling output repeat almost
verbatim and the stream compressor eats that.

The flag lives on `LineBuf`, not on `Screen`, which matters: `screen.c` swaps
`self->linebuf` temporarily in half a dozen places and restores it. A flag
attached to the buffer cannot be lost that way, and it catches every path that
moves lines by construction, rather than by an audit that has to stay right.

The real fix belongs to spike 4: a scroll record, so the client rotates its own
linebuf. It converges with history streaming, because a line that scrolls off
*is* a history append. Note that `cell_wire_serialize` resets
`history_line_added_count`, which counts exactly the lines spike 4 has to ship.
That delta must emit them before the reset.

**The compressed stream is in, and it settles the cost question.** One deflate
stream per connection, `Z_SYNC_FLUSH` per message, and a length prefix written
*inside* the stream so it compresses too. `CellStreamReader` assembles messages
from whatever chunks a socket hands over, which a test checks by feeding a byte
at a time.

Measured at level 1 on a 200x50 screen, where a raw payload is 240 KB:

| frame | compressed |
|---|---|
| blank snapshot | 1.2 KB |
| an `ls` screenful, attach-time snapshot | 7.9 KB |
| one keystroke | 71 bytes |
| one line of scrolling output | 8.0 KB |

The first three are comfortable. Attach costs single-digit KB per window, and
typing costs nothing. The fourth is the price of the full resend above: at
60 fps, scrolling output is ~480 KB/s on the wire. Survivable on a LAN, wrong
over a slow link, and the number that justifies the spike 4 scroll record.

One rule for the sender, tighter than §2 says. Serialization *consumes*
dirtiness: it marks the lines it sends clean and clears `content_moved`. So a
frame that is serialized and then dropped is lost for the rest of the
connection, because nothing resends those lines. §2 presents coalescing under
backpressure as an optimization. It is a correctness rule: **do not serialize
until the socket can take the output.** A reconnect is safe, because attach
starts with a snapshot; backpressure inside a connection is not.

Known gap: `attrs.mark` is always zero, because `mark_text_in_line` runs in the
render pass the server does not run. Marks are presentation config, so they
belong on the client (§7).

Unrelated to this work: two fish shell-integration tests fail in this checkout.
fish 4.8.1 is newer than this kitty tag targets, and those tests exercise only
shell scripts and a PTY.

---

Sizing: ~6–9k LOC. Weeks to a single-client prototype, months to shippable.
The bulk of the work is not the cell pipeline; it is unpicking the assumption
that an OS window exists (§5.1).

---

## 1. What kitty already gets right for this

### The parse path is font-independent

`fonts_data` enters `screen.c` in exactly two places, both on the render path:
`screen_update_cell_data` (`kitty/screen.c:3960`) and `render_overlay_line`
(`kitty/screen.c:4770`). Nothing in VT parsing, scrolling, scrollback, or
selection touches fonts. Grid width decisions come from `char-props` tables,
not font metrics.

The only font-derived value the parse path needs is `CellPixelSize cell_size` —
two integers on the `Screen` (set at `kitty/screen.c:141-143`), used for
graphics protocol placement (`screen.c:238-239, 753, 1819`), `CSI 14t`/`16t`
pixel reports (`screen.c:3083-3098`), and pixel-precise scrolling
(`screen.c:5643-5730`). The client supplies those at attach. That is the whole
coupling.

### The font/GPU dependency is one struct field

```c
typedef struct {                        // kitty/line.h:37
    color_type fg, bg, decoration_fg;
    sprite_index sprite_idx;            // <-- the only font-derived member
    CellAttrs attrs;
} GPUCell;                              // 20 bytes
```

`render_line` (`kitty/fonts.c:2074`) reads `cpu_cells` and writes **only**
`gpu_cells` — `sprite_idx`, plus `fg`/`decoration_fg` for the Powerline
PUA-plus-space case (`fonts.c:2138-2144`). It never mutates `CPUCell`s.
Shaping and ligatures change which sprite a cell draws, never grid occupancy.
So authoritative screen state is genuinely presentation-independent.

Corroborating signal: upstream already has a Go cell model at
`tools/vt/cell.go` that ports the C one **with `sprite_idx` omitted**:

```go
type Cell struct {
    Ch          Ch
    Fg, Bg, Dec CellColor
    Mc          MultiCell
    Attrs       CellAttrs
}
```

Someone has already drawn this exact line. It is a 95-line sketch, not an
implementation, but it is the same split.

### Colors are already wire-friendly

Cell colors are tagged, not resolved: `val & 0xff` gives the type — 0 default,
1 palette index in `(val >> 8) & 0xff`, 2 packed RGB (`kitty/line.c:865-866,
955-960`). Palette resolution happens against `screen->color_profile`, uploaded
to the shader. The protocol passes colors through verbatim and the client
resolves against a color table it receives separately — which makes per-client
theming of palette colors nearly free.

### The GLFW backend is already a runtime-loaded plugin

`kitty/main.py:97-106` picks a backend by name; `kitty/constants.py:216`
resolves it to `glfw-{module}.so`. The main loop is display-agnostic:
`run_main_loop` (`kitty/glfw.c:3187`) → `glfwRunMainLoop`, implemented
generically in `glfw/main_loop.h:27` over an `EventLoopData` from
`glfw/backend_utils.c` — a plain `poll()` loop with fd registration and timers.

A vendored `null_*` platform and an `osmesa` module already exist in
`glfw/source-info.json`. Two caveats: `setup.py:1262` builds only
`x11 wayland` (or `cocoa`), and `glfw/null_window.c:403-409` stubs
`_glfwPlatformWaitEvents` and `_glfwPlatformPostEmptyEvent` to no-ops, so it
would busy-loop. A real headless loop backend is small because
`backend_utils.c` already does the hard part.

### Transport infrastructure exists

`child-monitor.c` already runs a peer socket server for single-instance and
`kitty @`: `add_peer` (`:1883`), `peer_message_received` dispatch to Python
(`:573-587`), `send_response_to_peer` (`:239`), `talk_fd`/`listen_fd`
(`:44-57`). With unix-socket-over-SSH as the transport, this is the complete
substrate — reuse the accept/dispatch plumbing rather than inventing one.

### Invalidation on font change already exists

`screen_dirty_sprite_positions` (`screen.c:275`) plus
`screen->reload_all_gpu_data` (`state.c:512-533`), driven by
`Boss.on_dpi_change` (`boss.py:1735`) — already exercised on every monitor
change and `set-font-size`. This is exactly the path for "a client attached
with different fonts than the last one".

### Other useful precedents

- `paused_rendering` (`screen.h:214`) already snapshots a whole `LineBuf` plus
  cursor — a template for attach-time state dumps.
- Images live CPU-side in a disk cache (`graphics.c:47-64, 90, 1657`)
  independently of GPU textures (`graphics.c:769`), so image bytes survive with
  no client attached and are shippable.
- `Screen` is already constructed headlessly in tests against a pure-Python
  callbacks object (`kitty_tests/base.py:48, 300-303`).

### Where upstream stands

`docs/faq.rst:449-451` still names this as the one thing tmux does that kitty
does not. Issue 391 is the thread behind that line. It is worth reading before
planning any upstream contact, because the picture has three phases.

**2018-2021: the maintainer intended to build this himself, and asked for
help.** The issue carries the `help wanted` label to this day. He described
this exact architecture as the price of doing it properly:

> a daemon would need to either preserve all output or maintain its own
> internal terminal with no rendering, at which point it is basically a
> terminal emulator itself.

That is a statement of cost, not an objection, and it is a cost this work has
now paid. He was explicit about wanting it:

> Someday, I will get around to implementing a daemon for persistence of tty
> sessions in kitty. That will be more robust, feature-rich and efficient than
> any terminal multiplexer. [...] if you are willing to work on such a daemon,
> or you have ideas about how it should work, I am happy to hear from you

**2021: the issue closed, and not because the feature was refused.** The thread
turned abusive and he ended it with "This thread is now an unmitigated waste of
my time". GitHub records the close as "completed"; nothing shipped. Do not post
on 391.

**2026: he now points people at third-party tools.** In discussion 10153, June
2026, he recommends close-confirmation for local use and names `dtach` and
`abduco` for remote. He does not repeat the plan to build a daemon.

So the honest reading: the invitation to contributors was never withdrawn, but
it is old, and the current answer to "how do I get persistence" is "use
something else". A design document and a working demo are the only things that
can reopen the question. Two constraints follow, and this design already meets
both. Nothing may be tmux-shaped — he refused even to host a tmux fork in the
repository — and the daemon must be a real terminal with no rendering rather
than a re-serializer, which is §4's decision.

One more line of his shapes the protocol:

> a daemon can easily allow read-only access to a particular window by multiple
> clients

Single client is this project's v1 scope (§3), and it stays that way. But the
protocol must not *assume* one client in its shape. So the hello gives each
attachment an id, and kicking the incumbent is server policy sent as an
explicit "superseded" message, not an implicit close that only one client can
make sense of.

---

## 2. Scrollback

### What it actually costs

`HistoryBuf` stores **raw uncompressed cells** in 2048-line anonymous mmap
segments — `CPUCell` (12 B) + `GPUCell` (20 B) + `LineAttrs` (1 B) per cell-row
(`history.c:17-30`). So per line: `32 x columns + 1` bytes. Default
`scrollback_lines = 2000`.

| columns | per line | 2000 lines |
|---|---|---|
| 80 | 2.6 KB | **5.1 MB** |
| 200 | 6.4 KB | **12.8 MB** |
| 400 | 12.8 KB | **25.6 MB** |

**That is per window.** Scrollback belongs to the `Screen`, and every window has
its own. A session with ten 200-column windows is ~128 MB of raw history.
"Send the entire thing on connect" is fine for one window and not fine for a
session.

But the bytes are not the problem — the entropy is tiny, and generic
compression handles it. Blank runs are the single easiest thing for a
compressor to eat: `HistoryBuf` keeps `cpu_cells` and `gpu_cells` in
*separate* arrays (`history.c:23-25`), so within a line you get long runs of
byte-identical 12- and 20-byte structs.

**Trailing-blank trimming is therefore not needed for size.** Its remaining
argument is bytes *touched* — compression throughput, not output size. 12.8 MB
of raw history is ~85 ms at zlib-1 (~150 MB/s). Trimming an 80%-blank buffer
cuts that by about 5x. It is a latency optimization worth roughly five lines of
code, worth doing eventually, not worth doing in v1.

### The fix for attach latency

1. Send the visible grid for the focused window first — attach is instant.
2. Stream that window's history in the background.
3. Fetch other windows' history lazily on first display or first scroll.

Result: scrolling is client-local and instant, which is strictly better UX than
tmux, and attach does not stall on megabytes.

### Two transfers, not one

**(a) Bulk history transfer** — at attach, and after a rewrap invalidation.
Megabytes, one-shot, and there is no incremental option because the client has
nothing yet. This is where raw-array `memcpy` into the compressor applies: no
wire format, just blit `cpu_cells` / `gpu_cells` / `line_attrs`, subject to the
two caveats below.

**(b) Steady-state frame updates** — dirty lines only, never full state. Kitty
already tracks `has_dirty_text` per line (`line.h:86`) and
`screen_update_cell_data` walks only dirty lines (`screen.c:4000-4012`). A
typical frame is 1–3 changed lines — a few KB raw, well under a KB compressed —
plus lines appended to history as they scroll off
(`history_line_added_count`, `screen.c:2391-2392`, append-only and cheap), plus
cursor/selection/scroll-offset, which are bytes.

Nothing resends the world every frame.

Correction from the implementation: this holds for editing in place, not for
scrolling. Until a scroll record exists, a frame that scrolls sends every
line, because line attributes move with their lines. See the spike 3 notes.

### One long-lived zlib stream, not per-message compression

Run a single `deflate` stream for the whole connection with `Z_SYNC_FLUSH` per
frame rather than compressing each message independently. Cross-message
dictionary reuse is a large win in steady state, where consecutive frames repeat
prompts, redraws and the same styling over and over. This is precisely what
`Z_SYNC_FLUSH` exists for.

Consequence for the drop-on-overflow policy in §3: a persistent stream means you
cannot discard already-compressed frames without desyncing the decompressor.
Drop *before* compression instead — if the client is slow, do not queue frames,
just keep accumulating the dirty-line set and emit one combined update when the
socket drains. Since cell state is idempotent, coalescing is free and the stream
stays intact.

### zlib is already a hard dependency — zero new deps

`kitty/graphics.c:21` does `#include <zlib.h>` and uses `inflateInit`/`inflate`
for compressed image payloads (`graphics.c:368-401`), and `setup.py:814-815`
explicitly appends `-lz` if it is not already in the link line. So zlib is
guaranteed present on every platform kitty builds on.

lz4/zstd remain available later as a throughput optimization for the bulk
transfer (~5x faster compression), but they are a real new dependency and
should not gate v1.

### The blocker on raw blitting: TextCache indices are not stable

`CPUCell.ch_or_idx` holds a literal codepoint when `ch_is_idx` is clear, but an
index into a per-`Screen` `TextCache` when it is set — and **that cache garbage
collects and renumbers**. `screen.c:1395` triggers GC every 8192 insertions, and
`screen.c:1037-1052` rewrites `ch_or_idx` in place across every live cell,
history included. So raw indices are meaningless to a client and unstable over
time within a single session. A naive `memcpy` of `CPUCell`s would ship dangling
references.

The fix is cheap and makes the two caches fully independent: **never put an
index on the wire.** Cells with `ch_is_idx` set are rare — grapheme clusters,
combining marks, emoji ZWJ sequences — so ship the bulk raw and attach a small
side table of `(position -> codepoint list)` for just those cells. The client
interns them into its own cache and assigns its own indices. Each side then GCs
independently and neither needs to know about the other.

### Rewrap: mostly a non-problem, because kitty already freezes during a drag

`screen_resize` (`screen.c:627`) calls `rewrap` (`screen.c:299`), which re-flows
the **entire** history into a freshly allocated `HistoryBuf`, so any column
change invalidates the client's mirror wholesale.

But kitty already has the right semantics natively. `resize_debounce_time`
(default `0.1 0.5`) is applied at `child-monitor.c:1263-1291`: during a live
drag kitty does **not** call `screen_resize` at all. It renders the existing
grid into the new viewport and only resizes when the drag pauses, or when the OS
reports the resize finished (macOS distinguishes the two, hence two numbers).

So the client inherits "frozen grid during drag, authoritative resize on
release" for free, with **no client-side rewrap implementation at all**. On
debounce-fire the client sends the new size, the server rewraps, and the client
re-streams lazily. Visually identical to what local kitty already does.

Client-side rewrap remains available as an *optional* optimization if the
post-release latency spike turns out to be visible on a slow link. The usual
objection — that it creates a determinism contract you must preserve across
every version forever — dissolves once the local result is transient and
discarded the moment the server's authoritative version lands. Divergence then
shows up as a cosmetic flicker, not a correctness bug. Safe to add later, not
worth building first.

### The client gets thin

If the client mirrors grid + history, it needs no VT parser, no PTY handling,
no layout logic, no kittens, no `boss.py` tab management. It needs fonts,
shaping, the sprite atlas, GL, GLFW, input, and clipboard. That is closer to
"kitty's renderer as a standalone binary" than "kitty with a different backend".

---

## 3. What single-client buys

Multi-client geometry was the one problem with no clean answer: one PTY has one
size, and two clients with different fonts or DPI cannot both get a native-fit
grid. tmux chose "smallest attached client wins, letterbox the rest" and people
have resented it for twenty years. That entire question disappears:

- The attached client owns the grid, unconditionally.
- Detach freezes the grid as-is. No client attached means `render()` simply never
  runs, so stale `sprite_idx` values are harmless.
- Attach sets the grid from the new client's `(viewport_px, cell_px)`, resizes,
  and delivers `SIGWINCH`.
- Attaching while someone else is connected closes the incumbent's socket.

One design point falls out of "kick": the server must never block writing to a
client that has stopped reading. Cell updates are **idempotent state** — a
dropped frame just means marking lines dirty and resending — so the render
stream wants a bounded queue with drop-on-overflow (before compression, per §2).
Clipboard replies, notifications and `kitty @` responses are **events** and must
not be dropped. Two channels with different reliability semantics on one socket.

## 3a. Multiple OS windows — yes, and mostly for free

Single-client means one *process* attached, not one window. The client creates a
real native window per server-side OS window; the server keeps the logical set.

This falls out cleanly because kitty already keys font data per OS window:
`font_group_for(font_sz_in_pts, dpi_x, dpi_y)` (`fonts.c:294`) interns font
groups by size and DPI, and `OSWindow.fonts_data` / `temp_font_group_id`
(`fonts.c:207-229`) manage per-window assignment. Two windows on monitors with
different DPI already work today; per-viewport `cell_size` is already
per-`OSWindow` state, not global. So "each attached window reports its own
`(viewport_px, cell_px, dpi)`" is the existing model, not a new one.

Cross-OS-window operations — `move-window-to-new-os-window`, `detach-window`,
`detach-tab` — are all `boss.py` logic and stay server-side, so they work
unchanged.

Two wrinkles, both minor:

- **Placement on reattach.** The server tracks window position and size already,
  so it can hand back last-known geometry; but the client's screen layout may
  differ from wherever the session was last attached. Needs a sane clamp policy,
  not new machinery.
- **Window-level state becomes client-scoped config.** Fullscreen, maximized,
  decorations and `--start-as` are properties of a real window and have to be
  synced or re-derived on attach rather than living authoritatively on a
  headless server.

## 3b. macOS ↔ Linux interop

Viable in both directions, and the cell-grid design actually *helps*: fonts
differ between the two platforms, and since the client shapes locally with its
own fonts, that difference is by construction a non-issue rather than a bug.

### Raw struct blitting is not safe across platforms — pack `CPUCell`

`CPUCell` (`line.h:50-82`) is a union of **bitfields** across three `uint32`s.
C bitfield allocation order and straddling behaviour are implementation-defined.
In practice x86-64 Linux and arm64 macOS are both little-endian, LP64, and
clang-compatible, and `static_assert(sizeof(CPUCell) == 12)` holds on both — so
a naive blit would very likely work. "Very likely" is not a wire protocol.

The targeted fix keeps most of the win:

- `GPUCell` (`line.h:37`) is effectively wire-safe already — three `uint32`
  colors, a `uint32` sprite index, and `CellAttrs` which is a union over a
  single `uint32`. Blit it, byte-swapping only if a big-endian platform ever
  appears (kitty's `__BYTE_ORDER__` conditional at `line.h:52-58` shows this is
  already on the author's radar).
- `CPUCell` gets an explicit pack/unpack with a fixed little-endian field order.
  It has a handful of fields; this is ~30 lines and still applies in bulk per
  line, so the "one compressed blob per batch" shape survives.

`sprite_idx` should not be transmitted at all — it is client-derived (§1), so
the client fills it in. That shrinks the `GPUCell` payload by 4 bytes per cell
for free.

### Keybindings resolve client-side

macOS and Linux differ on modifier semantics — `cmd` vs `super`, Option-as-Alt —
so a Linux server dispatching a macOS client's raw events would apply the wrong
conventions, and a `kitty.conf` written with `cmd+` bindings would be
meaningless server-side. Resolving on the client fixes this and is better anyway:

- Platform modifier conventions stay where the platform is.
- Local actions — tab switching, window navigation, font size, scrolling — feel
  instant even on a laggy link, instead of costing a round trip.
- Keybindings become client-scoped config, which is where a user would expect
  them to live once they are using multiple client machines.

Cost: the client needs enough of the action vocabulary to know which actions it
handles locally and which it forwards as an RPC.

### Path-based operations diverge

`icat` on a local file, `edit-in-kitty`, `open`, and file transfer all involve a
filesystem, and the client's is not the server's. More visible cross-platform,
where even path shapes differ.

---

## 4. Architecture decision: ship cells

Three splits were considered.

- **Ship cells (chosen).** Server sends dirty lines as (text, style); the client
  shapes locally with its own fonts and owns the atlas. Server needs no fonts.
- **Ship sprites.** Server rasterizes with FreeType (CPU-only, so a GPU-less
  server *can* do it) and sends bitmaps. Rejected: the server becomes
  authoritative on shaping, only the visible region is meaningful, so **every
  scroll becomes a server round trip** — precisely the tmux behaviour being
  escaped. It also requires the client's fonts installed server-side.
- **Re-serialize to escape codes (tmux-style).** Much cheaper and needs no client
  changes, but it reintroduces exactly what kitty objects to in tmux
  (`docs/faq.rst:465` on images), and requiring a kitty client would then buy
  nothing — which defeats the premise.

---

## 5. What is still hard

### 5.1 The OS-window abstraction — the big grind

`boss.py` mentions `os_window` 301 times; `tabs.py` 62; `window.py` 51.
`kitty/glfw.c` is 3900 lines of OS-window management. Layout runs in **pixels** —
`kitty/layout/base.py:447` calls `viewport_for_window(os_window_id)` for a
central `Region` plus `cell_width`/`cell_height`, then divides into cells.

Keep layout server-side and have the client report
`(viewport_px, cell_px, dpi, scale)` at attach and on resize. That keeps all
Python state on the server and the client genuinely dumb. But it means auditing
that ~300-call-site surface for "there is a real window here, now, with a GL
context". Most is `os_window_id` plumbing that survives unchanged; a minority is
genuinely display-bound (decorations, fullscreen, monitors, IME, cursor shape,
drag-and-drop).

1–2k LOC of churn. Boring rather than deep, and it is the bulk of the project.

### 5.2 Config split

**Settled: the client mandates the front end.** Everything the user sees and
touches is the client's `kitty.conf`: `font_family`, `font_size`, keybindings
(§3b), colors, `background_opacity`, decorations, cursor blink and
`repaint_delay`. This is what a user expects once one server serves several
machines — the laptop's fonts and bindings should follow the laptop.

The server keeps only what it alone can own: the PTY and its environment, shell
integration, `scrollback_lines`, `allow_remote_control`, and the layout config,
because layout runs server-side in pixels (§5.1).

Colors need one more rule. The profile lives server-side, because `OSC 4`,
`10` and `11` queries need answering and SGR has to resolve. The tagged
encoding (§1) means the client can theme palette-index cells freely and
true-color cells not at all, which is the right split. What is still open is
what happens when a child program *changes* a color at runtime: that is
server-side state the client has to be told about, and it has to interact with
the client's own palette in a defined way. See §7.

### 5.3 Client-side services become RPCs

Every `CALLBACK` in `screen.c` reaching the OS needs a round trip: `on_bell`
(`:3055`), `desktop_notify` (`:3416`), `clipboard_control` (`:3441`),
`set_dynamic_color` (`:3427-3429`), `color_control` (`:3435`), `title_changed`
(`:3406`), `icon_changed` (`:3421`), `report_color_scheme_preference` (`:3145`),
URL open (`:4498`), `set_primary_selection` (`:5170, 6278`).

Most are fire-and-forget. Two are not: OSC 52 clipboard *read* and
`set_dynamic_color` queries are synchronous to the application and now involve a
network round trip. Kitty's protocols already anticipate multiplexers here
(`docs/clipboard.rst:209-233`, `docs/desktop-notifications.rst:171, 382`).

Kittens split the same way: `hints`, `unicode_input`, `themes` run as overlay
TUIs over a PTY and work server-side unchanged; `clipboard`, `icat` on local
files, `ssh`, `edit-in-kitty`, `open` need the service RPC.

### 5.4 Input: keybindings client-side, unmatched keys forwarded raw

`keys.c` encodes key events using keyboard-protocol flags stored **on the
Screen**, server-side, so the client cannot pre-encode keys destined for the
application — those forward as raw events (key, scancode, mods, action, text).

But keybinding *resolution* belongs on the client (§3b). So: client matches
against its own keymap first, handles what it owns locally, sends named actions
for what the server owns, and forwards everything unmatched as raw events. Mouse
forwards pixel coords plus viewport and the server maps to cells.

### 5.5 Latency, and no local echo

Every keystroke round-trips before the echo returns — no worse than running a
shell over SSH today, but worse than local kitty, and there is no mosh-style
predictive echo. `OPT(input_delay)`/`OPT(repaint_delay)` batching
(`child-monitor.c:1757, 1062-1070`) is tuned for a local 60 Hz compositor and
needs per-link tuning. Predictive echo is an explicit non-goal.

The compensation: because scrollback is client-side, *scrolling* has zero
latency, which is the interaction where tmux over a slow link feels worst.

### 5.6 Versions

**Settled: the protocol negotiates, it does not demand lockstep.** tmux demands
lockstep and users hate it. Two layers carry versions, and both follow a
pattern kitty already uses.

**The hello is JSON, and it is version-tolerant by construction.** Unknown
fields are ignored, so a field added later costs an older peer nothing. Each
side declares its kitty version and its protocol version. The refusal rule
copies remote control: `handle_cmd` (`remote_control.py:217-222`) compares
major and minor and refuses only a peer *newer* than itself, with an error that
says which side to update. An older peer is served.

**The binary layer declares its version in the hello and must match in v1.**
`CELL_WIRE_VERSION` is already a byte on every payload. This is the
`RC_ENCRYPTION_PROTOCOL_VERSION = '1'` pattern (`constants.py:31`): an explicit
protocol number that moves independently of kitty's own version. A
compatibility window can arrive later without changing the shape of the
handshake, which is the point of declaring it there.

**The hello must be uncompressed.** Compression is itself negotiated — the
hello names `"zlib"` so `zstd` can be added later (§2) — so the exchange cannot
happen inside the compressed stream. Both sides start their streams only after
the hello succeeds.

---

## 6. Spike order

1. **Headless build.** *(done)* Loop backend over `backend_utils.c` providing
   `run_main_loop` / `add_main_loop_timer` / `wakeup_main_loop` /
   `stop_main_loop` with no display, selected by `--server` alongside the
   existing `glfw-{module}.so` mechanism; replace the `null_window.c` no-op
   stubs. **Demo:** kitty starts on a GPU-less host, spawns a shell, and
   `kitty @ ls` / `kitty @ get-text` show a live correctly-parsed screen with no
   window anywhere. Useful on its own, and proves the parse path is display-free.

2. **Virtual OS window.** *(done)* The client-viewport abstraction; route layout through
   client-supplied `(viewport_px, cell_px)`. **Demo:** `kitty @ ls` reports sane
   geometry for a window nobody displays, and layouts respond to a simulated
   resize.

3. **Cell protocol, visible region only.** *(format and compression done,
   transport open)* Serializer walking the linebuf and emitting dirty lines as
   (text, style); the reader rebuilds `Line`s and runs `render_line` locally.
   The view traversal is deliberately not mirrored — see the progress notes.
   Still to come: the unix socket over SSH, the two channels per §3, and a real
   client on the far end. **Demo:** attach from a laptop, see the remote shell,
   type, detach, reattach, state intact.

4. **Client-side scrollback.** Compressed raw-array bulk transfer plus the
   codepoint side table for `ch_is_idx` cells (§2); background history
   streaming, lazy per-window fetch, invalidate-and-restream on rewrap.
   **Demo:** instant local scrolling over a deliberately laggy link.

5. **Service RPCs.** Clipboard, bell, notifications, title, dynamic colors, URL
   open.

6. **Graphics protocol.** Ship image bytes from the disk cache with a
   client-side texture cache keyed by image id plus content hash; move
   `grman_update_layers` placement math client-side, or send cell-coordinate
   placements.

7. **Config split** and session CLI. `kitty --server` exists; `kitty --attach`
   and list/kill do not.

---

## 7. Open questions

Questions 1 to 3 are answered. Config authority is split, the client mandating
the front end; the protocol carries versions rather than demanding lockstep;
keybindings are client-side. All three are in Settled decisions above.

1. Colors are client presentation, and the wire passes tagged colors through
   (§1), so per-client theming is free. But `OSC 4`, `10` and `11` let a
   *server-side* child program change them at runtime. Those changes have to
   reach the client as state, and it needs a rule for how they interact with
   the client's own palette.
2. How does the hyperlink pool reach the client? An id is meaningless without
   it, and it renumbers, so the attach protocol needs an id-to-URL table and a
   rule for incremental additions.
3. Should the reader validate cell fields it cannot use? Version 1 trusts the
   width, scale and alignment fields it is given. The transport is a unix
   socket over SSH, so the sender is as trusted as the user, but a malformed
   payload should still fail cleanly rather than draw nonsense.
4. Where do marks live? `attrs.mark` comes from `mark_text_in_line`, which the
   server does not run. Marker patterns are config, so the client can apply
   them itself, but then a `scroll_to_next_mark` needs a client-side answer.
