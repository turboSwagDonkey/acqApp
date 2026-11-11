"""What the window's parts share: the reserved mode name, the Modules
tooltip, and module order."""
from __future__ import annotations

from acqApp import config


# "None" leaves everything as set; other modes come from modes.json.
MODE_NONE = "None"

MODULES_TIP = ("Load or unload instruments without restarting the app, and "
                "see whether each one's hardware is detected")


def module_rank(key: str) -> int:
    """Position in config.MODULES; unknown keys sort last."""
    keys = list(config.MODULES)
    return keys.index(key) if key in config.MODULES else len(keys)
