"""A small dialog for choosing a saved ROI set: `roi_store`'s Qt front end.

Shared by the ROI editor's Load button and a routine step's Pattern picker —
both need the same "this session's sets first, older ones through Browse"
shape, so it lives once rather than being copied into each caller. That shape
itself is `widgets.SessionPicker`, shared in turn with the stage's FOV picker.
"""
from __future__ import annotations

from acqApp.devices.dmd import roi_store
from acqApp.widgets import SessionPicker


class RoiSetPicker(SessionPicker):
    """Modal picker. `.path` is the chosen file after `exec()`, else `None`."""

    def __init__(self, parent=None):
        super().__init__(
            parent, roi_store,
            title="Choose an ROI set",
            empty="(none saved this session)",
            browse_caption="Older ROI set",
            browse_filter="ROI sets (*.roi.json);;All files (*)",
            browse_tip="Sets from earlier runs of the app live here, not in "
                       "the quick list above, so a long history never slows "
                       "finding today's set.")
