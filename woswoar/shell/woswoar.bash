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

# 077 so the whole chain is owner-only from the moment it exists. `mkdir -p`
# would otherwise honour the ambient umask, which is 022 almost everywhere, and
# these files hold more than ~/.bash_history does -- which bash creates 0600.
#
# Only creation happens here. Re-tightening a tree from an older woswoar is
# `woswoar install`'s job: a recursive chmod is proportional to the number of
# days recorded, and this runs on every interactive shell.
#: Per-shell scratch, kept out of the data directory's root so that a file left
#: behind by a shell whose EXIT trap was already taken is contained and obvious.
__woswoar_run=$__woswoar_dir/run
(umask 077 && mkdir -p "$__woswoar_logdir" "$__woswoar_run") 2>/dev/null || return 0

# Scratch file for capturing `history 1`, always in a directory only this user
# can write. $TMPDIR and /tmp are never used: the name is just a pid, so in a
# world-writable directory a stranger can pre-create it, and then either the
# create below fails (recording silently off) or -- without
# fs.protected_regular -- their mode-666 file survives our O_TRUNC, because
# truncating changes neither owner nor mode, and they read every command. See
# issue #24; both need a directory a stranger can write in.
#
# Not `mktemp`: it costs an exec in every interactive shell, and once the
# directory is ours an unpredictable name buys nothing.
#
# XDG_RUNTIME_DIR is preferred but not trusted. It is a per-user 0700 tmpfs that
# systemd clears at logout, so a shell killed with -9 leaves nothing at all --
# and it is also just an inherited variable. `su` passes on the *calling* user's,
# which this uid cannot write, and returning there would switch recording off
# for the whole shell without a word: the very failure above, from the other
# direction. So a failure falls back rather than gives up.
#
# The fallback is disk-backed where a runtime dir is tmpfs, and the capture step
# reads and writes this file on every command. Measured on btrfs: 306us per
# command with a runtime dir, 339us without. Accepted -- it only applies to
# shells that have no XDG_RUNTIME_DIR at all, and 33us is not a keypress.
__woswoar_scratch=${XDG_RUNTIME_DIR:-$__woswoar_run}/woswoar-hist.$$
if ! (umask 077 && : >"$__woswoar_scratch") 2>/dev/null; then
    __woswoar_scratch=$__woswoar_run/woswoar-hist.$$
    (umask 077 && : >"$__woswoar_scratch") 2>/dev/null || return 0
fi

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
#
# `docs/shell-integration.md` owns the catalogue of what this catches and, more
# importantly, what it does not. Only what a reader of the regex itself needs is
# here.
#
# KEY, PASS and AUTH are matched only after `_` (`AWS_ACCESS_KEY_ID=`), never
# bare, or `MONKEY=`, `KEYS=` and `PASSAGE=` would all be dropped. TOKEN,
# SECRET, PASSW, CREDENTIAL and APIKEY are distinctive enough to match anywhere
# in the name, which is what catches `PGPASSWORD=` and `AWS_SECRET_ACCESS_KEY=`.
#
# Two shapes here are deliberate and easy to "tidy" into a bug:
#
# 1. No `(^|[[:space:]])[A-Za-z0-9_]*` in front of the assignment keywords, even
#    though that is the obvious way to say "this is a variable name". It costs
#    more than twice as much -- the leading `*` retries at every position -- and
#    buys nothing, because `[A-Za-z0-9_]*=` cannot cross a space anyway.
#
# 2. `--([a-z]+-)*` rather than `--[a-z-]*` before the option keywords. The
#    flat `[a-z-]*` is quadratic in the length of a `[a-z-]` run: it can start
#    an attempt at every interior `-`, and each attempt rescans the rest. A
#    command of 8000 dashes measured 654ms -- on the prompt path, before the
#    8000-char truncation below. Requiring a letter between dashes makes it
#    linear: the same input is 0.6ms.
#
# `[[ =~ ]]` recompiles on every command (bash caches nothing), so cost tracks
# the pattern's *length*, not how much of it can match. Measured here: 30us for
# the four-keyword pattern this replaces, 54us for this one, against a 288us ->
# 324us record path. Weigh anything added against that.
: "${WOSWOAR_IGNORE=(TOKEN|SECRET|PASSW|CREDENTIAL|APIKEY|_KEY|_PASS|_AUTH)[A-Za-z0-9_]*=|--([a-z]+-)*((passw|token|secret|credential)[a-z-]*|api-?key|access-?key|auth)([=[:space:]]|\$)|--from-literal=|://[^/[:space:]]+:[^/@[:space:]]+@|://(hooks\.slack\.com|discord(app)?\.com/api/webhooks)/[^[:space:]]|[Aa]uthorization:[[:space:]]*[A-Za-z]|(sshpass|htpasswd|openssl passwd)[[:space:]]|curl[^|;&]*[[:space:]]-u[[:space:]]|mysql[a-z]*[^|;&]*[[:space:]]-p[^-[:space:]]|docker login[^|;&]*[[:space:]]-p|ssh-keygen[^|;&]*[[:space:]]-N[[:space:]]}"

