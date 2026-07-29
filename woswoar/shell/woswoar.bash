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
# but forks. The redirect-and-read pair is faithful, fork-free, and ~28us.

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

# Identifies this shell for `--scope session`. EPOCHREALTIME plus the pid is
# unique per host without spawning uuidgen.
printf -v WOSWOAR_SESSION '%s-%s' "${EPOCHREALTIME//[.,]/}" "$$"
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
    local exit_code=$?

    if ((__woswoar_armed)); then
        # No command ran since the last prompt (empty line, or Ctrl-C at an
        # empty prompt). Nothing to record.
        return 0
    fi
    __woswoar_armed=1

    HISTTIMEFORMAT= history 1 >"$__woswoar_scratch" 2>/dev/null || return 0

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
    # need a second set of rules for the same thing.
    [[ $num == "$__woswoar_lastnum" ]] && return 0
    __woswoar_lastnum=$num

    local cmd=${raw#"$num"}
    cmd=${cmd#"${cmd%%[![:space:]]*}"} # drop the separator spaces
    cmd=${cmd%$'\n'}
    [[ -n $cmd ]] || return 0

    if [[ -n $WOSWOAR_IGNORE && $cmd =~ $WOSWOAR_IGNORE ]]; then
        return 0
    fi

    local duration=-1
    if [[ -n $__woswoar_start ]]; then
        local now=${EPOCHREALTIME//[.,]/}
        duration=$((${now%???} - __woswoar_start))
        __woswoar_start=
    fi

    if ((${#cmd} > __woswoar_max)); then
        cmd=${cmd:0:__woswoar_max}'...[truncated]'
    fi

    __woswoar_escape "$cmd"
    cmd=$__woswoar_escaped
    __woswoar_escape "$PWD"
    local cwd=$__woswoar_escaped

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

trap '__woswoar_preexec' DEBUG

# Append rather than prepend: anything else in PROMPT_COMMAND runs before us, so
# its own commands cannot be mistaken for something the user typed.
__woswoar_attrs=
if ((BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1))); then
    __woswoar_attrs=${PROMPT_COMMAND@a}
fi
if [[ $__woswoar_attrs == *a* ]]; then
    PROMPT_COMMAND+=(__woswoar_precmd)
elif [[ -z ${PROMPT_COMMAND:-} ]]; then
    PROMPT_COMMAND=__woswoar_precmd
else
    PROMPT_COMMAND=${PROMPT_COMMAND%$'\n'}$'\n'__woswoar_precmd
fi
unset -v __woswoar_attrs

if [[ -z ${WOSWOAR_NO_BIND:-} ]]; then
    bind -m emacs -x '"\C-r": __woswoar_widget' 2>/dev/null
    bind -m vi-insert -x '"\C-r": __woswoar_widget' 2>/dev/null
    bind -m vi-command -x '"\C-r": __woswoar_widget' 2>/dev/null
fi

__WOSWOAR_LOADED=1
