# shannon

GitHub repository activity, mirrored into Discord threads

**shannon** binds one GitHub repository to one Discord server. Each pull request and each issue
gets its own Discord thread. The bot updates the thread when the item changes, and posts the
item's comments and reviews in it.

GitHub tells the bot about changes through webhooks (HTTP requests GitHub sends when something
happens). The bot checks each webhook, saves it, and answers at once. A queue does all the slow
work later. The reason: GitHub waits only ten seconds for an answer, and it never sends again a
delivery it recorded as failed.

**To install it,** follow [INSTALLATION.md](INSTALLATION.md): from this repository to a working
bot in your Discord server, step by step. The rest of this README explains what the bot does and
every setting.

## What it does

Words used in this section:

- **Item**: a pull request, an issue, or a draft card on a project board.
- **Delivery**: one webhook event that GitHub sends to the bot.
- **Block**: the metadata message at the top of an item's thread.
- **Linked**: a GitHub account connected to a Discord account with `/link`, or a GitHub team
  connected to a Discord role with `/link_team`.
- **Mention**: a Discord `@name`. **Ring**: Discord notifies the person. A mention can be shown
  without ringing anybody.

### Threads

- Each item gets one thread. The bot opens it on the first event and edits it in place after
  that.
- The block shows: name, type, state, link, author, assignees, reviewers, status, priority, tags,
  last updated, and the description the item was opened with, if there is one.
- The block is a card with:
  - a bar down its side in GitHub's own colour for the item's state
  - the author's avatar beside it
  - the description under a rule
  - a button to the item on GitHub
- The description keeps its formatting: headings, bold, lists, quotes and code. Up to four
  pictures show under it.
- Discord draws three sizes of heading. GitHub's fourth to sixth sizes arrive as Discord's third.
- Discord cannot draw a heading inside a list item or under two quote marks. Such a heading
  arrives as its words, in bold, unless the words already hold `**` or end on a backslash.
- A line in a block of code keeps the hashes and marks it opens with.
- A link to GitHub stays a link. A link anywhere else keeps its words and also shows the host it
  really goes to, so `click here` cannot hide somewhere else.
