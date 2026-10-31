"""Shared constants for zfs-encrypt-migrate."""

from __future__ import annotations

import re
from pathlib import Path

DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*$")
# Sentinel for a command that could not be started (missing binary, not
# executable). Deliberately outside 0-255 so it can never collide with an exit
# status a started command returns.
COMMAND_STARTUP_ERROR_RETURNCODE = 1127

LOCK_FILE = Path("/run/zfs-encrypt-migrate.lock")
STAGING_MOUNT_ROOT = Path("/run/zfs-encrypt-migrate")
SCRATCH = ".zfs-encrypt-migrate"
MARK_SOURCE = "zfs-encrypt-migrate:file-move"
MARK_DIRS = "zfs-encrypt-migrate:dirs"
MARK_KEY = "zfs-encrypt-migrate:key-checked"
SPACE_ATTEMPTS = 30
SPACE_DELAY_SECONDS = 10.0
