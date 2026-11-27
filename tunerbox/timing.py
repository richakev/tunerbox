"""Where the time goes while playlists are read (a scan, a playlist's channels, its On Demand lists), for
the log. A slow install (often Windows: antivirus scanning the database, a synced folder, a slow disk) can
then be compared step by step with a quick one."""
import os
import platform
import sqlite3
import sys
import time

from . import config, db

SLOW = 2.0          # a download that took longer than this is logged with where its time went


class Steps:
    """lap("what") after each step; str() -> '12.3 s (what 1.0 s, next 11.3 s)'."""

    def __init__(self):
        self.start = self.t = time.monotonic()
        self.parts = []

    def lap(self, what):
        now = time.monotonic()
        self.parts.append(f"{what} {now - self.t:.1f} s")
        self.t = now

    def total(self):
        return time.monotonic() - self.start

    def __str__(self):
        return f"{self.total():.1f} s ({', '.join(self.parts)})" if self.parts else f"{self.total():.1f} s"


SYNCED = ("OneDrive", "Dropbox", "Google Drive", "iCloud")


def machine():
    """The machine and database a scan ran on: what tends to differ between a slow install and a quick one."""
    path = config.DATA / "tunerbox.db"
    where = f"database {path}"
    try:
        t = time.monotonic()
        with db.conn() as c:
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
            sync = {0: "off", 1: "normal", 2: "full", 3: "extra"}.get(c.execute("PRAGMA synchronous").fetchone()[0], "?")
        opened = time.monotonic() - t
        wal = path.with_name(path.name + "-wal")
        where += (f" {path.stat().st_size / 1e6:.0f} MB (journal {mode}, synchronous {sync}, "
                  f"WAL file {wal.stat().st_size / 1e6 if wal.exists() else 0:.0f} MB, opened in {opened * 1000:.0f} ms)")
    except Exception as e:  # noqa: BLE001
        where += f" ({e})"
    synced = next((s for s in SYNCED if s.lower() in str(path).lower()), "")
    if synced:
        where += f", inside a {synced} folder (it re-uploads the database after writes, which slows them down)"
    bundled = " (bundled)" if getattr(sys, "frozen", False) else ""
    return (f"{platform.platform()}, Python {platform.python_version()}{bundled}, SQLite {sqlite3.sqlite_version}, "
            f"{os.cpu_count()} CPUs; {where}")
