"""Build the sandbox the demo GIF is recorded in.

The GIF has to show the thing woswoar is for -- one Ctrl-R over the history of
*three* machines -- and nobody's real history can be filmed: it is exactly the
data this project exists to keep private. So the demo history is generated, and
generated here rather than recorded by hand, for the same reason the tape is
checked in: a demo nobody can rebuild is a demo that goes stale silently.

What it is not is a toy. It writes **54,000 commands across two years**, because
the numbers the README quotes -- ~105 ms to open the picker on a 54k history --
are only honest if the recording pays them. A three-hundred-line fixture would
open instantly and sell a speed that does not exist.

Everything about it is deterministic given ``--seed``: the same command mix, the
same directories, the same three machines. Only the timestamps move, and they
have to -- they are relative to ``--now`` so the ages on screen read ``2m`` and
``3h12m`` rather than "two years ago", which is what a fixed epoch would show by
the time anyone re-recorded.

    python -m tools.demo.seed /tmp/woswoar-demo

The sandbox is self-contained and nothing outside it is touched: its own
``$HOME``, its own ``WOSWOAR_DIR``, its own XDG directories, and a ``bin/woswoar``
that runs *this checkout* rather than whatever is installed on the machine.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from woswoar import entry, store

#: The three machines. Fixed ids, because a run with the same seed should
#: produce the same repository twice, and the id is the directory name.
#:
#: The names are `user@host` and the picker shows the *host* half, so these are
#: what the machine column reads: `thinkpad`, `DT-24YYQ3`, `nuc`. One of them is
#: deliberately a corporate asset tag -- the column earns its width on names
#: nobody chose, and a demo where every machine is called `laptop` hides that.
THIS_HOST = "7f3a9c21c4e18b02"
HOSTS: dict[str, str] = {
    THIS_HOST: "martin@thinkpad",
    "b18d47e5a0c93f6a": "martin@DT-24YYQ3",
    "2c5e0fa7d9b41e83": "martin@nuc",
}

#: Where the demo shell starts, and the directory `Ctrl`+`O` scopes to. It has
#: to be a real directory in the sandbox: the prompt shows it and `cd` has to
#: work.
DEMO_CWD = "~/src/api"

#: Directories the generated history moves through, and which machines use them.
#: `~/src/api` is on all three on purpose -- the `dir` scope's whole argument is
#: that the same project on two machines is one project.
PROJECTS: dict[str, tuple[str, ...]] = {
    "~/src/api": tuple(HOSTS),
    "~/src/woswoar": (THIS_HOST, "2c5e0fa7d9b41e83"),
    "~/src/infra": ("b18d47e5a0c93f6a", "2c5e0fa7d9b41e83"),
    "~/dl": (THIS_HOST,),
    "~": tuple(HOSTS),
}

#: The command pools, per directory. Written as short "task" runs rather than a
#: flat list, because history is bursty: you run six git commands in a row, not
#: one git command and one kubectl. The timeline (`Ctrl`+`T`) is only worth
#: showing if what surrounds a hit is what really surrounded it.
#:
#: `{placeholders}` are filled from :data:`VOCAB`. Without them a pool this size
#: generates 54,000 commands with sixty distinct lines in them, and the picker
#: -- which deduplicates -- films two thirds of a screen. A real history varies
#: in the arguments and repeats in the verbs, which is what these do.
TASKS: dict[str, tuple[tuple[str, ...], ...]] = {
    "~/src/api": (
        ("git pull --rebase", "git status", "git log --oneline -{n}"),
        ("docker compose build {svc}", "docker compose up -d --build", "docker compose ps"),
        ("docker compose logs -f {svc}", "docker compose restart {svc}"),
        ("pytest -q", "pytest -q tests/test_{mod}.py::test_{case}", "ruff check ."),
        ("curl -s localhost:{port}/{route} | jq .", "curl -sf localhost:{port}/metrics | head"),
        ("git add -A", "git commit -m '{msg}'", "git push"),
        ("alembic revision --autogenerate -m {mod}_index", "alembic upgrade head"),
        ("vim {file}", "git diff {file}", "git checkout -b {branch}"),
        ("psql -h localhost -U api -c 'select count(*) from {table}'",),
        ("docker image ls", "docker image prune -f"),
        ("rg -n '{needle}' app", "sed -n '{line},{line}p' {file}"),
        ("git show {sha}", "git revert {sha}", "git cherry-pick {sha}"),
        ("git bisect start", "git bisect bad {sha}", "git bisect good {sha}"),
        ("docker exec -it api-{hash} sh", "docker logs api-{hash} --since 10m"),
    ),
    "~/src/woswoar": (
        ("python -m tools.run_tests", "ruff format .", "mypy woswoar tests tools"),
        ("git rebase -i origin/main", "git push --force-with-lease"),
        ("woswoar stats", "woswoar doctor", "woswoar sync"),
        ("gh pr create --fill", "gh pr checks --watch", "gh issue view {n}"),
        ("WOSWOAR_BENCH=1 python -m unittest tests.test_perf",),
        ("python -m unittest tests.test_{mod}", "python -m tools.mutate {mod}.py"),
    ),
    "~/src/infra": (
        ("terraform plan -out tf.plan", "terraform apply tf.plan"),
        ("kubectl get pods -n {ns}", "kubectl logs -f deploy/{svc} -n {ns}"),
        ("kubectl rollout restart deploy/{svc} -n {ns}",),
        ("kubectl rollout status deploy/{svc} -n {ns}", "kubectl get events -n {ns}"),
        ("kubectl describe pod {svc}-{hash} -n {ns}", "kubectl delete pod {svc}-{hash} -n {ns}"),
        ("kubectl exec -it {svc}-{hash} -n {ns} -- sh", "kubectl top pod {svc}-{hash} -n {ns}"),
        ("git -C /srv/{svc} log --oneline {sha}", "git -C /srv/{svc} checkout {sha}"),
        ("ansible-playbook -i hosts site.yml --check", "ansible-playbook -i hosts site.yml"),
        ("ssh {box} 'systemctl --user status woswoar-sync.timer'",),
    ),
    "~/dl": (
        ("curl -LO https://example.com/{archive}.tar.zst", "zstd -d {archive}.tar.zst"),
        ("tar xf {archive}.tar", "du -sh {archive}", "rm -rf {archive} {archive}.tar"),
        ("sha256sum -c SHA256SUMS", "gpg --verify {archive}.tar.zst.asc"),
    ),
    "~": (
        ("df -h", "free -h", "btop"),
        ("systemctl --user status woswoar-sync.timer", "journalctl --user -u woswoar-sync -n {n}"),
        ("ssh {box}", "ssh {box} 'uptime'"),
        ("sudo dnf upgrade --refresh", "sudo dnf install -y {pkg}"),
        ("cd src/api", "cd src/woswoar", "ls -la"),
        ("kill -9 {pid}", "ps aux | grep {needle}", "lsof -p {pid}"),
        ("strace -p {pid} -f -e trace=openat", "gdb -p {pid}"),
    ),
}

#: The short commands a shell is mostly made of, drawn between the tasks above.
#: Without them the most-used list in `woswoar stats` is topped by whatever
#: three-command task the generator happened to like, which is not what anybody's
#: history looks like: the top of a real one is `git status`, `ls` and `cd`, by a
#: wide margin, and the interesting commands are in the long tail.
COMMON: dict[str, tuple[str, ...]] = {
    "~/src/api": ("git status", "ls", "git diff", "clear", "cd ..", "vim {file}", "cat {file}"),
    "~/src/woswoar": ("git status", "ls", "git diff", "clear", "vim {file}", "git log --oneline"),
    "~/src/infra": ("git status", "ls", "clear", "cd ..", "vim {file}", "kubectl get pods -n {ns}"),
    "~/dl": ("ls -la", "ls", "clear", "du -sh .", "rm -rf {archive}"),
    "~": ("ls", "clear", "cd src/api", "cd -", "htop", "df -h"),
}

#: What a `{placeholder}` can be. Drawn once per *session* -- one shell works on
#: one branch against one service -- except for :data:`PER_COMMAND`, which
#: varies line by line the way a filename does.
VOCAB: dict[str, tuple[str, ...]] = {
    "svc": ("api", "worker", "gateway", "billing", "search", "webhooks"),
    "ns": ("prod", "staging", "dev"),
    "port": ("8080", "8000", "3000", "5432", "9090"),
    "route": ("health", "readyz", "v1/orders", "v1/users", "v1/sessions"),
    "branch": (
        "fix/retry-502",
        "feat/rate-limit",
        "chore/bump-deps",
        "fix/flaky-timeout",
        "feat/idempotency-keys",
        "perf/cache-warmup",
    ),
    "table": ("orders", "users", "sessions", "payments", "webhooks_log"),
    "box": ("nuc", "DT-24YYQ3", "thinkpad", "build01"),
    "pkg": ("fzf", "age", "ripgrep", "jq", "zstd", "htop"),
    "archive": (
        "linux-6.9.3",
        "dataset-2026-05",
        "backup-nuc",
        "models-v3",
        "grafana-dashboards",
        "postgres-dump",
    ),
    "mod": ("routes", "models", "db", "auth", "cache", "queue", "billing", "search"),
    "case": ("health", "retry", "timeout", "unauthorized", "pagination", "idempotency"),
    "file": (
        "app/routes.py",
        "app/models.py",
        "app/db.py",
        "app/auth.py",
        "tests/test_routes.py",
        "compose.yaml",
        "Dockerfile",
        "pyproject.toml",
    ),
    "msg": (
        "fix: retry on 502",
        "feat: rate limit per key",
        "chore: bump deps",
        "fix: close the pool on shutdown",
        "test: cover the retry path",
        "perf: cache the warm path",
    ),
    "needle": ("TODO", "retry", "timeout", "session", "auth", "cache"),
    "hash": ("7f4c9", "2b81d", "c03ae", "9de17", "41f6b"),
    "n": tuple(str(i) for i in (3, 5, 10, 20, 30, 50, 100, 200)),
}

#: Placeholders that change within a session rather than being fixed by it.
PER_COMMAND = frozenset({"file", "msg", "needle", "n", "case"})

#: Placeholders drawn fresh every time from an effectively unbounded set: a
#: commit sha, a pod's suffix, a line number. They are what makes a history
#: deduplicate the way a real one does -- roughly a third of a working history
#: is commands that were typed exactly once and never again, and a corpus built
#: only from fixed vocabulary collapses to a few hundred distinct lines and
#: films a picker whose counter contradicts the README.
RANDOM: dict[str, Callable[[random.Random], str]] = {
    "sha": lambda rng: f"{rng.randrange(16**7):07x}",
    "hash": lambda rng: f"{rng.randrange(16**5):05x}{rng.choice('bcdfghjklmnpqrstvwxz')}",
    "line": lambda rng: str(rng.randint(12, 940)),
    "pid": lambda rng: str(rng.randint(1200, 98_000)),
}

#: The last few minutes before the recording, planted rather than drawn. The
#: first frame of the GIF is a `docker` search, and what sits at the top of it is
#: the one thing every viewer reads -- so it is the three lines the README's
#: ASCII mock has always shown, in the same order, from the same machines.
#:
#: `(seconds before now, host, directory, exit code, duration ms, command)`.
PLANTED: tuple[tuple[int, str, str, int, int, str], ...] = (
    (6 * 86400 + 4 * 3600, THIS_HOST, "~/src/api", 0, 41_300, "docker system prune -af"),
    (3 * 3600 + 12 * 60, "b18d47e5a0c93f6a", "~/src/api", 130, 8_400, "docker logs -f api"),
    (52 * 60, THIS_HOST, "~/src/api", 0, 640, "git pull --rebase"),
    (48 * 60, THIS_HOST, "~/src/api", 1, 12_900, "pytest -q"),
    (44 * 60, THIS_HOST, "~/src/api", 0, 2_100, "vim app/routes.py"),
    (36 * 60, THIS_HOST, "~/src/api", 0, 9_800, "pytest -q"),
    (31 * 60, THIS_HOST, "~/src/api", 0, 310, "git add -A"),
    (30 * 60, THIS_HOST, "~/src/api", 0, 180, "git commit -m 'fix: retry on 502'"),
    (12 * 60, THIS_HOST, "~/src/api", 0, 27_400, "docker compose build api"),
    (2 * 60, THIS_HOST, "~/src/api", 0, 1_240, "docker compose up -d --build"),
)


class Line(NamedTuple):
    """One generated record, before it is written to a host's day file."""

    ts: int
    host: str
    session: str
    cwd: str
    exit_code: int
    duration_ms: int
    cmd: str


