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

# Developers are the ones deliberately absent here. Status is the record of what a reviewer has
# decided about somebody's work, and the author of that work moving it to ready for merge is the
# review step going missing. An administrator still passes, as they do everywhere.
WORKFLOW_ROLES = frozenset({CommandRole.REVIEWER, CommandRole.PROJECT_MANAGER})
