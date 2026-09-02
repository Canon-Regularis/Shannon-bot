"""Which tier may run which command.

A product decision, kept beside the commands it names rather than in the permission gate. The
gate knows how to read a Discord member's roles; it has no business knowing that registering a
repository is an administrator's job and syncing a link is a developer's.
"""

from __future__ import annotations

from shannon.discord_bot.roles import CommandRole

REGISTER_ROLES = frozenset({CommandRole.ADMIN, CommandRole.PROJECT_MANAGER})

# Reviewers are deliberately absent: the permissions table grants /pr and /issue to developers
# and project managers only. Somebody who holds one of those as well as Reviewer still passes,
# because holding any listed role is what grants a command rather than holding only listed ones.
SYNC_ROLES = frozenset({CommandRole.DEVELOPER, CommandRole.PROJECT_MANAGER})

# The project manager's alone. Status is the record of a decision about somebody's work, and the
# author of that work moving it to ready for merge is the review step going missing, which is why
# developers were never here. Reviewers were, and are not any more: deciding that a change is good
# and recording that the project has accepted it are two different jobs, and this bot is where the
# second one is written down. An administrator still passes, as they do everywhere.
#
# `CommandRole.REVIEWER` and `SHANNON_ROLE_REVIEWER` are kept and now grant no command at all. The
# tier is still worth configuring: it is what the denial message offers, and it is the obvious
# place for a later command that is a reviewer's rather than a manager's.
#
# A board is the other way an item's status can move, and it does not pass through here at all,
# because nothing GitHub sends says who dragged the card. `SHANNON_BOARD_MAY_SET_STATUS` decides
# whether it may, and it is off unless somebody turns it on.
WORKFLOW_ROLES = frozenset({CommandRole.PROJECT_MANAGER})
