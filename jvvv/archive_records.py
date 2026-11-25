"""Verified project versions live in the catalogue registry, independently of queue history."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ArchiveRecords:
    def __init__(self, registry):
        self.registry = registry

    def _exists(self):
        return self.registry.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_projects'"
        ).fetchone() is not None

    def project(self, project_id):
        if self._exists():
            return self.registry.execute("SELECT * FROM archive_projects WHERE id=?", (project_id,)).fetchone()

    def copies(self, project_id):
        if not self._exists():
            return set()
        return {row[0] for row in self.registry.execute(
            "SELECT volume_uuid FROM archive_copies WHERE project_id=?", (project_id,)
        )}

    def removal(self, project_id):
        if self.registry.execute("SELECT 1 FROM sqlite_master WHERE name='archive_source_removals'").fetchone():
            return self.registry.execute("SELECT * FROM archive_source_removals WHERE project_id=?", (project_id,)).fetchone()

    def begin_removal(self, project_id, identity):
        with self.registry as conn:
            self._create_removals()
            conn.execute("INSERT INTO archive_source_removals VALUES (?,?,NULL)", (project_id, identity))

    def _create_removals(self):
        self.registry.execute("CREATE TABLE IF NOT EXISTS archive_source_removals "
                              "(project_id TEXT PRIMARY KEY, identity TEXT, deleted_at TEXT)")

    def finish_removal(self, project_id):
        with self.registry as conn:
            conn.execute("DELETE FROM archive_entries WHERE project_id=?", (project_id,))
            conn.execute("UPDATE archive_source_removals SET deleted_at=? WHERE project_id=?",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), project_id))

    def folder_project(self, volume_uuid, folder):
        if self._exists():
            row = self.registry.execute(
                "SELECT project_id FROM archive_copies WHERE volume_uuid=? AND folder=?", (volume_uuid, folder)
            ).fetchone()
            return row[0] if row else None

    def snapshot_index(self):
        if not self._exists():
            return {}
        projects = {row["id"]: {"source": row["source"], "algorithm": row["algorithm"], "copies": {}}
                    for row in self.registry.execute("SELECT * FROM archive_projects")}
        for volume, folder, project in self.registry.execute("SELECT volume_uuid, folder, project_id FROM archive_copies"):
            projects[project]["copies"][volume] = folder
        if self.registry.execute("SELECT 1 FROM sqlite_master WHERE name='archive_source_removals'").fetchone():
            for project, date in self.registry.execute(
                "SELECT project_id,deleted_at FROM archive_source_removals WHERE deleted_at IS NOT NULL"
            ):
                projects[project]["deleted_at"] = date
        if self._has_copy_dates():
            for project, volume, date in self.registry.execute(
                "SELECT project_id,volume_uuid,verified_at FROM archive_copies WHERE verified_at IS NOT NULL"
            ):
                projects[project].setdefault("verified_at", {})[volume] = date
        return projects

    def snapshot_entries(self, project):
        for row in self.registry.execute(
            "SELECT path,directory,size,mtime,hex(digest),media FROM archive_entries WHERE project_id=? ORDER BY path",
            (project,)
        ):
            yield json.dumps(tuple(row), separators=(",", ":")).encode("utf-8") + b"\n"

    def restore_project(self, project, record, lines):
        self._create_tables()
        self.registry.execute("INSERT INTO archive_projects VALUES (?,?,?)",
                              (project, record["source"], record["algorithm"]))

        def entries():
            for line in lines:
                path, directory, size, mtime, digest, media = json.loads(line)
                yield project, path, directory, size, mtime, bytes.fromhex(digest) if digest else None, media

        self.registry.executemany("INSERT INTO archive_entries VALUES (?,?,?,?,?,?,?)", entries())
        self.registry.executemany("INSERT INTO archive_copies VALUES (?,?,?,?)",
                                  ((volume, folder, project, record.get("verified_at", {}).get(volume))
                                   for volume, folder in record["copies"].items()))
        if record.get("deleted_at"):
            self._create_removals()
            self.registry.execute("INSERT INTO archive_source_removals VALUES (?,NULL,?)", (project, record["deleted_at"]))

    def load(self, project_id, source, algorithm, entries):
        if self.removal(project_id):
            raise OSError("Source deletion was started for this project. Further copies are disabled.")
        project = self.project(project_id)
        if project is None:
            return None
        if Path(project["source"]) != source or project["algorithm"] != algorithm:
            raise OSError("The source or hash algorithm differs from the saved project.")
        rows = {r["path"]: r for r in self.registry.execute(
            "SELECT * FROM archive_entries WHERE project_id=?", (project_id,)
        )}
        current = {entry.relative for entry in entries}
        if missing := rows.keys() - current:
            raise OSError(f"Missing from the original project: {min(missing)}")
        if added := current - rows.keys():
            raise OSError(f"File list changed. Added to the original project: {min(added)}")
        digests, media = {}, {}
        for entry in entries:
            row = rows[entry.relative]
            if (entry.directory != row["directory"] or entry.info.st_mtime_ns != row["mtime"]
                    or (not entry.directory and entry.info.st_size != row["size"])):
                raise OSError(f"The original project changed: {entry.relative or source.name}")
            if entry.directory:
                continue
            digests[entry.relative] = row["digest"]
            if row["media"]:
                metadata, preview = json.loads(row["media"])
                if preview and preview.get("source_hash"):
                    preview["source_hash"] = bytes.fromhex(preview["source_hash"])
                media[entry.relative] = metadata, preview
        return digests, media

    def _create_tables(self):
        conn = self.registry
        conn.execute("CREATE TABLE IF NOT EXISTS archive_projects "
                     "(id TEXT PRIMARY KEY, source TEXT NOT NULL, algorithm TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS archive_entries "
                     "(project_id TEXT, path TEXT, directory INTEGER, size INTEGER, mtime INTEGER, "
                     "digest BLOB, media TEXT, PRIMARY KEY(project_id,path))")
        conn.execute("CREATE TABLE IF NOT EXISTS archive_copies "
                     "(volume_uuid TEXT, folder TEXT, project_id TEXT, PRIMARY KEY(volume_uuid,folder))")
        if not self._has_copy_dates():
            conn.execute("ALTER TABLE archive_copies ADD COLUMN verified_at TEXT")

    def _has_copy_dates(self):
        return any(row[1] == "verified_at" for row in self.registry.execute("PRAGMA table_info(archive_copies)"))

    def copy_details(self, project_id):
        date = "verified_at" if self._has_copy_dates() else "NULL AS verified_at"
        return self.registry.execute(f"SELECT volume_uuid,folder,{date} FROM archive_copies WHERE project_id=?",
                                     (project_id,)).fetchall()

    def save_version(self, project_id, source, algorithm, entries, digests, media):
        """Called under the archive writer lock before publishing any verified destination."""
        with self.registry as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._create_tables()
            if self.project(project_id):
                saved, _ = self.load(project_id, source, algorithm, entries)
                if saved != digests:
                    raise OSError("The file contents differ from the saved project version.")
                return
            conn.execute("INSERT INTO archive_projects VALUES (?,?,?)", (project_id, str(source), algorithm))
            conn.executemany("INSERT INTO archive_entries VALUES (?,?,?,?,?,?,?)", (
                (project_id, e.relative, e.directory, e.info.st_size, e.info.st_mtime_ns, digests.get(e.relative),
                 json.dumps(media[e.relative], default=bytes.hex) if e.relative in media else None)
                for e in entries
            ))

    def verified(self, project_id, volume_uuid, folder):
        with self.registry as conn:
            self._create_tables()
            conn.execute("INSERT OR REPLACE INTO archive_copies VALUES (?,?,?,?)",
                         (volume_uuid, folder, project_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))
