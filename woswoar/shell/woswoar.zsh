# woswoar - zsh integration
#
# Source this from ~/.zshrc:
#     source /path/to/woswoar.zsh
#
# Requires zsh 5.0+ for the `zsh/datetime` module and `add-zsh-hook`.
#
# Design constraint: recording runs on *every* prompt, so it must not fork and
# must not exec. Everything below the startup section uses builtins only, and CI
# counts clones for 3 commands and for 30 to prove it.
#
# This file is about sixty lines shorter than woswoar.bash, and that is the
# point. zsh hands `preexec` the command line as $1 with newlines intact, so
# there is no `history 1` round trip, and with it go the scratch file, the
# XDG_RUNTIME_DIR fallback, the EXIT trap that cleans it up, and the parsing of
# a leading history number. $2 is *not* the same thing -- it renders a function
# definition as `f () { ... }`, which is data loss -- so $1 is used throughout.
#
# What zsh does not hand over is bash's "did the history number advance", which
# is how woswoar.bash defers to HISTCONTROL and HISTIGNORE for free. There is no
# equivalent: $HISTCMD moves *backwards* over a line zsh declined to store, and
# $history[$HISTCMD-1] still holds a space-hidden command while this runs. So
# the rules are reimplemented in __woswoar_preexec. docs/shell-integration.md
# lists which ones, and says plainly which are not mirrored.

[[ -n ${__WOSWOAR_LOADED:-} ]] && return 0
[[ -o interactive ]] || return 0

if (( ${ZSH_VERSION%%.*} < 5 )); then
    printf 'woswoar: zsh 5.0+ required, found %s; history recording disabled\n' \
        "$ZSH_VERSION" >&2
    return 0
fi

# ---------------------------------------------------------------------------
# Startup. Forks here are fine; they happen once per shell.
#
# Deliberately *not* opened with `emulate -L zsh`. The -L scopes options to the
# enclosing function, and a sourced file is not one: verified on 5.9, a `setopt`
# inside a sourced file is still set after the source returns, so emulating here
# would silently rewrite the user's options for the life of the shell. Every
# function below emulates for itself instead, and this section sticks to
# constructs that do not care which options are set.
# ---------------------------------------------------------------------------

# $EPOCHSECONDS, $EPOCHREALTIME and a builtin `strftime`, none of which fork.
# Its own error is left visible: a zsh that cannot load a shipped module is
# broken in a way worth hearing about, and recording cannot work without it.
zmodload zsh/datetime || return 0

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
# these files hold more than ~/.zsh_history does -- which zsh creates 0600.
#
# Only creation happens here. Re-tightening a tree from an older woswoar is
# `woswoar install`'s job: a recursive chmod is proportional to the number of
# days recorded, and this runs on every interactive shell.
(umask 077 && mkdir -p "$__woswoar_logdir") 2>/dev/null || return 0

# Identifies this shell for `--scope session`. Start second plus pid, both in
# hex, exactly as woswoar.bash builds it -- a bash started inside a zsh
# overwrites this rather than inheriting it, so nesting yields a fresh id.
#
# `[##16]` is arithmetic, so this forks nothing. The (L) is not cosmetic:
# `[##16]` yields upper case, and the session column is documented as lower-case
# hex.
WOSWOAR_SESSION="${(L)$(( [##16] EPOCHSECONDS ))}-${(L)$(( [##16] $$ ))}"
export WOSWOAR_SESSION

