# Contributing to Shannon

How to set the project up, make a change, check it and propose it, written down so nobody has to
ask (#176). The [README](README.md) says what the bot does and how it is built;
[`requirements.md`](requirements.md) is the product it set out to be, amended whenever the code
departs from it. Everybody taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

Every change starts from an issue, so look for one first. If there is none, open one with a form:

- **[Bug report](https://github.com/Canon-Regularis/Shannon-bot/issues/new?template=bug_report.yml)**:
  what happened, what you expected, how to make it happen, and the version.
- **[Feature request](https://github.com/Canon-Regularis/Shannon-bot/issues/new?template=feature_request.yml)**:
  the problem, the expected outcome, acceptance criteria and the scope, the shape this
  repository's own issues use.

Agree the scope on the issue before writing code. Some issues are parked until a maintainer says
to go ahead, and say so in a comment. One issue makes one pull request.

## Security problems

The details of a security problem never go in a public issue or a pull request. That covers
anything that could let somebody read, change or take over what they should not: a way past a
role or GitHub check, a token or private repository content reaching somebody it should not, a
forged webhook being accepted. Open a blank issue instead (the "Open a blank issue" link under the
forms on the new-issue page; the bug form asks you to confirm it is not a security problem),
titled for example "Request for a private channel". In it, only ask the maintainers
(`Canon-Regularis`) for a private channel, with no details, and a maintainer will answer with a
way to continue in private. The same route is how a Code of Conduct report is made.

## Setting up

Two ways, both in the README's [Install](README.md#install). Either works in the same checkout,
but both databases use localhost:5433, so only one runs at a time. The Nix shell's database keeps
running after you leave the shell, so stop it with `nix develop --command shannon-db stop` before
`docker compose up`; `shannon-db start` says so itself when the compose one holds the port.

### Docker and uv

Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) and Docker:

```bash
git clone https://github.com/Canon-Regularis/Shannon-bot.git && cd Shannon-bot
uv sync --extra dev --locked        # runtime and dev dependencies, exactly as uv.lock has them
cp .env.example .env                # its database URL already names the compose database
docker compose up -d --wait db      # PostgreSQL 17 on localhost:5433
uv run alembic upgrade head
```

The integration tests need a database of their own, and `SHANNON_TEST_DATABASE_URL` set in the
shell. `.env` is not read by the tests, and the tier skips without it:

```bash
docker compose exec db createdb -U shannon shannon_test
export SHANNON_TEST_DATABASE_URL=postgresql+asyncpg://shannon:shannon@localhost:5433/shannon_test
```

**Never point it at `shannon`.** Every test session drops and recreates every table in the
database it is given, so pointed at the database `uv run shannon` uses it throws that data away.

### Nix

[Nix](https://nixos.org/download/) and nothing else, in a git clone. The README's
[With Nix](README.md#with-nix) covers it in full:

```bash
nix --extra-experimental-features 'nix-command flakes' develop
cp .env.example .env                # skip if the Docker path already made one
shannon-db start                    # PostgreSQL 17 on localhost:5433, with shannon_test
uv run alembic upgrade head
```

The shell syncs the dependencies on entry and sets `SHANNON_TEST_DATABASE_URL` itself, so every
command below runs inside it as written. `.#py312`, `.#py313` and `.#py315` give the rest of CI's
Python versions.

### On Windows

Either path works from WSL 2, in a clone made inside WSL. The Docker path also works natively,
with Git Bash or with PowerShell 7 (`pwsh`); Windows PowerShell 5.1 does not accept the `&&` some
commands here chain with. In PowerShell the variable is set with
`$env:SHANNON_TEST_DATABASE_URL = "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon_test"`.
`$env:SHANNON_TEST_DATABASE_URL = ""` empties it for the rest of the window rather than for one
command, so set it again before the next `--cov` run.

Line endings are pinned by `.gitattributes`, so leave git's `core.autocrlf` as it is.
`scripts/deploy.sh` and the Nix files are LF in every checkout, because they break with a
carriage return.

## Making a change

Branch from an up-to-date `main`, named for the kind of change and what it does, the way the
history already is:

```bash
git switch main && git pull
git switch -c feat/descriptions-ring-who-they-name
```

The prefix is the kind of change, `feat/`, `fix/` or `refactor/`, and the rest says what the
change does. Documentation work has gone on `feat/` branches so far.

Without write access to the repository, fork it on GitHub and clone the fork instead. Add this
repository as `upstream` and branch from it, so the branch starts from the real `main` rather than
the fork's copy:

```bash
git remote add upstream https://github.com/Canon-Regularis/Shannon-bot.git
git fetch upstream && git switch -c feat/what-it-does upstream/main
```

The branch is pushed to the fork, and the pull request goes from there to `main` here.

### How the code is written

- **Where things live** is in the README's [Architecture](README.md#architecture), bottom up, and
  imports only ever run down that list. Anything that crosses the network sits behind a narrow
  protocol, so the services run in tests with Discord and GitHub replaced.
- **Comments and docstrings say why**, in whole sentences, and name the issue they come from
  (`#147`). What the code does is the code's job to say.
- **Types are strict.** mypy and pyright both run in strict mode, both for Python 3.12. Each
  carries a list in `pyproject.toml` of files that are not clean yet. A file leaves the list when
  it is fixed and never goes back on it, and a new file is never added to it.
- **Ruff** formats and lints, at a line length of 100. `uv run ruff format .` fixes the layout.

### Tests

- **Coverage is 100%, branches included,** over `shannon/`. A change that adds a line adds the test
  that runs it.
- **Two tiers.** `tests/unit/` needs nothing but the interpreter, and everything in it is marked
  `unit` for you. `tests/integration/` needs PostgreSQL, and every file there declares
  `pytestmark = pytest.mark.integration` itself. The database fixtures live in
  `tests/integration/conftest.py`, so a unit test cannot reach one by accident.
- **Named as sentences.** A class per situation and a test per claim about it:
  `class TestWhoMayRunEachHalf` holding
  `test_anybody_else_is_refused_before_anything_is_written`.
- **Stand-ins, not mocks.** The fakes in `tests/fakes/` replace Discord, GitHub and the rest; the
  helpers are in `tests/support/`. Wait for something with `tests/support/waiting.until` rather
  than sleeping, and inject a clock rather than waiting for one.
- **Hypothesis** is used where the input space is the point (`tests/unit/test_properties.py`,
  the layout and text-safety tests).

## Where a change has to reach

Some changes are only finished once they reach several places. For most of these a test fails
until they do; the rest are the convention the code already follows.

- **A setting.** A field on `Settings` in `shannon/config.py`, a line in `.env.example` (empty, or
  starting `your-`, for anything holding a token or a secret), and a row in the README's
  [Configuration](README.md#configuration). A credential is a `SecretStr`. In
  `tests/unit/test_config_secrecy.py` it gets a placeholder value, which goes into `LEAKS` and into
  the `settings` fixture, so the masking tests actually see it. `config.py` imports nothing from
  the project.
- **A migration.** A new file in `migrations/versions/` named `NNNN_what_it_does.py`, with
  `revision = "NNNN"` and `down_revision` the one before it, numbered by hand rather than by
  Alembic. Its docstring says why, and `downgrade` works. The README's "Alembic revisions" line
  moves to the new number.
- **A table.** All of the above, plus the model in `shannon/db/models.py`, a row in the README's
  [Data model](README.md#data-model), the `TABLES` tuple in `tests/integration/conftest.py`, and
  the set of table names in `tests/integration/test_migrations.py`.
- **A slash command.** It has to fit what Discord accepts (names, description lengths, choice and
  option counts), which `tests/unit/test_the_commands_discord_will_accept.py` checks against the
  real wiring. Its permission tier comes from `shannon/commands/_permissions.py`; a command that
  takes no gate at all goes into `UNGATED` there, deliberately. It gets a row in the README's
  [Commands](README.md#commands).
- **Words a member reads.** `tests/unit/test_the_words_this_bot_uses.py` (#147) holds every reply
  to the same rules. A `/command` it names exists, a group with its subcommand (`/board link`). A
  follow-up is "Run /x". A reply ends in a full stop, a question mark or an exclamation mark,
  unless it ends on a link or a mention after a colon. No sentence opens with a mention. A status
  or priority is never spelt the way the database spells it. A value in an error a person reads
  goes in a code span, never through `!r`. And the ✅, ⚠️ and ❌ marks are only ever written in
  `shannon/discord_bot/responses.py`.
- **A protocol or a fake.** Its pair goes into `IMPLEMENTATIONS` in
  `tests/unit/test_stand_ins_match_what_they_replace.py`, which checks every fake still has every
  member of what it replaces. Nothing finds a new one for you.
- **An HTTP route.** A matching `handle` block in `Caddyfile`, or the proxy never forwards it.
- **A loop that opens threads.** It goes into `OPENING_THREADS` in
  `tests/unit/test_the_shutdown_budget_fits_the_container.py`, so the containers' shutdown grace
  still covers it.
- **A webhook event.** `SUPPORTED_EVENTS` in `shannon/github/webhooks/events.py`, its name in
  `WEBHOOK_NAMES` in `tests/unit/test_the_readme_describes_this_repository.py`, and the README's
  list of events to choose.

## Running the checks

What CI runs, and worth running before every push:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pyright
uv run pytest -q --cov                         # needs SHANNON_TEST_DATABASE_URL, see above
SHANNON_TEST_DATABASE_URL= uv run pytest -q    # the suite with the integration tier skipped
```

`--cov` only reaches 100% with the integration tier running, and the second line is the check CI
makes that a fresh clone with no database still passes. A single file or test runs as
`uv run pytest tests/unit/test_config_secrecy.py -q`.

Running the bot and building the image:

```bash
uv run shannon       # needs the database up and migrated
docker build .       # the image the release publishes
```

The Discord token `.env.example` holds is a placeholder. Left in place, it is tried as a real
token and the process stops at login, so set a real one, or empty the line: with
`SHANNON_DISCORD_TOKEN` empty the bot warns and runs without Discord.

CI also runs the suite on Python 3.12, 3.13, 3.14 and 3.15, builds and smoke-tests the image,
audits the dependencies with pip-audit and the whole history with gitleaks, and runs everything
again inside `nix develop` on Linux and macOS. `nix develop .#py312` and its siblings reproduce
another version locally.

## Documentation

A change is not finished until the documents agree with it:

- **README.** Keep it true. Its own test holds it to the code: every table, every setting, the
  migration range and the webhook events.
- **CHANGELOG.** Append one section at the **end** of `CHANGELOG.md`, newest last, headed with a
  plain sentence saying what changed (`## A description rings whoever it names`). Bullets with a
  bold lead-in: what was wrong or missing, naming the issue (`(#259)`), then what is true now. Close
  with **For whoever deploys it.** (settings, migrations, anything to do on a server, or
  "Nothing.") and **Tests.** (what now proves it).
- **requirements.md.** Where a change departs from it, amend it in the same pull request rather
  than leaving the two to disagree.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), usually with a scope, and a subject
that is a sentence about the behaviour rather than a list of files:

```text
feat(sync): a new item's opening block rings whoever its description names
fix(link-team): a personal repository's refusal reaches whoever ran it
docs(readme): an @name in a description reaches people
```

The types in use are `feat`, `fix`, `refactor`, `test`, `docs`, `build`, `ci`, `chore` and `perf`.
The scope names the part of the project it touches, for example `github`, `discord`, `sync`,
`services`, `commands`, `db`, `boards`, `container`, `deps`, `readme` or `changelog`. A change that
spans the project, such as a test or docs change across several areas, leaves it off
(`test: …`, `docs: …`).

Keep commits small and each one whole: every commit should pass the checks on its own, so the
history can be bisected and reverted a piece at a time. The README and the CHANGELOG usually get a
commit each, after the code they describe. A body is welcome wherever the subject cannot carry the
reason.

## Dependencies

`uv add <package>` (or `uv add --optional dev <package>` for a tool) updates `pyproject.toml` and
`uv.lock` together; commit both. CI installs with `--locked`, so a lock that no longer matches
fails rather than resolving something else. Dependabot proposes updates weekly. `flake.lock` moves
with `nix flake update`, which Dependabot does not read, and has to bring the uv version
`.github/workflows/ci.yml` pins, or `nix flake check` fails.

## Pull requests

- **Against `main`, from your branch.** Keep it to the one issue. A second fix found on the way is
  a second issue and a second pull request, unless it is too small to stand on its own.
- **Title** like the issue's: `feat: Add a CONTRIBUTING.md`.
- **Description.** Start with the issue it closes, then say what changed, why it was done this way
  and how you checked it, including anything you could not check:

  ```text
  Resolves: https://github.com/Canon-Regularis/Shannon-bot/issues/176

  What changed, and why this way.
  How it was checked.
  ```

- **CI** has to be green. Look at the check called `CI`: it fails if any job before it failed or
  was cancelled. A pull request always runs everything. A first-time contributor's run waits for a
  maintainer to approve it.
- **Review.** `main` needs an approving review of the latest push and every review conversation
  resolved, and a new push dismisses an earlier approval. A maintainer reviews it, may ask for
  changes, and merges it with a merge commit, so the commits land on `main` as they are.

## Deploying

Maintainers only. Deployment needs the server, and [`scripts/README.md`](scripts/README.md) is
the whole of it. A contributor's part is the CHANGELOG's **For whoever deploys it.**, saying what
a deploy of the change needs.

## Licence

Shannon is licensed under [Apache-2.0](LICENSE). Unless you say otherwise, anything you submit for
inclusion is under the same licence, as section 5 of that licence sets out.
