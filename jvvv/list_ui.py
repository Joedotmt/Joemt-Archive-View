"""Shared interaction and presentation for catalogue lists."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QAbstractItemView, QTableView, QTableWidget, QTableWidgetItem, QTreeWidget


class _ListInteraction:
    def _configure_list(self, selection_mode: QAbstractItemView.SelectionMode) -> None:
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(selection_mode)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def updateGeometries(self) -> None:
        super().updateGeometries()
        if self.model() is not None and self.model().rowCount():
            scroll_bar = self.verticalScrollBar()
            scroll_bar.setMaximum(scroll_bar.maximum() + self.viewport().height() // 2)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.indexAt(event.position().toPoint()).isValid()
        ):
            selection = self.selectionModel()
            if selection is not None:
                # Selection-change handlers should see that there is no current row.
                selection.clearCurrentIndex()
                selection.clearSelection()
            event.accept()
            return
        super().mousePressEvent(event)


def _configure_table(table: QTableView) -> None:
    table.setShowGrid(False)
    table.verticalHeader().hide()


class ListTableView(_ListInteraction, QTableView):
    def __init__(self) -> None:
        super().__init__()
        self._configure_list(QAbstractItemView.SelectionMode.SingleSelection)
        _configure_table(self)


class ListTableWidget(_ListInteraction, QTableWidget):
    def __init__(
        self,
        headers: Sequence[str],
        *,
        selection_mode: QAbstractItemView.SelectionMode = QAbstractItemView.SelectionMode.SingleSelection,
    ) -> None:
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self._configure_list(selection_mode)
        _configure_table(self)
        self.horizontalHeader().setStretchLastSection(True)

    def append_text_row(
        self, values: Sequence[str], *, user_data: object = None, tooltip: str | None = None,
    ) -> None:
        row = self.rowCount()
        self.insertRow(row)
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if user_data is not None:
                item.setData(Qt.ItemDataRole.UserRole, user_data)
            if tooltip is not None:
                item.setToolTip(tooltip)
            self.setItem(row, column, item)


class ListTreeWidget(_ListInteraction, QTreeWidget):
    def __init__(self) -> None:
        super().__init__()
        self._configure_list(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHeaderHidden(True)

    def scrollTo(
        self,
        index: QModelIndex,
        hint: QAbstractItemView.ScrollHint = QAbstractItemView.ScrollHint.EnsureVisible,
    ) -> None:
        scrollbar = self.verticalScrollBar()
        position = scrollbar.value()
        rect = self.visualRect(index)
        keep_position = (
            hint == QAbstractItemView.ScrollHint.EnsureVisible
            and rect.isValid()
            and rect.top() >= 0
            and rect.bottom() < self.viewport().height()
        )
        super().scrollTo(index, hint)
        if not keep_position:
            position = scrollbar.value()
        self._restore_scroll_space(position)

    def dataChanged(
        self, top_left: QModelIndex, bottom_right: QModelIndex, roles: Sequence[int] = (),
    ) -> None:
        position = self.verticalScrollBar().value()
        super().dataChanged(top_left, bottom_right, roles)
        self._restore_scroll_space(position)

    def _restore_scroll_space(self, position: int) -> None:
        # QTreeView can recalculate its range during navigation and after row
        # height changes without going through the shared geometry hook.
        self.updateGeometries()
        self.verticalScrollBar().setValue(position)
