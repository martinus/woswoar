# woswoar - bash integration
#
# Source this from ~/.bashrc:
#     source /path/to/woswoar.bash
# `woswoar install` writes that line for you.
#
# Requires bash 5.0+ for $EPOCHSECONDS and $EPOCHREALTIME.
#
# Design constraint: recording runs on *every* prompt, so it must not fork and
# must not exec. Everything below the startup section uses builtins only.
# `history 1 > file` followed by `read < file` is how the full command line is
# captured -- $BASH_COMMAND is free but lossy (it reports `for i in 1 2` for a
# loop, and only the first element of `a && b`), while $(history 1) is faithful
# but forks. The redirect-and-read pair is faithful, fork-free, and ~30us --
# the largest single item in a hook that costs ~150us per command end to end.

# shellcheck shell=bash

[[ -n ${__WOSWOAR_LOADED:-} ]] && return 0
[[ $- == *i* ]] || return 0

if ((BASH_VERSINFO[0] < 5)); then
    printf 'woswoar: bash 5.0+ required, found %s; history recording disabled\n' \
        "$BASH_VERSION" >&2
    return 0
fi

# ---------------------------------------------------------------------------
# Startup. Forks here are fine; they happen once per shell.
# ---------------------------------------------------------------------------

__woswoar_dir=${WOSWOAR_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/woswoar}
__woswoar_conf=${XDG_CONFIG_HOME:-$HOME/.config}/woswoar

__woswoar_id=
if [[ -r $__woswoar_conf/machine ]]; then
    while IFS='=' read -r __woswoar_k __woswoar_v; do
        [[ $__woswoar_k == id ]] && __woswoar_id=$__woswoar_v
    done <"$__woswoar_conf/machine"
    unset -v __woswoar_k __woswoar_v
fi

if [[ -z $__woswoar_id ]]; then
    printf "woswoar: not initialised yet, run 'woswoar install'\n" >&2
    unset -v __woswoar_dir __woswoar_conf __woswoar_id
    return 0
fi

__woswoar_logdir=$__woswoar_dir/logs/hosts/$__woswoar_id
mkdir -p "$__woswoar_logdir" 2>/dev/null || return 0

# Scratch file for capturing `history 1`. Prefer XDG_RUNTIME_DIR: it is a
# per-user tmpfs that systemd clears at logout, so a shell killed with -9 cannot
# leave the last command lying around in /tmp.
__woswoar_scratch=${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/woswoar-hist.$$
(umask 077 && : >"$__woswoar_scratch") 2>/dev/null || return 0

# Only claim the EXIT trap if nothing else owns it; clobbering a user's trap
# would be a far worse bug than leaving one scratch file behind.
if [[ -z $(trap -p EXIT) ]]; then
    # shellcheck disable=SC2064  # expand the path now, not at exit
    trap "rm -f -- '$__woswoar_scratch'" EXIT
fi

# Identifies this shell for `--scope session`. Start second plus pid, both in
# hex: two shells cannot share a pid at the same instant, and a reused pid must
# land in a later second, so the pair is unique on this host without spawning
# uuidgen. Hex rather than the full microsecond clock because this string is
# repeated on every recorded line -- 14 bytes instead of 23.
printf -v WOSWOAR_SESSION '%x-%x' "$EPOCHSECONDS" "$$"
export WOSWOAR_SESSION

# Commands matching this extended regex are never recorded. Note that bash's own
# HISTCONTROL/HISTIGNORE already apply for free -- anything bash declines to put
# in history is invisible to us -- so this is only for things you want in your
# local history but never written to a file that gets synced.
: "${WOSWOAR_IGNORE=(^|[[:space:]])[A-Za-z_]*(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY)=|--password[=[:space:]]|--token[=[:space:]]}"

#: Mirrors MAX_CMD_CHARS in woswoar/entry.py.
__woswoar_max=8000

__woswoar_armed=1
__woswoar_start=
__woswoar_lastnum=

# ---------------------------------------------------------------------------
# Hot path. Builtins only below this line.
# ---------------------------------------------------------------------------

# Escapes $1 into __woswoar_escaped. Mirrors escape() in woswoar/entry.py --
# these two implementations must agree exactly, which is what
# tests/test_shell_hook.py checks by running this function against the Python
# one over a shared corpus.
#
# Order matters: backslash first, or the escapes introduced below would
# themselves be escaped.
__woswoar_escape() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//$'\t'/\\t}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    __woswoar_escaped=$value
}

