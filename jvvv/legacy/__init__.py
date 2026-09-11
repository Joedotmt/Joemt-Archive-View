"""LEGACY: conversion of single-file ``.jvvv`` catalogues (schema 12).

Everything in this package exists only to migrate catalogues written before
the directory format.  Once every catalogue has been converted, delete this
package together with:

* ``LegacyCatalogueError`` and the ``is_file()`` check in
  ``jvvv.database.Database.__init__``;
* the ``# LEGACY`` blocks in ``jvvv/app.py`` (the conversion prompt, worker and
  menu action);
* ``tests/test_legacy_migration.py``.
"""

from .single_file import (
    LEGACY_SCHEMA_VERSION,
    LegacyCatalogueInfo,
    MigrationProgress,
    MigrationResult,
    inspect_legacy_catalogue,
    is_legacy_catalogue_file,
    legacy_archive_path,
    migrate_legacy_catalogue,
)
from ..database import LegacyCatalogueError

__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "LegacyCatalogueError",
    "LegacyCatalogueInfo",
    "MigrationProgress",
    "MigrationResult",
    "inspect_legacy_catalogue",
    "is_legacy_catalogue_file",
    "legacy_archive_path",
    "migrate_legacy_catalogue",
]
