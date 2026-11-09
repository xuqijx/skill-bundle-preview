"""Secret-like filename rules, v0.

Matching uses the file basename only. File contents are never opened here.
"""

from __future__ import annotations

import fnmatch

# First match wins. Patterns are case-sensitive, same as a Linux basename.
SECRET_RULES: tuple[str, ...] = (
    ".env",
    ".env.*",
    "id_rsa*",
    "*.pem",
    "*.p12",
    "*.key",
    "credentials.json",
    "*.keystore",
)


def matching_secret_rule(basename: str) -> str | None:
    """Return the first secret-filename rule that matches this basename."""
    for pattern in SECRET_RULES:
        if pattern in {".env", "credentials.json"}:
            if basename == pattern:
                return pattern
            continue
        if fnmatch.fnmatchcase(basename, pattern):
            return pattern
    return None
