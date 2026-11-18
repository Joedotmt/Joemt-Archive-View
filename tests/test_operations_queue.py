from __future__ import annotations

import pytest

from jvvv.operations_queue import Operation, OperationsQueue


def test_queue_persists_order_and_requires_apply_after_interruption(tmp_path):
    queue = OperationsQueue(tmp_path, "catalogue")
    first = queue.add("scan", {"uuid": "drive-a"}, "Drive A")
    dependent = queue.add("archive", {"uuid": "drive-a"}, "Drive A", source="Project A", requires=first.id)
    independent = queue.add("scan", {"uuid": "drive-b"}, "Drive B")

    with pytest.raises(ValueError, match="required scan"):
        queue.move(dependent.id, -1)
    with pytest.raises(ValueError, match="dependent archive"):
        queue.remove(first.id)
    queue.move(independent.id, -1)
    queue.apply()
    queue.set_state(first.id, "running")

    recovered = OperationsQueue(tmp_path, "catalogue")
    assert [job.id for job in recovered.jobs] == [first.id, independent.id, dependent.id]
    assert recovered.get(first.id).state == "paused"
    ready = lambda _: ("ready", "")
    assert recovered.next_ready(ready).id == independent.id
    recovered.set_state(independent.id, "completed")
    assert recovered.next_ready(ready) is None
    recovered.apply()
    assert recovered.next_ready(ready).id == first.id
    recovered.set_state(first.id, "warning")
    assert recovered.next_ready(ready).id == dependent.id


def test_pending_jobs_can_move_past_completed_history(tmp_path):
    queue = OperationsQueue(tmp_path, "catalogue")
    first = queue.add("scan", {"uuid": "drive-a"}, "Drive A")
    completed = queue.add("scan", {"uuid": "drive-b"}, "Drive B")
    last = queue.add("scan", {"uuid": "drive-c"}, "Drive C")
    queue.set_state(completed.id, "completed", "Completed earlier")
    queue.move(last.id, -1)
    assert [job.id for job in queue.jobs] == [last.id, completed.id, first.id]
    queue.apply()
    assert queue.get(completed.id).state == "completed"
    assert queue.get(completed.id).detail == "Completed earlier"
    assert queue.apply_count(active=True) == 0
    queue.set_state(last.id, "running")
    queue.move(first.id, -1)
    queue.move(last.id, 1)
    assert [job.id for job in queue.jobs] == [last.id, completed.id, first.id]


def test_queue_manager_sorts_by_status_and_edits_the_right_clicked_row(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMenu, QPushButton

    from jvvv import operations_ui

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = operations_ui.OperationsDialog()
    states = ("queued", "ready", "running", "waiting", "relink", "paused", "failed", "completed", "warning")
    jobs = [
        Operation(state, "scan", state, state, state=state, detail=f"Details for {state}")
        for state in states
    ]
    seen = []
    invoked = []
    choice = "Move Up"

    class TestMenu(QMenu):
        def exec(self, _point):
            actions = {action.text(): action for action in self.actions()}
            seen.append(set(actions))
            actions[choice].trigger()

    monkeypatch.setattr(operations_ui, "QMenu", TestMenu)
    dialog.move_requested.connect(lambda job_id, offset: invoked.append(("move", job_id, offset)))
    dialog.retry_requested.connect(lambda job_id: invoked.append(("retry", job_id)))
    dialog.preview_failures_requested.connect(lambda: invoked.append(("preview",)))

    try:
        dialog.refresh(jobs, active=True, preview_failure_job_id="warning")
        dialog.show()
        app.processEvents()
        assert list(dialog.tables) == ["Pending", "Failed", "Completed"]
        assert [table.rowCount() for table in dialog.tables.values()] == [6, 1, 2]
        assert {button.text() for button in dialog.findChildren(QPushButton)} == {"Apply", "Pause Queue"}

        pending = dialog.tables["Pending"]
        assert [pending.item(row, 3).text() for row in range(pending.rowCount())] == [
            "Queued", "Ready", "Running", "Waiting for Drive", "Relink Required", "Paused",
        ]
        pending.selectRow(0)
        pending.customContextMenuRequested.emit(pending.visualItemRect(pending.item(1, 0)).center())
        assert seen[-1] == {"Move Up", "Remove"}
        assert invoked[-1] == ("move", "ready", -1)

        failed = dialog.tables["Failed"]
        choice = "Retry"
        dialog.tabs.setCurrentWidget(failed)
        failed.customContextMenuRequested.emit(failed.visualItemRect(failed.item(0, 0)).center())
        assert seen[-1] == {"Retry", "Remove"}
        assert invoked[-1] == ("retry", "failed")
        assert dialog.detail.text() == "Details for failed"

        completed = dialog.tables["Completed"]
        choice = "Preview Failures"
        dialog.tabs.setCurrentWidget(completed)
        completed.customContextMenuRequested.emit(completed.visualItemRect(completed.item(1, 0)).center())
        assert seen[-1] == {"Preview Failures", "Remove"}
        assert invoked[-1] == ("preview",)
    finally:
        dialog.close()
