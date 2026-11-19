"""Sentences more than one command says, kept in one place so they cannot drift apart (#267).

Here rather than in the command that said it first, so one command never imports another: renaming
or removing `/link` must not break `/register`. `_replies` holds what an error becomes; this holds
the rest a command says in more than one place.
"""

from __future__ import annotations

# What an admin has to set before anybody can sign in, said by each command that needs a sign-in
# and refuses without one: `/link`, `/register` and `/unregister`. One list, so that none of them
# sends an admin off to set the wrong things. `/board` lists its own, because it signs in to a
# different application.
SIGN_IN_SETTINGS = (
    "An admin needs to set the GitHub App's client secret, this deployment's public URL, "
    "SHANNON_DISCORD_CLIENT_ID and SHANNON_DISCORD_CLIENT_SECRET."
)

# What /refresh and /regenerate end on once they have written to a thread. True because the sync
# services behind both have no notifier, and every block they write reaches Discord with an empty
# allow-list: it names linked people as real mentions, and a block that is posted rings none of
# them. Issue #185 moved it off plain-text names.
NOBODY_PINGED = "Nobody was pinged."
