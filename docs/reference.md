# Reference

Every command and every environment variable, in one place. `woswoar --help` and
`woswoar <command> --help` say the same things at the terminal.

## Commands

| command | |
|---|---|
| `woswoar` | where this machine stands, and what to run next |
| `woswoar setup` | guided first run: tools, hook, import, sync repo |
| `woswoar search` | interactive picker (what <kbd>Ctrl</kbd>+<kbd>R</kbd> runs) |
| `woswoar list` | plain output, used by fzf's scope-switch reload |
| `woswoar import bash\|zsh\|atuin` | import an existing history |
| `woswoar stats` | entry counts, date range, most-used commands |
| `woswoar doctor` | check the installation and the tools it needs |
| `woswoar doctor --prove` | demonstrate in a sandbox that nothing readable is published |
| `woswoar init [url]` | create or join an encrypted history repo |
| `woswoar sync` | exchange history with the remote |
| `woswoar accept` | add a machine you own: `grant` and `trust` at once |
| `woswoar grant` | let newly enrolled machines read the older history |
| `woswoar trust` | accept another machine's published history here |
| `woswoar compact` | merge old chunks to reduce the working-tree file count |
| `woswoar forget <text>` | remove recorded commands from this machine (dry run without `--yes`) |

## Environment

| variable | meaning |
|---|---|
| `WOSWOAR_DIR` | data directory (default `~/.local/share/woswoar`) |
| `WOSWOAR_IGNORE` | extended regex of commands never to record |
| `WOSWOAR_IGNORE_EXTRA` | extra regex joined onto the default, instead of replacing it |
| `WOSWOAR_SYNC_INTERVAL` | seconds between background syncs; `0` turns them off (default `60`) |
| `WOSWOAR_SCOPE` | default scope for <kbd>Ctrl</kbd>+<kbd>R</kbd>: `global`, `host`, `session` or `dir` (default `global`) |
| `WOSWOAR_NO_BIND` | set to skip binding <kbd>Ctrl</kbd>+<kbd>R</kbd> |

The two ignore patterns are the ones worth reading about before setting: what
they already cover, and what a rule of your own has to look like, is in
[commands that are never recorded](shell-integration.md#commands-that-are-never-recorded).