# The webhook alternative above measured +7.4us per command, 58.3us to 65.7us
# for the test itself against a record path of ~306us. Paid because the URL *is*
# the credential -- anyone holding it can post as that integration -- and because
# the hook is what protects commands typed from now on; `import` only covers what
# is already recorded. Sharing the `://` prefix with the rule before it measured
# no faster and reads worse, so the two stay separate.

# Appended, not merged into the default above, so that adding one rule of your
# own does not pin you to today's default forever. Overriding `WOSWOAR_IGNORE`
# outright means copying 450 characters into ~/.bashrc and never receiving the
# next fix to them -- and this very pattern grew because the old one missed
# `AWS_SECRET_ACCESS_KEY=`. Joined once at startup rather than tested as a
# second variable on every command, so an unused knob costs nothing.
if [[ -n ${WOSWOAR_IGNORE_EXTRA:-} ]]; then
    WOSWOAR_IGNORE=${WOSWOAR_IGNORE:+$WOSWOAR_IGNORE|}$WOSWOAR_IGNORE_EXTRA
fi

#: Mirrors MAX_CMD_CHARS in woswoar/entry.py.
__woswoar_max=8000

#: Seconds between prompt-triggered syncs; 0 turns them off entirely, which is
#: what to set if you run the systemd timer in `contrib/systemd` instead. The
#: two are safe together -- `sync.lock` is non-blocking, so whichever arrives
#: second exits at once -- but running both is just paying twice.
#:
#: Validated rather than trusted: this reaches `((...))`, where a value like
#: `x[$(...)]` is not a syntax error but a command substitution.
: "${WOSWOAR_SYNC_INTERVAL:=60}"
[[ $WOSWOAR_SYNC_INTERVAL =~ ^[0-9]+$ ]] || WOSWOAR_SYNC_INTERVAL=60

#: Shared between every shell on this machine, deliberately. Ten open terminals
#: with ten private clocks all come due at the same moment and fire ten syncs;
#: nine of them lose the lock instantly, but an interpreter start is ~40ms and
#: nine of those is real. See store.sync_stamp_file.
__woswoar_syncstamp=$__woswoar_dir/sync-stamp

#: Earliest $EPOCHSECONDS at which the stamp above is worth opening. Keeps the
#: per-command cost to one integer comparison: a stamp can only ever move
#: forward, so "not due until T" cannot become due sooner than T. Another
#: shell syncing in the meantime pushes T out, and the worst that costs is one
#: file read that decides against syncing.
__woswoar_next_sync=0

#: Set by preexec, consumed by precmd. Empty also means "no command has been
#: timed since the last prompt", which is what makes a separate armed flag
#: unnecessary: preexec only stamps when it is empty, so the first simple
#: command of a compound line wins and the rest are ignored.
__woswoar_start=
__woswoar_status=
__woswoar_lastnum=
#: The day whose log file is known to exist already. See __woswoar_precmd.
__woswoar_today=

