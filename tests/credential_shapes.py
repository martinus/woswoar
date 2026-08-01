"""Command shapes the credential filters are held to, shared by two test modules.

The bash hook (`woswoar/shell/woswoar.bash`) and the importer
(`woswoar/credentials.py`) implement the same rules in two languages, for two
very different budgets -- one recompiles a regex on every prompt, the other runs
once over a decade of history. This is the corpus that keeps them from drifting.
"""

from __future__ import annotations

#: Command shapes the default `WOSWOAR_IGNORE` must drop. Most come from #23,
#: which listed them as *not* caught by the pattern this replaces.
SECRET_SHAPES = [
    # A credential that is a *path component* of a URL rather than a
    # `user:pass@` prefix. Measured against 55,017 commands of one maintainer's
    # real history, this was the only shape the rules missed -- twice, in `curl`
    # calls to a Slack webhook. Anyone holding the URL can fire that integration,
    # so it is a credential in the ordinary sense.
    #
    # The tokens here are deliberately too short and too obviously fake to match
    # a vendor's own shape. A corpus of credential examples is a file full of
    # things that look like credentials, and GitHub's push protection rejected
    # an earlier draft of it: a 24-character token after `/services/` is exactly
    # what its Slack rule matches. woswoar's rule needs only the host and one
    # character of path, so nothing is lost by staying well clear.
    'curl -X POST -d \'{"text":"hi"}\' https://hooks.slack.com/services/EXAMPLE-NOT-REAL',
    # The one that actually turned up: a workflow *trigger*, not an incoming
    # webhook. Anchoring the rule on `/services/` -- the shape that comes to
    # mind -- would have missed the only real instance in 55,017 commands, which
    # is why the rule names the host and not a path under it.
    "curl -X POST https://hooks.slack.com/triggers/EXAMPLE-NOT-REAL",
    "curl -H 'Content-type: application/json' https://discord.com/api/webhooks/EXAMPLE-NOT-REAL",
    "curl -d @body.json https://discordapp.com/api/webhooks/EXAMPLE-NOT-REAL",
    "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI",
    "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7",
    "AWS_SESSION_TOKEN=abc aws s3 ls",
    "export MY_SECRET_TOKEN=abc123",
    "export GITHUB_TOKEN=ghp_xxx",
    "API_KEY=abc",
    "APIKEY=abc",
    "PGPASSWORD=hunter2 psql",
    "export PASSWORD=x",
    "export SECRET_KEY_BASE=deadbeef",
    "export DB_PASSWD=x",
    "export DB_PASS=x",
    "export MY_AUTH_TOKEN=x",
    "export STRIPE_API_KEY=sk_live_x",
    "mysql --password=hunter2",
    "wget --password hunter2 http://x",
    "curl --token abc https://x",
    "gh auth login --with-token",
    "kubectl create secret generic s --from-literal=password=x",
    "aws --secret-key x",
    "helm install --api-key=abc",
    "s3cmd --access-key AKIAIOSFODNN7",
    'curl -H "Authorization: Bearer eyJ0eXAi"',
    'curl -H "authorization: Basic dXNlcg=="',
    "curl -u user:pass https://api.example.com",
    'psql "postgres://user:pass@host/db"',
    "git clone https://user:token@github.com/o/r",
    "sshpass -p hunter2 ssh host",
    "htpasswd -b .htpasswd user pass",
    "openssl passwd -6 hunter2",
    "mysql -pSECRETpw -u root",
    "mysql -u root -pSECRETpw",
    "docker login -p hunter2 -u me",
    "ssh-keygen -t ed25519 -N hunter2 -f k",
]


#: Ordinary commands the pattern must leave alone. A filter that eats these is
#: worse than useless: it deletes history and says nothing.
INNOCENT_SHAPES = [
    "git status",
    "ls -la",
    "make -j8",
    "ssh-keygen -t ed25519 -C me@host",
    "ssh-keygen -lf ~/.ssh/id_ed25519.pub",
    "ssh-keygen -R oldhost",
    "git clone https://github.com/martinus/woswoar",
    'curl -H "Accept: application/json" https://api.example.com',
    "curl -sSL https://example.com/install.sh | bash",
    "curl -fsSL -o out.json https://api.example.com/v1/items",
    "docker run -p 8080:80 nginx",
    "docker run -u 1000:1000 alpine",
    "docker compose up -d --build",
    "mysql --protocol=tcp -u root",
    "mysql -u root -h db01 mydb",
    "mysql < dump.sql",
    "kubectl get secret",
    "kubectl describe secrets",
    "vault kv get secret/app",
    "psql postgres://localhost/mydb",
    "export EDITOR=vim",
    "export MONKEY=banana",
    "export KEYS=3",
    "export KEYBOARD=us",
    "export PASSAGE=x",
    "export PATH=/usr/bin:$PATH",
    "make KEYS=3",
    "aws s3 ls",
    "aws configure list",
    "openssl req -new -x509",
    "openssl x509 -in c.pem -text",
    "grep -r password .",
    "vim ~/.aws/credentials",
    "cat /etc/passwd",
    "git log --oneline -20",
    "git commit -m 'add auth handler'",
    "ssh -p 2222 user@host",
    "npm install --save-dev typescript",
    "touch a_key_file.txt",
    "man curl",
    # A long option that merely *starts* with a keyword. This is what the
    # `([=[:space:]]|$)` boundary after each keyword is for: without it, `auth`
    # matches inside `--auth-mode` and a real azure command disappears.
    "az storage blob list --auth-mode login",
]


#: Shapes the pattern does **not** catch, each one a claim `docs/shell-integration.md`
#: makes in prose. They are pinned here because an unbacked claim in a security
#: document is worse than a known gap: a later broadening would falsify the
#: document with CI green, and nobody would look.
#:
#: Not a wish list. Some of these genuinely carry a secret and are recorded
#: anyway -- `redis-cli -a`, `aws configure set`, `vault kv put`. Chasing them
#: means an unbounded list of tool names priced per character on every prompt,
#: so the project documents them instead of pretending otherwise.
DOCUMENTED_GAPS = [
    # No secret is on these lines at all -- the tool prompts for it.
    "mysql -p",
    "docker login",
    "gh auth login",
    # A secret with no tell: nothing distinguishes it from any other argument.
    "deploy.sh AKIAIOSFODNN7EXAMPLE",
    # Lower-case assignment. Upper case is the convention and the pattern's cost
    # is proportional to its length, so both cases are not spelled out.
    "token=abc",
    # A tool that takes a secret as a positional argument or a bare flag.
    "aws configure set aws_secret_access_key wJalr",
    "vault kv put secret/app value=hunter2",
    "redis-cli -a hunter2",
]
