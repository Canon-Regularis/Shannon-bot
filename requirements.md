# Shannon Bot Technical Requirements

## Goal

Build a Discord bot that syncs GitHub repository activity into Discord.

Both GitHub and Discord are considered sources of truth for this project (duplex communication).

---

## Core Stack

* Python
* PostgreSQL
* Discord API
* GitHub API
* GitHub Webhooks
* FastAPI

---

## Required Integrations

The bot must integrate with:

* GitHub repositories
* GitHub pull requests
* GitHub issues
* GitHub Projects
* Discord channels
* Discord forum posts or threads
* Discord roles and user pings

---

## Main Flow

1. A PR, issue, or project item is created or updated on GitHub.
2. GitHub sends a webhook event to the bot.
3. The bot reads the event.
4. The bot stores or updates the record in PostgreSQL.
5. The bot creates or updates the matching Discord post/thread.
6. The bot pings the relevant users or roles.

---

## Required Bot Commands

```text
/register <github_repo_link>
```

Registers a GitHub repository with the Discord server.
There can only be one repository registered with a Discord server.

```text
/pr <pr_link>
```

Fetches a GitHub pull request and creates or updates its Discord thread.

```text
/issue <issue_link>
```

Fetches a GitHub issue and creates or updates its Discord thread.

```text
/SET_BACKLOG
```

Marks an item as `BACKLOG`, on the Github side + the channel.

```text
/SET_NOT_REVIEWED
```

Marks an item as `NOT REVIEWED`, on the Github side + the channel.

```text
/SET_IN_REVIEW
```

Marks an item as `IN REVIEW`,  on the Github side + the channel.

```text
/SET_READY_FOR_MERGE
```

Marks an item as `READY_FOR_MERGE`, on the Github side + the channel.

```text
/SET_HIGH_PRIORITY
```

Marks an item as high priority, on the Github side.

```text
/SET_MED_PRIORITY
```

Marks an item as medium priority, on the Github side.

```text
/SET_LOW_PRIORITY
```

Marks an item as low priority, on the Github side.

```text
/SET_DONE
```

Marks an item as `DONE`, on the Github side + the channel

---

## Required Statuses

The bot must support these statuses (existing as tags in the relevant repository):

```text
NOT_REVIEWED
IN_REVIEW
READY_FOR_MERGE
BACKLOG
DONE
```

---

## Required Priorities

The bot must support these priorities:

```text
HIGH
MEDIUM
LOW
```

Stored alongside them is `UNSET`, which is not a fourth priority but the absence of the three. An
item carries no priority label until somebody gives it one, and that state has to be nameable.

---

## Discord Output Format

For every synced PR / issue, the bot must generate a Discord message (in the relevant thread) with
the fields below. Three differences from the list as first written: a `State:` line carries
GitHub's own open, closed or merged, which the status field does not; the `Reviewers:` line is
omitted for issues, because GitHub issues have no reviewers and a row that always reads `None` is
noise rather than information; and a `Description:` section is added at the end, because the list
said what an item was called and who was on it and nothing about what it was for. The description
is left out entirely when the item was opened without one.

