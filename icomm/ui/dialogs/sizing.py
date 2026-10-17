"""Screen-aware sizing helpers for tool and settings windows."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLayout, QScrollArea, QWidget


def fit_to_available_screen(widget: QWidget) -> None:
    """Keep a dialog/window resizable and within the available screen geometry."""
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    max_width = max(1, available.width() - 24)
    max_height = max(1, available.height() - 48)

    layout = widget.layout()
    if layout is not None:
        layout.setSizeConstraint(QLayout.SetNoConstraint)

    widget.setMaximumSize(
        min(widget.maximumWidth(), max_width),
        min(widget.maximumHeight(), max_height),
    )
    widget.setMinimumSize(
        min(widget.minimumWidth(), max_width),
        min(widget.minimumHeight(), max_height),
    )

    hint = widget.sizeHint()
    width = min(max(widget.width(), hint.width()), max_width)
    height = min(max(widget.height(), hint.height()), max_height)
    widget.resize(width, height)
    frame = widget.frameGeometry()
    if not available.intersects(frame):
        widget.move(
            available.left() + max(0, (available.width() - width) // 2),
            available.top() + max(0, (available.height() - height) // 2),
        )


def scrollable_page(page: QWidget) -> QScrollArea:
    """Wrap long dialog pages so their controls remain reachable on small screens."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    page.setMinimumSize(0, 0)
    area.setWidget(page)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    return area
