# Contributing to Shannon

This guide says how to set the project up, make a change, check it and propose it, so that nobody
has to ask (#176). The other documents:

- The [README](README.md) says what the bot does.
- [ARCHITECTURE.md](ARCHITECTURE.md) says how it is built and why.
- [`requirements.md`](requirements.md) is the product it set out to be. It is amended whenever the
  code departs from it.

Everybody who takes part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

Every change starts from an issue.

1. Look for an issue about your change.
2. If there is none, open one with a form:
   - **[Bug report](https://github.com/Canon-Regularis/Shannon-bot/issues/new?template=bug_report.yml)**:
     what happened, what you expected, how to make it happen, and the version.
   - **[Feature request](https://github.com/Canon-Regularis/Shannon-bot/issues/new?template=feature_request.yml)**:
     the problem, the expected outcome, the acceptance criteria and the scope. This repository's
     own issues use the same shape.
3. Agree the scope on the issue before you write code. A maintainer parks some issues until they
   say to go ahead, and says so in a comment.
4. Make one pull request for the issue. One issue makes one pull request.

## Security problems

A security problem is anything that could let somebody read, change or take over what they should
not. For example:

- a way past a role check or a GitHub check
- a token or private repository content reaching somebody it should not
- a forged webhook being accepted

Never put the details of a security problem in a public issue or a pull request. Do this instead:

1. Open a blank issue. Use the "Open a blank issue" link under the forms on the new-issue page. Do
   not use the bug form: it asks you to confirm that the bug is not a security problem.
2. Give it a title such as "Request for a private channel".
3. In the issue, only ask the maintainers (`Canon-Regularis`) for a private channel. Give no
   details.
4. A maintainer answers with a way to continue in private.

You report a Code of Conduct problem the same way.

## Setting up

There are two ways to set up. The README's [Install](README.md#install) describes both. You can use
either one in the same checkout, but both databases use `localhost:5433`, so only one can run at a
time.

- The Nix shell's database keeps running after you leave the shell. Stop it with
  `nix develop --command shannon-db stop` before you run `docker compose up`.
- If the compose database already holds the port, `shannon-db start` tells you so.

### Docker and uv

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) and Docker:

```bash
git clone https://github.com/Canon-Regularis/Shannon-bot.git && cd Shannon-bot
uv sync --extra dev --locked        # runtime and dev dependencies, exactly as uv.lock has them
cp .env.example .env                # its database URL already names the compose database
docker compose up -d --wait db      # PostgreSQL 17 on localhost:5433
uv run alembic upgrade head
```

The integration tests need their own database, and `SHANNON_TEST_DATABASE_URL` set in the shell.
The tests do not read `.env`. Without the variable, the integration tests are skipped.

```bash
docker compose exec db createdb -U shannon shannon_test
export SHANNON_TEST_DATABASE_URL=postgresql+asyncpg://shannon:shannon@localhost:5433/shannon_test
```

**Never point it at `shannon`.** Every test session drops and recreates every table in the
database it is given. If you point it at the database that `uv run shannon` uses, you lose that
data.

### Nix

You need only [Nix](https://nixos.org/download/), in a git clone. The README's
[With Nix](README.md#with-nix) covers it in full:

```bash
nix --extra-experimental-features 'nix-command flakes' develop
cp .env.example .env                # skip if the Docker path already made one
shannon-db start                    # PostgreSQL 17 on localhost:5433, with shannon_test
uv run alembic upgrade head
```

The shell syncs the dependencies when you enter it, and sets `SHANNON_TEST_DATABASE_URL` itself.
So every command below runs inside it as written. `.#py312`, `.#py313` and `.#py315` give the
rest of CI's Python versions.

### On Windows

- Both paths work from WSL 2, in a clone made inside WSL.
- The Docker path also works natively, with Git Bash or with PowerShell 7 (`pwsh`). Windows
  PowerShell 5.1 does not work: some commands here chain with `&&`, and 5.1 does not accept it.
- In PowerShell, set the variable with
  `$env:SHANNON_TEST_DATABASE_URL = "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon_test"`.
- `$env:SHANNON_TEST_DATABASE_URL = ""` empties it for the rest of the window, not for one command.
  Set it again before the next `--cov` run.

`.gitattributes` pins the line endings, so leave git's `core.autocrlf` as it is.
`scripts/deploy.sh` and the Nix files are LF in every checkout, because a carriage return breaks
them.

## Making a change

Start a branch from an up-to-date `main`:

```bash
git switch main && git pull
git switch -c feat/descriptions-ring-who-they-name
```

Name the branch the way the history already does:

- The prefix is the kind of change: `feat/`, `fix/` or `refactor/`. Documentation work has used
  `feat/` so far.
- The rest says what the change does.

If you do not have write access to the repository, work from a fork:

1. Fork the repository on GitHub and clone your fork.
2. Add this repository as `upstream` and branch from it. The branch then starts from the real
   `main`, not from your fork's copy:

   ```bash
   git remote add upstream https://github.com/Canon-Regularis/Shannon-bot.git
   git fetch upstream && git switch -c feat/what-it-does upstream/main
   ```

3. Push the branch to your fork, and open the pull request from there to `main` here.

### How the code is written

- **Where things live.** [ARCHITECTURE.md](ARCHITECTURE.md#where-the-code-lives) lists the
  packages from the bottom layer up. A package only imports the packages listed before it.
- **Protocols at the edges.** Almost everything that crosses the network sits behind a narrow
  protocol (a small interface). So the services run in tests with Discord and GitHub replaced.
  ARCHITECTURE.md names the exceptions.
- **Comments and docstrings say why**, in whole sentences, and name the issue they come from
  (`#147`). The code itself says what it does.
- **Types are strict.** mypy and pyright both run in strict mode, both for Python 3.12. Each one
  has a list in `pyproject.toml` of files that are not clean yet. A file leaves the list when it is
  fixed and never goes back on it. A new file is never added to it.
- **Ruff** formats and lints, at a line length of 100. `uv run ruff format .` fixes the layout.

### Tests

- **Coverage is 100%, branches included,** over `shannon/`. If a change adds a line, it also adds
  the test that runs it.
- **Two tiers** (groups of tests, split by what they need to run):
  - `tests/unit/` needs only the Python interpreter. Every test in it is marked `unit` for you.
  - `tests/integration/` needs PostgreSQL. Every file there declares
    `pytestmark = pytest.mark.integration` itself.
  - The database fixtures live in `tests/integration/conftest.py`, so a unit test cannot reach one
    by accident.
- **Named as sentences.** One class per situation, and one test per claim about it. For example,
  `class TestWhoMayRunEachHalf` holds `test_anybody_else_is_refused_before_anything_is_written`.
- **Stand-ins, not mocks.** A stand-in is a small working fake. The ones in `tests/fakes/` replace
  Discord, GitHub and the rest. The helpers are in `tests/support/`.
  - To wait for something, use `tests/support/waiting.until`. Do not sleep.
  - To control time, inject a clock. Do not wait for a real one.
- **Hypothesis** is used where the point is to cover a large space of inputs:
  `tests/unit/test_properties.py`, and the layout and text-safety tests.

## Where a change has to reach

Some changes are finished only when they reach several places. For most of them a test fails until
they do. The rest follow the convention the code already uses.

- **A setting.**
  - Add a field on `Settings` in `shannon/config.py`. `config.py` imports nothing from the project.
  - Add a line in `.env.example`. A token or a secret gets an empty value, or one starting `your-`.
  - Add a row in the README's [Configuration](README.md#configuration).
  - A credential is a `SecretStr`. Give it a placeholder value in
    `tests/unit/test_config_secrecy.py`, and put that value into `LEAKS` and into the `settings`
    fixture, so the masking tests see it.
- **A migration.**
  - Add a file in `migrations/versions/` named `NNNN_what_it_does.py`.
  - Set `revision = "NNNN"` and `down_revision` to the one before it. You number it by hand, not
    Alembic.
  - Its docstring says why. Its `downgrade` works.
  - Move the README's "Alembic revisions" line to the new number.
- **A table.** Everything for a migration, plus:
  - the model in `shannon/db/models.py`;
  - a row in the README's [Data model](README.md#data-model);
  - the `TABLES` tuple in `tests/integration/conftest.py`;
  - the set of table names in `tests/integration/test_migrations.py`.
- **A slash command.**
  - It must fit what Discord accepts: names, description lengths, choice counts and option counts.
    `tests/unit/test_the_commands_discord_will_accept.py` checks this against the real wiring.
  - Its permission tier (who may run it) comes from `shannon/commands/_permissions.py`. A command
    with no gate at all goes into `UNGATED` there, on purpose.
  - Add a row in the README's [Commands](README.md#commands).
- **Words a member reads.** `tests/unit/test_the_words_this_bot_uses.py` (#147) holds every reply
  to these rules:
  - A `/command` it names exists. A group is named with its subcommand (`/board link`).
  - A follow-up is "Run /x".
  - A reply ends in a full stop, a question mark or an exclamation mark. The exception: it ends on
    a link or a mention after a colon.
  - No sentence opens with a mention.
  - A status or priority is never spelt the way the database spells it.
  - A value in an error a person reads goes in a code span, never through `!r`.
  - In a reply, only `shannon/discord_bot/responses.py` puts on the ✅, ⚠️ and ❌ marks.
    No command or service writes one itself. The check-run headings in `formatting.py` use ✅ and
    ❌ separately.
- **A protocol or a fake.** A fake is the stand-in a test uses in place of a real class. Add the
  pair to `IMPLEMENTATIONS` in `tests/unit/test_stand_ins_match_what_they_replace.py`. That test
  checks every fake still has every member of what it replaces. Nothing finds a new pair for you.
- **An HTTP route.** Add a matching `handle` block in `Caddyfile`, or the proxy never forwards it.
- **A loop that opens threads.** Add it to `OPENING_THREADS` in
  `tests/unit/test_the_shutdown_budget_fits_the_container.py`, so the containers' shutdown grace
  still covers it.
- **A webhook event.**
  - Add it to `SUPPORTED_EVENTS` in `shannon/github/webhooks/events.py`.
  - Add its name to `WEBHOOK_NAMES` in `tests/unit/test_the_readme_describes_this_repository.py`.
  - Add it to the README's list of events to choose.

## Running the checks

CI runs these checks. Run them yourself before every push:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pyright
uv run pytest -q --cov                         # needs SHANNON_TEST_DATABASE_URL, see above
SHANNON_TEST_DATABASE_URL= uv run pytest -q    # the suite with the integration tier skipped
```

- Coverage (`--cov`) reaches 100% only when the integration tier runs.
- The last line is a CI check too: it proves that a fresh clone with no database still passes.
- To run one file or test: `uv run pytest tests/unit/test_config_secrecy.py -q`.

To run the bot and build the image:

```bash
uv run shannon       # needs the database up and migrated
docker build .       # the image the release publishes
```

The Discord token in `.env.example` is a placeholder. If you leave it, the bot tries it as a real
token and the process stops at login. Either set a real token or empty the line. When
`SHANNON_DISCORD_TOKEN` is empty, the bot logs a warning and runs without Discord.

CI also does these things:

- runs the test suite on Python 3.12, 3.13, 3.14 and 3.15;
- builds the image and smoke-tests it;
- audits the dependencies with pip-audit;
- scans the whole git history for secrets with gitleaks;
- runs everything again inside `nix develop` on Linux and macOS.

To test another Python version locally, use `nix develop .#py312` or one of its siblings.

## Documentation

A change is not finished until the documents agree with it. Update these in the same pull request:

| Document | What to do |
| --- | --- |
| `README.md` | Keep it true. Its own test, `tests/unit/test_the_readme_describes_this_repository.py`, checks it against the code: every table, every setting, the migration range and the webhook events. |
| `CHANGELOG.md` | Add one entry at the **end** of the file. The newest entry is last. The shape is below. |
| `ARCHITECTURE.md` | Update it when a layer, a background loop or one of the big decisions changes. |
| `requirements.md` | This is the technical requirements document. Where a change departs from it, amend it. Say what was built instead and name the issue, so the two do not disagree. |

### How to write

The reader may be new to the project, new to Discord bots, or reading in a second language (#241).

- Write short sentences, one idea each. Use the active voice: say who does what.
- Use plain words. When you need a project term (tier, lease, stand-in, delivery), say what it
  means the first time it appears in that document or section.
- Use a list or a table for anything somebody looks up: steps, settings, commands, options,
  limits. Number steps that happen in order. Use prose for reasons.
- Make each heading say what is under it.
- Keep the issue reference on every fact, written `#94`.
- Leave out asides, jokes, rhetorical questions and hedging.
- Give reasons in one or two sentences. Leave the full story to the CHANGELOG, the issue or a code
  comment, and link to it when that helps.
- "You" is fine in instructions.
- Wrap prose at 100 columns. Put code, commands, settings, file names and paths in backticks. No
  emoji.

### A CHANGELOG entry

1. A heading that says what changed, as a plain sentence. For example:
   `## A description rings whoever it names`.
2. Bullets, each with a bold lead-in. Say what was wrong or missing, and why it
   mattered. The first bullet names the issue, for example `(#259)`. Then say what is true now.
3. A **For whoever deploys it.** bullet: settings, migrations, anything to do on a server, or
   "Nothing."
4. A **Tests.** bullet: what now proves the change.

Older entries keep the shape they were written in. Do not rewrite them.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/). Most commits have a scope. The
subject is a sentence about the behaviour, not a list of files:

```text
feat(sync): a new item's opening block rings whoever its description names
fix(link-team): a personal repository's refusal reaches whoever ran it
docs(readme): an @name in a description reaches people
```

| Part | What to use |
| --- | --- |
| Type | `feat`, `fix`, `refactor`, `test`, `docs`, `build`, `ci`, `chore` or `perf` |
| Scope | The part of the project the commit touches, for example `github`, `discord`, `sync`, `services`, `commands`, `db`, `boards`, `container`, `deps`, `readme` or `changelog` |
| No scope | A change across the whole project, such as a test or docs change in several areas (`test: …`, `docs: …`) |

Rules:

- Keep each commit small and complete. Every commit passes the checks on its own, so you can bisect
  the history and revert one piece at a time.
- The README and the CHANGELOG usually get one commit each, after the code they describe.
- Add a body when the subject cannot carry the reason.

## Dependencies

- **Add a package:** run `uv add <package>`. For a development tool, run
  `uv add --optional dev <package>`. Both update `pyproject.toml` and `uv.lock` together. Commit
  both files.
- **Lock must match:** CI installs with `--locked`. If `uv.lock` no longer matches
  `pyproject.toml`, CI fails. It does not resolve something else.
- **Updates:** Dependabot proposes updates every week.
- **Nix:** `nix flake update` moves `flake.lock`. Dependabot does not read `flake.lock`, so update
  it yourself. The new lock must bring the uv version that `.github/workflows/ci.yml` pins.
  Otherwise `nix flake check` fails.

## Pull requests

- **Base and scope.** Open it against `main`, from your branch. Keep it to one issue. If you find a
  second fix on the way, open a second issue and a second pull request for it, unless it is too
  small to stand on its own.
- **Title.** Write it like the issue's title: `feat: Add a CONTRIBUTING.md`.
- **Description.** Start with the issue it closes. Then say what changed, why you did it this way,
  and how you checked it. Say what you could not check:

  ```text
  Resolves: https://github.com/Canon-Regularis/Shannon-bot/issues/176

  What changed, and why this way.
  How it was checked.
  ```

- **CI.** The check called `CI` must be green. It fails if any job before it failed or was
  cancelled. A pull request always runs every check. A first-time contributor's run waits until a
  maintainer approves it.
- **Review.** To merge into `main`, the pull request needs:
  - an approving review of the latest push;
  - every review conversation resolved.

  A new push dismisses an earlier approval. A maintainer reviews the pull request and may ask for
  changes. The maintainer merges it with a merge commit, so your commits land on `main` as they
  are.

## Deploying

Only maintainers deploy, because a deploy needs access to the server.
[`scripts/README.md`](scripts/README.md) covers all of it. As a contributor, you write the
**For whoever deploys it.** bullet in the CHANGELOG entry. It says what a deploy of your change
needs.

## Licence

Shannon is licensed under [Apache-2.0](LICENSE). Anything you submit for inclusion is under the same
licence, unless you say otherwise. Section 5 of that licence sets this out.
