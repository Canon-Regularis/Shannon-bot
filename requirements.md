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
the fields below. Two differences from the list as first written: a `State:` line carries GitHub's
own open, closed or merged, which the status field does not, and the `Reviewers:` line is omitted
for issues, because GitHub issues have no reviewers and a row that always reads `None` is noise
rather than information.

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
```

For every synced ticket, the bost must generate a Discord message (in the relevant thread) with:

```text
Ticket Name:
GitHub Link:
Status:
```

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
