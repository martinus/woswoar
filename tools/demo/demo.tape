# The woswoar demo, as a VHS screenplay (https://github.com/charmbracelet/vhs).
#
# Checked in because a recorded cast is an artefact nobody can review or
# regenerate, and this is the one part of the documentation that cannot be
# proof-read -- it has to be re-run. `tools/demo/record.sh` does that: it builds
# the sandbox, runs this, and shrinks the result. To do it by hand:
#
#     python -m tools.demo.seed /tmp/woswoar-demo
#     DEMO_HOME=/tmp/woswoar-demo/home vhs tools/demo/demo.tape
#
# Every beat is an argument, in the order a stranger needs them:
#
#   1. a history of 54,000 commands from three machines exists
#   2. Ctrl-R searches all of it, and the machine column says whose is whose
#   3. Ctrl-R and Ctrl-O narrow it: this machine, then this directory
#   4. Ctrl-T stops searching and reads the timeline around a hit
#   5. Enter puts the command on the prompt and does not run it
#
# Nothing here is sped up. The pauses are reading time; the picker takes exactly
# as long as it takes, which on this history is the ~105 ms the README quotes.

Output "demo.gif"

Set Shell bash
Set FontFamily "IosevkaTerm Nerd Font"
Set FontSize 16
Set Width 1200
Set Height 700
Set Padding 20
Set Theme "Dracula"
Set WindowBar Colorful
Set BorderRadius 10
Set TypingSpeed 55ms

# No `Set Framerate`: VHS 0.11 writes a GIF at 25 fps whatever it is set to
# (measured -- 12 and 50 produce the same file), and it is not worth a knob that
# does nothing. The size comes from gifsicle instead: a terminal only repaints on
# a keystroke, so its ~560 frames collapse to ~70 merged ones and well under
# half the bytes, at the same 22.5 seconds.

# Into the sandbox `bash` -- its own $HOME, its own history, this checkout's
# `woswoar` on PATH. Hidden because the plumbing is not the demo.
Hide
Type `exec bash --rcfile "$DEMO_HOME/.bashrc"`
Enter
Sleep 2s
Type "clear"
Enter
Show

Sleep 800ms

# 1) What there is to search: two years, three machines, one history.
Type "woswoar stats"
Sleep 400ms
Enter
Sleep 3s

# 2) Ctrl-R over all of it. The machine column is the whole pitch in one frame:
#    `docker` matches on every machine at once, newest first, and the age of the
#    one that was interrupted is red.
Ctrl+R
Sleep 1200ms
Type "docker"
Sleep 3s

# The details pane is missing from this tape and not from the picker: it is
# bound to Ctrl-/, which is byte 0x1F, and VHS 0.11 can send `Ctrl+<letter>` and
# nothing else -- `Ctrl+/` is a parse error and `Type "\x1f"` types four
# characters. docs/searching.md shows what the pane looks like instead.

# 3) Narrowing, without leaving the picker: this machine, then this directory
#    *on every machine* -- `~/src/api` is the same project on all three.
Ctrl+R
Sleep 2500ms
Ctrl+O
Sleep 3s
Ctrl+G
Sleep 1200ms

# 4) Half of what you want from history is the command *after* the one you
#    remember. Ctrl-T drops the search and unfolds the timeline around the hit,
#    cursor still on it, repeats kept.
Ctrl+T
Sleep 3500ms

# 5) Enter puts it on the prompt, cursor at the end, and does not run it. The
#    recording ends here on purpose: that is the safety story, and no amount of
#    prose sells it.
Enter
Sleep 3s
