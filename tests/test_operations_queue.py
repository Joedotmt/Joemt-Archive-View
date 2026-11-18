from __future__ import annotations

import pytest

from jvvv.operations_queue import OperationsQueue


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
