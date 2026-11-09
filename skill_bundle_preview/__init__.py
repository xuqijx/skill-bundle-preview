"""Offline dry-run for an Agent Skill folder before sharing or zipping it.

Only the root SKILL.md is opened. Other files are listed from lstat sizes
and are not opened or hashed. Symlink targets are not read.
"""

__version__ = "0.1.0"
