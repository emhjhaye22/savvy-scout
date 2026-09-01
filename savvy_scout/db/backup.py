"""Daily database backup. Wired to Windows Task Scheduler, see README."""

import os
import shutil
from datetime import datetime
from pathlib import Path

# SAVVY_SCOUT_BACKUPS_DIR lets a production deploy (Render's persistent
# disk, e.g. /var/data/backups) point backups somewhere that actually
# survives a redeploy -- the default keeps local dev unchanged.
DEFAULT_BACKUPS_DIR = os.environ.get("SAVVY_SCOUT_BACKUPS_DIR", "backups")

# 2026-08-19 incident: backups were never pruned, so the persistent disk
# (1 GB on Render) silently filled to 100% after 12 days of accumulated
# daily snapshots, breaking every DB write in the live app (including
# login) until the oldest backups were manually deleted.
#
# 2026-08-23 and 2026-09-01 recurrence: the first fix pruned *after*
# copying the new backup, so once the disk was already full the copy
# itself failed with a disk I/O error before pruning ever ran -- a
# deadlock that made every subsequent backup fail the same way until
# someone deleted old backups by hand again. Pruning now runs first,
# to keep - 1, so there is always room for the new copy regardless of
# how full the disk got; deleting files never needs free space, so
# this step can never itself be blocked by the disk being full.
#
# Lowered the default from 7 to 3 in the same fix: the live database
# has grown from ~120MB to ~154MB in under two weeks, so 7 full-copy
# backups no longer fit in the 1GB disk even when pruning works
# perfectly. This buys headroom, not a permanent fix for that growth --
# see the OneDrive/Graph offload discussed for savvy-scout, which gets
# backups off this disk entirely instead of managing around its size.
KEEP_BACKUPS = int(os.environ.get("SAVVY_SCOUT_KEEP_BACKUPS", "3"))


def _prune_old_backups(backup_dir: Path, stem: str, suffix: str, keep: int) -> None:
    existing = sorted(
        backup_dir.glob(f"{stem}_*{suffix}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in existing[keep:]:
        stale.unlink(missing_ok=True)


def backup_database(db_path: str, backup_dir: str = DEFAULT_BACKUPS_DIR, keep: int = KEEP_BACKUPS) -> str:
    source = Path(db_path)
    if not source.exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    dest_dir = Path(backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Prune to keep - 1 before writing the new copy, not after: this is
    # the only ordering where a full disk can't permanently wedge backups
    # (see the 2026-08-23/09-01 note above).
    _prune_old_backups(dest_dir, source.stem, source.suffix, max(keep - 1, 0))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{source.stem}_{stamp}{source.suffix}"
    shutil.copy2(source, dest)
    return str(dest)
