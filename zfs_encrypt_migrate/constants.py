"""Shared constants for zfs-encrypt-migrate."""

from __future__ import annotations

import re

DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*$")
# Sentinel for a command that could not be started (missing binary, not
# executable). Deliberately outside 0-255 so it can never collide with an exit
# status a started command returns.
COMMAND_STARTUP_ERROR_RETURNCODE = 1127