# Commands matching this extended regex are never recorded.
#
# Unlike bash, none of zsh's own history rules apply for free here -- see
# __woswoar_preexec, which reimplements them -- but this pattern is a separate
# thing regardless: it is for what you want in your local history and never in a
# file that gets synced.
#
# The value is byte-identical to woswoar.bash's and must stay that way. Every
# alternative in it was argued for and measured there, and that is where the
# reasoning lives; `tests/test_credentials.py`'s `TestParityWithTheHook` holds
# *every* hook against `credentials._SHARED`, so a rule can never be added to
# one shell alone -- nor to both shells and not to the importer.
: "${WOSWOAR_IGNORE=(TOKEN|SECRET|PASSW|CREDENTIAL|APIKEY|_KEY|_PASS|_AUTH)[A-Za-z0-9_]*=|--([a-z]+-)*((passw|token|secret|credential)[a-z-]*|api-?key|access-?key|auth)([=[:space:]]|\$)|--from-literal=|://[^/[:space:]]+:[^/@[:space:]]+@|://(hooks\.slack\.com|discord(app)?\.com/api/webhooks)/[^[:space:]]|[Aa]uthorization:[[:space:]]*[A-Za-z]|(sshpass|htpasswd|openssl passwd)[[:space:]]|curl[^|;&]*[[:space:]]-u[[:space:]]|mysql[a-z]*[^|;&]*[[:space:]]-p[^-[:space:]]|docker login[^|;&]*[[:space:]]-p|ssh-keygen[^|;&]*[[:space:]]-N[[:space:]]}"

# Appended, not merged into the default above, so that adding one rule of your
# own does not pin you to today's default forever.
if [[ -n ${WOSWOAR_IGNORE_EXTRA:-} ]]; then
    WOSWOAR_IGNORE=${WOSWOAR_IGNORE:+$WOSWOAR_IGNORE|}$WOSWOAR_IGNORE_EXTRA
fi

#: Mirrors MAX_CMD_CHARS in woswoar/entry.py.
__woswoar_max=8000

