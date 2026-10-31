import gc
from pathlib import Path
from weakref import WeakSet

from diskly.scanner.ranking import BoundedRanking


class _Candidate:
    pass


def test_ranking_does_not_retain_discarded_candidates() -> None:
    limit = 17
    ranking = BoundedRanking[_Candidate](limit)
    live: WeakSet[_Candidate] = WeakSet()
    for number in range(2000):
        candidate = _Candidate()
        live.add(candidate)
        ranking.offer(candidate, size=number, path=Path(str(number)))
    del candidate
    gc.collect()

    assert len(ranking.values()) == limit
    assert len(live) == limit


def test_ranking_does_not_compare_values_when_keys_are_equal() -> None:
    ranking = BoundedRanking[object](3)
    for _ in range(10):
        ranking.offer(object(), size=10, path=Path("same"))

    assert len(ranking.values()) == 3