def _exit_code(rng: random.Random) -> int:
    """Mostly zero, and the rest weighted the way a real history is.

    The age of a failed command is painted red, so a history with no failures in
    it films a picker that looks like it has no such feature -- and one where
    every third line failed looks like a machine on fire.
    """
    roll = rng.random()
    if roll < 0.90:
        return 0
    if roll < 0.96:
        return 1
    if roll < 0.98:
        return 130  # Ctrl-C, which is most of what actually "fails"
    return rng.choice((2, 127, 143))


def _duration(rng: random.Random) -> int:
    """Milliseconds, log-uniform between 5 ms and ~90 s."""
    return int(5 * (18_000 ** rng.random()))


def _fill(template: str, rng: random.Random, session: dict[str, str]) -> str:
    """Resolve `{placeholders}`, remembering the per-session ones.

    `str.format` is not enough on its own: the point is that two commands in one
    shell talk about the *same* service and the same branch, which is what makes
    a timeline read like something that happened rather than like a shuffle.
    """
    values: dict[str, str] = {}
    for key, options in VOCAB.items():
        if "{" + key + "}" not in template:
            continue
        if key in PER_COMMAND:
            values[key] = rng.choice(options)
        else:
            values[key] = session.setdefault(key, rng.choice(options))
    for key, draw in RANDOM.items():
        if "{" + key + "}" in template:
            values[key] = draw(rng)
    return template.format(**values)


