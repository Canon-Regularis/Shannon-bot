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
# notifies you, where every other command decides something about the server. A test holds this
# set against what the command factories actually take.
#
# `/link` is not here and reads as though it should be: connecting your own GitHub account needs
# no role either, because GitHub decides which account it is and a gate would only stop somebody
# proving who they are. It keeps a gate because its other half pings a member in public, and that
# half takes the tier that speaks for the server. A command gated on one of its arguments cannot
# be described by a list of names, so this stays a list of commands anybody may run whatever they
# type, and `/link`'s own tests hold the tier.
UNGATED = frozenset({"mentions"})
