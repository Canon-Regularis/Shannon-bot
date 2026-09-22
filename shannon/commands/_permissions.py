"""Which tier may run which command, kept beside the commands rather than in the gate."""

from __future__ import annotations

from shannon.discord_bot.roles import CommandRole

REGISTER_ROLES = frozenset({CommandRole.ADMIN, CommandRole.PROJECT_MANAGER})

# Reviewers are absent: the permissions table grants /pr, /issue, /refresh and /regenerate to
# developers and project managers only. Holding any listed role grants a command, so a reviewer
# who is also a developer still passes.
SYNC_ROLES = frozenset({CommandRole.DEVELOPER, CommandRole.PROJECT_MANAGER})

# The project manager's alone: status records a decision about somebody's work, so its author
# cannot move it to ready for merge. An administrator passes, as everywhere. `CommandRole.REVIEWER`
# and `SHANNON_ROLE_REVIEWER` grant no command at all, and the tier is kept because the denial
# message offers it. A board moves an item's status without passing through here, since nothing
# GitHub sends says who dragged the card; `SHANNON_BOARD_MAY_SET_STATUS` decides whether it may.
WORKFLOW_ROLES = frozenset({CommandRole.PROJECT_MANAGER})

# The commands that take no gate at all, by name. `/mentions` decides only whether your own name
# notifies you, and `/verify` binds the one GitHub account whoever ran it has just signed into,
# where every other command decides something about the server. A test holds this set against
# what the command factories actually take.
#
# `/verify` is the odd one, since `/link` does something that looks the same and is gated. The
# difference is what stands behind the claim: `/link` records a login nobody checked, so an
# ungated one lets anybody take any name, and this records one GitHub has just vouched for to the
# person in front of it. A gate would only stop somebody proving who they are.
UNGATED = frozenset({"mentions", "verify"})