def _generate(rng: random.Random, now: int, wanted: int) -> list[Line]:
    """Walk backwards from ``now``, emitting bursts of related commands."""
    lines: list[Line] = []
    for offset, host, cwd, code, took, cmd in PLANTED:
        ts = now - offset
        lines.append(
            Line(
                ts=ts,
                host=host,
                # One planted session per burst of nearby plants, so the timeline
                # around the newest hit is the session it belongs to.
                session=f"{ts // 3600:x}-{0x1F00 + offset % 97:x}",
                cwd=cwd,
                exit_code=code,
                duration_ms=took,
                cmd=cmd,
            )
        )

    # Start the generated history where the planted one ends, so the two never
    # interleave and the newest rows on screen are the ones chosen above.
    ts = now - 2 * 3600
    while len(lines) < wanted:
        cwd = rng.choice(list(PROJECTS))
        host = rng.choice(PROJECTS[cwd])
        task = rng.choice(TASKS[cwd])

        # A session is a shell, so it holds one machine and one burst. Real gaps
        # between shells are long and heavy-tailed; within one they are seconds.
        ts -= rng.randint(180, 9000)
        session = f"{ts // 3600:x}-{rng.randrange(0x1000, 0xFFFF):x}"
        context: dict[str, str] = {}
        # The task, then a tail of whatever short commands that directory
        # attracts -- a shell is opened to do one thing and then lingers.
        burst = (*task, *rng.choices(COMMON[cwd], k=rng.randint(0, 3)))
        for template in burst:
            ts -= rng.randint(3, 240)
            lines.append(
                Line(
                    ts=ts,
                    host=host,
                    session=session,
                    cwd=cwd,
                    exit_code=_exit_code(rng),
                    duration_ms=_duration(rng),
                    cmd=_fill(template, rng, context),
                )
            )
    return lines


