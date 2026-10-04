"""Keys for the tests that exercise an encrypted board authorisation.

Fixed rather than generated per run. A test that asserts a stored value is not the plaintext wants
the same key every time so a failure is reproducible, and a second key has to exist for the
rotation tests to have anything to rotate to.

Neither of these is a secret. They encrypt nothing but fixtures, and they are in the repository on
purpose so that nobody has to generate one to run the suite. Anything a real deployment stores is
encrypted with `SHANNON_BOARD_CREDENTIAL_KEY`, which is not here and never will be.
"""

from __future__ import annotations

from typing import Final

# What the suite writes with.
BOARD_KEY: Final = "OOVe7OEH1LLb_em3DanAEiOiKi3ZQ_xJX_CfYVRX_Bk="

# A second, for the two cases a single key cannot express: reading a row written under a key this
# deployment no longer holds, and reading one written before a rotation.
OTHER_BOARD_KEY: Final = "VcCeDjqmaFSEZNh6Me8pJNqVhSRHeDiwNG1gu-Ksk1M="
