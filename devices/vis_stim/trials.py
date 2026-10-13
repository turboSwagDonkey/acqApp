"""Loop variables -> a full-factorial list of StimParams, and the shuffled
presentation order the region types use.

Port of logicLibHelpers.genParamCombos. Order differs from ndgrid's
column-major flattening; the set of combinations is identical.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import replace

from .settings import LoopVar, StimParams


def gen_param_combos(base: StimParams,
                      loops: dict[str, LoopVar]) -> list[StimParams]:
    """Every combination of the loop values over `base`. Names that aren't
    StimParams fields are skipped."""
    names = [n for n, lv in loops.items()
             if lv.values and n in StimParams.__dataclass_fields__]
    if not names:
        return [base]
    value_lists = [loops[n].values for n in names]
    return [replace(base, **dict(zip(names, combo)))
            for combo in itertools.product(*value_lists)]


def shuffled_blocks(n_items: int, n_blocks: int,
                    rng: random.Random | None = None) -> list[list[int]]:
    """One random order of range(n_items) per block (repeat). A block never
    opens on the item the previous one closed on (no back-to-back repeat)."""
    rng = rng or random.Random()
    blocks: list[list[int]] = []
    for _ in range(max(1, n_blocks)):
        order = list(range(n_items))
        rng.shuffle(order)
        if blocks and n_items > 1 and order[0] == blocks[-1][-1]:
            k = rng.randrange(1, n_items)
            order[0], order[k] = order[k], order[0]
        blocks.append(order)
    return blocks
