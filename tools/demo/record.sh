#!/usr/bin/env bash
# One command to re-record docs/demo.gif, from nothing.
#
#   tools/demo/record.sh                 # -> docs/demo.gif
#   OUT=/tmp/try.gif tools/demo/record.sh
#   DEMO_DIR=/tmp/mysandbox KEEP=1 tools/demo/record.sh
#
# It builds a throwaway sandbox (its own $HOME, its own history of 54,000
# commands from three machines), records the tape against it with VHS, and
# shrinks the result with gifsicle. Nothing outside the sandbox is touched and
# nothing installed on this machine is used: the `woswoar` the recording calls
# is a wrapper around *this checkout*.
#
# The GIF is checked in, so this exists to be re-run rather than to be run once:
# when the picker changes, the demo is regenerated from the tape instead of
# being quietly wrong. That is the whole reason for choosing VHS over a
# hand-recorded cast.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

WORK="${DEMO_DIR:-${TMPDIR:-/tmp}/woswoar-demo}"
OUT="${OUT:-$repo/docs/demo.gif}"
# gifsicle's lossy level. 60 is where a flat terminal palette stops shrinking
# and starts speckling; the text stays sharp because it is one colour on one
# background, which is what this quantises so well.
LOSSY="${DEMO_LOSSY:-60}"

for tool in vhs gifsicle fzf; do
  command -v "$tool" >/dev/null || {
    echo "error: '$tool' is not installed." >&2
    echo "  Fedora: sudo dnf install vhs ttyd gifsicle fzf" >&2
    exit 1
  }
done

echo "building the sandbox in $WORK ..."
( cd "$repo" && python3 -m tools.demo.seed "$WORK" )

# Read by the tape, which types `exec bash --rcfile "$DEMO_HOME/.bashrc"`. VHS
# passes its own environment through to the shell it starts.
export DEMO_HOME="$WORK/home"

echo "recording ..."
# From the work directory: the tape writes `demo.gif` beside wherever vhs runs,
# and a tape that hardcoded an absolute path could not be run by anyone else.
( cd "$WORK" && vhs "$here/demo.tape" )

mkdir -p "$(dirname "$OUT")"
gifsicle -O3 --colors 64 --lossy="$LOSSY" "$WORK/demo.gif" -o "$OUT"

# Only ever a directory this run built. `seed.py` writes that marker and refuses
# to touch a non-empty directory without one, and the same test belongs on the
# way out: `DEMO_DIR` is a path from the environment, and "tidy up afterwards"
# must not be able to mean "delete what you pointed me at".
if [ "${KEEP:-0}" != 1 ] && [ -e "$WORK/.woswoar-demo-sandbox" ]; then
  rm -rf "$WORK"
fi

echo "wrote $OUT ($(du -h "$OUT" | cut -f1), $(gifsicle --info "$OUT" | head -1 | grep -o '[0-9]* images'))"
