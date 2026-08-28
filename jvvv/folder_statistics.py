from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


FolderStatistics = dict[int, dict[str, int]]
FolderStatsProgress = Callable[[int, int, str], None]
Row = Mapping[str, Any]


def calculate_folder_statistics(
    folder_rows: Iterable[Row],
    direct_file_rows: Iterable[Row],
    duplicate_file_rows: Iterable[Row],
    progress_callback: FolderStatsProgress | None = None,
) -> tuple[FolderStatistics, int]:
    """Calculate canonical recursive folder totals from catalogue rows."""
    stats: FolderStatistics = {}
    depth_by_id: dict[int, int] = {}
    parent_by_id: dict[int, int | None] = {}
    children_by_parent: dict[int, list[int]] = {}

    for row in folder_rows:
        folder_id = int(row["id"])
        relative_path = str(row["relative_path"] or "")
        parent_value = row["parent_id"]
        parent_id = int(parent_value) if parent_value is not None else None
        stats[folder_id] = {
            "direct_size": 0,
            "direct_file_count": 0,
            "direct_subfolder_count": 0,
            "recursive_size": 0,
            "recursive_file_count": 0,
            "recursive_subfolder_count": 0,
        }
        depth_by_id[folder_id] = 0 if not relative_path else relative_path.count("/") + 1
        parent_by_id[folder_id] = parent_id
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(folder_id)

    for folder_id, children in children_by_parent.items():
        if folder_id in stats:
            stats[folder_id]["direct_subfolder_count"] = len(children)

    indexed_file_count = 0
    for row in direct_file_rows:
        direct_file_count = int(row["direct_file_count"] or 0)
        indexed_file_count += direct_file_count
        folder_value = row["folder_id"]
        if folder_value is None:
            continue
        folder_id = int(folder_value)
        if folder_id in stats:
            stats[folder_id]["direct_size"] = int(row["direct_size"] or 0)
            stats[folder_id]["direct_file_count"] = direct_file_count

    total = len(stats)
    for processed, folder_id in enumerate(
        sorted(depth_by_id, key=depth_by_id.get, reverse=True),
        start=1,
    ):
        folder_stats = stats[folder_id]
        recursive_size = folder_stats["direct_size"]
        recursive_file_count = folder_stats["direct_file_count"]
        recursive_subfolder_count = folder_stats["direct_subfolder_count"]
        for child_id in children_by_parent.get(folder_id, ()):
            child_stats = stats.get(child_id)
            if child_stats is None:
                continue
            recursive_size += child_stats["recursive_size"]
            recursive_file_count += child_stats["recursive_file_count"]
            recursive_subfolder_count += child_stats["recursive_subfolder_count"]
        folder_stats["recursive_size"] = recursive_size
        folder_stats["recursive_file_count"] = recursive_file_count
        folder_stats["recursive_subfolder_count"] = recursive_subfolder_count

        if progress_callback and (processed == total or processed % 1000 == 0):
            progress_callback(processed, total, "Calculating folder statistics")

    _deduplicate_linked_file_sizes(
        duplicate_file_rows,
        stats,
        parent_by_id,
    )
    return stats, indexed_file_count


def _deduplicate_linked_file_sizes(
    rows: Iterable[Row],
    stats: FolderStatistics,
    parent_by_id: Mapping[int, int | None],
) -> None:
    current_identity: tuple[int, int] | None = None
    current_size = 0
    ancestor_counts: dict[int, int] = {}

    def apply_current_group() -> None:
        if current_identity is None:
            return
        for folder_id, count in ancestor_counts.items():
            if count > 1 and folder_id in stats:
                stats[folder_id]["recursive_size"] -= (count - 1) * current_size

    for row in rows:
        identity = (int(row["identity_device"]), int(row["identity_inode"]))
        if identity != current_identity:
            apply_current_group()
            current_identity = identity
            current_size = int(row["size_bytes"] or 0)
            ancestor_counts = {}

        current = int(row["folder_id"])
        visited: set[int] = set()
        while current in stats and current not in visited:
            visited.add(current)
            ancestor_counts[current] = ancestor_counts.get(current, 0) + 1
            parent = parent_by_id.get(current)
            if parent is None:
                break
            current = parent

    apply_current_group()