# ---------------------------------------------------------------------------
# Hot path. Builtins only below this line.
#
# Two sections below are deliberately outside that rule and say so at their own
# headers: the Ctrl-R widget, which runs on a keypress, and the boot string,
# which runs once at the first prompt and then removes itself. Everything on the
# per-command path is builtins, and CI proves it by counting clones for 3
# commands and for 30.
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
    [[ -z $__woswoar_start ]] || return 0 # DEBUG refires per simple command
    local now=${EPOCHREALTIME//[.,]/}    # locale may use a comma, not a period
    __woswoar_start=${now%???}
}

__woswoar_precmd() {
    # Not `$?`. PROMPT_COMMAND entries run in order, and `$?` only holds the
    # user's exit status for the *first* of them -- after that it is the status
    # of the previous entry. Anything already in PROMPT_COMMAND (a title hook,
    # a prompt framework) therefore made every recorded command look successful.
    # __woswoar_status is set by a string prepended to PROMPT_COMMAND, so it
    # captures the real one before any of that runs. Consumed rather than
    # cached: leaving it set would make a shell whose PROMPT_COMMAND was
    # replaced wholesale record a *stale* code, which is worse than falling
    # back to whatever $? holds here.
    local exit_code=${__woswoar_status:-$?}
    __woswoar_status=

    # Recording first, so a command reaches the repository in the sync it
    # triggers rather than in the one after it -- which for someone who types
    # one command and walks away is the difference between a minute and never.
    #
    # Two functions rather than one because `__woswoar_record` returns early
    # from seven places -- an ignored command, a duplicate, a bare Enter -- and
    # a shell must still come due for a sync in every one of those cases.
    __woswoar_record "$exit_code"
    __woswoar_maybe_sync
}

