#!/usr/bin/env bash
# Show a kitty client displaying a kitty server's session, with no display of
# its own. See SERVER-MODE.md.
#
# The client is a real kitty: it loads fonts, opens a window and draws with
# OpenGL. So it needs a compositor. This script starts a headless weston and
# software OpenGL, which is why it runs on a machine with no screen.
#
#   nix shell nixpkgs#weston -c ./server-mode-demo.sh
#
# MESA_PREFIX must point at a mesa that has lib/dri and the EGL vendor file,
# for example the result of: nix build nixpkgs#mesa -o /tmp/mesa
set -u

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KITTY=${KITTY:-$HERE/kitty/launcher/kitty}
MESA_PREFIX=${MESA_PREFIX:-/tmp/mesa}

if [ ! -x "$KITTY" ]; then
    echo "No kitty at $KITTY. Run 'make debug' first, or set KITTY." >&2
    exit 1
fi
if ! command -v weston >/dev/null; then
    echo "weston is not on PATH. Try: nix shell nixpkgs#weston -c $0" >&2
    exit 1
fi

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
if [ -d "$MESA_PREFIX/lib/dri" ]; then
    export LIBGL_DRIVERS_PATH=$MESA_PREFIX/lib/dri
    export __EGL_VENDOR_LIBRARY_DIRS=$MESA_PREFIX/share/glvnd/egl_vendor.d
    export LD_LIBRARY_PATH=$MESA_PREFIX/lib:${LD_LIBRARY_PATH:-}
fi

TD=$(mktemp -d)
SERVER_SOCK=$TD/server
CLIENT_SOCK=$TD/client
SOCKET_NAME=wayland-kitty-demo
PIDS=()
cleanup() {
    kill "${PIDS[@]}" 2>/dev/null
    wait 2>/dev/null
    rm -rf "$TD"
}
trap cleanup EXIT

wait_for() {
    local target=$1 i
    for i in $(seq 100); do
        [ -e "$target" ] && return 0
        sleep 0.1
    done
    return 1
}

say() { printf '\n== %s\n' "$1"; }

say "Starting a headless compositor, so the client has somewhere to draw"
weston --backend=headless --renderer=gl --socket=$SOCKET_NAME --width=1280 --height=800 >"$TD/weston.log" 2>&1 &
PIDS+=($!)
wait_for "$XDG_RUNTIME_DIR/$SOCKET_NAME" || {
    echo "weston did not start:"
    tail -5 "$TD/weston.log"
    exit 1
}

say "Starting the server: it holds the shell, and has no display at all"
"$KITTY" --server -o allow_remote_control=yes --listen-on "unix:$SERVER_SOCK" >"$TD/server.log" 2>&1 &
PIDS+=($!)
wait_for "$SERVER_SOCK" || {
    echo "The server did not start:"
    cat "$TD/server.log"
    exit 1
}

say "Starting the client: it draws, with its own fonts, and holds nothing"
WAYLAND_DISPLAY=$SOCKET_NAME DISPLAY= "$KITTY" --attach "unix:$SERVER_SOCK" \
    -o allow_remote_control=yes -o remember_window_size=no -o initial_window_width=800 -o initial_window_height=480 \
    --listen-on "unix:$CLIENT_SOCK" >"$TD/client.log" 2>&1 &
PIDS+=($!)
wait_for "$CLIENT_SOCK" || {
    echo "The client did not attach:"
    cat "$TD/client.log"
    exit 1
}
sleep 2

grid() {
    "$KITTY" @ --to "unix:$1" ls 2>/dev/null | python3 -c '
import json, sys
w = json.load(sys.stdin)[0]["tabs"][0]["windows"][0]
print(str(w["columns"]) + "x" + str(w["lines"]))
' 2>/dev/null
}

say "The server prints. The client shows it."
"$KITTY" @ --to "unix:$SERVER_SOCK" send-text --match all $'printf MIRROR%sCLIENT -TO-THE-\r' >/dev/null 2>&1
sleep 2
"$KITTY" @ --to "unix:$CLIENT_SOCK" get-text --match all 2>/dev/null | grep -m1 MIRROR- | sed 's/^/   client: /'

say "Type into the client. The shell on the server runs it."
"$KITTY" @ --to "unix:$CLIENT_SOCK" send-text --match all 'printf TYPED%sCLIENT -INTO-THE-' >/dev/null 2>&1
sleep 1
"$KITTY" @ --to "unix:$CLIENT_SOCK" send-key --match all enter >/dev/null 2>&1
sleep 2
"$KITTY" @ --to "unix:$SERVER_SOCK" get-text --match all 2>/dev/null | grep -m1 TYPED- | sed 's/^/   server: /'

say "Resize the client. The server's grid follows."
echo "   before: server $(grid "$SERVER_SOCK")  client $(grid "$CLIENT_SOCK")"
"$KITTY" @ --to "unix:$CLIENT_SOCK" resize-os-window --action resize --unit pixels --width 640 --height 400 >/dev/null 2>&1
sleep 2
echo "   after:  server $(grid "$SERVER_SOCK")  client $(grid "$CLIENT_SOCK")"

say "Done"
