"""The identity of a group of datasets (lane GV2-5a, used by GV2-5b and GV2-7a)."""
from __future__ import annotations

import hashlib
import re

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def member_set_sha256(shas) -> str:
    """The sha256 of the members' dataset sha256s, sorted, joined by a newline (utf-8).

    The order the members are given in does not matter. An empty set, or a value that is not 64 lower-case hex
    characters, raises ValueError: a group with no members, or a made-up fingerprint, must never get an identity.
    """
    values = list(shas)
    if not values:
        raise ValueError("member_set_sha256: the group has no members")
    for value in values:
        if not isinstance(value, str) or not _SHA256.match(value):
            raise ValueError("member_set_sha256: %r is not a sha256 (64 lower-case hex characters)" % (value,))
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()
