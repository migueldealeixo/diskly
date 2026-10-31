from dataclasses import dataclass
from heapq import heappush, heapreplace
from pathlib import Path
from typing import Self


@dataclass(slots=True)
class _RankedItem[T]:
    size: int
    path: str
    value: T

    def __lt__(self, other: Self) -> bool:
        if self.size != other.size:
            return self.size < other.size
        return self.path > other.path


class BoundedRanking[T]:
    def __init__(self, limit: int) -> None:
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("Ranking limit must be a non-negative integer")
        self.limit = limit
        self._items: list[_RankedItem[T]] = []

    def offer(self, value: T, *, size: int, path: Path) -> None:
        if self.limit == 0:
            return
        candidate = _RankedItem(size, str(path), value)
        if len(self._items) < self.limit:
            heappush(self._items, candidate)
        elif self._items[0] < candidate:
            heapreplace(self._items, candidate)

    def values(self) -> tuple[T, ...]:
        return tuple(
            item.value for item in sorted(self._items, key=lambda item: (-item.size, item.path))
        )