# Records when the current command started. $BASH_COMMAND is deliberately not
# used for the command text (see the header), only as a timing signal.
__woswoar_preexec() {
    [[ -n ${COMP_LINE:-} ]] && return 0 # tab completion, not a real command
    ((__woswoar_armed)) || return 0
    __woswoar_armed=0
    local now=${EPOCHREALTIME//[.,]/} # locale may use a comma, not a period
    __woswoar_start=${now%???}
}

__woswoar_precmd() {
    # Not `$?`. PROMPT_COMMAND entries run in order, and `$?` only holds the
    # user's exit status for the *first* of them -- after that it is the status
    # of the previous entry. Anything already in PROMPT_COMMAND (a title hook,
    # a prompt framework) therefore made every recorded command look successful.
    # __woswoar_stamp is prepended so it captures the real one before any of
    # that runs; the fallback covers a shell where PROMPT_COMMAND was replaced
    # wholesale after we loaded.
    local exit_code=${__woswoar_status:-$?}

    # Re-arm the timer whatever happens below. Recording deliberately does not
    # depend on the DEBUG trap having fired: if some other tool claims that trap
    # after we do, we lose the *duration* of a command, not the command. Gating
    # the whole function on it meant a single `trap ... DEBUG` anywhere later in
    # .bashrc silently turned recording off with nothing to show for it.
    __woswoar_armed=1

    # Empty HISTTIMEFORMAT for this one call so the output format is "  N  cmd"
    # regardless of the user's setting. Written as '' rather than a bare = so
    # that SC1007 can tell it is a deliberate override and not a typo.
    HISTTIMEFORMAT='' history 1 >"$__woswoar_scratch" 2>/dev/null || return 0

    # -d '' reads the whole file, newlines included, so multi-line commands
    # survive intact. It reports failure at EOF because it never finds the NUL
    # delimiter, which is expected -- check the payload, not the status.
    local raw
    IFS= read -r -d '' raw <"$__woswoar_scratch"
    [[ -n $raw ]] || return 0

    # `history 1` yields "  1631  the command", possibly spanning lines.
    raw=${raw#"${raw%%[![:space:]]*}"} # drop leading whitespace
    local num=${raw%%[![:digit:]]*}    # leading digits are the history number
    [[ -n $num ]] || return 0

    # If the number did not advance, bash never stored this command --
    # HISTCONTROL=ignorespace/ignoredups or HISTIGNORE rejected it, or the user
    # just pressed Enter. Respecting bash's own decision here means we never
    # need a second set of rules for the same thing. This check is also what
    # makes recording independent of the DEBUG trap: it, not a flag the trap
    # sets, is what distinguishes "a command ran" from "nothing happened".
    [[ $num == "$__woswoar_lastnum" ]] && { __woswoar_start=; return 0; }

    # Empty only at the very first prompt of a shell, where `history 1` is
    # whatever the previous session left in the history file rather than
    # anything typed here.
    local previous=$__woswoar_lastnum
    __woswoar_lastnum=$num
    [[ -n $previous ]] || { __woswoar_start=; return 0; }

    local cmd=${raw#"$num"}
    cmd=${cmd#"${cmd%%[![:space:]]*}"} # drop the separator spaces
    cmd=${cmd%$'\n'}
    [[ -n $cmd ]] || { __woswoar_start=; return 0; }

    if [[ -n $WOSWOAR_IGNORE && $cmd =~ $WOSWOAR_IGNORE ]]; then
        __woswoar_start=
        return 0
    fi

    # -1 means "unknown", which is what a lost DEBUG trap leaves behind. Cleared
    # on every path out of this function so one skipped command's start time
    # cannot be charged to the next one.
    local duration=-1
    if [[ -n $__woswoar_start ]]; then
        local now=${EPOCHREALTIME//[.,]/}
        duration=$((${now%???} - __woswoar_start))
    fi
    __woswoar_start=

    if ((${#cmd} > __woswoar_max)); then
        cmd=${cmd:0:__woswoar_max}'...[truncated]'
    fi

    __woswoar_escape "$cmd"
    cmd=$__woswoar_escaped

    # Store paths under $HOME as ~/... -- the prefix would otherwise repeat on
    # nearly every line. Matched anchored rather than with ${PWD/#$HOME/~},
    # which would rewrite /home/martinuscopy into ~copy when $HOME is
    # /home/martinus. The ~ means the *recording* user's home, so it is not
    # expanded on read; see woswoar/entry.py.
    local cwd=$PWD
    if [[ $cwd == "$HOME" ]]; then
        cwd='~'
    elif [[ -n $HOME && $cwd == "$HOME"/* ]]; then
        cwd='~'${cwd#"$HOME"}
    fi
    __woswoar_escape "$cwd"
    cwd=$__woswoar_escaped

    local day
    printf -v day '%(%F)T' -1 # local time, matching store.day_for()

    # One O_APPEND write. Linux serialises these under the inode lock, so
    # concurrent shells on this host cannot interleave lines.
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$EPOCHSECONDS" "$WOSWOAR_SESSION" "$cwd" "$exit_code" "$duration" "$cmd" \
        >>"$__woswoar_logdir/$day.tsv" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Ctrl-R. Forks freely -- it runs on a keypress, not on every prompt.
# ---------------------------------------------------------------------------

__woswoar_widget() {
    local selection
    selection=$(woswoar search --scope "${WOSWOAR_SCOPE:-global}" --query "$READLINE_LINE" \
        </dev/tty) || return 0
    [[ -n $selection ]] || return 0
    READLINE_LINE=$selection
    READLINE_POINT=${#READLINE_LINE}
}

# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

# Chain onto whatever already owns the DEBUG trap rather than replacing it.
# Terminal-title hooks, bash-preexec, atuin and ble.sh all live there, and a
# bare `trap ... DEBUG` silently breaks whichever of them loaded first -- the
# same reasoning that already guards the EXIT trap above.
#
# The wiring is deferred to the first prompt, which is not fussiness: a sourced
# file cannot see the DEBUG trap at all. `trap -p DEBUG` reports nothing from
# inside one (bash gives sourced files their own trap scope), so from here we
# could neither chain onto an existing handler nor even notice we were about to
# replace one. A PROMPT_COMMAND *string* runs at top level, where it can. The
# delay is a feature: by the first prompt the whole of .bashrc has run, so we
# chain onto whoever actually ended up owning the trap rather than whoever
# happened to load before us -- which makes the order of the lines in .bashrc
# stop mattering.
__woswoar_prior_debug=

__woswoar_chained_debug() {
    # The previous owner goes first, so it still sees the exit status of the
    # command before this one; title hooks routinely read it. $BASH_COMMAND
    # survives the extra stack frame -- bash holds it for the whole handler.
    eval "$__woswoar_prior_debug"
    __woswoar_preexec
}

__woswoar_wire_debug() {
    local spec=
    [[ -s $__woswoar_scratch ]] && IFS= read -r -d '' spec <"$__woswoar_scratch"

    __woswoar_prior_debug=
    if [[ -n $spec ]]; then
        # `trap -p` prints a command that would restore the trap, with the
        # handler requoted. Running that with `trap` shadowed recovers it
        # exactly; hand-written unquoting mangles embedded quotes, and handlers
        # containing quotes are the common case rather than the exotic one.
        # shellcheck disable=SC2329  # invoked indirectly, by the eval below
        trap() {
            if [[ $1 == -- ]]; then
                __woswoar_prior_debug=$2
            else
                __woswoar_prior_debug=$1
            fi
        }
        eval "$spec"
        unset -f trap
    fi

    # Never chain onto ourselves: re-sourcing the hook would otherwise nest a
    # handler inside itself once per source.
    if [[ -z $__woswoar_prior_debug || $__woswoar_prior_debug == *__woswoar* ]]; then
        trap '__woswoar_preexec' DEBUG
    else
        trap '__woswoar_chained_debug' DEBUG
    fi
}

#: Runs at every prompt but does its work once. A string, not a function,
#: because only a string element is evaluated at top level where `trap -p` sees
#: anything. Placed before __woswoar_precmd so both can share the scratch file.
# shellcheck disable=SC2016  # single quotes are the point: this expands later
__woswoar_boot='[[ -n ${__woswoar_wired:-} ]] || {
    __woswoar_wired=1
    builtin trap -p DEBUG >"$__woswoar_scratch" 2>/dev/null
    __woswoar_wire_debug
}'

#: Prepended, so it runs before every other PROMPT_COMMAND entry -- including
#: ones that were already there. A bare assignment, so it cannot be mistaken for
#: a command the user typed, and it is the only part that has to go first.
__woswoar_stamp='__woswoar_status=$?'

# The rest is appended: anything else in PROMPT_COMMAND then runs before us, so
# its own commands cannot be mistaken for something the user typed.
__woswoar_attrs=
if ((BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1))); then
    __woswoar_attrs=${PROMPT_COMMAND@a}
fi
if [[ $__woswoar_attrs == *a* ]]; then
    PROMPT_COMMAND=("$__woswoar_stamp" "${PROMPT_COMMAND[@]}" "$__woswoar_boot" __woswoar_precmd)
elif [[ -z ${PROMPT_COMMAND:-} ]]; then
    PROMPT_COMMAND=$__woswoar_stamp$'\n'$__woswoar_boot$'\n'__woswoar_precmd
else
    PROMPT_COMMAND=$__woswoar_stamp$'\n'${PROMPT_COMMAND%$'\n'}$'\n'$__woswoar_boot$'\n'__woswoar_precmd
fi
unset -v __woswoar_attrs

if [[ -z ${WOSWOAR_NO_BIND:-} ]]; then
    bind -m emacs -x '"\C-r": __woswoar_widget' 2>/dev/null
    bind -m vi-insert -x '"\C-r": __woswoar_widget' 2>/dev/null
    bind -m vi-command -x '"\C-r": __woswoar_widget' 2>/dev/null
fi

__WOSWOAR_LOADED=1