def _write_logs(lines: list[Line]) -> int:
    """Group by host and day, then write each file once.

    Appending per line would reopen the same file some hundreds of times and is
    the slow half of this script; the records are generated newest-first and the
    files want oldest-first, so they are sorted here rather than reversed.
    """
    by_file: dict[tuple[str, str], list[Line]] = {}
    for line in lines:
        by_file.setdefault((line.host, store.day_for(line.ts)), []).append(line)

    for (host, day), day_lines in by_file.items():
        path = store.log_file(host, day)
        store.private_dir(path.parent)
        text = "".join(
            "\t".join(
                (
                    str(line.ts),
                    line.session,
                    entry.escape(line.cwd),
                    str(line.exit_code),
                    str(line.duration_ms),
                    entry.escape(line.cmd),
                )
            )
            + "\n"
            for line in sorted(day_lines)
        )
        path.write_text(text, encoding="utf-8")
        path.chmod(store.FILE_MODE)
    return len(by_file)


def _sandbox_env(root: Path, repo: Path) -> dict[str, str]:
    """The environment every part of the demo runs under.

    `PATH` first, because the shell hook calls `woswoar` by name and the point
    of the sandbox is that it is *this* checkout that answers, not a `pipx`
    install that happens to be on the machine and a release behind.
    """
    # Built from `store.sandbox_environ` rather than from `os.environ` plus
    # overrides, which is what this was and which is the shape `tools/sandbox.py`
    # rejects in its own docstring: that one overrode four of `store.ENV_KEYS`'
    # six names, and this one missed `WOSWOAR_SESSION` -- exported by the
    # installed hook in every interactive shell, so a demo recorded from a real
    # terminal carried the maintainer's own session id into the sandbox.
    env = store.sandbox_environ(root, root / "home")
    env.update(
        PATH=f"{root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        PYTHONPATH=str(repo),
        # The recording is of a person searching, not of a machine syncing: a
        # background `git push` in the middle of a take would be a spinner
        # nobody asked about, and there is no remote here to push to anyway.
        WOSWOAR_SYNC_INTERVAL="0",
    )
    return env