#: Seconds between prompt-triggered syncs; 0 turns them off entirely. Validated
#: rather than trusted, because the value is inherited -- whatever started your
#: shell chooses it -- and it reaches `((...))`.
#:
#: The hazard is *not* the one woswoar.bash guards, and the difference is worth
#: writing down rather than copying the comment across. bash runs a command
#: substitution it finds in an arithmetic array subscript, so `x[$(...)]` there
#: is code execution. zsh does not: verified, `last='x[$(touch f)]'` followed by
#: `(( 1 - last ))` creates nothing, where the same two lines under bash create
#: the file. What zsh does instead is print `bad math expression` -- on the
#: shell's *own* stderr, so at the user's prompt after every command -- and
#: abandon the assignment, which leaves this shell not syncing either. That is
#: the shape of the bug 0.4.1 shipped, and reason enough on its own.
#:
#: "Nothing is left once the digits are removed" rather than woswoar.bash's
#: `=~ ^[0-9]+$`. A match sets $MATCH, $MBEGIN and $MEND, and this line runs at
#: file scope where there is no function to make them local -- so the regex form
#: leaves three parameters behind in the user's shell. Same answer, no residue,
#: and character classes need no options to be set a particular way.
#:
#: Then normalised through `10#`, because this line runs at file scope, under
#: whatever the user set -- and with `setopt octal_zeroes` a perfectly
#: reasonable `WOSWOAR_SYNC_INTERVAL=08` is `bad math expression: operator
#: expected` at every prompt, while `010` quietly means eight. That is the shape
#: of the bug 0.4.1 shipped. The same prefix inside a function would be dead
#: code: `emulate -L zsh` turns the option off there.
[[ -n ${WOSWOAR_SYNC_INTERVAL:-} && -z ${WOSWOAR_SYNC_INTERVAL//[0-9]/} ]] \
    || WOSWOAR_SYNC_INTERVAL=60
WOSWOAR_SYNC_INTERVAL=$((10#$WOSWOAR_SYNC_INTERVAL))

#: Shared between every shell on this machine, deliberately: ten open terminals
#: with ten private clocks would sync ten times an hour between them rather than
#: once. See store.sync_stamp_file.
__woswoar_syncstamp=$__woswoar_dir/sync-stamp

#: Earliest $EPOCHSECONDS at which the stamp above is worth opening. Keeps the
#: per-command cost to one integer comparison.
typeset -gi __woswoar_next_sync=0

#: Set by preexec, consumed by precmd. The two move together, so unlike the bash
#: hook there is no "the command ran but we never timed it" case and no -1
#: duration: bash can lose its DEBUG trap to another tool, and there is nothing
#: to lose here.
__woswoar_cmd=
typeset -gi __woswoar_start=0
#: The last command line zsh stored, which is what HIST_IGNORE_DUPS compares
#: against. Not "the last command recorded": one dropped by $WOSWOAR_IGNORE is
#: still in zsh's history, so a repeat of the command before it is not a
#: duplicate.
__woswoar_last=
#: The day whose log file is known to exist already. See __woswoar_record.
__woswoar_today=

# ---------------------------------------------------------------------------
# Hot path. Builtins only below this line.
#
# The Ctrl-R widget is the one deliberate exception and says so at its own
# header: it runs on a keypress, not on every prompt.
# ---------------------------------------------------------------------------

# Escapes $1 into __woswoar_escaped. Mirrors escape() in woswoar/entry.py --
# these implementations must agree exactly, which is what tests/test_shell_hook.py
# checks by running each of them against the Python one over one shared corpus.
#
# Character-for-character the body in woswoar.bash, deliberately: the two shells
# agree on all four substitutions, so keeping one text is one less thing that can
# drift. It carries no `emulate` of its own because its only caller has already
# emulated, and adding one would make the two bodies differ for no gain.
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

# Receives the command line as $1. Fires for every line, including the ones zsh
# has already decided not to keep -- which is what the middle of this function
# is about.
__woswoar_preexec() {
    # Read before `emulate`, which turns EXTENDED_GLOB off. $HISTORY_IGNORE is
    # the user's pattern, written in whichever glob dialect their options select;
    # matching it in a different dialect than zsh itself used would record a
    # command they told zsh to forget. Verified: `^echo*` matches `ls -l` only
    # with EXTENDED_GLOB on.
    local -i extglob=0
    [[ -o extended_glob ]] && extglob=1

    # Localises options for this function: NO_CLOBBER (which would stop the
    # append below ever creating a day file), KSH_ARRAYS, SH_WORD_SPLIT,
    # EXTENDED_GLOB, WARN_CREATE_GLOBAL -- which would otherwise print a line
    # about `__woswoar_escaped` at the prompt -- and REMATCH_PCRE, which would
    # reinterpret $WOSWOAR_IGNORE. It does *not* touch the HIST_* options, which
    # is what makes the tests below work -- checked, not assumed.
    emulate -L zsh
    (( extglob )) && setopt extended_glob

    local cmd=$1

    # HIST_IGNORE_SPACE is how a person hides a secret from history, and in
    # woswoar a missed one is published to a git repository -- so this is the
    # first rule applied and it errs towards dropping: any leading whitespace,
    # not just the space zsh itself looks for.
    [[ -o hist_ignore_space && $cmd == [[:space:]]* ]] && return 0

    # zsh's signal that the history mechanism was inactive: $1 arrives empty.
    # Not the bare-Enter case, which never reaches here at all -- `preexec` does
    # not fire for a blank line, checked against 5.9 rather than assumed.
    [[ -n ${cmd//[[:space:]]/} ]] || return 0

    # Both options mean the same thing to a log that only ever appends: do not
    # store this line if it repeats the one before it.
    if [[ -o hist_ignore_dups || -o hist_ignore_all_dups ]]; then
        [[ $cmd == "$__woswoar_last" ]] && return 0
    fi

    # `${~}` interprets the value as a pattern. It is not `eval`: no command
    # substitution is reachable through it, which
    # `test_a_history_ignore_holding_a_command_substitution_runs_nothing` pins.
    if [[ -n ${HISTORY_IGNORE-} && $cmd == ${~HISTORY_IGNORE} ]]; then
        return 0
    fi

    __woswoar_last=$cmd
    __woswoar_cmd=$cmd

    # $EPOCHREALTIME is "seconds.fraction" with *ten* fractional digits where
    # bash gives six, so woswoar.bash's "strip the last three characters"
    # (`${now%???}`) is wrong here by a factor of 10,000. zsh's own float
    # arithmetic does not care how many digits there are, and a double carries
    # ~15.9 significant ones against the 13 this needs.
    #
    # The comma substitution is not dead code for a comma-decimal locale. zsh 5.9
    # formats this parameter with a period whatever LC_NUMERIC says -- bash does
    # not -- but a comma reaching `$((...))` is the *comma operator*, which would
    # quietly evaluate to the fraction and call it a timestamp. Free, and the
    # alternative is a silent 1000x duration on some future zsh.
    (( __woswoar_start = ${EPOCHREALTIME//,/.} * 1000 ))
}

__woswoar_precmd() {
    # The first line, before anything else. `emulate` clobbers $? -- verified:
    # a function reading it after emulating sees 0 for a command that failed,
    # which is the same "every command looks successful" bug woswoar.bash
    # prepends an entry to PROMPT_COMMAND to avoid.
    local exit_code=$?

    emulate -L zsh

    # Recording first, so a command reaches the repository in the sync it
    # triggers rather than in the one after it. Two functions rather than one
    # because `__woswoar_record` returns early from several places and a shell
    # must still come due for a sync in every one of them.
    __woswoar_record "$exit_code"
    __woswoar_maybe_sync

    # zsh restores $? for each precmd_functions entry, so a hook registered
    # after this one sees the user's status whatever we return -- observed on
    # 5.9, but observed is not promised. Returning it explicitly costs nothing
    # and makes the hook correct either way.
    return $exit_code
}

__woswoar_record() {
    local exit_code=$1

    # Taken and cleared in one place, so every path out of this function is
    # covered by construction. Empty at the prompt where this file was sourced
    # (preexec had not been registered yet when that line ran) and at every
    # prompt zsh draws without having run anything.
    local cmd=$__woswoar_cmd
    local -i start=$__woswoar_start
    __woswoar_cmd=
    [[ -n $cmd ]] || return 0

    # A match sets six parameters, and all six leak into the user's shell -- on
    # every single command -- unless they are declared here first.
    local MATCH MBEGIN MEND
    local -a match mbegin mend
    if [[ -n $WOSWOAR_IGNORE && $cmd =~ $WOSWOAR_IGNORE ]]; then
        return 0
    fi

    local -i duration=$(( ${EPOCHREALTIME//,/.} * 1000 - start ))

    if (( ${#cmd} > __woswoar_max )); then
        # `${cmd:0:$__woswoar_max}`, with the `$`. zsh reads a bare name in the
        # length position as a history modifier and fails the whole expansion
        # with `unrecognized modifier`.
        cmd=${cmd:0:$__woswoar_max}'...[truncated]'
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
    strftime -s day '%F' $EPOCHSECONDS # local time, matching store.day_for()

    # A file's mode is set when it is created and never again, so the first
    # write of each day is the only moment this can be got right -- and `>>`
    # would create it 0644 under the usual umask. Guarded on the day string
    # rather than testing the file every time: this runs on every command, and
    # the answer changes once a day.
    #
    # In a subshell, so the caller's umask is untouched no matter what. What it
    # costs is once per shell *and* then once per day: `__woswoar_today` starts
    # empty, so this branch also fires at the first recorded command of every
    # shell, duplicating the `mkdir -p` that startup has just run. Measured at
    # ~5.4 ms of the 6.8 ms this hook adds to a shell that otherwise starts in
    # 1.25 ms -- and woswoar.bash pays exactly the same twice, so it is filed
    # (#183) rather than fixed in one shell here. It is off the per-command
    # path, which is what this file's fork-count tests are about.
    #
    # zsh has a fork-free alternative -- `zmodload -F zsh/files b:chmod` makes
    # chmod a builtin -- but the mode still has to be right at creation, so it
    # would trade one fork for a module load in every shell. Kept as bash has it.
    #
    # `mkdir -p` as well, because the directory is otherwise only made when the
    # shell starts, and a shell outlives a lot: an uninstall, a home moved, a
    # work machine's cleanup. Once a day is the cheapest place to notice.
    if [[ $day != "$__woswoar_today" ]]; then
        __woswoar_today=$day
        (umask 077 && mkdir -p "$__woswoar_logdir" && : >>| "$__woswoar_logdir/$day.tsv") \
            2>/dev/null
    fi

    # One O_APPEND write. Linux serialises these under the inode lock, so
    # concurrent shells on this host -- including a bash and a zsh sharing this
    # machine id -- cannot interleave lines.
    #
    # Two things here are zsh-specific and each is a silent failure on its own:
    #
    # `>>|` rather than `>>`, because under `setopt noclobber` an append to a
    # file that does not exist yet *fails*. A zsh user with noclobber set would
    # record nothing, ever, while the hook loaded, `doctor` was happy and the
    # log file was simply never created. `emulate -L zsh` in the caller already
    # clears NO_CLOBBER; this is the second of two guards because the cost of
    # both being wrong is silence.
    #
    # `{ ... } 2>/dev/null` rather than a redirection on the printf itself.
    # bash reports a failed redirection to the command's own stderr, so ordering
    # `2>/dev/null` first is enough there; zsh reports it to the shell's, and
    # both orders print `no such file or directory` at the user's prompt after
    # every command. That is the 0.4.1 bug (woswoar.bash:381) in the shape zsh
    # gives it, and only a surrounding block catches it.
    {
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$EPOCHSECONDS" "$WOSWOAR_SESSION" "$cwd" "$exit_code" "$duration" "$cmd" \
            >>| "$__woswoar_logdir/$day.tsv"
    } 2>/dev/null
}

# Fires a sync when one is due. Runs on every prompt, and forks on roughly one
# prompt a minute -- the one deliberate exception to the builtins-only rule.
# The steady-state cost is the single `((...))` below; everything that touches
# the filesystem is behind it.
__woswoar_maybe_sync() {
    (( WOSWOAR_SYNC_INTERVAL > 0 && EPOCHSECONDS >= __woswoar_next_sync )) || return 0

    # `{ ... } 2>/dev/null`, for the reason spelled out above the append: zsh
    # puts a failed redirection on the shell's stderr, so a missing stamp file
    # would otherwise print a path at the user's prompt after every command.
    local last=
    { read -r last <"$__woswoar_syncstamp" } 2>/dev/null
    # A torn or truncated write must not reach `((...))`. `>` is not atomic and
    # every shell on this machine writes this file, so a value that is not a
    # number needs no attacker. What it costs here is not what it costs under
    # bash -- see the note on WOSWOAR_SYNC_INTERVAL above -- but it is still
    # `bad math expression` at the prompt after every command, and a shell that
    # stops syncing for the rest of its life.
    #
    # Rejected outright rather than stripped to the digits in it: stripping
    # turns half of `1785...` plus a leftover tail into a *plausible* epoch, and
    # one in the future stops this shell syncing until the file changes again.
    #
    # Tested by removing the digits rather than with `=~`, as at startup: this
    # runs once a minute in the user's shell, and a regex match would leave
    # $MATCH, $MBEGIN and $MEND behind unless three more locals were declared
    # for it.
    #
    # No `10#` here, unlike the interval at startup, and the difference is the
    # point: `emulate -L zsh` has already turned OCTAL_ZEROES off for this
    # function, so `010` is ten. The startup line runs at file scope under
    # whatever the user set, where `08` is `bad math expression` and `010` is
    # eight -- so it needs the prefix and this does not. A `10#` that cannot be
    # shown to do anything is one more line to keep true.
    [[ -n $last && -z ${last//[0-9]/} ]] || last=0

    if (( EPOCHSECONDS - last < WOSWOAR_SYNC_INTERVAL )); then
        __woswoar_next_sync=$(( last + WOSWOAR_SYNC_INTERVAL ))
        return 0
    fi

    # Set before the check below rather than in both of its arms: coming due and
    # finding nothing to do still costs a `stat` on every command until the
    # interval is put back.
    __woswoar_next_sync=$(( EPOCHSECONDS + WOSWOAR_SYNC_INTERVAL ))

    # Nothing to sync with, so nothing to fork for. `history/.git`, matching
    # `sync.is_repo`, not `history/` itself: a clone that died partway leaves the
    # directory without the repository in it, and `woswoar sync` calls that "not
    # initialised" -- so testing the directory would have this machine fork an
    # interpreter a minute, forever, to print that error into /dev/null.
    [[ -e $__woswoar_dir/history/.git ]] || return 0

    # `( ... & )`: the subshell backgrounds the sync and exits immediately, so
    # the prompt waits for two forks (~1ms) and never for the sync itself. A
    # plain `&` here would put it in this shell's job table and report it at the
    # prompt once a minute, forever.
    #
    # The stamp is written *inside* that subshell, which is why it costs no
    # extra fork -- and under `umask 077`, because a file's mode is fixed when it
    # is created and `>` would otherwise make it 0644 for `doctor` to flag.
    #
    # Stamped before the sync runs, not after it succeeds: a `woswoar` that is
    # not on PATH must cost one attempt a minute, not one per command.
    (
        umask 077
        printf '%s\n' "$EPOCHSECONDS" >| "$__woswoar_syncstamp"
        woswoar sync >/dev/null 2>&1 </dev/null &
    ) 2>/dev/null
}

# ---------------------------------------------------------------------------
# Ctrl-R. Forks freely -- it runs on a keypress, not on every prompt.
# ---------------------------------------------------------------------------

__woswoar_widget() {
    emulate -L zsh
    local selection
    selection=$(woswoar search --scope "${WOSWOAR_SCOPE:-global}" --query "$BUFFER" \
        </dev/tty) || return 0
    [[ -n $selection ]] || return 0
    BUFFER=$selection
    CURSOR=${#BUFFER}
    # The picker drew over the screen; without this the prompt is left where it
    # was and the recalled line appears under the picker's wreckage.
    zle reset-prompt
}

# ---------------------------------------------------------------------------
# Wiring
#
# All of it. zsh needs none of woswoar.bash's apparatus here: no boot string, no
# self-removing PROMPT_COMMAND entry, no chaining onto a DEBUG trap, and no
# exit-status-restoring wrapper, because `precmd_functions` entries each see the
# user's real $? and `preexec` is handed the command line outright.
# ---------------------------------------------------------------------------

autoload -Uz add-zsh-hook
if add-zsh-hook preexec __woswoar_preexec 2>/dev/null; then
    add-zsh-hook precmd __woswoar_precmd
else
    # `add-zsh-hook` is a shipped function, so this is a zsh whose $fpath does
    # not reach its own function directory. Appending to the arrays is what
    # add-zsh-hook does anyway; what is lost is only its "already registered"
    # check, and __WOSWOAR_LOADED above has already made that unreachable.
    preexec_functions+=(__woswoar_preexec)
    precmd_functions+=(__woswoar_precmd)
fi

# Outside the binding guard below, so that `WOSWOAR_NO_BIND=1` leaves a widget
# you can still `bindkey` to a key of your own. Under bash that falls out for
# free -- `__woswoar_widget` is an ordinary function and `bind -x` takes its
# name -- but zle needs the widget declared before any keymap can name it.
zle -N __woswoar_widget

if [[ -z ${WOSWOAR_NO_BIND:-} ]]; then
    # Bound at source time, as woswoar.bash is, so put the woswoar line *last*
    # in ~/.zshrc: Oh My Zsh, Prezto and atuin all bind ^R when they load and
    # whoever goes last wins. Deferring to the first prompt would make that an
    # arms race with atuin, which also rebinds -- and losing it silently is
    # worse than an ordering rule you can see. `WOSWOAR_NO_BIND=1` keeps
    # another tool's binding instead.
    #
    # Three keymaps, mirroring woswoar.bash's three `bind -m`. What each
    # displaces: `history-incremental-search-backward` in main, `redisplay` in
    # viins, `redo` in vicmd.
    bindkey '^R' __woswoar_widget 2>/dev/null
    bindkey -M viins '^R' __woswoar_widget 2>/dev/null
    bindkey -M vicmd '^R' __woswoar_widget 2>/dev/null
fi

# No `__woswoar_maybe_sync` call here, deliberately. zsh runs precmd before the
# *first* prompt, so a shell where nobody has typed anything yet has already been
# through `__woswoar_precmd` and has already synced -- which is what makes
# sitting down and pressing Ctrl-R show current history.

__WOSWOAR_LOADED=1
