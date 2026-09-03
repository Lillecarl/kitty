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

What remains of spike 2 is the other half: `cell_px` currently comes from
server-side fonts rather than from the client, and there is no attach protocol
to carry either number yet.

Known gap: the server still loads fonts, because `create_os_window` derives
window geometry from cell metrics. Spike 2 replaces that with client-supplied
`(viewport_px, cell_px)`, after which the server needs no fonts at all, as §4
intends.

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

### Upstream wants this

`docs/faq.rst:449-451`: multiplexers are "a bad idea", kitty does everything
tmux does "but better, with the exception of remote persistence (issue 391)".
The acknowledged gap, not a fight against the grain.

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

`font_family`, `font_size`, `background_opacity`, decorations, cursor blink and
`repaint_delay` are client properties. Scrollback, shell integration and
`allow_remote_control` are session properties. Keybindings are client properties
(§3b). Colors are ambiguous: the profile must live server-side because OSC
4/10/11 queries need answering and SGR must resolve — but users want per-client
themes. The tagged encoding means per-client theming works for palette-index
cells and not true-color ones, which is defensible but needs documenting.

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

### 5.6 Version lockstep

The protocol carries internal-ish structures, so client and server need matching
or negotiated versions. tmux has this and users hate it. Design a real version
handshake with a declared compatibility window, not a bare equality check.

---

## 6. Spike order

1. **Headless build.** *(done)* Loop backend over `backend_utils.c` providing
   `run_main_loop` / `add_main_loop_timer` / `wakeup_main_loop` /
   `stop_main_loop` with no display, selected by `--server` alongside the
   existing `glfw-{module}.so` mechanism; replace the `null_window.c` no-op
   stubs. **Demo:** kitty starts on a GPU-less host, spawns a shell, and
   `kitty @ ls` / `kitty @ get-text` show a live correctly-parsed screen with no
   window anywhere. Useful on its own, and proves the parse path is display-free.

2. **Virtual OS window.** *(geometry done; cell metrics still server-side)* The client-viewport abstraction; route layout through
   client-supplied `(viewport_px, cell_px)`. **Demo:** `kitty @ ls` reports sane
   geometry for a window nobody displays, and layouts respond to a simulated
   resize.

3. **Cell protocol, visible region only.** Serializer mirroring
   `screen_update_cell_data`'s traversal (`render_line_for_virtual_y`,
   scrollback, overlay line, pixel-scroll offset) emitting dirty lines as
   (text, style); client reconstructs `Line`s and runs `render_line` locally.
   Unix socket over SSH, two channels per §3. **Demo:** attach from a laptop,
   see the remote shell, type, detach, reattach, state intact.

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

7. **Config split** and session CLI (`kitty --server`, `kitty --attach`,
   list/kill).

---

## 7. Open questions

1. Where does `kitty.conf` get read, and what is authoritative for colors?
2. Is protocol version lockstep acceptable, or is a compatibility window
   required?
3. Does keybinding config move wholesale to the client, or is it split (session
   actions server-side, presentation actions client-side)? §3b argues for
   client-side; the exact boundary needs drawing.