- An `@someone` in the description is a mention where that login is linked, the same as in a
  comment. The block that opens the thread rings them: see [Pings](#pings).

### Comments and reviews

- Each one is posted into the item's thread, with a link back.
- Whoever wrote it is named as a mention but not rung: they know what they wrote.
- An inline review comment gets a message of its own, naming the file and the line it sits on.
- A reply to an inline comment names whom it answers: everybody who wrote in that comment thread
  before it, the opener first, apart from the person replying, at most ten. It rings them where
  they are linked. So answering a review reaches the reviewer, and answering that answer reaches
  the author.
- The bot asks GitHub who they are when the reply arrives, because the webhook names only the
  comment the reply was left under. This means it also works in comment threads started before
  this feature existed.
- If GitHub cannot be reached, a reply waits up to half an hour after it was written, then goes
  out without those names. If GitHub refuses to answer for it, it goes out at once.
- A review that carries nothing but inline notes posts no message of its own. GitHub wraps every
  note on a diff in a review. Mirroring that wrapper would post "left a review" with nothing under
  it, once for every reply.
- An edit rewrites the message the comment went out as, and rings nobody. A deletion is not
  mirrored.
- `/track comments off` stops all of this in one thread.

### Reviews that ask for changes

A review that asks for changes reaches whoever has to make them.

- Its message names the people who made the code the review is about, and rings them where they
  are linked.
- With inline comments, it lists each file the comments are on, up to ten. Each file shows the
  authors of this pull request's commits that touched it:
  `` `shannon/services/notes.py` made by authors @alice, @bob ``.
- A file whose only commits are merges, or have no account behind them, reads `made by Unknown`.
- Limits on the file lines:
  - Each file has room for at least five people, more when there are fewer files.
  - It names fewer when the message runs short of room, and counts the rest as `and N more`.
  - So the file lines ring at most fifty people. Whoever the review's own `@tags` reach is rung
    as usual, on top of that.
  - Files past what fits in the message are counted as well.
  - Only the people shown are rung.
- When the files cannot be worked out, one line names the pull request's author and everybody who
  committed to it instead. This happens for:
  - a review with no inline comments
  - more than ten files
  - a review on a commit the pull request no longer has, after a force-push or past the 250
    commits GitHub lists
  - a file history GitHub refuses or no longer has
- The reviewer is named on a file they made, left out of the one line, and never rung.
- Every request for changes rings: a second one too, and one on a draft. Unlike a CI run, asking
  for changes is something a person chose to do.
- Who to name is read from GitHub when the review arrives:
  1. the pull request's commits and its inline comments: two lists, read a page at a time
  2. then one read per file
- A file's history is kept. So an edit, or another review on the same commit, reads only the two
  lists again.
- If GitHub cannot be reached, the review waits up to half an hour after it was submitted, then
  names whoever could be read. If GitHub refuses to answer for it, it goes out at once in one
  line.
- An approval, or a review that only comments, makes none of these reads.

### Tags in a comment

- `@someone` in a comment body becomes a real Discord mention where that login is linked.
  `@org/team` becomes a role mention where that team is linked.
- Anybody not linked is still named, in plain text.
- At most ten tags per comment become mentions. Without a limit, one comment could ping every
  linked member of the server.
- An issue's or a pull request's description reads its tags the same way: ten at most, and only in
  the part the block shows. One difference: there, `@org/team` stays plain text and is never a
  role mention.

### Commit hashes become links

- A commit hash in a description, a comment, a review, an inline comment or a commit message links
  to that commit on GitHub.
- It reads the way GitHub shows it: `abc1234` (its first seven characters), or
  `owner/repo@abc1234` for another repository's commit.
- A hash is not linked:
  - inside code written with backticks, which is the code Discord draws
  - inside a link or an address
  - on a draft card, which belongs to no repository, so a bare hash has none to belong to
- Five per body. Any past that are shown as written.
- A hash is recognised by its shape: see [Known limitations](#known-limitations).

### Pings

- Reviewers and assignees are told once each, as mentions where the account is linked.
- The bot records that it has told somebody before the message goes out, and takes the record
  back if the message fails.
- When a thread is first opened, the block carries those mentions and is a real message, so the
  block is the ping.
- The separate ping line is for people added later, because editing the block would reach nobody.
- The first block also rings whoever its description `@`-names, once each, if the item was opened
  no more than a day before. A name added by a later edit, or in an item this bot first sees more
  than a day after it was opened, is shown as a mention and rings nobody.
- Anybody who has run `/mentions off` is still named in all of it, as a mention Discord shows and
  does not ring.
- `/track people off` stops the separate line in one thread. Nobody added while it is off is rung
  for it later.

### Lines in the thread

- A tag moving says so. Priority is coloured by level, and status labels are told apart from
  ordinary labels.
- A tag the opening block already listed says nothing. GitHub sends those as their own deliveries
  a moment after the block.
- Closing, merging or reopening posts a header saying what became of the thread.
- A finished item's thread is shut: locked, and archived out of the channel. It opens again if the
  item does.
- A draft card's thread follows its card off the board the same way:
  - shut when the card is archived
  - opened again when the card is restored, followed by the usual line for anything that changed
    meanwhile, where the thread asked for it
  - shut for good when the card is deleted
- These lines exist because a Discord edit is silent. It posts no message, notifies nobody, and
  does not bump the thread. A change that only edits the block looks, from the channel, like
  nothing happening.
- Tag lines, and a card's line for its fields, are off until a thread asks with
  `/track labels on`.
- The headers are on until a thread says `/track state off`. That drops the header, and the bot
  still shuts and reopens the thread.
- A close this bot was not allowed to lock is said anyway. The header is the only place that
  refusal shows, and somebody has to grant the permission.

### What CI made of a commit

- When every job on a commit has finished, its thread says how many passed and links the logs of
  the ones that did not.
- It rings the people who put the code there: the author, the assignees, and anybody with a commit
  on the pull request.
- A pass and a fail reach the same people, because a green run is as much news as a red one to
  whoever pushed.
- That includes you, for your own push, on purpose. Nobody knows what CI makes of a push until it
  has run. A comment or review you wrote tells you nothing new, so its message names you without
  ringing you.
- A requested reviewer is not rung by CI. They are asked for a review instead.
- A merge commit names nobody. Its account is whoever pressed "Update branch", not whoever wrote
  anything.
- It is said once per set of runs, so two CI apps on one commit produce one message. Nothing is
  said until every job has stopped.
- A draft posts its results and rings nobody.
- `/mentions off` opts you out.
- `/track checks off` stops these lines in one thread, before GitHub is asked anything.

### Every review in, and approving

- When the last person a pull request was waiting on approves it, its thread says so. It rings the
  author and the assignees, because the pull request waits on them next.
- The bot reads this from GitHub at that moment. It does not count the reviews that have arrived:
  GitHub rewrites a review in place when it is dismissed, and the bot receives no event for that,
  so a count would keep an approval somebody had taken back.
- A comment left after an approval does not clear it, the same as GitHub's own rule.
- It is said once per commit. A push that invalidates the approvals, then a fresh round, says it
  again.
- It is not said while anybody asked has still to answer. That includes a team, because no payload
  says who is in one.
- Withdrawing the last outstanding review request says it too. At that moment everybody remaining
  has approved and no further review is coming, so nothing else would ever notice.
- `/track approvals off` stops it in one thread.

### The draft switch

- CI on a draft rings nobody, on purpose. GitHub runs CI on a draft like any other pull request,
  but a draft is the state in which this bot asks nothing of anybody on its own account. The card
  is grey, not green, to show this.
- Leaving draft and going back into draft each post a header naming whoever pressed the button.
- Both reach everybody GitHub still lists on the item: its author, its assignees, its reviewers and
  its review teams, once each. The person who pressed the button is left out, because they know.
- Leaving draft is an ask. Going back into draft withdraws the ask, so the people who were about
  to answer are told, and do not have to find out by opening it.
- A review asked of a team is a role mention when leaving draft, and the team's plain name when
  going back. Discord rings everybody holding a role and cannot leave one person out. That is
  worth it to ask, but not to stand down.
- `/track draft off` stops both in one thread.

### Commits

- A push to an open pull request posts one message per commit. It shows who wrote it, its
  subject, and its message capped at 250 characters. Under them: its short hash, linking to the
  commit, with the additions, deletions and files changed.
- Ten per push, newest first, with a footnote counting any left over.
- Merge commits and commits somebody else wrote are skipped, so pulling main in says nothing.
- A force push posts one message saying it was force-pushed instead. It names the head it
  replaced and the new head, each linked. Every commit on a rewritten branch has a new hash, and
  would otherwise read as work nobody had just done.
- None of it pings anybody.
- Off until a thread asks with `/track commits on`. Until then, nothing is asked of GitHub about a
  push.

### Choosing what a thread is told

`/track`, run in an item's thread, picks which kinds of line that thread gets on top of the block.
The block always keeps up whatever you pick.

| Kind | Starts | Pull request | Issue | Board card |
| --- | --- | --- | --- | --- |
| `comments` | on | comments and reviews | comments | - |
| `labels` | off | labels, statuses and priorities | labels, statuses and priorities | the card's fields changing |
| `commits` | off | commits and force pushes | - | - |
| `checks` | on | CI results | - | - |
| `approvals` | on | everybody having approved | - | - |
| `draft` | on | leaving draft and going back into it | - | - |
| `people` | on | review requests and assignments | assignments | - |
| `state` | on | closing, merging and reopening | closing and reopening | the card being archived, restored, deleted or converted |

- Labels and commits start off, because an item can be tagged a dozen times in a minute and a
  branch pushed to all afternoon.
- Off stops the line and nothing else. A closed item's thread is still shut, and a review still
  closes the request for it.
- Turning a kind back on replays nothing that was skipped. Only something that happens again is
  said: a comment edited afterwards is posted as new, and another approval on the same commit
  still brings the round-up.
- The choice belongs to the item, so a thread rebuilt or moved keeps it.
- A kind nobody chose follows the default, including a default changed later.
- These are always said, whatever `/track` is set to: a thread moving channel, logging starting,
  stopping or giving up, reminders, somebody being asked to link their GitHub account, and a close
  this bot was not allowed to lock.

### Manual sync

- `/pr` and `/issue` pull one item from the REST API, for whatever the webhooks missed.
- `/refresh` does the whole repository, or one kind of item if you pick one:
  - every open item with no thread gets one
  - every open pull request and issue that has a thread is redrawn from the same listing, so
    nothing more is asked of GitHub
- People are named as real mentions and nobody is rung.
- A thread Discord archived for going quiet is archived again once it is redrawn, so the channel
  looks the same afterwards.
- Cards are left to the board, which redraws its own.
- Each run makes up to 25 new threads, then redraws for up to five minutes. Redrawing starts with
  the items whose record this bot changed longest ago. A comment, review or CI result posted into
  a thread does not change the record.
- The reply says what is left. The next `/refresh` with the same scope starts there.
- One run per server at a time, and the next a minute after the last finished.

### Redrawing one that has gone quiet

- `/regenerate`, run in an item's own thread, reads the item from GitHub again and rewrites the
  block.
- It is for the threads `/refresh` does not reach, and for one thread right now. For example, a
  closed pull request gains labels and assignees afterwards, and no delivery ever comes to say so.
- Nobody is pinged. Everybody on the item is named as a real mention, and Discord is told to ring
  none of them.
- One run per thread at a time, and the next five seconds after the last finished.

### Reminders

- `/remind` pings somebody once, after the days, hours and minutes you give, in the channel or
  thread you ran it in. Up to 500 characters of your own words go under it.
- Anybody may remind themselves. Reminding somebody else takes Admin or Project Manager, or GitHub
  admin where a server lets GitHub access stand in for a role.
- The private answer:
  - for a reminder fourteen minutes away or less, counts down and disappears as it goes off
  - for one further away, gives the time and how long after setting it that is. A Discord
    countdown runs on past zero, and Discord lets a bot take its answer back only for fifteen
    minutes.
- The reminder always rings, `/mentions off` or not, because somebody asked for exactly that.
- The words ring nobody else. A mention in them shows as text, and so does `@everyone`.
- One that falls due while the bot is down goes out once it is back, and says it is late.
- One caught by a restart at the wrong moment can go out twice. The bot errs on that side.
- Nothing recurs. Nothing lists or takes back a reminder once it is set.
- One person may have twenty-five reminders waiting in a server.

### Late deliveries

GitHub does not guarantee order, and retries land at any time. Each item has a high-water mark:
the newest GitHub update time the bot has applied to it. The mark stops an old delivery undoing a
newer one.

## How a delivery becomes a thread

A delivery is one webhook request from GitHub. Each delivery goes through these steps.

1. `POST /webhooks/github` checks the signature against the raw body, then decodes the body.
   Nothing in this step touches Discord.
2. The bot answers `ignored` to events and actions it does not act on, and writes no row for them.
   This keeps stars and forks out of the queue. Pushes to an open pull request are acted on and do
   get a row. They are the largest single share of what the queue holds.
3. The bot claims the delivery by its `X-GitHub-Delivery` id and writes it to `webhook_events` with
   its payload.
   - Anything new answers `accepted`.
   - A repeat answers `duplicate`.
   - A repeat of a delivery the bot has given up on (after a permanent error, or once
     `SHANNON_WORKER_MAX_ATTEMPTS` runs out) is the exception. The bot puts it back on the queue
     with its attempts reset and answers `accepted`. GitHub's Redeliver button reuses the delivery
     id, so this is how a redelivery takes effect. A repeat of a delivery still waiting for a retry
     answers `duplicate`.
4. The worker leases a batch with `SELECT ... FOR UPDATE SKIP LOCKED`. A lease holds the rows for
   a set time, so no other worker takes them. The worker handles the batch oldest first, one
   delivery at a time.
5. One transaction finds the repository and channel, upserts the item, records who is on it and
   renders the metadata. The bot calls Discord only after the transaction commits, never inside it.

### Order and failures

The queue does not guarantee order. A failed delivery is retried after a backoff, behind
deliveries that came after it, and a second worker leases different rows. The item's high-water
mark protects against this. The high-water mark is the newest GitHub `updated_at` the item was
written from. When two deliveries carry the same `updated_at`, the one that reached the bot later
wins. An older delivery cannot undo a newer one.

- A failure that can heal reschedules the whole delivery, until the attempts run out
  (`SHANNON_WORKER_MAX_ATTEMPTS`, 16 by default). So every step is written to be repeatable:
  upserts rather than inserts, compare-and-swap on the thread pointer, claims taken before posting.
- A failure that cannot heal, such as a missing permission or a channel that cannot hold threads,
  is given up at once.

## Install

This section sets up a checkout to work on the code. To run the bot for yourself, follow
[INSTALLATION.md](INSTALLATION.md) instead.

You need Python 3.12 or newer, Docker for the database, and [uv](https://docs.astral.sh/uv/). Or
you need [Nix](https://nixos.org/download/) and a git clone of this repository: see
[With Nix](#with-nix).

```bash
uv sync --extra dev --locked        # runtime and dev dependencies
cp .env.example .env                # to change POSTGRES_*, do it now: they are read only once
docker compose up -d --wait db      # PostgreSQL 17 on localhost:5433
uv run alembic upgrade head
uv run shannon
```

At startup the app does not open its port until the database answers and has been migrated.
The GitHub and Discord settings come later, in
[Connecting it to GitHub and Discord](#connecting-it-to-github-and-discord). With
`SHANNON_DISCORD_TOKEN` empty, the app still serves the endpoint and works the queue, and logs a
warning that it is doing so. `.env.example` puts a placeholder there: blank that line if you have no
token yet, or the app tries the placeholder as a real token, fails to log in and exits.

### Checks to run before you push

CI runs these checks:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                          # strict on shannon, relaxed on tests
uv run pyright                       # the engine behind Pylance, so the editor agrees
uv run pytest -q --cov
```

- **Files not clean yet.** Both type checkers keep a list of files that are not clean yet, in
  `pyproject.toml`. A file leaves that list when it is fixed and does not go back. Every file not
  on the list must stay clean.
- **Pull requests.** These jobs run on every pull request.
- **Pushes to `main` and version tags.** CI first asks whether this exact tree has already been
  through the workflow and passed. If it has, CI runs only the audit and the publish. A merge
  commit is a new commit, but its content is what the pull request already tested.
- **The audit always runs.** Whether a dependency has a known vulnerability depends on today, not
  on the tree.

Before your first pull request, read [CONTRIBUTING.md](CONTRIBUTING.md). It covers the rest: the
integration tier's own database, the conventions the tests hold a change to, and commits and pull
requests.

### With Nix

`nix develop` is the whole install (#175). It opens a shell that holds everything above, at the
versions `flake.lock` names:

- Python 3.14
- uv, at the version CI pins
- PostgreSQL 17, in place of Docker
- Node.js, for pyright
- git, gitleaks, openssl, curl and jq

Before you start:

- **Turn on flakes.** A fresh install from nixos.org has flakes switched off. Add
  `experimental-features = nix-command flakes` to `~/.config/nix/nix.conf` once. Every command
  after the first one below assumes you did. Otherwise put
  `--extra-experimental-features 'nix-command flakes'` after `nix` in each command, as the first
  one does.
- **Use a git clone.** Nix reads a flake from the files git tracks, so it leaves the ignored
  `.nix/` out. In an unpacked archive it copies the whole tree into its store every time you enter.
- **First entry is slow.** It downloads a few hundred megabytes. After that, entering takes about
  a second.

```bash
nix --extra-experimental-features 'nix-command flakes' develop
cp .env.example .env                # its database URL already names this shell's database
shannon-db start                    # PostgreSQL 17 on localhost:5433; never started on entry
uv run alembic upgrade head
uv run shannon
```

When you enter, the shell runs `uv sync --extra dev --locked` and activates the result, so the
checks above run exactly as written. It also sets `SHANNON_TEST_DATABASE_URL` to the database's
`shannon_test`. With the database started, `uv run pytest -q --cov` then runs the integration tier
and reaches the coverage floor. With the database stopped, that tier errors at setup instead of
skipping. So run `shannon-db start` first, or set the variable to an empty string to skip the
tier, as CI's no-database step does.

`shannon-db` manages the shell's database:

| Command | What it does |
| --- | --- |
| `shannon-db start` | Starts the database. |
| `shannon-db stop` | Stops the database. |
| `shannon-db status` | Says whether the database is up. |
| `shannon-db psql` | Opens a prompt on the database. Takes any psql arguments, a database name too. |
| `shannon-db reset` | Throws the data away and starts again. |

Everything the shell writes stays under `.nix/`: one environment per Python version in
`.nix/venv/`, uv's cache, and the database. The shell leaves `.venv` to the steps without Nix, so
both ways work in one checkout. The two are kept apart because an environment built inside the
shell runs only with the shell's libraries.

- **Other versions.** `nix develop .#py312`, `.#py313` and `.#py315` are the rest of the CI matrix.
  Each has its own environment.
- **One database on the port.** The shell's database uses the same port the compose one publishes,
  so the URL in `.env.example` reaches either. Only one can run at a time. If the port is taken,
  `shannon-db start` says so, instead of failing somewhere less obvious. Leaving the shell leaves
  its database running, and nothing on the compose side tells you. Stop it before you go back to
  Docker: `shannon-db stop` inside the shell, or `nix develop --command shannon-db stop` from
  outside it.
- **Where it runs.** Linux on x86_64 and ARM, and macOS on Apple silicon. Not Intel Macs: nixpkgs
  stopped building for them after 26.05. On Windows, run it inside WSL 2, in a clone made there.
- **Programs from outside the shell.** On Linux the shell puts its own libstdc++ ahead of the
  system's. This is what lets PyPI's wheels import. But a distribution's own C++ program, started
  inside the shell, can then fail with a `GLIBC` version error on a release older than glibc 2.38,
  such as Ubuntu 22.04 or Debian 12. Start those programs outside the shell.
- **Moving the versions.** `nix flake update` moves every tool at once. Dependabot does not read
  `flake.lock`. `nix flake check` fails while the shell's uv is not the one `ci.yml` pins, so the
  two move together.
- **CI uses the shell too.** A job runs every check above inside the shell, on Linux x86_64, Linux
  ARM and macOS, as part of the `CI` check.

## Connecting it to GitHub and Discord

This repository does not configure GitHub or Discord, and the bot cannot do either for you.
[INSTALLATION.md](INSTALLATION.md) has every step: the Discord application, the GitHub App, the
optional board OAuth App, the invite and the first commands. This section explains the parts that
need more than a step.

### The GitHub webhook

The webhook is the GitHub App's own, signed with `SHANNON_GITHUB_APP_WEBHOOK_SECRET`. The six
events it subscribes to are listed in [INSTALLATION.md](INSTALLATION.md#2-make-the-github-app). The
bot answers any other event `ignored` and writes no row for it.

A per-repository webhook signed with `SHANNON_GITHUB_WEBHOOK_SECRET` is still accepted while a
deployment moves across to the App. A new deployment leaves that setting empty. Delete the
per-repository webhook once the App is installed. Until you do, GitHub sends everything twice and
every commit line posts twice (see [One bot, several servers](#one-bot-several-servers)).

The endpoint checks each delivery's signature against both secrets. If neither is set, it fails
closed: every delivery answers 500 rather than being waved through, so a missing secret shows up
at once. On GitHub's Recent Deliveries page:

- **401**: the delivery matches no configured secret. Either the secret is wrong, or the one it
  was signed with is not set here while the other one is.
- **500**: neither secret is set.
- **200 answering `ignored`**: the event arrived, and this bot does not act on it.

Adding a permission to an installed App suspends its event delivery until somebody accepts the
change. Until then no check suite arrives at all, and the feature looks broken rather than
pending. When the first check suite does arrive, the log says so.

### The Discord bot

**Intents.** The bot needs no privileged intent, unless `SHANNON_CAPTURE_DISCORD_MESSAGES` is on.
Then tick Message Content under Bot first; see `/log_conversation` below. Otherwise there is
nothing to turn on and nothing for Discord to approve:

- Pinging somebody uses the account map that `/link` builds.
- Reading somebody's roles uses what Discord sends with the command itself.
- Following a board link asks Discord for that one member over REST. That needs no intent either.

**Permissions.** [INSTALLATION.md](INSTALLATION.md#6-invite-the-bot-to-your-server) lists the six
the invite asks for. Two of them are easy to miss, and without either one things fail after
everything looks fine.

**`Read Message History`** lets the bot edit the metadata block. The metadata block is one
message. The bot writes it when the thread opens and rewrites it on every later event. To rewrite
it, the bot must read it first, and Discord counts that as reading history even though the bot
wrote it. Without this permission the thread appears once, correctly, and then never changes.
Every later delivery for that item is refused as a missing permission, not retried.

**`Manage Threads`** shuts a finished item's thread. To shut a thread means to lock it against
replies and archive it out of the channel; Discord's client calls this closing a thread. The bot
also shuts a draft card's thread when the card is archived or deleted off the board. Lock and
archive go in one edit, so a server without the permission gets neither, and its threads stay
open.

The bot also needs `Manage Threads` to reopen a shut thread and write in it. So a comment on a
closed issue needs it as much as the close did.

- Grant it before anything closes.
- Adding it later is enough on its own, because the row remembers that a thread is owed a shut.
- Taking it away later is not safe. The shut threads cannot be reopened to write in, so those
  items stop mirroring until the permission is back.
- Draft cards do not catch up when it is added later. The bot reads the board every couple of
  seconds, so it writes off a refused shut or reopen instead of asking again. A card archived,
  restored or deleted while the permission was missing keeps its thread as it was.

A repository registered before `/register` checked this permission never passed the check, and
the permission can be revoked at any time. So a refused close says so in the thread, not only in
the log.

**What `/register` and `/set_channel` check.** Both refuse a channel where the bot lacks View
Channels, Send Messages in Threads or Manage Threads. They also refuse a text channel where it
lacks Create Public Threads, and a forum where it lacks Send Messages. Read Message History is
the one of these not checked at the door. They also refuse a forum channel set to **Require
Tags**, because nothing here picks a tag and Discord rejects every post without one.

**The first start.** The bot registers its commands globally on its very first start. Discord
serves global commands from a cache that can take up to an hour to catch up, and the log says so.
Until then, typing `/` shows nothing, and nothing is wrong. Wait, or restart the Discord client,
which usually shortens the wait.

### Discord sign-in

Every one-time link goes through Discord before GitHub, so only the member who ran the command can
finish it. A link that went straight to GitHub was finished by whoever clicked it, and GitHub
skips its consent page for an application somebody has authorised before. The sign-in uses the
bot's own application as an OAuth client; [INSTALLATION.md](INSTALLATION.md#1-make-the-discord-application)
sets it up. Until its client id and secret are set, every command that hands out a link refuses,
and the bot says so at startup.

### Then, in the server

[INSTALLATION.md](INSTALLATION.md#7-set-it-up-in-discord) gives the commands in order. Only
`/register` has to come first. The channel you run it in becomes the pull request channel;
`/set_channel pull requests #channel` moves it later. Each kind of item goes to its own channel:

- Issues fall back to the pull request channel until they are given one of their own.
- Project tickets have no fallback. A board stays unmirrored until `/set_channel` names a channel
  for them.

The issue fallback lasts until either kind's channel is set. If you run
`/set_channel pull requests` with a new channel, the bot first gives issues their own channel: the
one they were already using. So issues stay where they are and do not follow the pull requests.
The reply says so and names the command that moves them. Setting one kind's channel moves that
kind's threads and no others.

`/link @member` posts a public note asking that person to run `/link`. It links nobody: the only
account this bot connects is the one somebody has just signed into.

**The role given to `/link_team`** needs **Allow Anyone To @mention This Role** turned on in its
settings. Discord notifies a role's members only when that is on, or when the sender holds
Mention @everyone, @here, and All Roles. Roles are created with it off, so otherwise the ping shows
in the thread as a blue pill and reaches nobody. `/link_team` says so when it sees it. The other
way is to add Mention @everyone, @here, and All Roles to the invite. That is safe here because the
bot refuses to resolve `@everyone` in anything it sends, whoever wrote the text.

### Pointing a kind somewhere new takes its threads with it

Discord cannot move a thread between channels. So when you set a kind's channel to a new one:

1. Each item gets a replacement thread in the new channel.
2. The thread it leaves gains a line linking to that replacement.
3. The bot locks and archives the old thread. The mirrored comments stay readable there.

Ten threads move per run, and the reply says how many are left. To carry on, run the same command
again with the same channel; a long backlog takes a few runs.

This fixes registering in the wrong channel, which used to be permanent. The mapping was
corrected and new items landed correctly, but every item that already had a thread went on being
written to in the wrong place.

## Running it on a server

`compose.prod.yaml` and `Caddyfile` are the server stack. They do these things that the laptop
stack does not:

- They pull the image that CI built. Nothing is built on the server.
- They publish no port except Caddy's. The database and the app are reachable only across the
  compose network.
- Caddy terminates TLS.
- Caddy answers 404 to `/docs`, `/redoc` and `/openapi.json`. FastAPI otherwise serves these to
  anyone.

Like the laptop stack, the server stack runs `alembic upgrade head` to completion before the app
starts. Nothing else would catch a deploy that skipped it: the startup check proves only that the
database was migrated at all, not that it reached the revision this code expects.

### First start and upgrades

[INSTALLATION.md](INSTALLATION.md#5a-on-a-server) has the first start, and
[Keeping it up to date](INSTALLATION.md#keeping-it-up-to-date) has how to deploy a new version
with `scripts/deploy.sh`. [scripts/README.md](scripts/README.md) has the full deploy
procedure.

Only one copy may run at a time. Two copies would both hold the Discord gateway, and both would
lease deliveries from the queue (take them to work on).

What the deploy script does:

- It pins the image to `sha-<commit>`, not to a moving tag such as `edge`. So you know exactly
  what is running.
- It refuses if CI never published an image for the commit you asked for.
- It rolls the image back if the new container never reports healthy.
- It reports what it did in the ssh session it was run from.
- It cannot roll the database back. The migration has been applied, and the script does not run
  `alembic downgrade`. `scripts/README.md` says so plainly.

**A person decides when to deploy, not the script.** A pull loop would restart a live bot on a
green merge with nobody watching, for example on a Friday evening. The cost is that the server
stays behind `main` until somebody deploys. The hourly `Deployed` workflow opens an issue when that
happens. That issue is a reminder, not a failure.

`curl https://<hostname>/health` returns the commit that is running. Use it to tell a broken change
from an undeployed one. Once, a merged but undeployed change was taken for a bug in the bot for an
afternoon.

### One bot, several servers

One instance serves every Discord server it is invited to. Because of that:

- Commands register globally.
- Every table that holds a decision a server made is keyed by guild (Discord's word for a server):
  which repository it mirrors, which channel each kind of item threads into, who is linked to
  whom, who asked not to be pinged.
- A webhook finds its server through the repository it came from, never through a configured
  guild id.

What is shared, and what that costs:

- **Nothing shared reaches a repository any more.** Every GitHub call carries a token minted for
  the installation that covers the repository's owner. So the call can see only what that account
  granted. This makes private repositories safe to register. Before it, one token saw everything,
  and `/register` needed only the Admin or Project Manager role in a server this bot was invited
  to. Now `/register` also needs the caller to prove to GitHub that they administer the repository
  (#135).
- **One GitHub App, so one private key.** It is the credential to protect, because it mints tokens
  for every installation. Losing it is worse than losing the token it replaced.
- **One OAuth App for project boards, and no shared token.**
  - The App cannot reach a board at all: GitHub's Projects permission exists at organisation level
    only.
  - Asking the App for an organisation permission suspends event delivery to the installation
    until somebody accepts. That would stop every webhook in every registered repository.
  - So boards go through a separately registered classic **OAuth App**. Each person authorises it
    for themselves: `/board link` asks as part of linking a board, and `/board authorise` asks on
    its own.
  - An organisation's board also needs the organisation to approve that app, because
    organisations restrict OAuth apps by default. GitHub's sign-in screen shows a **Grant** button
    to an owner and a **Request** button to anybody else.
  - Where the organisation enforces SAML single sign-on, it also needs an active SSO session.
  - Neither of these is a scope, so no scope fixes them.
- **A board is read as whoever linked it, and a card is moved as whoever moved it.** This replaced
  one classic token that belonged to one human account and was shared by every server. That token
  had the `project` scope across every project the account could see. So one leak was write
  access to all of them, and the board's history named the token's owner whoever had actually
  asked. Authorisations are stored encrypted with `SHANNON_BOARD_CREDENTIAL_KEY`. This is the only
  encrypted column in the schema. Anybody can withdraw theirs at any time with `/board withdraw`,
  which needs no role.
- **One webhook secret per source.** The App has its own. The endpoint also accepts
  `SHANNON_GITHUB_WEBHOOK_SECRET`, so a repository configured the old way keeps working while a
  deployment moves across. Delete the per-repository webhook once the App is installed. Until you
  do, GitHub sends everything twice under different delivery ids. The queue's own duplicate check
  cannot see this, and every commit line is posted twice.
- **One set of role names.** `SHANNON_ROLE_*` are read once at startup and apply everywhere. Each
  takes a comma-separated list of names, so one deployment can accept several names for a tier (a
  permission level: Admin, Project Manager, Reviewer, Developer). A server that calls its managers
  something else gets no tier from its roles. Then only guild administrators pass the role checks,
  plus, where `SHANNON_GITHUB_ACCESS_GRANTS_TIERS` is on, members whose proved GitHub account has
  write access (counts as Developer) or admin access (counts as Project Manager too, but not for
  `/register`, `/link @someone` or the Admin tier, which stay Discord-only) (#94). This is the
  setting that surprises people most.
- **One board per repository, set from Discord.** `/board link` records which project a server's
  repository mirrors. So two servers each get their own board, and the bot never guesses one. A
  board is read under the authorisation of whoever linked it, so `/board link` is the only way to
  mirror one. No setting names a board.
  - A board linked as the repository owner's own board follows a renamed repository. It also
    follows a renamed account, once the bot has recorded the account id.
  - When the repository is transferred to another account, the board stays with the old owner.
    The same number under the new owner is a different board.
  - When the bot cannot tell which of these happened, it keeps the board under the old owner and
    stops reading it until somebody links it again. After a transfer, link it naming the old
    owner, since a bare number would now mean the new owner's board. After an account only renamed
    itself, link it by the bare number, because GitHub releases the old login.
- **One poller, still.** Nothing elects a leader. Two pollers racing on one card can each put its
  row back and undo the other's finished move, permanently. Set `SHANNON_POLL_BOARDS=false` on
  every replica but one to prevent that. This used to be done by setting the project number to
  zero. That stopped being enough once a board could be linked by a command: a second replica
  would start polling as soon as somebody ran it, with no environment change anywhere to notice.

### Adding a server

Invite the bot, install the GitHub App on the new server's repository, and run the commands in
the new server: [INSTALLATION.md](INSTALLATION.md#keeping-it-up-to-date) has the steps. The App's
webhook already covers the repository. Do not add a per-repository webhook, and delete any that
exists, or every event arrives twice.

A global command takes up to an hour to appear the first time. That looks like a broken deploy,
but it is not.

### Limits that do not change

- One repository per server.
- One server per repository.

`/unregister` undoes a binding. Only somebody who has proved to GitHub that they hold admin on the
repository can complete it. A Discord role cannot decide that, and an earlier `/link` does not
count: the proof must be fresh. `/unregister` also throws away every mirror record. The threads
already in the channel are orphaned, and registering again opens new ones.

## Configuration

Shannon reads its settings from environment variables that start with `SHANNON_`, or from a `.env`
file. Every setting has a default, so none is required. A missing setting does not stop Shannon
from starting. It fails later, when something needs that setting. A value of the wrong type or out
of range (for example a timeout of `0`) does stop it from starting, and so does the lease rule
under [the worker table](#delivery-worker).

Two words used below:

- A **delivery** is one webhook event that GitHub sent.
- An **HMAC secret** is a secret shared with GitHub. GitHub signs each delivery with it, and Shannon
  checks the signature, so only GitHub can send deliveries.

### Database, server and logs

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_DATABASE_URL` | `postgresql+asyncpg://shannon:shannon@localhost:5433/shannon` | The database. The default is the compose database |
| `SHANNON_API_HOST` | `0.0.0.0` | The address the HTTP server listens on |
| `SHANNON_API_PORT` | `8000` | The port the HTTP server listens on |
| `SHANNON_LOG_LEVEL` | `INFO` | Uppercased, not validated |
| `SHANNON_BUILD` | `unknown` | The commit the image was built from. `/health` reports it. The Dockerfile writes it, so do not set it: compose passes `.env` into the container, and a value there would override the real one. For that reason it is the only setting missing from `.env.example` |

### GitHub

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_GITHUB_WEBHOOK_SECRET` | empty | The repository webhook's HMAC secret. When this and `SHANNON_GITHUB_APP_WEBHOOK_SECRET` are both empty, every delivery gets 500 instead of being let through |
| `SHANNON_GITHUB_APP_WEBHOOK_SECRET` | empty | The App's own HMAC secret. Shannon accepts it alongside `SHANNON_GITHUB_WEBHOOK_SECRET` while a deployment moves across |
| `SHANNON_GITHUB_APP_CLIENT_ID` | empty | The App's client id. One value serves both as the JWT issuer and as the OAuth `client_id` |
| `SHANNON_GITHUB_APP_PRIVATE_KEY` | empty | The `.pem` GitHub issued, on one line, with each newline written as `\n`. It signs the JWT that Shannon trades for an installation token |
| `SHANNON_GITHUB_APP_CLIENT_SECRET` | empty | Exchanges the OAuth code for `/link`, `/register` and `/unregister`. Empty makes all three refuse rather than hand out a broken link. Since #135, a deployment without it cannot bind a repository at all |
| `SHANNON_PUBLIC_BASE_URL` | empty | The origin that every OAuth `redirect_uri` is built from, for Discord and for GitHub. It must match the redirects set on the App, the board OAuth App and the Discord application. It is the same origin GitHub already reaches for webhooks |
| `SHANNON_GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise |
| `SHANNON_GITHUB_OAUTH_URL` | `https://github.com` | Where `authorize` and `access_token` live, which is not `api.github.com`. For GitHub Enterprise, beside `SHANNON_GITHUB_API_URL` |
| `SHANNON_GITHUB_TIMEOUT_SECONDS` | `10.0` | The timeout on a request to GitHub |

A **JWT** is a signed token that proves a request comes from the App. GitHub trades it for an
**installation token**, which can see only the one account the App is installed on.

### Discord

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_DISCORD_TOKEN` | empty | Bot token. Empty runs without the gateway |
| `SHANNON_DISCORD_CLIENT_ID` | empty | The bot's own Discord application, used as an OAuth client |
| `SHANNON_DISCORD_CLIENT_SECRET` | empty | Exchanges Discord's code for who signed in. The bot token is a separate credential |

What the Discord sign-in does:

- Every one-time link opens on this bot first. The bot asks Discord who is holding the browser
  before it asks GitHub anything. So a link only works for the member who ran the command.
- Shannon asks Discord for nothing else: the scope is `identify`.
- If the client id or the client secret is empty, `/link`, `/register`, `/unregister`,
  `/board link` and `/board authorise` refuse. They do not hand out a link that anybody it was
  forwarded to could finish. The bot says so at startup.

To set it up, in the Developer Portal, under OAuth2 for the bot's application:

1. Copy the client id into `SHANNON_DISCORD_CLIENT_ID`.
2. Reset the client secret and put it in `SHANNON_DISCORD_CLIENT_SECRET`.
3. Add `<SHANNON_PUBLIC_BASE_URL>/oauth/discord/callback` as a redirect.

### Roles and access

A **tier** is a level of permission: Admin, Project Manager, Reviewer or Developer. A Discord role
puts a member in a tier.

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_ROLE_ADMIN` | `Admin` | Role names per tier, comma separated for more than one |
| `SHANNON_ROLE_PROJECT_MANAGER` | `Project Manager` | |
| `SHANNON_ROLE_REVIEWER` | `Reviewer` | Grants no command today. Deciding that a change is good and recording that the project has accepted it are different jobs, and Shannon records only the second |
| `SHANNON_ROLE_DEVELOPER` | `Developer` | |
| `SHANNON_REQUIRE_PROVED_LINKS` | `false` | Whether a link nobody proved may be used to write to GitHub (#144). See below |
| `SHANNON_GITHUB_ACCESS_GRANTS_TIERS` | `false` | Whether GitHub access may stand in for a Discord role (#94). See below |

`SHANNON_REQUIRE_PROVED_LINKS` (#144):

- Before #144, `/link` recorded a login somebody typed, and GitHub was never asked whose it was. A
  wrong one acts on a repository under another person's name.
- Off by default. Turning it on before people have run `/link` again refuses every assignment.
  Until then, the reply says the link is unproved.
- A link made since #144 is proved when it is made. It stays proved until its member signs in from
  the same server as another GitHub account for `/register`, `/unregister` or `/board`, because
  only the last sign-in is kept. So this can refuse two kinds of row: rows written before #144, and
  rows whose member has since signed in as somebody else.
- Ignored where `SHANNON_GITHUB_APP_CLIENT_ID`, `SHANNON_GITHUB_APP_CLIENT_SECRET` or
  `SHANNON_PUBLIC_BASE_URL` is unset.
- Still enforced where only Discord sign-in is missing. There a member who never proved a link is
  refused, and cannot prove one until `SHANNON_DISCORD_CLIENT_ID` and
  `SHANNON_DISCORD_CLIENT_SECRET` are set. `/link` says so.
- Separate from the rule added by #135, which is about mentions, not writes. A link with no account
  id behind it no longer turns into a Discord ping at all, whatever this setting says. A typed login
  could reach the wrong member, and the thread cannot show that.
- A member can see whether their own link is proved with `/linked`.

`SHANNON_GITHUB_ACCESS_GRANTS_TIERS` (#94):

- On: a member with no Developer role, whose own account has write on the registered repository,
  passes wherever Developer does. GitHub admin also passes wherever Project Manager does.
- The Admin tier, `/register` and asking somebody else to `/link` stay Discord only. A tier
  switched off by an empty role setting stays off.
- It counts only for an account proved with `/link` since migration `0039`. A member who proved
  one before runs `/link` once more.
- It fails closed: where GitHub cannot be asked, nothing is granted and the role alone decides.
- Off: every gate is the role check alone, and asks GitHub nothing about letting anybody in.
- It needs the same settings `/link` does, and says so at startup where they are missing.

See [Commands](#commands).

### Project boards

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_GITHUB_BOARD_CLIENT_ID` | empty | The **second** registered application: a classic **OAuth App**, not a GitHub App. Used only to authorise a project board. See below |
| `SHANNON_GITHUB_BOARD_CLIENT_SECRET` | empty | Exchanges the OAuth code for a board authorisation. Empty makes `/board link` and `/board authorise` refuse, rather than send somebody to GitHub to grant something that cannot be used. An empty `SHANNON_BOARD_CREDENTIAL_KEY` does the same |
| `SHANNON_BOARD_CREDENTIAL_KEY` | empty | The Fernet key that board authorisations are encrypted with. See below |
| `SHANNON_POLL_BOARDS` | `true` | Whether **this** process reads project boards. Run the poller in one replica only: nothing elects a leader, and two pollers racing on one card can each undo the other's finished move. This switch says which replica polls, a job a board number in the environment used to do |
| `SHANNON_PROJECT_POLL_SECONDS` | `2.0` | How often linked boards are read. It must be at least `1.0`, or Shannon does not start. A board that fits in one page is read with a conditional request, so an unchanged board costs no rate-limit budget. A board that needs more than one page cannot be read that way and is read whole every time. While any linked board is like that, every board is read every 60 s instead |
| `SHANNON_BOARD_MAY_MOVE_CARDS` | `true` | Whether a status set in Discord moves the item's card on the board. See below |
| `SHANNON_BOARD_MAY_SET_STATUS` | `false` | Whether dragging a card may change the item's status. Off, because nothing GitHub sends says who moved a card. A board that could move items would be a way past the Project Manager role |

A **replica** is one running copy of Shannon. The **poller** is the part that reads boards.

Why boards need a second application: GitHub publishes no App permission for a user-owned board.
Granting an installed App an organisation permission suspends its event delivery until an admin
accepts. So boards cannot go through the App above. Register the OAuth App with:

- redirect URI `<SHANNON_PUBLIC_BASE_URL>/oauth/github/callback`
- wildcard matching **off**
- device flow **off**
- *Expire user access tokens* **off**

`SHANNON_BOARD_CREDENTIAL_KEY`:

- It is the only encryption in this schema. A board authorisation is the one thing stored here
  that *acts as* somebody, rather than describing them.
- It is a comma-separated list, newest first. The cipher writes with the first key and reads with
  any of them, so a key rotation does not make everybody authorise again.
- Unset or unreadable means boards are off, and the bot says so once. The process still starts.
- Generate a key with:

  ```sh
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

`SHANNON_BOARD_MAY_MOVE_CARDS`:

- On by default, because a server that ran `/board link` asked for its board to be the truth.
- The card is written as whoever moved it, with the authorisation they granted through
  `/board authorise` or `/board link`. That is the `project` scope, and the sign-in checks that
  GitHub actually granted it.
- `/status` and `/priority` refuse somebody who has granted none, before anything is written.
- Turning it off stops the card being written, and nothing else. The board is still read, so a
  move that the board's column order forbids is still refused.
- Either way, Shannon leaves alone a card it has never seen on a board. It learns which card
  belongs to which item by polling, and GitHub answers no per-item project lookup.

#### How a board is read

Shannon reads a project board on a timer, because GitHub does not deliver changes for every board:

- GitHub sends `projects_v2` webhooks for organisation projects only, and none at all for a
  personal account's.
- The `project_card` events the requirements name belong to Projects (classic), which was sunset
  in August 2024.

Shannon reads the board with the authorisation of whoever linked it. So that person must be able
to see the board. An organisation's board also needs the organisation to approve the board OAuth
App.

A board's tickets (its cards, mirrored into Discord threads) need a channel. They have no
fallback, so `/set_channel project tickets` is what turns the mirror on.

#### Who owns a board

Either kind of owner works: a user or an organisation. Shannon asks GitHub which kind it is,
because a setting for it could be wrong and go stale. The board number alone cannot tell you the
owner. GitHub numbers boards per account, so `github.com/users/you/projects/3` and
`github.com/orgs/yours/projects/3` are two different boards.

When the board is not owned by the account that owns the registered repository, give
`/board link` an `owner`, or paste the board's URL, which carries the owner. Without one, Shannon
takes the owner from the repository's name. That is right for most deployments. For the rest it
is quietly wrong, not obviously broken.

#### Cards taken off a board

Shannon also notices a card taken off the board (#198).

- An archived card comes back from the read, marked as archived.
- A card missing from the read proves nothing. A board past its page cap, or a card dragged during
  the read, can leave a card out without saying so.
- So the poller asks GitHub about that one card on its own, under the same authorisation. Per
  board, it asks about at most one card every ten seconds. It asks again about a card GitHub still
  has after ten minutes, and about an archived card hourly. That is 360 requests an hour at worst,
  and none while nothing is missing.
- A card counts as deleted only once GitHub has answered 404 for it twice, on two passes.
- Shannon only asks about the linked board's own cards. So relinking to another board never shuts
  the old board's threads.

### Delivery worker

The worker is the part that acts on deliveries. It **leases** a batch of deliveries: it claims them
for a fixed time, so no other worker takes them.

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_WORKER_POLL_SECONDS` | `2.0` | How often an empty queue is checked |
| `SHANNON_WORKER_BATCH_SIZE` | `10` | How many deliveries one lease claims |
| `SHANNON_WORKER_MAX_ATTEMPTS` | `16` | Roughly two hours of backoff before a delivery is dropped |
| `SHANNON_WORKER_MAX_BACKOFF_SECONDS` | `900.0` | Cap on the doubling delay between attempts |
| `SHANNON_WORKER_LEASE_SECONDS` | `900.0` | How long a leased delivery is held |
| `SHANNON_WORKER_DELIVERY_TIMEOUT_SECONDS` | `60.0` | Deadline on one delivery |
| `SHANNON_WORKER_SHUTDOWN_GRACE_SECONDS` | `5.0` | How long shutdown waits for the work in hand, once for each loop it stops |
| `SHANNON_DELIVERY_RETENTION_DAYS` | `7` | How long finished deliveries and their payloads are kept |

One rule spans settings: `worker_lease_seconds` must cover
`worker_batch_size * worker_delivery_timeout_seconds`, or construction of the settings fails and
Shannon does not start. A lease that expired mid-batch would let a second worker take deliveries
this one is still on.

### Conversations and reminders

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_CAPTURE_DISCORD_MESSAGES` | `false` | Whether `/log_conversation` works. Tick the message content intent in the Developer Portal first; see `/log_conversation` under [Commands](#commands) |
| `SHANNON_CONVERSATION_QUIET_SECONDS` | `60.0` | How long a logged thread must go quiet before what was said in it is published |
| `SHANNON_CONVERSATION_FLUSH_TICK_SECONDS` | `5.0` | How often the publisher looks for a conversation that is ready |
| `SHANNON_REMINDER_TICK_SECONDS` | `5.0` | How often the reminder sender looks for a reminder that has fallen due |

### Where private repository content ends up

Registering a private repository copies parts of it into places GitHub does not control. Know this
before you point Shannon at one, and when somebody asks what a backup contains.

| Where | What |
| --- | --- |
| `webhook_events.payload` | The whole delivery body: titles, descriptions, comment and review text, author names. Pruned seven days (`SHANNON_DELIVERY_RETENTION_DAYS`) after a delivery finishes |
| `tracked_items` | `title`, `github_url`, `shown_labels`, `project_column` and, for board tickets, `shown_fields` (the text of each board field), kept for as long as the repository is registered |
| `item_assignments` | GitHub logins of authors, assignees and reviewers |
| The Discord channel | Everything a thread shows. A thread's NAME is the item's title, and anybody who can see the channel can see it |
| The log | Repository full name and item number on every delivery retry, and item titles at INFO from the project board poller |

Pruning has two gaps:

- A delivery stuck `PENDING` or `PROCESSING` is never pruned, at any age.
- Pruning runs only while the worker runs. A deployment whose worker has died keeps everything.

Nothing here is encrypted at rest beyond what the database and disk already do.

## Commands

Commands work only inside a server (they are guild only). Most commands need a *tier*: a level of
access that a Discord role gives. The tiers that grant commands are Admin, Project Manager and
Developer. Each is a role name set in configuration (`SHANNON_ROLE_ADMIN` and the settings beside
it). A fourth, Reviewer (`SHANNON_ROLE_REVIEWER`), grants no command.

- Role names are matched case insensitively. Renaming a Discord role takes the tier away until the
  setting is changed to match.
- Holding several roles gives the union of what each allows.
- A guild administrator passes every gate, whatever the configuration says.

Replies are ephemeral (only the person who ran the command sees them), with two exceptions. Both
are addressed to somebody who is not watching for a reply:

- `/link @member` posts its note where that person can see it.
- A reminder goes off in public, in the channel it was set in. Setting one is private.

In the table, *or GitHub write* and *or GitHub admin* mark where GitHub access can stand in for the
role, when a server turns that on. See "GitHub can let somebody in" below.

| Command | Who | What |
| --- | --- | --- |
| `/register <github_repo_link>` | Admin, Project Manager, **and GitHub** | Binds a repository to this server and points PR threads at the current channel. Run it once to get a one-time link that proves who you are on GitHub, then run it again with the same repository link to finish. Only an account with admin on the repository can do it: mirroring a repository into a channel discloses everything in it, and a Discord role cannot establish who may make that decision. One repository per server. If the GitHub App is not installed on the repository, it refuses and gives a link. GitHub access never stands in for the role here, even where a server allows it elsewhere: until this has run there is no repository to ask about |
| `/unregister <repository>` | Admin, Project Manager or *GitHub admin*, **and GitHub** | Unbinds it. Run it once to get a one-time link that proves who you are on GitHub, then again to finish. Only an account with admin on the repository can do it. A Discord role cannot establish that, and a linked account is not enough: the command needs a sign-in made just now. You type the full name as confirmation. Everything mirrored is forgotten, and the threads already open are orphaned: they stay where they are and stop updating |
| `/board link <board> [owner]` | Admin, Project Manager, *or GitHub admin* | Chooses which GitHub project board this server's repository mirrors. The picker lists the boards that owner has, read as you, so your private boards are offered. Until you have authorised, the list is empty and nothing is asked of GitHub: type the board's number, or paste its URL, which carries the owner. The board is opened with your own authorisation before it is stored, so a wrong number is refused here with a sentence, not a warning in a log. If you have not authorised yet, or the board will not open with what you granted, it hands you one link. Signing in there authorises **and** links; there is nothing else to run. Following the link asks Discord again whether you still hold the role, and GitHub too where that counts. So a role taken away in between cancels the link, and for a member GitHub let in, so does GitHub not answering. Two servers may not share a board, because each would open its own thread for every card |
| `/board unlink` | Admin, Project Manager, *or GitHub admin* | Stops mirroring the board. It also forgets the authorisation the board was read with, and the reply names whose it was: forgetting this copy does not revoke the grant, and only that person can do that, on GitHub |
| `/board authorise` | Admin, Project Manager, *or GitHub admin* | Makes the cards you move with `/status` and `/priority` move as you on GitHub. It gives one link, and signing in is all there is to do. Following the link asks Discord again whether you still hold the role, and GitHub too where that counts. It does not change which board is mirrored |
| `/board withdraw` | Anyone, for their own | Forgets the authorisation you gave this bot in this server. It needs no role on purpose: deleting your own credential must not depend on a role you may have lost since |
| `/board show` | Admin, Project Manager, *or GitHub admin* | Shows which board this server mirrors, who linked it, whether it still opens with their authorisation, and whether you have authorised yourself |
| `/set_channel <object_type> <channel>` | Admin, Project Manager, *or GitHub admin* | Sets where threads of one kind appear, and moves the ones already open there. Only that kind moves: a kind that was borrowing this channel is given it outright instead, so it stays put, and the reply names the command that would move it. Ten threads per run; the reply says how many are left |
| `/pr <pr_link>` | Developer, Project Manager, *or GitHub write* | Fetches a pull request and mirrors it |
| `/issue <issue_link>` | Developer, Project Manager, *or GitHub write* | Fetches an issue and mirrors it |
| `/refresh [scope]` | Developer, Project Manager, *or GitHub write* | Opens a thread for every open pull request, issue and board ticket that has none yet, and redraws every open pull request and issue that has one, from the same listing. Scope is `all`, `pull requests`, `issues` or `tickets`; no scope means `all`. Nobody is pinged: people are named as real mentions, and Discord is told to ring none of them. A thread Discord archived for going quiet is archived again after its redraw. Closed items are left to `/regenerate`, and tickets that already have a thread are left to the board. Each run opens at most 25 new threads across all kinds, then redraws for up to five minutes, starting with the items whose record this bot changed longest ago; the reply says how many of each are left. One run at a time per server, and the next one a minute after the last one finished. Admins wait too, and the reply says how long. `tickets` needs `/board link` and `/set_channel project tickets`; without either, `all` covers the other kinds and says which command is missing. An archived card is never offered |
| `/regenerate` | Developer, Project Manager, *or GitHub write* | Run it inside an item's thread, with no argument. It reads the item from GitHub again and redraws the block, also for a closed item whose thread is locked and archived, which `/refresh` never redraws. Nobody is pinged. It is also what turns a name into a mention for somebody who linked after the thread was opened. One run at a time per thread, and the next five seconds after the last one finished |
| `/track [event] [state]` | Developer, Project Manager, *or GitHub write* | Run it inside an item's thread. It sets which kinds of line that thread gets besides its block: `comments`, `labels`, `commits`, `checks`, `approvals`, `draft`, `people` and `state`. Give a kind and `on` or `off` to change it; leave out the state to see one kind; leave out both to see every kind the item can get. Labels and commits start off, and the rest start on. A kind an issue or a board card never gets is refused, and the reply says which ones it does get. The answer is private, and nothing is posted in the thread |
| `/link [member]` | Anyone, for their own account; Admin or Project Manager to ask somebody else | Connects your GitHub account, so pings become mentions. Run it, open the link, and you are done. GitHub decides which account it is, so nobody types a login and nobody can be connected to an account that is not theirs. It replaces whatever was linked before, including a login somebody else had claimed: a proof beats a claim. Naming a member posts a public note asking them to run it, and connects nobody. That half stays with the role even where GitHub access counts elsewhere, because the note has to be the command's first answer |
| `/linked` | Anyone, about their own account | Says whether you are linked to a GitHub account in this server: which one, since when, whether GitHub has vouched that it is yours, and which repository the server mirrors. It reads what this bot already holds and asks GitHub nothing, so it does not see a GitHub rename since the link, and the answer says to run `/link` after one. Where something is still owed, it says what, and to run `/link`. That covers: a link nothing proves, because it was made without signing in, or a later sign-in as another account replaced its proof; a link whose name GitHub has since given to another account; a link by name alone, from before this bot asked GitHub whose a login was; and a sign-in for `/register`, `/unregister` or `/board`, which links no account to you. Where `/link` cannot run, it says an admin has to finish setting up sign-in instead. There is no member argument, because another member's link names their GitHub account |
| `/link_team <github_team> <role>` | Admin, Project Manager, *or GitHub admin* | Points a Discord role at a GitHub team, so a review asked of that team pings the role. Refused where the registered repository belongs to a personal account: teams belong to an organisation, so GitHub would never ask one for a review there, and the mapping could never match anything |
| `/assign <member>` | Developer, Project Manager, *or GitHub write* | Run it inside an item's thread. It puts that person on the item's assignees, which a pull request and an issue both have. They need a linked GitHub account; if its owner has renamed it since, the new name is followed rather than refused. GitHub takes an assignee with write access or better, and a refusal says which reason it was. Nothing is posted here: GitHub sends the change back, and the ordinary mirror says so in the thread, once, unless the thread ran `/track people off` |
| `/unassign <member>` | Developer, Project Manager, *or GitHub write* | Takes them off the assignees |
| `/request_review <member>` | Developer, Project Manager, *or GitHub write* | Asks that person for a review. Pull requests only, because an issue has no reviewers; on an issue the reply says so and points at `/assign`. A person can be an assignee and a reviewer on the same pull request |
| `/unrequest_review <member>` | Developer, Project Manager, *or GitHub write* | Withdraws the review request |
| `/mentions [state]` | Anyone | Sets whether this bot's messages about items notify you in this server. It does not cover somebody running `/link @you` or a reminder set for you with `/remind`: those are a person addressing you, not this bot reporting on anything. Off still names you on every item you are on, as a mention that Discord shows and does not ring. It does not reach a transcript published to GitHub. With no argument it says which way you have it set |
| `/remind <member> [days] [hours] [minutes] [message]` | Anyone, for themselves; Admin, Project Manager or *GitHub admin* to remind somebody else | Pings them once, here, after the time given. The parts are added together: up to 365 days, 23 hours and 59 minutes, and at least a minute. The message is up to 500 characters and rings nobody else. Refused for a bot, for somebody who cannot see the channel, and where you or this bot cannot post, because it goes off later with nobody there to be told it failed. For a reminder fourteen minutes away or less, the private answer counts down and disappears at zero; for anything further, it gives the time and how long after setting it that is. Up to 25 may be waiting per person per server |
| `/label <name>` | Developer, Project Manager, *or GitHub write* | Run it inside an item's thread. It puts an ordinary label on the item, with a picker listing the labels the repository already has. A name the repository does not have is refused rather than created, because GitHub would create it and nothing here can delete one. Also refused, with a pointer to `/status` or `/priority`: a status label (`status: ` and a column, or one of the older spellings such as `IN_REVIEW`) and anything read as a priority |
| `/unlabel <name>` | Developer, Project Manager, *or GitHub write* | Takes one off |
| `/log_conversation` | Developer, Project Manager or *GitHub write*, and GitHub | Run it inside an item's thread, with no argument. From then on, everything said in that thread is published to the item's GitHub comments, as one comment per burst rather than one per message. It posts a visible line in the thread saying so, and a thread that will not take that line is not logged. It needs `SHANNON_CAPTURE_DISCORD_MESSAGES` and the message content intent, and says so if they are missing |
| `/stop_conversation` | Developer, Project Manager, *or GitHub write* | Stops it, and publishes whatever was still waiting. It works whether or not capture is switched on, so somebody with the tier can always stop a thread that was told logging is on. A member GitHub alone let in is asked again, like any other command, so one who has lost write since cannot |
| `/status <to>` | Project Manager, *or GitHub admin* | Moves the item whose thread you are in. The picker lists this server's board columns first, then whichever of Backlog, Not reviewed, In review and Done the board has no column for. With no board linked, or a board that cannot be read in time, it lists just those four. Every column the board has can be picked, including one this bot has no status for, such as `Ready To Merge` (#246): that moves the card and the label, and the item keeps its status. Done shuts the thread. A card may move forward one column at a time, and back as far as you like, measured against the board's own column order |
| `/priority <to>` | Project Manager, *or GitHub admin* | The same, for High, Medium and Low. There is no way to clear a priority back to none |

### GitHub checks the commands that write to it

Nine commands write to GitHub: `/label`, `/unlabel`, `/status`, `/priority`, `/assign`,
`/unassign`, `/request_review`, `/unrequest_review` and `/log_conversation`. Besides the Discord
role, each one asks GitHub whether the caller's own account may write to the repository.

- It asks only when the caller has proved an account with `/link`. A caller who has not is decided
  by the Discord role alone, unless `SHANNON_REQUIRE_PROVED_LINKS` is on.
- Read and triage are not enough; write, maintain and admin are. GitHub folds `maintain` onto write
  and `triage` onto read before it answers, so those are the whole ladder. The refusal says so, so
  somebody with triage does not think this bot got it wrong.
- `/stop_conversation` is the one logging command deliberately not asked about, for the same reason
  it ignores the capture setting: a thread has been told logging is on, and taking away the way to
  turn it off is worse than letting somebody who has lost access turn it off. Stopping writes
  nothing to GitHub in any case.

This check is a floor, not a wall. It only ever takes access away. It raises the bar for a role
holder who has connected an account, and does not lock out anybody who has not. That also means
that never running `/link` kept the old behaviour.

`SHANNON_REQUIRE_PROVED_LINKS` closes that gap: it refuses an unproved caller outright. It is off by
default, because turning it on takes those commands away from every member who has not linked, and
that is a decision for each deployment. Letting somebody in without the role is a separate setting,
described next. It also needs a proof, and it fails the other way.

The check **fails open** when GitHub cannot be reached, so that an outage does not turn every one of
those commands, in every server, into a refusal. Somebody who waits for an outage gets the
Discord-role-only behaviour back for as long as it lasts, and the log says so while it lasts. This
never helps a member GitHub alone let in: during an outage they are refused.

### GitHub can let somebody in

With `SHANNON_GITHUB_ACCESS_GRANTS_TIERS` on, a member the Discord role turns away is asked about on
GitHub instead (#94):

- Write on the registered repository passes wherever Developer does.
- Admin passes wherever Project Manager does too.

Those are the rows above marked *or GitHub write* and *or GitHub admin*. With the setting off, none
of them asks GitHub whether to let somebody in.

Limits:

- The Admin tier is never granted, so GitHub admin does not pass every gate the way a guild
  administrator does.
- A tier a server switched off by emptying its role setting stays off.
- `/register` and asking somebody else to `/link` stay Discord only. There is no repository to ask
  about until `/register` has run. The note `/link @member` posts has to be the command's first
  answer, and a question to GitHub would come before it.

Which account counts:

- Only the account you proved with `/link`, and only a proof made since migration `0039`. Those are
  the proofs Discord bound to your own browser; one from before cannot show that. So run `/link`
  once more after the setting is turned on.
- GitHub is asked about that account by name, and its answer is held against the account's id. So
  if somebody else has taken the name since a rename, nobody is let in, and the reply says to run
  `/link` again.
- GitHub includes an organisation's base permission and its owners in the answer. In an
  organisation whose base permission is write, every member who links gets the Developer tier.
- GitHub admin counts as Project Manager, so an admin who wrote a pull request can move it with
  `/status` themselves. Keeping `/status` to Project Manager was otherwise there to stop that.

Unlike the check above, this one **fails closed**. It only ever adds access, so where GitHub cannot
be asked, nothing is granted, the reply says GitHub could not be asked, and the Discord role works
as it always did.

- A role holder is never asked about at all.
- Anybody GitHub is asked about sees Discord's "thinking" for a moment before the answer, because
  the question goes out after Discord has been told an answer is coming.
- An answer that lets nobody in is remembered for a minute. Somebody just given write on GitHub
  may wait that long, or run `/link` again. An answer that lets somebody in is never remembered, so
  access taken away on GitHub is gone here at once.
- On a command the check above also asks about, a member let in this way costs GitHub the same
  question twice.

### Commands that need no role

`/mentions` and `/linked` are the two commands with no role behind them. `/link` for your own
account, `/remind` for yourself and `/board withdraw` are the halves of commands that take none.
Every other command decides something about the server; these are about whoever runs them.

- `/linked` only reads back what this server holds about you.
- `/mentions` decides whether your own name notifies you. You should not have to ask a project
  manager to turn off your own pings. It reaches everything this bot writes about items except a
  role: `@org/team` pings the whole Discord role, and Discord gives nobody a way to leave one
  person out of one. A reminder is not about an item, and always rings.
- Connecting your own GitHub account needs no role. It used to need one, because a login nobody
  checked is a claim anybody could make. That was true while somebody typed a login: GitHub was
  asked whether the name existed, never whose it was, so anybody could take any login and receive
  every mention meant for it. Nothing is typed now. GitHub says which account signed in, and a gate
  would only stop somebody proving who they are.

Asking somebody else to connect theirs needs Admin or Project Manager. That half pings a member in
public, and a bot that will ping anybody on anybody's say-so is a spam tool. It hands out no link:
a link is only ever issued for whoever ran the command, so the member is asked to run `/link` and
get their own. Discord refuses anybody else who follows a link, before GitHub is asked anything.

Reminding somebody else needs the same tier, for the same reason: this bot pings them in public on
your say-so. Reminding yourself needs none. Unlike asking somebody to `/link`, GitHub admin counts
here where a server allows it, because the answer to whoever set a reminder is private and can
wait for GitHub.

### The board acts with nobody running it

A poll opens and updates threads for the cards on a board. That is the same work `/pr`, `/issue`,
`/refresh` and `/regenerate` do behind the Developer tier, but there is no caller to check a role
against. Each part is gated differently, on purpose:

- **Mirroring a card into a thread** is behind the Developer tier, like those commands, because
  `/refresh tickets` is its on-demand equivalent.
  - Before that scope existed, it had no flag and no equivalent. It relied on the same reasoning as
    the webhook path, which is ungated by design: on a public registered repository a stranger can
    cause a thread by opening an issue, while adding a card needs write access to the board.
  - It cannot happen at all until somebody with Admin or Project Manager, or GitHub admin where a
    server lets GitHub access stand in for a role, runs **both** `/board link` and
    `/set_channel project tickets`. Tickets are the one kind with no fallback channel.
  - A Developer can therefore spend the GitHub rate limit of whoever ran `/board link`, since the
    board poll reads the board with their authorisation, while linking the board stays with Admin
    or Project Manager, or GitHub admin where that counts.
  - It pings nobody: a draft card names no assignees, and its block carries no mentions.
- **A card changing an item's status** is behind `SHANNON_BOARD_MAY_SET_STATUS`, off by default.
  This one does have a Discord equivalent to bypass: `/status` is Project Manager only (or GitHub
  admin, where a server lets GitHub access stand in for a role), and nothing GitHub sends with a
  board says who dragged the card.
- **A card being archived, restored or deleted** shuts, reopens or ends its thread, and is not
  gated. It is the mirror keeping up with the board, not an action anybody takes in Discord. No
  command does it, and archiving or deleting a card needs write access to the board (#198).

The other direction, a status set here moving the card, is behind `SHANNON_BOARD_MAY_MOVE_CARDS`.
It is **on** by default, because a board somebody linked is a board they want kept up to date.

- The card is moved as whoever set the status, so they need to have authorised. `/status` and
  `/priority` refuse up front for somebody who has not, because that is the one board failure they
  can fix themselves.
- Turning the setting off stops the write and nothing else. The board's own column order is still
  read, and a move that skips a column is still refused.

### Status and priority labels

`/status` and `/priority` act on the thread they are run in, which is the item you are looking at.
Status and priority are stored as labels on the repository. Each has one value at a time: setting
one takes the previous one off, in whatever spelling the repository was using.

| Label | Colour |
|---|---|
| `priority: High` | red, `#d73a4a` |
| `priority: Medium` | amber, `#f59e0b` |
| `priority: Low` | yellow, `#fde047` |
| `status: ` and the board's column, such as `status: Ready To Merge` | the column's colour on the board |
| `status: Backlog`, `status: Not reviewed`, `status: In review`, `status: Done` where no board is linked | grey, blue, purple and green |

- The bot creates each label on the repository, in its colour, before it first puts it on an item.
  The exception is while the board will not read: then the label is the column name given, put on
  as it is, and GitHub makes it grey if the repository does not have it yet.
- Every label starting `status:` counts as a status label, including any the repository already
  had, so a move takes it off.
- The labels the bot wrote before (`HIGH`, `MEDIUM`, `LOW`, `BACKLOG`, `NOT_REVIEWED`, `IN_REVIEW`
  and `DONE`) are renamed in place the first time their new name is written in a repository. That
  moves them on every item at once.
- GitHub will not rename a label onto a name the repository already has. So where the new label
  exists already, made by hand or by an earlier write, the old one stays, and comes off each item
  as it is next set. Old labels are still read, so an item carrying one is cleared properly.
- On a board with two columns for one status, such as `In progress` and `In review`, the old label
  for that status is not renamed to either, because there is no telling which column each item is
  in.
- A column name that would make a label longer than GitHub's 50 characters is refused, and the
  reply says to shorten it on the board.

### Logging a thread to GitHub

`/log_conversation` is the only command here that reads Discord rather than writing to it. It
needs setting up in two places before it works. Message content is a privileged intent (a
permission Discord must grant the bot): it is a checkbox under Bot in the Discord Developer Portal,
and Discord asks an application in over a hundred servers to apply for it.

1. Tick the message content box in the Developer Portal.
2. Then set `SHANNON_CAPTURE_DISCORD_MESSAGES=true`.

Keep that order. A process that asks for an intent it has not been granted does not start at all,
and it takes the webhook mirror, the delivery worker and the board poller down with it. Until both
steps are done, the command refuses with a sentence saying which half is missing, and nothing else
is affected.

What gets published is what people typed:

- Bot messages are skipped. That is also what stops comments mirrored in from GitHub being sent
  straight back to it. Attachments, stickers and the lines Discord writes itself are skipped too.
- Editing a message afterwards changes nothing, because a transcript is a record of what was said
  when it was said. Deleting a message before the batch goes out keeps it out.
- Tagging somebody in the thread reaches them on GitHub. A tag of a member who has run `/link` is
  published as a real `@login` and notifies that account. A tag of anybody else is published as
  their Discord name and notifies nobody, because a display name that happens to match a login
  would otherwise ring a stranger who was never in the conversation.
- At most ten accounts are tagged per comment, for the same reason the comment mirror caps its
  own: without a limit, one thread could ping every linked member of the server. People past the
  limit are still named.
- Everything else that would notify an account or touch another item is neutralised on the way,
  with one exception on purpose: a full GitHub URL somebody pasted stays a working link, and GitHub
  does cross-reference those.
- Role and channel mentions are published as their names, and notify nobody, here or there.

Nobody in a thread can opt out of being transcribed on their own. That is why the notice is posted
before anything is captured, not after.

## Architecture

One process runs the Discord bot, the webhook endpoint and every background loop. The code is in
layers. The bottom layer is `config.py` and `domain/`, and the top layer is `container.py` and
`main.py`.

[ARCHITECTURE.md](ARCHITECTURE.md) says:

- where each part of the code lives
- the path a webhook takes to its thread
- why it is built that way
- what is still to do

## Data model

Each row is one table in the database. A *delivery* is one webhook GitHub sent. A *claim* is a
record written before an action (a row, or a time or id on a row), so a retry finds it and does not
do the action twice.

| Table | Holds |
| --- | --- |
| `repositories` | One row per registered repository. Unique on guild (server) and on GitHub id, so a webhook resolves to exactly one server |
| `channel_mappings` | Which channel each kind of item threads into |
| `tracked_items` | One row per mirrored PR, issue or draft card: its thread, its message and its state. A card's state is where it stands on the board: open, archived or deleted. Also the high water mark, `github_updated_at`: the newest GitHub update time already applied to the item, so an older delivery cannot overwrite a newer one. `tracking` holds only what `/track` was told in that thread. A kind nobody chose is not written, so it follows the default |
| `item_assignments` | Who is on an item and in what capacity. `notified_at` is the claim on the ping. `fulfilled_at` closes a review request |
| `mirrored_notes` | Comments, reviews and thread announcement lines (commits, check reports, approvals, force pushes, commits left, label, draft and state changes) already posted. Each is claimed before posting, so a retry cannot repeat one |
| `webhook_events` | The queue: payload, status, attempts, backoff (when to retry), lease (how long a worker holds it), last error |
| `user_links` | GitHub login to Discord account, per server. `created_at` is the day `/linked` shows for a link. It is kept when the same login is linked again or a rename is followed. It starts again when a different login is linked |
| `muted_members` | Who asked not to be notified, per server. A row is the whole fact: no row means pinged |
| `team_links` | GitHub team slug to Discord role, per server. Separate from `user_links` because on GitHub a slug and a login are separate namespaces, and only a login can be claimed here |
| `github_installations` | Which App installation covers a GitHub account. Keyed on the account, because an App is installed on an account. A cache with a fallback: GitHub is the authority. When this table has nothing, the bot reads the App's own installations from GitHub and writes the answer here |
| `identity_verifications` | Outstanding one-time links. `state` names the row a browser is following and completes nothing on its own. `bound_browser` is the browser Discord said is held by the member the link was issued for, stored as a hash keyed by a cookie only that browser holds. Only that browser can spend the link. `purpose` is the only record of which command sent it. A board link also carries the chosen board, so following it both authorises and links. The board is kept here and never in the URL, so nothing can change it on the way to GitHub and back. `tier` is the roles the board command was gated on. Following the link asks Discord about them again before anything is kept |
| `verified_identities` | Who a Discord account last proved to be on GitHub from this server, kept until a later sign-in replaces it. Separate from `user_links` for two reasons: `/link` replaces that row whenever the linked login changes, and a link is a claim while this is something GitHub vouched for. `through_discord` is true for every proof written since `0039`. Each of those came back through a link Discord bound to the member's own browser. A proof from before cannot show that, so only the newer ones can let somebody in |
| `board_authorizations` | The GitHub authorisation one person granted so one server can reach their project board. The token is **encrypted**. It is the only encrypted column in the schema and the first credential this project stores: everything else it keeps describes a person, but this one acts as them. One row per person per server. Two readers use it: a card move uses the row of whoever ran the command, and a poll uses the row of the member named in `repositories.project_linked_by` |
| `logged_conversations` | Which threads are being published to GitHub, and the claim on the batch each is publishing. Kept after logging stops, so you can still see who turned it on and when. Unique on the item only while open, so an item can be logged again later |
| `logged_messages` | What was said in a logged thread and has not reached GitHub yet. Deleted as soon as the comment carrying it lands, because these rows hold what people said |
| `reminders` | What `/remind` still owes: where it goes off, who it rings, who asked, the message, when it is due, and the claim of the sender taking it. Deleted once it has gone out or been given up on, so the table is the queue |

Enums are stored as `VARCHAR`, not as native PostgreSQL types, so adding a status needs no
`ALTER TYPE`. The database does not constrain them. The column type (`varchar_enum` in
`shannon/db/base.py`) leaves SQLAlchemy's `create_constraint` at its default of off, so no `CHECK`
is emitted. The column accepts any string that fits, and only the application enforces the values.

Alembic revisions `0001` to `0039`. Two tests keep this section true:

- one applies the revisions to an empty database and compares the result with the models, so the
  two cannot drift apart;
- one compares this section with what is on disk, because the range and the table above had both
  gone stale once.

How each table grows:

- `logged_messages` is emptied by publishing, and `reminders` by sending.
- `webhook_events` and `identity_verifications` are pruned.
- `mirrored_notes` grows by one row per comment, review and announced line (commits, force pushes,
  checks, approvals, labels, draft and state changes) and has no cleanup path.
- `github_installations` holds one row per account for as long as the App is installed on it.

## HTTP surface

The web server answers these routes.

| Route | Answers |
| --- | --- |
| `POST /webhooks/github` | 200 with `accepted`, `duplicate` or `ignored`. 400 for a missing `X-GitHub-Event` or `X-GitHub-Delivery` header, or an unusable body. 401 for a missing or bad signature. 413 past the 25MB cap. 500 if neither webhook secret is set. 503 when no database connection is free to record the delivery |
| `GET /health` | `healthy`, `database`, `worker`, `bot`, `poller`, `flusher` and `reminders` as booleans, and `version` as the commit answering. `healthy` is `database`, `worker` and `bot` together. The answer is 503 when `healthy` is false |
| `GET /oauth/start` | Where a one-time link opens. 303 to Discord. It leaves the `__Host-shannon_round_trip` cookie, which holds the round trip to this browser, and writes nothing. 400 for a link that has expired, been used or never existed |
| `GET /oauth/discord/callback` | 303 on to GitHub once Discord names the member the link was issued for, in the browser that opened it. 400 for anybody else, another browser, or a cancelled sign-in |
| `GET /oauth/github/callback` | A plain-text page saying who signed in and what happens next, for that browser alone. 400 otherwise |

All three `/oauth/*` routes answer 500 where Discord sign-in is not configured.

The two webhook secrets are `SHANNON_GITHUB_APP_WEBHOOK_SECRET` and
`SHANNON_GITHUB_WEBHOOK_SECRET`. A delivery signed with either one is accepted. GitHub does not
resend a delivery that got a 503. To retry it, press Redeliver on that delivery in GitHub's
delivery log.

### `version`

`version` is the commit the image was built from. `unknown` means an image built by hand rather
than by CI.

It is there because, from outside, a change that was merged and never deployed looks the same as
a change that does not work. Before `version`, telling them apart meant logging in to the server.

Anyone can read it. The repository is public, so the commit says which fixes this deployment has
and which it has not. `Caddyfile` says why that is accepted.

### What `/health` checks

`/health` reports whether the process is doing its work, not only whether it is listening. The
worker is the task that acts on recorded deliveries. The gateway is the bot's live connection to
Discord. If the worker dies or the gateway drops, the endpoint still accepts deliveries, but
nothing acts on them. That needs a restart.

The database check is cached for 5 seconds. Without the cache, anyone calling the public endpoint
could use up the connection pool the worker runs on.

### `poller`, `flusher` and `reminders` are reported, not counted

These three do not count towards `healthy`. Without them the process still does its main job:
webhooks arrive and threads are written. Only this stops:

| Task | Field | What stops when it dies |
| --- | --- | --- |
| Reads linked project boards | `poller` | Board movement |
| Publishes logged conversations | `flusher` | Publishing a logged conversation |
| Sends reminders | `reminders` | Sending a reminder |

Failing the check for these would restart a working process and throw away whatever the worker
had in hand.

They are reported because nothing stops the process when one of these tasks dies. Without the
report, the task would end with one line in the log, and every later check would say all is well.
What the flusher and the reminder sender have not done waits in the database for a process that
can do it.

`poller` is also true where `SHANNON_POLL_BOARDS` is off, because then nothing polls. `reminders`
is also true where there is no Discord token.

### Other routes

`/docs`, `/redoc` and `/openapi.json` are served unconditionally. `/health` needs no
authentication. There is no middleware of any kind. `Caddyfile` forwards only `/health`,
`/webhooks/*` and `/oauth/*` to the app.

## Known limitations

The bot gets fourteen things wrong. Each one is narrow, and each is written down here instead of
fixed. Only the second one loses anything: the bot never mirrors the comment it drops, not even
later.

**A review handled before the request it answers.** GitHub sends a review request and the review
as two separate deliveries. Nothing guarantees the order the bot handles them in: if one delivery
fails once, it backs off, and the one behind it goes first.

- What goes wrong: if the review is handled first, there is no request row for it to close. The
  request written a moment later reads as outstanding, so the bot pings the reviewer for a review
  they have already given. The ping is wrong; the stored state is not.
- How it clears: the row closes the next time that person reviews. A request GitHub has dropped is
  deleted on the next ordinary event for the pull request.
- The fix, left for later: a review writes down that it happened even when its request row is not
  there yet.

**A comment on an item that has never been tracked.** A comment is not an item event. It carries
nothing the bot can build a thread from.

- What goes wrong: if the repository is registered but the item has no row, the bot logs the
  comment and drops it. It does not mirror that comment later either.
- When it happens: for an item that was already open before the repository was registered, or one
  whose events all arrived while the bot was down.
- How it clears: it stops once any item event for that number arrives and builds the thread.
- A deleted thread is different: the bot rebuilds it, because that item is tracked and the sync
  has a number to read GitHub with.
- Why it is not fixed: the hook that rebuilds a deleted thread could serve this case too. But then
  every comment on anything untracked would cost a call to GitHub, for a case that is mostly a
  first run. It may be fixed later.

**A status label moved on GitHub, against a status the block still shows.** Statuses live as labels
on the repository. Nothing on the webhook path reads a label back onto the item: only the slash
commands and the board write the status column.

- What goes wrong: label an item `status: Backlog` in GitHub, and a thread that asked for tag lines
  gets a line saying so. The block above it still reads In review.
- Both are true: the line reports what somebody did to the labels, as every line in a thread does.
- Why it is not fixed: making a GitHub label move the stored status means deciding whether GitHub
  or Discord wins. That is a bigger question than the line.

**A tag read from a comment body cannot be checked against the account that wrote it.** Every other
mention the bot builds is checked against the GitHub id in the payload. That check stops a stranger
who takes a freed login from inheriting the previous holder's mention. A name read from a comment
body or a description has no id, so the check cannot run and the name alone decides. This is the
rule the link table already states for a payload with no id: no evidence is not evidence, and
refusing on it would take away mentions that work.

**The same commit announced in two threads.** The bot announces a commit where it lands, in a
thread that asked for commit lines. Merging a branch that carries your own earlier work lands that
commit again somewhere else.

- Why it is not fixed: to suppress it, the bot would have to ask GitHub, for each commit, whether
  the default branch already has it. That triples what a push costs, to hide something true.
- A rebase is a different case, and it is handled. Every commit on a rewritten branch has a new
  hash, so the thread says the branch was force-pushed instead of announcing every commit again.

**A link opened on a phone.** A one-time link goes through Discord before GitHub. On a phone with
the Discord app installed, Discord's page can open in the app instead of in the browser where the
link started.

- What goes wrong: a cookie ties the round trip to the starting browser. So the bot refuses the
  return, shows a page saying to finish where you began, and records nothing.
- What works: open the link in a desktop browser, or paste it into the phone's browser while signed
  in to discord.com.
- Somebody who has never authorised the bot's Discord application sees its consent page once. After
  that, Discord skips it in any browser.

**A message this bot has sent cannot go back to plain text.** Discord's components flag is one way:
once a message has carried it, the flag can never be removed. Every block and every line this bot
writes now carries it.

- Rolling back: a build from before that change sends plain content to a message that Discord
  accepts only components for. Discord answers 400. The worker retries for two hours, then gives up.
  The block freezes where it is, while its thread keeps receiving lines. So this change is
  roll-forward only.
- Link previews: a components message carries no embeds. The automatic preview that used to appear
  under a bare GitHub URL is gone.

**A hash is recognised by its shape.** GitHub links a commit hash in a comment once it has found the
commit. This bot cannot look without one call to GitHub per hash. So it links any run of seven to
forty lowercase hex characters that:

- holds at least one digit and one letter, and
- stands on its own, outside code written with backticks, links and addresses.

What follows from that:

- A hex word that is not a commit, such as `ed25519` or a container id, links to GitHub's page for
  a commit that does not exist. It shows its first seven characters like any hash, and the link
  keeps all of it.
- A real hash written all in digits or all in letters stays plain text.
- Code indented or fenced with tildes is plain text in Discord, so a hash in it is linked like one
  in prose.
- A commit's own line and a force push name commits that GitHub reported. Those are linked whatever
  their shape.

**A heading GitHub draws that Discord cannot.** Discord draws some Markdown differently from GitHub:

- A line of text with `===` or `---` under it is a heading on GitHub. Discord has no such heading,
  so it shows the line and its underline.
- Code fenced with tildes instead of backticks is ordinary text in Discord. So is a block of HTML,
  which GitHub draws as HTML instead of reading as Markdown. In either, a line that Discord would
  draw as a heading shows its `#` instead, and the rest is read as markup.
- Three backticks written mid-sentence are shown as they are on GitHub. In Discord they open a code
  block that runs to the next three backticks or to the end of the text, so a heading in between
  arrives as code.
- GitHub's footnotes and link reference definitions are read as ordinary words. From one of them to
  the next blank line, and on a footnote's own line and the indented lines under it, a heading can
  be drawn where GitHub did not draw one, or the other way round.

**Who made a file is read from its history.** A review that asks for changes names each file's
authors. The bot reads them from GitHub's list of the commits that touched that path, kept to the
pull request's own commits.

- That list does not follow a rename. A file renamed on the pull request, or one changed only by a
  merge, can read `made by Unknown`.
- The bot reads one page of a hundred commits per file, newest first from the reviewed commit. On a
  long-lived pull request, a file that is busy on the source branch can push the older commits off
  that page.
- A commit names only the account that authored it. The bot does not read `Co-authored-by` lines.

**Two commands need the role whatever GitHub says.** A server can let GitHub access stand in for a
role. Then GitHub admin passes every Project Manager gate except two:

- `/register`: there is nothing to ask GitHub. The bot judges GitHub access on the registered
  repository, and there is none until `/register` has run. Once there is one, `/register` refuses
  anyway.
- Asking somebody else to `/link`: this posts a public note that has to be the command's first
  answer. Asking GitHub first would send a private answer before it.

Both are left to the role instead of worked around.

**A proof that GitHub access cannot count yet.** A proof is the record of which GitHub
account a member last signed in as from this server (`verified_identities`). GitHub vouched for it,
it is kept apart from the link `/link` stores, and any later sign-in replaces it. GitHub access stands in for a role only on a proof made since
migration `0039` (issue #94). Only those proofs record that Discord bound the link to the member's
own browser.

- Proofs made between `0033` and `0039` were bound too, but nothing on the row says so. They wait,
  with the rest, for one more `/link`.
- An account renamed since it was proved waits too. The bot asks GitHub by the old name, the answer
  is about another account or none, and nothing is granted until the member runs `/link` again.

**The last sign-in decides what GitHub lets somebody do.** A server keeps one proof per member. A
sign-in for `/register`, `/unregister` or `/board` replaces it. A member who authorises a board as
another account is checked as that account from then on. It may have more or less access than the
first. `SHANNON_REQUIRE_PROVED_LINKS` already follows this rule, and `/linked` says
which account is held.

**A member let in by GitHub alone may not be able to stop a logged conversation.**
`/stop_conversation` exists so that a thread told logging is on can always be stopped. But stopping
is not exempt from the tier (the access level a command needs).

- A member who ran `/log_conversation` on GitHub write access, and has since lost it, is refused at
  the tier, like a member whose role was taken away.
- So is a member who tries while the bot cannot ask GitHub.
- Anybody else with the tier can still stop it.

## Contributing

Every change starts from an issue. Use the form for a
[bug report](https://github.com/Canon-Regularis/Shannon-bot/issues/new?template=bug_report.yml) or a
[feature request](https://github.com/Canon-Regularis/Shannon-bot/issues/new?template=feature_request.yml).

- [CONTRIBUTING.md](CONTRIBUTING.md): how to set the project up, make a change, check it and propose
  it. It also says what to do about a security problem. A security problem never goes in a public
  issue.
- [ARCHITECTURE.md](ARCHITECTURE.md): how the code is laid out, and why.
- [INSTALLATION.md](INSTALLATION.md): how to set the bot up, from GitHub to Discord.
- [Code of Conduct](CODE_OF_CONDUCT.md): everybody who takes part agrees to it.

## License

Apache-2.0. See [LICENSE](LICENSE).