__woswoar_record() {
    local exit_code=$1

    # Taken and cleared in one place, so every path out of this function is
    # covered by construction. Recording deliberately does not depend on the
    # DEBUG trap having fired: if some other tool claims that trap after we do,
    # we lose the *duration* of a command, not the command. Gating the whole
    # function on a flag the trap set meant a single `trap ... DEBUG` later in
    # .bashrc silently turned recording off with nothing to show for it.
    local start=$__woswoar_start
    __woswoar_start=

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
    [[ $num == "$__woswoar_lastnum" ]] && return 0

    # Empty only at the very first prompt of a shell, where `history 1` is
    # whatever the previous session left in the history file rather than
    # anything typed here.
    local previous=$__woswoar_lastnum
    __woswoar_lastnum=$num
    [[ -n $previous ]] || return 0

    local cmd=${raw#"$num"}
    cmd=${cmd#"${cmd%%[![:space:]]*}"} # drop the separator spaces
    cmd=${cmd%$'\n'}
    [[ -n $cmd ]] || return 0

    if [[ -n $WOSWOAR_IGNORE && $cmd =~ $WOSWOAR_IGNORE ]]; then
        return 0
    fi

    # -1 means "unknown", which is what a lost DEBUG trap leaves behind.
    local duration=-1
    if [[ -n $start ]]; then
        local now=${EPOCHREALTIME//[.,]/}
        duration=$((${now%???} - start))
    fi

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

    # A file's mode is set when it is created and never again, so the first
    # write of each day is the only moment this can be got right -- and `>>`
    # would create it 0644 under the usual umask. Guarded on the day string
    # rather than testing the file every time: this runs on every command, and
    # the answer changes once a day.
    if [[ $day != "$__woswoar_today" ]]; then
        __woswoar_today=$day
        # A file's mode is fixed when it is created, so this is the only moment
        # it can be got right -- `>>` alone would create it 0644 under the usual
        # umask. `>>` rather than `>` so a day already underway is never
        # truncated, which also removes the need to test for existence first.
        #
        # In a subshell, so the caller's umask is untouched no matter what: the
        # fork happens once per day, not once per command, and the guard above
        # keeps it off the hot path entirely. Saving and restoring the umask
        # inline instead would be fork-free, but reading it costs a fork at
        # startup anyway and a failed read would leave this *setting* the user's
        # umask to a guess -- loosening it, in the one case it must not.
        # `mkdir -p` as well, because the directory is otherwise only made when
        # the shell starts, and a shell outlives a lot. Delete the data
        # directory -- an uninstall, a home moved, a work machine's cleanup --
        # and every command in every open shell writes into nothing until each
        # of them is restarted. Once a day, in the subshell that was already
        # being forked, is the cheapest place to notice.
        (umask 077 && mkdir -p "$__woswoar_logdir" && : >>"$__woswoar_logdir/$day.tsv") \
            2>/dev/null
    fi

    # One O_APPEND write. Linux serialises these under the inode lock, so
    # concurrent shells on this host cannot interleave lines.
    #
    # `2>/dev/null` *before* the append, not after. Redirections are applied
    # left to right, so with it second bash reports a failed `>>` to a stderr it
    # has not redirected yet -- and prints
    # `bash: .../2026-08-03.tsv: No such file or directory` at the user's
    # prompt, after every command, which is how this was reported. Recording is
    # meant to fail silently; the whole hook is written that way.
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$EPOCHSECONDS" "$WOSWOAR_SESSION" "$cwd" "$exit_code" "$duration" "$cmd" \
        2>/dev/null >>"$__woswoar_logdir/$day.tsv"
}

# Fires a sync when one is due. Runs on every prompt, and forks on roughly one
# prompt a minute -- which is the one deliberate exception to the builtins-only
# rule above, and the reason `test_it_syncs_once_per_interval_not_once_per_command`
# exists beside the strict fork-count test rather than replacing it.
#
# The steady-state cost is the single `((...))` below. Everything that touches
# the filesystem is behind it.
__woswoar_maybe_sync() {
    ((WOSWOAR_SYNC_INTERVAL > 0 && EPOCHSECONDS >= __woswoar_next_sync)) || return 0

    # `2>/dev/null` *before* the `<`, not after. Redirections are applied left
    # to right, so the other order reports a missing stamp to a stderr that has
    # not been redirected yet -- and prints a path at the user's prompt after
    # every command. That is not hypothetical: it is the 0.4.1 bug, in the
    # append a few lines above, written the other way round.
    local last=
    read -r last 2>/dev/null <"$__woswoar_syncstamp"
    # A torn or truncated write must not reach `((...))`, where a non-numeric
    # value is a variable name to dereference rather than an error.
    last=${last//[^0-9]/}
    last=${last:-0}

    if ((EPOCHSECONDS - last < WOSWOAR_SYNC_INTERVAL)); then
        __woswoar_next_sync=$((last + WOSWOAR_SYNC_INTERVAL))
        return 0
    fi

    # Set before the check below rather than in both of its arms: coming due
    # and finding nothing to do still costs a `stat` on every command until the
    # interval is put back.
    __woswoar_next_sync=$((EPOCHSECONDS + WOSWOAR_SYNC_INTERVAL))

    # Nothing to sync with, so nothing to fork for. Checked here rather than
    # once at startup because `woswoar init` may well be the command that just
    # ran, and a per-shell flag would leave that shell never syncing.
    [[ -d $__woswoar_dir/history ]] || return 0

    # `( ... & )`: the subshell backgrounds the sync and exits immediately, so
    # the prompt waits for two forks (~1ms) and never for the sync itself. A
    # plain `&` here would put it in this shell's job table and print `[1] 1234`
    # and `[1]+ Done` at the prompt once a minute, forever.
    #
    # The stamp is written *inside* that subshell, which is why it costs no
    # extra fork -- and under `umask 077`, because a file's mode is fixed when
    # it is created and `>` would otherwise make it 0644 for `doctor` to flag.
    #
    # Stamped before the sync runs, not after it succeeds: a `woswoar` that is
    # not on PATH must cost one attempt a minute, not one per command.
    (
        umask 077
        printf '%s\n' "$EPOCHSECONDS" >"$__woswoar_syncstamp"
        woswoar sync >/dev/null 2>&1 </dev/null &
    ) 2>/dev/null
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
__woswoar_wired=

__woswoar_wire() {
    [[ -n $__woswoar_wired ]] && return 0
    __woswoar_wired=1

    # ble.sh does not use either of the mechanisms below. It replaces bash's
    # prompt machinery outright: PROMPT_COMMAND runs once, at the prompt where
    # .bashrc finishes, and never again, and the DEBUG trap fires only on
    # ble.sh's own internals rather than on anything the user typed. Wiring the
    # trap there records nothing at all -- silently, since every part looks
    # installed. It offers `blehook` instead, which is a better fit anyway:
    # PREEXEC is handed the command line as $1, and PRECMD sees the real $?.
    if [[ -n ${BLE_VERSION-} ]]; then
        blehook PREEXEC+=__woswoar_preexec
        blehook PRECMD+=__woswoar_precmd
        return 0
    fi

    # The spec arrives as an argument, from a command substitution in the
    # PROMPT_COMMAND string. It used to travel through the scratch file, which
    # meant a file that could not be written produced an *empty* spec -- and an
    # empty spec is indistinguishable from "there was no prior trap", so the
    # hook replaced whoever owned it and that tool silently stopped working for
    # the life of the shell. A missing file must not be able to look like an
    # absent trap. Taking the `eval` below's input off disk is the second
    # reason, and after #24 only defence in depth.
    # The only caller that legitimately passes nothing is the ble.sh branch
    # above, which has already returned. So an empty argument here means the
    # command substitution in the boot string failed to fork -- and empty is
    # otherwise indistinguishable from "there is no prior trap", which would
    # clobber another tool's handler: the same silent failure by a different
    # route. `printf .` gives "I could not look" an answer of its own, and the
    # answer to that is to install nothing rather than to guess.
    local spec=${1-}
    [[ -n $spec ]] || return 0
    spec=${spec%.}

    # `trap -p` prints a command that would restore the trap, with the handler
    # requoted -- `trap -- 'handler' DEBUG`. Re-splitting that as an array
    # unquotes it exactly, which hand-written unquoting does not: handlers
    # containing quotes are the common case, not the exotic one. Deliberately
    # *not* done by shadowing the `trap` builtin with a function and re-running
    # the spec: a shell function is process-global, and ble.sh ships its own
    # `trap` wrapper, so defining and unsetting one would silently disable
    # ble.sh's trap manager for the rest of the session.
    local -a parts=()
    [[ -n $spec ]] && eval "parts=($spec)"
    local prior=${parts[1]-}
    [[ $prior == -- ]] && prior=${parts[2]-}

    # Never chain onto ourselves: otherwise re-wiring would nest a handler
    # inside itself.
    if [[ -z $prior || $prior == *__woswoar* ]]; then
        trap '__woswoar_preexec' DEBUG
    else
        # Composed once, here, rather than by a wrapper function that evals the
        # prior handler on every command: same behaviour for ~10us less per
        # command, and $BASH_COMMAND is identical either way. The prior owner
        # goes first so it still sees the exit status of the command before this
        # one, and the separator is a newline rather than `;` so a handler that
        # ends in a comment still works.
        trap "$prior"$'\n''__woswoar_preexec' DEBUG
    fi
}

#: Removes itself from PROMPT_COMMAND once the trap is wired, so the steady
#: state costs nothing. Leaving it in place measured ~12us on every command for
#: the life of the shell -- 8% of the hook's budget to re-test a flag.
__woswoar_unboot() {
    local entry
    if [[ ${PROMPT_COMMAND@a} == *a* ]]; then
        local -a kept=()
        for entry in "${PROMPT_COMMAND[@]}"; do
            [[ $entry == "$__woswoar_boot" ]] || kept+=("$entry")
        done
        PROMPT_COMMAND=("${kept[@]}")
    else
        # shellcheck disable=SC2178  # PROMPT_COMMAND is a string on this branch
        PROMPT_COMMAND=${PROMPT_COMMAND/"$__woswoar_boot"$'\n'/}
    fi
}

#: Runs at the first prompt and then deletes itself. A string, not a function,
#: because only a string element is evaluated at top level, and `trap -p DEBUG`
#: reports nothing from inside a function or a sourced file. A command
#: substitution is not a function: it forks, but it reads the parent's trap, and
#: it runs here at top level where there is a trap to read.
#:
#: That fork is the one exception to the builtins-only rule above, so it is
#: guarded twice over. `__woswoar_unboot` removes this entry after the first
#: prompt -- and the `__woswoar_wired` test in front of the substitution means
#: that even where removal fails, the fork is paid once rather than on every
#: prompt. Removal *can* fail: it is an exact match on a variable other prompt
#: frameworks rewrite freely, and it was harmless when this string only wrote a
#: file. Measured with removal broken: 6 clones for 3 commands and for 30 with
#: the guard, 9 and 36 without.
#:
#: The guard also pays for itself under ble.sh, where `__woswoar_wire` has
#: already run at source time and the substitution would fork only to have its
#: argument discarded.
#:
#: Ordered before `__woswoar_precmd` rather than last for a related reason: the
#: string branch removes this text plus the newline after it, so a boot with
#: nothing following it is never removed at all.
#:
#: One cost, once per shell: under `set -T` a prior DEBUG handler sees the
#: substitution as a command it did not run.
# shellcheck disable=SC2016  # single quotes are the point: this expands later
__woswoar_boot='{
    [[ -n $__woswoar_wired ]] || __woswoar_wire "$(builtin trap -p DEBUG 2>/dev/null; printf .)"
    __woswoar_unboot
}'

#: Prepended, so it runs before every other PROMPT_COMMAND entry -- including
#: ones that were already there. It has to restore the status it just read:
#: an assignment succeeds, so leaving it at 0 would hand every *other* entry the
#: same wrong `$?` this hook exists to stop getting itself. `return` is a
#: builtin, so this stays fork-free. Same approach as bash-preexec.
__woswoar_ret() { return "$1"; }
# shellcheck disable=SC2016  # single quotes are the point: this expands later
__woswoar_stamp='__woswoar_status=$?; __woswoar_ret "$__woswoar_status"'

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

# Under ble.sh, PROMPT_COMMAND may run only once ever, so the boot entry above
# is not something to rely on. ble.sh is normally already loaded by the time
# .bashrc reaches us, so wire straight away when it is; `__woswoar_wire` is
# idempotent, and boot still covers the case where ble.sh loads after us.
if [[ -n ${BLE_VERSION-} ]]; then
    __woswoar_wire
fi

if [[ -z ${WOSWOAR_NO_BIND:-} ]]; then
    bind -m emacs -x '"\C-r": __woswoar_widget' 2>/dev/null
    bind -m vi-insert -x '"\C-r": __woswoar_widget' 2>/dev/null
    bind -m vi-command -x '"\C-r": __woswoar_widget' 2>/dev/null
fi

# No `__woswoar_maybe_sync` call here, deliberately. Bash runs PROMPT_COMMAND
# once before the *first* prompt, so a shell where nobody has typed anything yet
# has already been through `__woswoar_precmd` and has already synced -- which is
# what makes sitting down and pressing Ctrl-R show current history. An explicit
# call at the end of this file was written first and is redundant; a mutation
# that deleted it changed no behaviour, which is how that was noticed.

__WOSWOAR_LOADED=1