_BASHRC = """\
# Sandbox rc for the woswoar demo recording -- see tools/demo/record.sh.
export HOME={home}
export PATH={bin}:$PATH
export PYTHONPATH={repo}
export WOSWOAR_DIR={data}
export XDG_CONFIG_HOME={config}
export XDG_CACHE_HOME={cache}
export WOSWOAR_SYNC_INTERVAL=0

# A prompt with nothing in it but the directory. A demo of a *history* tool has
# no business filming somebody's git-branch prompt segments.
PS1='\\[\\033[38;5;141m\\]\\w\\[\\033[0m\\] $ '
HISTFILE={home}/.bash_history

cd {cwd}
"""


def _write_wrapper(root: Path, repo: Path) -> None:
    """`bin/woswoar`, which is what the hook and the tape both call."""
    wrapper = root / "bin" / "woswoar"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        f'#!/bin/sh\nexec env PYTHONPATH="{repo}" "{sys.executable}" -m woswoar "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


#: Written at the top of a sandbox this script owns. Nothing is deleted from a
#: directory without it -- `record.sh` takes a path from the environment, and
#: "rebuild the sandbox" must never be able to mean "empty the home directory
#: somebody pointed it at by mistake".
MARKER = ".woswoar-demo-sandbox"


def _clear(root: Path) -> None:
    """Start from nothing, so two recordings of the same tape agree.

    Left in place, a sandbox accumulates: every take appends the commands the
    tape types to the history, so the third recording shows three `woswoar
    stats` in its own timeline. Small, and exactly the kind of thing that makes
    a demo look staged.
    """
    if not root.exists():
        return
    if not (root / MARKER).exists() and any(root.iterdir()):
        raise SystemExit(
            f"{root} is not empty and has no {MARKER} in it, so it was not built by this "
            f"script. Refusing to delete anything; pick another directory."
        )
    for name in ("home", "data", "config", "cache", "bin"):
        shutil.rmtree(root / name, ignore_errors=True)