```text
PR / issue Name:
Type: PR / issue
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

For every synced ticket, the bot must generate a Discord message (in the relevant thread) with the
fields below, and must say in that thread when any of them changes.

The list grew twice. It was three lines as first written; a `Type:` and a `Last Updated:` joined it
so the three blocks read alike and because the board read already carried the timestamp. The rest
arrived with the board read itself: it asked GitHub for Title and Status alone, and a card could
carry nothing else. It asks for the board's own fields now.

A row with nothing in it is still left out rather than rendered `None`, because an always-empty
field reads as data missing rather than data absent. The field NAMES belong to whoever owns the
board, so a board that calls one of these something else simply has no value for that row - and
`Story Point` is singular because that is how the board spells it. `State:` stays off for a
different reason: a ticket's state is open and nothing can close it, because a board column is not
a closed state, so the row could only ever say `Open`.

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

A change to any of these except `Creator` is announced in the thread, in ONE message listing
everything that moved since the last poll - a board read sees a card's fields together, so somebody
dragging a card and setting its points did one thing and hears about it once. `Creator` is the only
exclusion, because a card's creator does not change.

`Ticket Name` and `Description` are announced as well, and they are in the list because `Updated`
is. The poll only looks at a card whose timestamp moved, so `Updated` differs every time a message
is posted; on its own that would report that a card changed without saying what. A title and a
description are the two edits that move a timestamp without moving anything else, so naming them
is what gives such a message a cause. A message carrying nothing but a timestamp now means what it
says: GitHub re-stamped the card and nothing a reader can see moved with it.

Each value in such a message is cut short. The message is a signpost rather than a diff, and the
block directly above it carries every value in full - without a cut, a description of several
hundred characters on each side would put the message past what Discord accepts and cost all of it
rather than the extra words.

A card seen for the first time announces nothing. Its fields are recorded and the block itself is
the announcement, which is what stops every card on a board reporting every field at once the first
time this runs.

All of these blocks, and every other message this bot posts into a thread, show the formatting the
text was written with: a heading, a list, a code span or a link in a GitHub body arrives as what it
means rather than as the characters somebody typed. One conversion does it for all of them.

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

Project boards are read rather than delivered. This section previously named
`project_card.created`, `project_card.moved` and `project_card.updated`, and none of the three can
fire: they belong to Projects (classic), which GitHub sunset on 23 August 2024, whose REST API was
sunset on 1 April 2025, and which was removed from GitHub Enterprise Server in 3.17. The last
release that still contained it went end of life on 1 July 2026. `project_card.updated` was never
a valid action even while classic existed; the five were `converted`, `created`, `deleted`,
`edited` and `moved`.

They are still documented on GitHub's webhook page, complete with an availability line, which is
a leftover in the published schema rather than a promise. A bot subscribed to them receives
nothing, for ever, with no error.

The replacement events are `projects_v2`, `projects_v2_item` and `projects_v2_status_update`, and
they are **organisation scope only**. A repository webhook receives none of them, and a project
owned by a user account emits none of them at all. Since this bot registers against a repository
owned by a personal account, there is no event to subscribe to, so the board is polled through the
Projects v2 REST API instead. See `shannon/services/projects.py`.

### What polling costs, and why it costs that

Polling means a floor on how fast a card can reach Discord, because there is no event to wait on.
The floor is half the interval on average and a whole one at worst, plus the read and the sync. An
issue has no such floor: its webhook is queued and a worker takes it within two seconds.

The interval is **two seconds**, so a ticket and an issue now arrive on the same clock. It used to
be sixty, which is why a ticket took thirty to forty-five seconds - that was the window, halved.

Two seconds is affordable because of one measured fact: GitHub honours `If-None-Match` on the
project items endpoint, and **a 304 carries no body and spends no rate-limit budget**. Ten
conditional polls two seconds apart left the remaining-requests counter untouched. A board nobody
has touched therefore costs one request and nothing else, and thirty times as many passes cost no
more per hour than the old interval did. The `Cache-Control: max-age=60` on that response is advice
to a client rather than a staleness floor - GitHub sends no `Age` header and its `Date` advances on
every request - so a short interval genuinely sees a change promptly.

The saving is in the transport only. An unchanged board answers out of the cards parsed last time,
which is sound because a 304 proves the body was byte-identical, and nothing downstream behaves any
differently for it: every comparison the poll makes, and every card still waiting for a thread, is
reached exactly as often as before.

A board with more than one page of cards is the exception. A validator hashes one response body, so
such a board cannot be checked without downloading the whole of it, and it is polled on the slow
clock instead - slower rather than more expensive, which is the direction a surprise should fail
in. It is logged once when it happens. The current board has eighty cards against a page of a
hundred, so this will arrive eventually and should not be a mystery when it does.

### Who a board is read as

Issue #170. Each person authorises this bot for themselves, and what they grant is used for exactly
two things: a board they link is read under their authorisation, and a card **they** move from
Discord is moved as them. Nothing is shared between servers, and nothing acts for somebody who did
not ask.

What that replaced is worth recording, because the shape of it is the reason for the rest. Every
board in every server was read and written through **one classic personal access token belonging to
one human account**, with the `project` scope - read and write across every project that account
could see. Nothing scoped it per server: the credential supplier took the board's owner as an
argument and discarded it. So a card moved from Discord appeared on GitHub as the token's owner
whoever had asked for it, and one leaked token was write access to every board that account could
reach.

The authorisation goes through a **classic OAuth App**, registered separately from the GitHub App,
and that is forced rather than chosen. GitHub publishes no App permission for a **user-owned**
Projects v2 board - its Projects permission exists at organisation level only, for Apps and
fine-grained tokens alike - and granting an installed App an organisation permission suspends its
event delivery until an admin accepts, which would stop every webhook in every registered
repository. OAuth scopes have neither problem, and `project` covers user and organisation projects
alike.

A board authorisation is the **first credential this bot stores**, and the only encrypted column in
the schema. Everything else it keeps about a person is a fact *about* them - a login, an account id,
a preference. This is a thing that *acts as* them, so it is encrypted at rest with a key held in the
environment and never in the database, which is what makes a stolen copy of the table worth nothing
on its own. The key is a comma-separated list, newest first, so rotating it is one deploy rather
than everybody authorising again; a row that will not decrypt counts as absent, and the person
authorises again to replace it.

There is no shared credential left at all. `SHANNON_GITHUB_PROJECT_TOKEN` is gone, and with it the
object that existed to hand the client a fixed token — so there is no longer any way to give this
bot one credential that sees everything. The board's write client carries no credential of its own,
which makes "a card is moved as somebody" a fact of the wiring rather than a check somebody could
forget: a write with nobody behind it goes out anonymous and GitHub answers 401.

Linking a board therefore requires having authorised. A board linked on somebody else's
authorisation would put a server straight back on one person's credential.

### One command, and one click

Issue #201. That rule made linking a board for the first time two commands with a trip to GitHub
between them, and undoing it was split the same way. It is one command now, `/board`, with five
halves:

| Half | Who | What it does |
|---|---|---|
| `/board link` | Admin, Project Manager | Mirrors a board. With no authorisation yet, it hands out ONE link that remembers the board chosen: following it authorises and links, and there is nothing else to run |
| `/board unlink` | Admin, Project Manager | Stops mirroring, forgets the linker's authorisation, and names them - forgetting this copy is not revoking the grant, and only they can do that, under Applications in their GitHub settings |
| `/board authorise` | Admin, Project Manager | Grants your own, so a card you move with `/status` or `/priority` moves as you |
| `/board withdraw` | anybody | Forgets your own. No tier, deliberately: deleting a credential that is yours must not depend on a role you may since have lost |
| `/board show` | Admin, Project Manager | The board, who linked it, whether it still opens, and your own authorisation |

The tier on `authorise` and `show` is the set of tiers whose authorisation is ever actually used -
linkers and card movers - which today is the same two, so nobody who needs one lost it, and nobody
whose credential nothing would use can hand one over.

The board a link was issued for rides on the pending verification row, never in the URL: the state
stays the only thing in the link, and nothing can edit the board on its way to GitHub and back.

Three things the merge found, and fixed, on the way:

- **The board was opened and listed as the App.** The check that a board exists, and the picker,
  sent no credential, which the client fills in with the App installation's token - and the App
  holds no Projects permission. A private board could not be linked, and the refusal blamed the
  person's own authorisation. Both now go out as the person choosing.
- **A board was told apart by how its owner was written down.** Null means the repository's own
  owner, and the comparison did not resolve it: two servers each linking their own account's #1 were
  one board (the second was refused and told the first's repository name), while a server naming
  another's board by its owner was a different one - after which a poll read that board under the
  wrong server's member. The owner is resolved in the query now, and a refusal names no other server.
- **The scope GitHub granted was never checked.** GitHub lets a person grant less than was asked.
  The sign-in now refuses, and keeps nothing, when `project` is missing.

No scope was added. `project` covers user and organisation boards alike; `read:org` is asked for by
no Projects v2 document; and `repo`, the only scope that might let a board show a card wrapping a
private repository's issue, is full control of every private repository the person can reach -
the opposite of the narrowest grant the feature needs. An organisation's board needs the
organisation to approve the OAuth App, which is an access restriction rather than a scope, and the
sign-in text says where to press.

### What "anonymity" can and cannot mean here

The issue asked for *"full security and anonymity wherever possible"*, and the achievable property
is **least authority plus correct attribution** rather than anonymity. GitHub sees the grant and
sees the write; there is no arrangement under which it does not. What changed is that it now sees
the right person, with the narrowest grant the feature needs, revocable by them - where before it
saw one account standing in for everybody.

Worth stating plainly so it is not re-litigated: **nothing else in this database is encrypted.**
`user_links`, `verified_identities`, `item_assignments`, `logged_messages.content`, `muted_members`
and `webhook_events.payload` are all plaintext, and most of them are queried on, so an encrypted
column could not be an index key. Encrypting them is a database-wide project with a key-management
story and a backfill, and folding it into a board-linking change would have made both worse. It is
a known posture, not an oversight.

**What would remove the floor entirely:** move the board to an organisation and subscribe to
`projects_v2_item`. The board then joins the same delivery queue as everything else and inherits
the worker's latency, with no poller involved. Two caveats: those events are in public preview, and
granting a new permission to an already-installed App suspends its deliveries until an admin
accepts. Until then the floor is GitHub's, not this bot's.

---

## Required Database Tables

Two more exist than are listed here, each because a specific failure demanded it. `user_links`
holds the GitHub login to Discord account pairing, which pinging needs somewhere to read from
before an assignment row exists. `mirrored_notes` records which comments and reviews have already
been posted, because the delivery queue is at-least-once and putting a comment in a thread is the
one step that cannot be undone.

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

The last five were not in the original list and each was added for a reason worth keeping.
`github_object_number` is how a comment or a review finds its item, because those payloads report
an issue id even for a pull request. `github_updated_at` is the high water mark that stops a late
delivery undoing a newer one. `project_column` is the board column as of the last poll, which is
what tells a card that has moved from one that merely disagrees with a status somebody set.

### item_assignments

Stores assignees, reviewers and authors. Project managers are not among them: that is a Discord
permission tier, and no fact about a pull request or an issue produces one, so the role was
removed from `ActorRole` rather than left as a value nothing could ever write.

A requested team is stored here too, as an ordinary row whose `github_username` is the team slug.
It is named in the thread like anybody else; it cannot be mentioned, because `/link` binds a
GitHub login to a Discord account and a team has no login to bind.

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

`discord_user_id` was here and was dropped in migration `0008`. It was a copy of
`user_links.discord_user_id`, which is the authoritative table and is read at render time anyway,
so the column could only ever hold a stale duplicate. `notified_at` and `fulfilled_at` are the two
claim stamps that stop somebody being pinged twice for one request.

### webhook_events

Not a log of what has happened. It is a leased work queue, and became one because GitHub allows an
endpoint ten seconds and never redelivers a delivery it recorded as failed, so the route writes the
delivery down and answers while a worker does everything slow behind it. That is why it carries the
payload, an attempt count, a backoff, a lease and the last error alongside the columns below.

Stores processed webhook events.

```text
id
github_delivery_id
event_type
payload_hash
processed_at
status
```

---

## Required Behaviour

When a GitHub PR is created:

* Create a Discord PR thread.
* Add PR metadata.
* Set default status to `NOT_REVIEWED`.
* Ping reviewers if assigned.

When a GitHub issue is created:

* Create a Discord issue thread.
* Add issue metadata.
* Set priority if GitHub labels contain priority data.
* Ping assignees if assigned.

When a PR or issue changes:

* Update the existing Discord thread.
* Do not create a duplicate thread.

When a reviewer is assigned:

* Update the Discord thread.
* Ping the reviewer.

When a status changes:

* Update GitHub first.
* Then update Discord.

When a priority changes:

* Update GitHub labels or GitHub Project fields first.
* Then update Discord.

---

## Permissions

### Developers

Can:

```text
/pr_link
/issue_link
```

### Reviewers

Can:

```text
/SET_BACKLOG
/SET_NOT_REVIEWED
/SET_IN_REVIEW
/SET_READY_FOR_MERGE
/SET_HIGH_PRIORITY
/SET_MED_PRIORITY
/SET_LOW_PRIORITY
/SET_DONE
```

### Project Managers

Can:

```text
/register <github_repo_link>
/pr <pr_link>
/issue <issue_link>
/SET_BACKLOG
/SET_NOT_REVIEWED
/SET_IN_REVIEW
/SET_READY_FOR_MERGE
/SET_HIGH_PRIORITY
/SET_MED_PRIORITY
/SET_LOW_PRIORITY
/SET_DONE
```

### Admins

Can:

```text
/register <github_repo_link>
```

In practice a guild administrator passes every gate, not only this one. Refusing them the others
would be theatre: an administrator can give themselves any role in the server in two clicks, so a
check they can walk around is an inconvenience rather than a control. It also keeps a freshly
registered server usable, where nobody has set the four role names up yet and there would
otherwise be no one able to run anything.

### Important Detail

If `/pr <pr_link>` / `issue <issue_link` is duplicated in a channel, then the channel should be overwritten with the new link. 

If `/SET_BACKLOG` is duplicated, then no action should be taken; however, if `/SET_BACKLOG` is followed by `/SET_NOT_REVIEWED`, 
then we should remove the status effect of `/SET_BACKLOG`, and follow it by applying the `/SET_NOT_REVIEWED` effect. 

Once `/SET_DONE` is performed, the thread should then be locked - this should only be available once a PR is set 
to be ready for merging, later on we should also discuss the relevant framework we can add for issues (for now, 
simply assume that once an issue is closed, the `/SET_DONE` effect is performed and the thread is locked.

At a later date, we will also discuss the register functionality in further detail - for now, it should be
assumed that once we register a github repository in a discord server, it is bound indefinitely to that
server.

---

## MVP Requirements

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
