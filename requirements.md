# Shannon Bot Technical Requirements

This is the product brief: what the bot was asked to do. Where the build departs from the brief,
the section says so, with the issue where there is one.

## Goal

Build a Discord bot that syncs GitHub repository activity into Discord.

GitHub and Discord are both sources of truth. The sync goes both ways (duplex communication):
GitHub events update Discord, and the bot's Discord commands update GitHub.

---

## Core Stack

| Part            | What the bot uses it for                                               |
| --------------- | ---------------------------------------------------------------------- |
| Python          | The language of the bot. It needs Python 3.12 or later.                |
| PostgreSQL      | The database that stores everything the bot tracks.                    |
| Discord API     | Reading and posting in Discord, through `discord.py`.                  |
| GitHub API      | Reading and changing repositories and Projects, over REST (not GraphQL). |
| GitHub Webhooks | The messages GitHub sends to the bot when something changes.           |
| FastAPI         | The web server: webhooks, health checks, and the OAuth pages of one-time links. |

---

## Required Integrations

The bot must work with these parts of GitHub and Discord:

* GitHub repositories
* GitHub pull requests
* GitHub issues
* GitHub Projects
* Discord channels
* Discord forum posts or threads
* Discord roles and user pings

---

## Main Flow

1. A PR or issue is created or updated on GitHub, or a card changes on the linked project board.
2. For a PR or issue, GitHub sends a webhook event to the bot. A project board sends none, so the
   bot polls the board instead (see
   ["What polling costs, and why it costs that"](#what-polling-costs-and-why-it-costs-that)).
3. The bot reads the event, or the changed card.
4. The bot stores or updates the record in PostgreSQL.
5. The bot creates or updates the matching Discord post/thread.
6. The bot pings the relevant users or roles.

The original brief said project items arrive by webhook too. The build departed from that in MVP 4:
the project events the brief named can no longer fire, so the board is polled. "GitHub Webhook
Events Required" below gives the reasons.

---

## Required Bot Commands

The brief asked for the commands below. The build kept `/register`, `/pr` and `/issue`. Issue #158
replaced the eight `/SET_*` commands with two: `/status` and `/priority`.

### Repository and item commands

```text
/register <github_repo_link>
```

Registers a GitHub repository with the Discord server. A server can have only one registered
repository.

```text
/pr <pr_link>
```

Fetches a GitHub pull request and creates or updates its Discord thread.

```text
/issue <issue_link>
```

Fetches a GitHub issue and creates or updates its Discord thread.

### Status and priority commands

Run both commands inside the item's thread. They work in a pull request's or an issue's thread. A
ticket's thread refuses both, because a draft card has no labels on GitHub: change the card on the
board instead.

```text
/status <to>
```

Moves the item to a status: Backlog, Not reviewed, In review or Done. If the server has a linked
board, `<to>` can also be any column on that board. The bot writes the status label on GitHub,
moves the item's card on the board and redraws the thread. Done also locks the thread. An issue
follows GitHub: close it there to make it Done, and reopen it there to give it another status.

A board column with no status of its own, such as `Ready To Merge`, moves the label and the card,
and the item keeps its status (#246).

```text
/priority <to>
```

Sets the priority to High, Medium or Low. The bot writes the priority label on GitHub, sets the
Priority field on the item's board card if the board has that field, and redraws the thread.

### Where the build departs from the brief

The brief asked for eight commands with no argument, one per state. Each status command changed
the item "on the Github side + the channel". Each priority command changed it "on the Github side"
only. Issue #158 replaced them:

| Brief command | What to run now |
| --- | --- |
| `/SET_BACKLOG` | `/status Backlog` |
| `/SET_NOT_REVIEWED` | `/status Not reviewed` |
| `/SET_IN_REVIEW` | `/status In review` |
| `/SET_READY_FOR_MERGE` | No replacement status. A board column such as `Ready To Merge` works with `/status` (#246). |
| `/SET_DONE` | `/status Done` |
| `/SET_HIGH_PRIORITY` | `/priority High` |
| `/SET_MED_PRIORITY` | `/priority Medium` |
| `/SET_LOW_PRIORITY` | `/priority Low` |

The brief's priority commands changed GitHub only. `/priority` also redraws the Discord thread.

Why the change:

* Eight commands were eight entries to scroll past in Discord's command list.
* Status and priority stay two commands, because a priority is not a status. The rules about a
  closed item and about the order of a board's columns apply to status only.

---

## Required Statuses

A status says where an item is in the review workflow. The original brief asked for five statuses,
each one a label (a "tag") in the item's repository:

```text
NOT_REVIEWED
IN_REVIEW
READY_FOR_MERGE
BACKLOG
DONE
```

**What was built.** The bot has four statuses, not five:

| Status         | Label with no board linked |
| -------------- | -------------------------- |
| `BACKLOG`      | `status: Backlog`          |
| `NOT_REVIEWED` | `status: Not reviewed`     |
| `IN_REVIEW`    | `status: In review`        |
| `DONE`         | `status: Done`             |

- **`READY_FOR_MERGE` is retired.** It existed so that a pull request could not be `DONE` before
  a reviewer said it could be merged. GitHub's default board has no such column, so on that board
  no pull request could reach `DONE`. Migration `0028` moved every `READY_FOR_MERGE` row to `IN_REVIEW`.
  The board's column order now does the job: a card moves forward one column at a time and back
  as far as you like. The CHANGELOG entry "The board's column order became the rule, and
  READY_FOR_MERGE went" records this departure from the brief.
- **Labels are named for what they are** (#246). A status label is `status:`, a space, then the
  board column, spelled as the board spells it. With no board linked, it is one of the four
  labels in the table above.
- **A column with no status still gets a label.** A board column the bot has no status for, such
  as `Ready To Merge`, gets the status label `status: Ready To Merge` like any other column.
- **Old spellings are recognised, never written.** An item that still carries a label in the enum's own
  spelling, such as `IN_REVIEW`, is read as that status. The bot renames such a label to its new
  name the first time it writes that status in the repository. `shannon/github/labels.py` lists
  the two cases where it does not.

---

## Required Priorities

The bot must support these priorities:

```text
HIGH
MEDIUM
LOW
```

The bot also stores `UNSET`. It is not a fourth priority. It means the item has none of the three.
An item has no priority label until somebody gives it one, so the bot needs a name for that state.

---

## Discord Output Format

### Pull requests and issues

For every synced PR or issue, the bot must post a message in its thread with these fields:

```text
PR / issue Name:
Type: PR / issue
State:
GitHub Link:
Author:
Assignees:
Reviewers:
Status:
Priority:
Tags:
Last Updated:
Description:
```

The build differs from the list as first written in three ways:

- **`State:` is added (#15).** It shows GitHub's own state: open, closed or merged. The `Status:` field
  does not carry this.
- **`Reviewers:` is left out for issues (#36).** GitHub issues have no reviewers, so the row would always
  read `None`. That is noise, not information.
- **`Description:` is added at the end (#75).** The first list said what an item was called and who was
  on it, but nothing about what it was for. The bot leaves the description out completely when the
  item was opened without one.

### Tickets

A ticket is a draft card on a GitHub project board: a card that is not an issue or a pull request.
For every synced ticket, the bot must post a message in its thread with these fields, and say there
when any of them changes.

```text
Ticket Name:
Type: Ticket
GitHub Link:
Creator:
Assignees:
Status:
Priority:
Story Point:
Iteration:
Area:
Tags:
Created:
Last Updated:
Description:
```

The list grew twice from the three lines first written:

1. `Type:` and `Last Updated:` were added, so that the three kinds of block read alike. The board
   read already carried the timestamp.
2. The other fields came with the board read itself (#182). The board read used to ask GitHub for
   Title and Status only, so a card could show nothing else. It now asks for the board's own
   fields.

Rules for the rows:

- **An empty row is left out (#166).** The bot does not render it as `None`, because a field that
  is always empty reads as data missing, not data absent.
- **The board owner owns the field names.** If a board calls one of these fields something else,
  that row has no value and is left out. `Story Point` is singular because that is how the board
  spells it.
- **There is no `State:` row.** A board column is not a closed state: a card in Done is as open as
  a card in Todo. The one thing that ends a card, taking it off the board, also shuts the thread
  the row would be shown in. So the row could only ever say `Open`.

### A ticket taken off the board

A card taken off the board takes its thread with it (#198):

- **Archived.** The bot shuts the thread (locks and archives it, as for a closed issue). It says so
  in the thread, and says how to get the card back.
- **Restored.** The bot opens the thread again and says so. Then it reports what changed while the
  card was away, compared with what the thread last showed. The bot never mirrors an archived card,
  so nothing the card gained while archived is lost.
- **Deleted.** The bot ends the thread the same way as for a card converted to an issue: it shuts
  the thread, says so, and lets it go for good.

### Changes to a ticket

The bot announces a change to any ticket field except `Creator` in the thread (#182). Rules:

- **One message per poll.** The message lists everything that moved since the last poll. A board
  read sees a card's fields together, so dragging a card and setting its points is one action and
  gets one message.
- **`Creator` is not announced**, because a card's creator does not change.
- **`Ticket Name` and `Description` are announced because `Updated` is.** The poll only looks at a
  card whose timestamp moved, so `Updated` differs in every message. On its own, that would say a
  card changed without saying what. A title edit and a description edit are the two changes that
  move the timestamp and nothing else, so naming them gives the message a cause. A message with
  only a timestamp means GitHub re-stamped the card and nothing a reader can see changed.
- **Each value is cut short.** The message is a signpost, not a diff. The block directly above it
  carries every value in full. Without the cut, a description of several hundred characters on
  each side would make the message longer than Discord accepts, and the whole message would be
  lost, not only the extra words.
- **A card seen for the first time announces nothing.** The bot records its fields, and the block
  itself is the announcement. This stops every card on a board reporting every field at once the
  first time the bot runs.

### Formatting in every thread message

All of these blocks, and every other message the bot posts into a thread, show the formatting the
text was written with. A heading, a list, a code span or a link in a GitHub body arrives as what it
means, not as the characters somebody typed. One conversion does this for all of them.

- **Headings.** A heading arrives at the size it was written, up to the three sizes Discord draws.
  GitHub's fourth to sixth sizes arrive as the third. A heading Discord cannot draw (in a list item,
  or under two quote marks) arrives as its words, in bold unless the words hold `**` already or end
  on a backslash.
- **Code and HTML.** A line in a block of code keeps the hashes and marks it starts with. A line
  GitHub shows as text, such as one inside a block of HTML, is not made a heading. The README's
  known limitations name the few places where either rule does not hold.
- **Commit hashes (#186).** A commit hash in the text arrives as a link to the commit when the text
  belongs to a repository. A draft card's text does not. The bot finds a hash by its shape alone and
  does not ask GitHub whether the commit exists. GitHub itself links a hash only after checking that
  the commit exists. A commit line and a force push link the commits they name.

---

## GitHub Webhook Events Required

The bot must handle:

```text
pull_request.opened
pull_request.edited
pull_request.closed
pull_request.reopened
pull_request.review_requested
pull_request.labeled
pull_request.assigned
issues.opened
issues.edited
issues.closed
issues.reopened
issues.labeled
issues.assigned
issue_comment.created
pull_request_review.submitted
pull_request_review_comment.created
check_suite.completed
```

Project boards are not on this list. The bot reads boards itself instead of receiving them as
events.

The original brief named `project_card.created`, `project_card.moved` and `project_card.updated`.
None of the three can fire:

- They belong to Projects (classic). GitHub sunset Projects (classic) on 23 August 2024 and its
  REST API on 1 April 2025, and removed it from GitHub Enterprise Server in 3.17. The last release
  that still contained it reached end of life on 1 July 2026.
- `project_card.updated` was never a valid action, even while classic existed. The five valid
  actions were `converted`, `created`, `deleted`, `edited` and `moved`.
- GitHub's webhook page still documents them, with an availability line. That line is left over in
  the published schema and is not a promise. A bot subscribed to them receives nothing, for ever,
  and gets no error.

The replacement events are `projects_v2`, `projects_v2_item` and `projects_v2_status_update`. They
are **organisation scope only**:

- A repository webhook receives none of them.
- A project owned by a user account sends none of them at all.

This bot registers against a repository owned by a personal account, so there is no event to
subscribe to. Instead the bot polls the board through the Projects v2 REST API. To poll means to
read the board again on a fixed timer and compare. See `shannon/services/projects.py`.

### What polling costs, and why it costs that

Issue #189. With no event to wait for, polling sets a floor on how fast a card can reach Discord.
The floor is half the interval on average and the whole interval at worst, plus the read and the
sync. An issue has no such floor: its webhook is queued, and a worker takes it within two seconds.

The interval is **two seconds**, so a ticket and an issue now arrive on the same clock. It used to
be sixty seconds. That is why a ticket took thirty to forty-five seconds: half the interval, plus
the read and the sync.

Two seconds is affordable because of one measured fact. GitHub honours `If-None-Match` on the
project items endpoint, and **a 304 carries no body and spends no rate-limit budget**. (A 304 is
GitHub's "not modified" answer to a conditional request.) Ten conditional polls two seconds apart
left the remaining-requests counter untouched. So:

- A board nobody has touched costs one request and nothing else.
- Thirty times as many passes cost no more per hour than the old interval did.
- The response carries `Cache-Control: max-age=60`, but that is advice to a client, not a floor on
  staleness. GitHub sends no `Age` header, and its `Date` advances on every request. So a short
  interval does see a change promptly.

The saving is in the transport only. When the board is unchanged, the poller reuses the cards it
parsed last time. This is safe because a 304 proves the body is byte-identical. Nothing downstream
behaves differently: the poller makes every comparison, and reaches every card still waiting for a
thread, exactly as often as before.

**Boards with more than one page of cards.** A validator (the ETag that a conditional request
sends back) hashes one response body. So the bot cannot check such a board without downloading all
of it. Such a board is read every sixty seconds instead, on a clock of its own: every other board
on the deployment keeps the fast interval. It is slower rather than more expensive, which is the
right way for a surprise to fail. The bot logs it once per board when it happens. The current board has eighty cards, and a page holds a hundred, so this will happen
eventually.

**Cards missing from the read.** On a quiet board, one thing spends budget, and only while a card
is missing from the read: asking GitHub about that one card (#198). A missing card may be deleted,
archived out of a read that leaves the archive out, or beyond what one read could prove it saw.
Only a request per card tells these apart. So the requests are paced:

| Limit | Value |
|---|---|
| Cards asked about, per board | At most one every ten seconds |
| A card GitHub still has | Asked about again after ten minutes |
| An archived card | Asked about hourly |
| Worst case | 360 requests an hour per board |
| Every tracked card is on the read | No requests |

The bot believes a card is deleted only on the second "no such card", two passes apart. Letting its
thread go cannot be undone.

### Who a board is read as

Issue #170. Each person authorises this bot for themselves. The bot uses what they grant for
exactly two things:

1. A board they link is read under their authorisation.
2. A card **they** move from Discord is moved as them.

Nothing is shared between servers. Nothing acts for somebody who did not ask.

**What this replaced.** Before, the bot read and wrote every board in every server through **one
classic personal access token belonging to one human account**. The token had the `project` scope:
read and write across every project that account could see. Nothing scoped it per server: the
credential supplier took the board's owner as an argument and discarded it. So:

- A card moved from Discord appeared on GitHub as the token's owner, whoever had asked for it.
- One leaked token gave write access to every board that account could reach.

**Why a classic OAuth App.** The authorisation goes through a classic OAuth App, registered
separately from the GitHub App. This was forced, not chosen:

- GitHub publishes no App permission for a **user-owned** Projects v2 board. Its Projects
  permission exists at organisation level only, for Apps and fine-grained tokens alike.
- Granting an installed App an organisation permission suspends its event delivery until an admin
  accepts. That would stop every webhook in every registered repository.

OAuth scopes have neither problem, and `project` covers user and organisation projects alike.

**How the credential is stored.** A board authorisation is the **first credential this bot
stores**, and the only encrypted column in the schema. Everything else the bot keeps about a person
is a fact *about* them: a login, an account id, a preference. A board authorisation *acts as* them.
So:

- It is encrypted at rest with a key held in the environment, never in the database. A stolen copy
  of the table is worth nothing on its own.
- The key is a comma-separated list, newest first. Rotating it is one deploy, and nobody has to
  authorise again.
- A row that will not decrypt counts as absent. The person authorises again to replace it.

**No shared credential is left.** `SHANNON_GITHUB_PROJECT_TOKEN` is gone, and so is the object that
handed the client a fixed token. There is no longer any way to give this bot one credential that
sees everything. The board's write client carries no credential of its own. So "a card is moved as
somebody" is a fact of the wiring, not a check somebody could forget: a write with nobody behind it
goes out anonymous, and GitHub answers 401.

Linking a board therefore requires having authorised. A board linked on somebody else's
authorisation would put a server back on one person's credential.

### One command, and one click

Issue #201. The rule above made a first board link take two commands, with a trip to GitHub
between them. Undoing it was split the same way. Now it is one command, `/board`, with five halves.
A tier is a Discord role level that a command requires.

| Half | Who | What it does |
|---|---|---|
| `/board link` | Admin, Project Manager | Mirrors a board. With no authorisation yet, it hands out ONE link that remembers the board chosen: following it authorises and links, and there is nothing else to run |
| `/board unlink` | Admin, Project Manager | Stops mirroring, forgets the linker's authorisation, and names them. Forgetting this copy does not revoke the grant; only they can do that, under Applications in their GitHub settings |
| `/board authorise` | Admin, Project Manager | Grants your own, so a card you move with `/status` or `/priority` moves as you |
| `/board withdraw` | anybody | Forgets your own. No tier, on purpose: deleting a credential that is yours must not depend on a role you may since have lost |
| `/board show` | Admin, Project Manager | The board, who linked it, whether it still opens, and your own authorisation |

The tier on `authorise` and `show` is the set of tiers whose authorisation the bot ever uses:
linkers and card movers. Today that is the same two tiers. So nobody who needs to authorise lost
the ability, and nobody whose credential would never be used can hand one over.

The board a link was issued for is stored on the pending verification row, never in the URL. The
state stays the only thing in the link, so nothing can change the board on its way to GitHub and
back.

The merge found and fixed three problems:

- **The board was opened and listed as the App.** The check that a board exists, and the picker,
  sent no credential. The client then fills in the App installation's token, and the App holds no
  Projects permission. So a private board could not be linked, and the refusal blamed the person's
  own authorisation. Both now go out as the person choosing.
- **A board was identified by how its owner was written down.** A null owner means the repository's
  own owner, and the comparison did not resolve it. Two servers each linking their own account's #1
  were treated as one board: the second was refused and told the first's repository name. A server
  naming another's board by its owner got a different board, and a poll then read that board under
  the wrong server's member. The query now resolves the owner, and a refusal names no other server.
- **The scope GitHub granted was never checked.** GitHub lets a person grant less than was asked.
  The sign-in now refuses, and keeps nothing, when `project` is missing.

No scope was added:

- `project` covers user and organisation boards alike.
- No Projects v2 document asks for `read:org`.
- `repo` is the only scope that might let a board show a card wrapping a private repository's
  issue. But it is full control of every private repository the person can reach, the opposite of
  the narrowest grant the feature needs.

An organisation's board needs the organisation to approve the OAuth App. That is an access
restriction, not a scope, and the sign-in text says where to press.

### What "anonymity" can and cannot mean here

The issue asked for *"full security and anonymity wherever possible"*. The property the bot can
achieve is **least authority plus correct attribution**, not anonymity. GitHub sees the grant and
sees the write, and no arrangement prevents that. What changed is that GitHub now sees the right
person, with the narrowest grant the feature needs, which they can revoke. Before, it saw one
account standing in for everybody.

**Nothing else in this database is encrypted.** `user_links`, `verified_identities`,
`item_assignments`, `logged_messages.content`, `muted_members` and `webhook_events.payload` are all
plaintext. Most of them are queried on, and an encrypted column cannot be an index key. Encrypting
them is a database-wide project, with key management and a backfill. Folding it into a
board-linking change would have made both worse. This is a known position, not an oversight.

**What would remove the floor entirely:** move the board to an organisation and subscribe to
`projects_v2_item`. The board then joins the same delivery queue as everything else and gets the
worker's latency, with no poller. Two caveats:

- Those events are in public preview.
- Granting a new permission to an already-installed App suspends its deliveries until an admin
  accepts.

Until then the floor is GitHub's, not this bot's.

---

## Required Database Tables

The brief asked for the five tables below. The build has sixteen. Eleven were added, each because
something needed it:

- `user_links` holds the pairing of a GitHub login with a Discord account. Pinging needs it before
  any assignment row exists.
- `mirrored_notes` records every one-time line already posted into a thread: comments and reviews,
  and also the lines for commits, force pushes, labels, state changes, draft switches, CI results
  and the all-approved round-up. The delivery queue is at-least-once, so a delivery can be handled
  twice. Posting into a thread is the one step that cannot be undone, so the bot claims the line
  here first.
- `muted_members`, `board_authorizations`, `team_links`, `github_installations`,
  `identity_verifications`, `verified_identities`, `logged_conversations`, `logged_messages` and
  `reminders`.

The README's schema table describes all sixteen. Each column list below starts from the brief's
list. The changes are named under each table. The code has more columns than these lists;
`shannon/db/models.py` is the full schema.

### repositories

Stores linked GitHub repositories.

```text
id
github_repo_id
repo_name
repo_url
discord_guild_id
created_at
updated_at
```

### channel_mappings

Stores which Discord channels are used for PRs, issues, and tickets.

```text
id
repository_id
object_type
discord_channel_id
created_at
updated_at
```

### tracked_items

Stores synced PRs, issues, and tickets.

```text
id
repository_id
github_object_id
github_object_type
github_object_number
github_url
title
github_state
status
priority
github_updated_at
project_column
discord_message_id
discord_thread_id
created_at
updated_at
```

Five of these columns were not in the brief:

| Column | Why it was added |
| --- | --- |
| `title` | The item's name, shown in the block. |
| `github_state` | GitHub's state for an issue or pull request: open, closed or merged, shown in the block. A ticket (a draft card) has none, so its row holds where the card stands on its board instead: open, archived or deleted (#198). |
| `github_object_number` | How a comment or a review finds its item. Those payloads report an issue id even for a pull request. |
| `github_updated_at` | The high water mark: the newest GitHub update time the row has seen. It stops a late delivery undoing a newer one. |
| `project_column` | The board column as of the last poll. It tells a card that has moved apart from one that only disagrees with a status somebody set. |

### item_assignments

Stores assignees, reviewers and authors.

The brief also listed project managers. The build has no such role. A project manager is a
Discord permission tier (a level of command access), and no fact about a pull request or an issue
produces one. So the role was removed from `ActorRole` rather than kept as a value nothing could
write.

A requested team is stored here too, in a row with `role_type` `REVIEWER_TEAM` and the team slug
(the team's short name in its GitHub URL) in `github_username`. The block names the team in plain
text. A separate notifier posts a line that pings the Discord role `/link_team` mapped to that team
in `team_links`. If no role is mapped, the line names the team and pings nobody.

```text
id
tracked_item_id
github_username
role_type
notified_at
fulfilled_at
created_at
updated_at
```

Changes from the brief:

- `discord_user_id` was dropped in migration `0008`. It copied `user_links.discord_user_id`, which
  is the authoritative table and is read at render time anyway. So the column could only hold a
  stale duplicate.
- `notified_at` and `fulfilled_at` were added. They are the two claim stamps that stop somebody
  being pinged twice for one request.

### webhook_events

The brief said this table stores processed webhook events. The build made it a leased work queue
instead. A worker that takes a row holds it for a set time, its lease. If the worker dies, the lease
runs out and another worker can take the row. GitHub allows an endpoint ten seconds and never
redelivers a delivery it recorded as failed. So the route writes the delivery down and answers,
and a worker does the slow work later.

A row is written when a delivery arrives, as `PENDING`. A worker takes it (`PROCESSING`) and
finishes it as `PROCESSED`, `IGNORED` or `FAILED`. A delivery the bot would never act on gets no
row. `processed_at` is stamped on insert and again when the outcome is recorded.

```text
id
github_delivery_id
event_type
payload_hash
processed_at
status
```

The queue also needs the payload, an attempt count, a backoff, a lease and the last error, so the
table carries those columns as well.

---

## Required Behaviour

The brief asked for the behaviour in this table. "Metadata" is the block at the top of an item's
thread: its title, status, priority and the people on it. The list after the table says where the
build added to the brief or departed from it.

| When | The bot |
| --- | --- |
| A GitHub pull request (PR) is created | Creates a Discord PR thread and adds the PR metadata. Sets the status to `NOT_REVIEWED`. Pings the reviewers, if any are assigned. |
| A GitHub issue is created | Creates a Discord issue thread and adds the issue metadata. Sets the priority if the GitHub labels contain priority data. Pings the assignees, if any are assigned. |
| A PR or issue changes | Updates the existing Discord thread. Never creates a duplicate thread. |
| A reviewer is assigned | Updates the Discord thread and pings the reviewer. |
| A status changes | Updates GitHub first, then Discord. |
| A priority changes | Updates the GitHub labels or GitHub Project fields first, then Discord. |

### What was built

- **Where a thread goes.** The bot opens each thread in the channel mapped for that kind of item.
  `/register` maps pull requests only. Until `/set_channel` maps issues, issues use the pull
  request channel (#134).
- **Starting status.** Issues start at `NOT_REVIEWED` too, not only pull requests. Closing an
  issue sets it to `DONE`. Reopening an issue at `DONE` sets it back to `NOT_REVIEWED`. Closing a
  pull request leaves its status as it was.
- **Closed items.** Closing an issue, or closing or merging a pull request, locks and archives its
  thread (#76, #77).
- **Pings.** The first post of the metadata block is the ping: it names the author, the assignees
  and the reviewers, so no separate ping line follows it for those people (#81). A requested team
  still gets its own line (see below). A person added later gets one ping line. The `notified_at`
  stamp in `item_assignments` stops a second ping for the same request. People the description
  @names are pinged only by the post that opens the thread, and only if the item was opened no more
  than 24 hours ago (#259).
- **Who can be pinged.** Only a person whose GitHub login is linked to a Discord account with
  `/link`, or a team linked to a Discord role with `/link_team`. A requested team is named in plain
  text in the metadata block. If `/link_team` links the team to a Discord role, a separate line
  pings that role. A member cannot turn off a role ping. A member who ran `/mentions off` is still
  named, but Discord does not notify them (#80).
- **Status and priority are GitHub labels.** A priority is `priority: High`, `priority: Medium` or
  `priority: Low`. A status is `status: ` followed by the board column name, as the board spells
  it, for example `status: Ready To Merge`. With no board linked, it is one of the bot's own four:
  `status: Backlog`, `status: Not reviewed`, `status: In review` or `status: Done` (#246). A
  command writes the label first, and the item's stored row (its row in `tracked_items`) and the thread follow it. The stored priority
  is always read from the labels. The stored status is not: a status label put on by hand on GitHub
  does not change it.
- **The commands.** `/status` and `/priority` replaced the eight `/set_*` commands (#158). A
  change is written in this order:
  1. The labels on GitHub.
  2. The stored row and the Discord thread.
  3. The item's card on the linked project board, moved under the GitHub authorisation of the
     person who ran the command (#158, #170).

  If a run stops part way, GitHub is correct and Discord is stale. The next event or command
  corrects Discord. The other order would leave Discord showing a change GitHub never made.
- **Priority on a board.** The bot writes a priority to a board only if the board has a Priority
  field. GitHub's default board template has none (#201).

---

## Permissions

The brief sorts members into four tiers. A tier is a permission level. A member is in a tier when
they hold the Discord role named for it. Each role name is a setting:

| Tier | Setting | Default role name |
| --- | --- | --- |
| Developer | `SHANNON_ROLE_DEVELOPER` | `Developer` |
| Reviewer | `SHANNON_ROLE_REVIEWER` | `Reviewer` |
| Project Manager | `SHANNON_ROLE_PROJECT_MANAGER` | `Project Manager` |
| Admin | `SHANNON_ROLE_ADMIN` | `Admin` |

Each command names the tiers that may run it. Holding any one of them is enough.

The brief's lists used the `/SET_*` command names. Issue #158 replaced them with `/status` and
`/priority` (see [Required Bot Commands](#required-bot-commands)). Each part below gives the
brief's list first, then what was built.

### Developers

The brief listed `/pr_link` and `/issue_link`. No command has those names. They are the parameter
names of `/pr` and `/issue`.

What was built: a Developer can run these commands. A Project Manager can run them too.

```text
/pr <pr_link>
/issue <issue_link>
/refresh
/regenerate
/label  /unlabel
/assign  /unassign  /request_review  /unrequest_review
/log_conversation  /stop_conversation
/track
```

### Reviewers

The brief gave Reviewers the status and priority commands: `/SET_BACKLOG`, `/SET_NOT_REVIEWED`,
`/SET_IN_REVIEW`, `/SET_READY_FOR_MERGE`, `/SET_HIGH_PRIORITY`, `/SET_MED_PRIORITY`,
`/SET_LOW_PRIORITY` and `/SET_DONE`.

What was built: the Reviewer tier grants no command today. Only a Project Manager may move an
item's status or priority. Deciding that a change is good and recording that the project has
accepted it are different jobs, and the bot records only the second. The tier and
`SHANNON_ROLE_REVIEWER` stay, as the place for a later command that is a reviewer's. The CHANGELOG
entry "Who may move an item" records this change.

### Project Managers

The brief listed `/register <github_repo_link>`, `/pr <pr_link>`, `/issue <issue_link>` and the
eight `/SET_*` commands.

What was built: a Project Manager can run every Developer command above, and these:

```text
/register <github_repo_link>
/unregister <repository>
/set_channel
/link_team
/status <to>
/priority <to>
/board link  /board unlink  /board authorise  /board show
/link <member>      to ask somebody else to link their GitHub account
/remind <member>    to remind somebody else
```

`/status` and `/priority` are open to the Project Manager tier alone (and to the Admin tier, which
passes every gate). A card dragged on the linked board changes the item's status only when
`SHANNON_BOARD_MAY_SET_STATUS` is on. It is off by default, because nothing GitHub sends says who
moved the card.

### Admins

The brief gave Admins only `/register <github_repo_link>`.

What was built: anyone in the Admin tier passes every role gate, not only `/register`. The Admin
tier covers:

- a member who holds the role named by `SHANNON_ROLE_ADMIN`
- a member with Discord's Administrator permission, even where no role names are set up

Why for guild administrators: an administrator can give themselves any role in the server in two
clicks, so refusing them a gate would protect nothing. It also keeps a newly registered server
usable before anybody has set up the four role names. Without it, nobody could run anything there.
The CHANGELOG records this as a known departure from the brief (#17).

### Commands anyone can run

These take no tier:

- `/linked` and `/mentions`
- `/link` for your own GitHub account
- `/remind` for yourself
- `/board withdraw`

### GitHub access in place of a role

With `SHANNON_GITHUB_ACCESS_GRANTS_TIERS` on (it is off by default), GitHub access can stand in for
a role (#94). Write on the registered repository passes wherever Developer does. Admin on it passes
wherever Project Manager does. It never stands in for the Admin tier, for `/register`, or for
asking somebody else to `/link`. See [Commands](README.md#commands) for the details.

### Repeated commands, Done, and unbinding

**Repeating `/pr` or `/issue`.** The brief said that if `/pr <pr_link>` or `/issue <issue_link>`
is repeated in a channel, the channel is overwritten with the new link. What was built: each item
has one thread, in the channel mapped for its kind. Running `/pr` or `/issue` again with the same
link updates that thread and does not open a second one. A different link opens or updates that
item's own thread. Nothing is overwritten.

**Repeating a status.** The brief wrote these rules with `/SET_BACKLOG` and `/SET_NOT_REVIEWED`
(#158). They hold with the new names:

- If `/status Backlog` is repeated, no label changes. The repeat still sets the thread's lock
  state and moves the board card again, so it repairs either one if it failed the first time. The
  bot answers that the item is already
  Backlog.
- If `/status Backlog` is followed by `/status Not reviewed`, the bot removes the Backlog status
  label and applies the Not reviewed one.

**Done.** The brief said `/SET_DONE` locks the thread, and is available only once a pull request
is ready for merging. For issues it left the details for later, and said to assume that closing an
issue applies Done and locks the thread. What was built:

- `/status Done` on a pull request locks its thread.
- Where the item has a card on a linked board, a move may go forward only one column at a time.
  Backward moves are free. So on GitHub's default board template, a card cannot go from Ready to
  Done without passing through In review. This replaced the brief's "ready for merging" rule. The
  CHANGELOG entry "The board's column order became the rule, and READY_FOR_MERGE went" says why.
- With no linked board, or no card on it, nothing gates Done.
- An issue becomes Done when it is closed on GitHub, which also locks its thread. `/status Done` on
  an open issue is refused.

**Unbinding a repository.** The brief left registration to discuss later, and said to assume a
registered repository stays bound to its Discord server for ever. What was built (#98): it stays
bound until somebody runs `/unregister`. The caller needs the Admin or Project Manager tier, and
GitHub must confirm they have admin on the repository:

1. Run `/unregister owner/name`, with the repository's full name. The bot replies with a one-time
   link.
2. Open the link and sign in to GitHub.
3. Run `/unregister owner/name` again. The bot unbinds the repository only if GitHub says your
   account has admin on it.

Unbinding cannot be undone. It deletes everything the bot stored under the repository. Threads
already open stay in Discord and stop updating.

---

## MVP Requirements

The brief planned the build in five stages. Each stage is an MVP (minimum viable product): a working
version of the bot that adds the features listed under it.

### MVP 1

* Register a GitHub repository.
* Receive GitHub PR webhook events.
* Create Discord PR threads.
* Update Discord PR threads when PRs change.

### MVP 2

* Receive GitHub issue webhook events.
* Create Discord issue threads.
* Update Discord issue threads when issues change.

### MVP 3

* Add status commands.
* Add priority commands.
* Sync status and priority changes back to GitHub.

### MVP 4

* Sync GitHub Projects status into Discord.
* Mirror project board movement into Discord.

### MVP 5

* System-wide feature re-planning.
* System-wide refactorisation.