def _refuse_a_real_store(root: Path) -> None:
    """Stop unless every path `store` resolves is inside the sandbox just built.

    The environment is the only thing pointing `store` at that sandbox, and this
    module is one a mutation sweep rewrites -- so a mutant that drops the
    `WOSWOAR_DIR` line from `_sandbox_env` sends every write below into the
    developer's live installation. That is not a hypothesis. It overwrote a real
    `machine` file with `THIS_HOST`, put three fabricated machines into 399 days
    of real history, and published one of them to a shared remote, where it
    carried the real machine's own signing key (#245).

    So the check is here rather than in `_sandbox_env`: one mutation can break
    the environment or this guard, and it takes both to reach a real store. The
    marker is required as well as the prefix, because a developer who points
    `root` at `~/.local/share/woswoar` deserves the same refusal.
    """
    inside = root.resolve()
    if not (root / MARKER).is_file():
        raise SystemExit(
            f"{root} has no {MARKER}: this run did not build it, so it is not a sandbox"
        )
    for where in (store.data_dir(), store.config_dir(), store.logs_dir()):
        if not where.resolve().is_relative_to(inside):
            raise SystemExit(
                f"refusing to seed: store resolves to {where}, which is outside {inside}. "
                f"The sandbox environment did not take, and writing there would land in a real "
                f"installation -- see #245."
            )


def build(root: Path, repo: Path, entries: int, seed: int, now: int) -> None:
    _clear(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).touch()
    for name in ("home", "data", "config", "cache", "bin"):
        (root / name).mkdir(parents=True, exist_ok=True)
    home = root / "home"
    for project in PROJECTS:
        if project != "~":
            (home / project.removeprefix("~/")).mkdir(parents=True, exist_ok=True)

    env = _sandbox_env(root, repo)
    # Replaced, not updated: `os.environ.update` leaves everything it does not
    # name, and `WOSWOAR_SESSION` is exported by the installed hook in every
    # interactive shell -- so a demo recorded from a real terminal inherited the
    # maintainer's own session id.
    os.environ.clear()
    os.environ.update(env)
    _refuse_a_real_store(root)

    store.private_dir(store.config_dir())
    store.save_machine(store.Machine(id=THIS_HOST, name=HOSTS[THIS_HOST]))
    for host_id, name in HOSTS.items():
        store.private_dir(store.host_dir(host_id))
        store.write_atomic(store.name_file(host_id), f"{name}\n".encode())

    lines = _generate(random.Random(seed), now, entries)
    files = _write_logs(lines)

    (home / ".bashrc").write_text(
        _BASHRC.format(
            home=home,
            bin=root / "bin",
            repo=repo,
            data=root / "data",
            config=root / "config",
            cache=root / "cache",
            cwd=DEMO_CWD.replace("~", str(home)),
        ),
        encoding="utf-8",
    )
    _write_wrapper(root, repo)

    # The hook, installed the way a reader installs it -- `install` appends its
    # marked block, so the exports above are already in place when it is sourced.
    subprocess.run(
        [str(root / "bin" / "woswoar"), "install", "--rcfile", str(home / ".bashrc")],
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    # Build the parse cache now rather than on camera. A first-ever Ctrl-R reads
    # every log file; every later one reads the cache, and steady state is what
    # the demo is claiming. Filming the cold build would misrepresent it in the
    # other direction.
    warm = subprocess.run(
        [str(root / "bin" / "woswoar"), "list", "--scope", "global"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    print(
        f"{len(lines):,} commands, {len(HOSTS)} machines, {files} day files"
        f" -> {root}\n{len(warm.stdout.splitlines()):,} lines in the picker"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.demo.seed", description=__doc__)
    parser.add_argument("root", help="sandbox directory to build (created if missing)")
    parser.add_argument("--entries", type=int, default=54_000, help="commands to generate")
    parser.add_argument("--seed", type=int, default=1, help="RNG seed")
    parser.add_argument("--now", type=int, default=None, help="epoch seconds the history ends at")
    args = parser.parse_args(argv)

    build(
        root=Path(args.root).expanduser().resolve(),
        repo=Path(__file__).resolve().parent.parent.parent,
        entries=args.entries,
        seed=args.seed,
        now=int(time.time()) if args.now is None else args.now,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
