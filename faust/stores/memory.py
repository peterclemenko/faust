"""In-memory table storage."""
from typing import Any, Callable, Iterable, MutableMapping, Optional
from mode.utils.collections import FastUserDict
from . import base
from ..types import EventT, TP


class Store(base.Store, FastUserDict):
    """Table storage using an in-memory dictionary."""

    def on_init(self) -> None:
        self.data: MutableMapping = {}

    def _clear(self) -> None:
        self.data.clear()

    def apply_changelog_batch(self, batch: Iterable[EventT],
                              to_key: Callable[[Any], Any],
                              to_value: Callable[[Any], Any]) -> None:
        # default store does not do serialization, so we need
        # to convert these raw json serialized keys to proper structures
        # (E.g. regenerate tuples in WindowedKeys etc).
        self.data.update((
            (to_key(event.key), to_value(event.value))
            for event in batch
        ))

    def persisted_offset(self, tp: TP) -> Optional[int]:
        return None

    def reset_state(self) -> None:
        ...
