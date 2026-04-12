import weakref
from contextlib import ExitStack, contextmanager, nullcontext
from typing import TYPE_CHECKING, Any, Iterator, Optional

from ._chipmunk_cffi import ffi, lib

if TYPE_CHECKING:
    from .space import Space

_dead_ref: weakref.ref[Any] = weakref.ref(set())


def _space_lock(space: Optional["Space"]):
    if space is None:
        return nullcontext()
    return space._lock


def _lock_from_cp_space(cp_space: Any):
    if cp_space == ffi.NULL:
        return nullcontext()

    user_data = lib.cpSpaceGetUserData(cp_space)
    if user_data == ffi.NULL:
        return nullcontext()

    return ffi.from_handle(user_data)


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
