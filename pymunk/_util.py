import weakref
from contextlib import ExitStack, contextmanager, nullcontext
from typing import TYPE_CHECKING, Any, Iterator, Optional

if TYPE_CHECKING:
    from .space import Space

_dead_ref: weakref.ref[Any] = weakref.ref(set())


def _space_lock(space: Optional["Space"]):
    if space is None:
        return nullcontext()
    return space._lock


@contextmanager
def _locked_spaces(*spaces: Optional["Space"]) -> Iterator[None]:
    unique_spaces = []
    seen = set()
    for space in spaces:
        if space is None:
            continue
        key = id(space)
        if key in seen:
            continue
        seen.add(key)
        unique_spaces.append(space)

    with ExitStack() as stack:
        for space in sorted(unique_spaces, key=id):
            stack.enter_context(space._lock)
        yield
