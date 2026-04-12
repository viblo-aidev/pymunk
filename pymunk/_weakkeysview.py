from collections.abc import Callable, Iterator, KeysView, Mapping
from contextlib import nullcontext
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

# Can be simplified in Python 3.12, PEP 695
KT = TypeVar("KT")
VT = TypeVar("VT")


class SynchronizedKeysView(KeysView[KT]):
    def __init__(
        self,
        mapping: Mapping[KT, VT],
        lock_getter: Callable[[], Any] = nullcontext,
    ) -> None:
        self._mapping = mapping
        self._lock_getter = lock_getter

    def _lock(self) -> Any:
        return self._lock_getter()

    def __iter__(self) -> Iterator[KT]:
        with self._lock():
            return iter(tuple(self._mapping.keys()))

    def __len__(self) -> int:
        with self._lock():
            return len(self._mapping)

    def __contains__(self, key: object) -> bool:
        with self._lock():
            return key in self._mapping

    def __repr__(self) -> str:
        with self._lock():
            return f"{self.__class__.__name__}({list(self._mapping.keys())})"


class WeakKeysView(SynchronizedKeysView[KT]):
    def __init__(
        self,
        weak_dict: WeakKeyDictionary[KT, VT],
        lock_getter: Callable[[], Any] = nullcontext,
    ) -> None:
        super().__init__(weak_dict, lock_getter)
        self._lockless = lock_getter is nullcontext

    def __iter__(self) -> Iterator[KT]:
        if self._lockless:
            return iter(self._mapping.keys())

        with self._lock():
            return iter(tuple(self._mapping.keys()))
