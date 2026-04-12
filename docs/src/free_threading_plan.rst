Free-Threading Implementation Plan
==================================

Goal
----

Add safe support for Python 3.14 free-threaded builds to Pymunk without a large
callback architecture redesign.

This plan assumes:

- Pymunk should build and run on ``cp314t``.
- Importing Pymunk on a free-threaded interpreter must not rely on the runtime
  re-enabling the GIL.
- Concurrent access must be made safe for existing APIs.
- The implementation should preserve the current callback timing and general API
  shape.
- The implementation should not attempt a larger redesign where callbacks are
  deferred out of ``Space.step()``.

Non-goals
---------

- Reworking Chipmunk callback execution order.
- Replacing Python callbacks with a queued event system.
- Making every object independently lock-free.
- Increasing same-space parallelism beyond what Chipmunk already provides.

Summary of the proposed model
-----------------------------

The recommended implementation is a per-``Space`` synchronization model with
explicit serialization of access to a given space, while still allowing
different spaces to be used concurrently from different Python threads.

The core rule should be:

- Only one thread may execute inside a given ``Space`` at a time.

This matches the practical model already implied by Chipmunk and by Pymunk's
 current Python-side mutable state. It avoids a large redesign while still
making free-threaded execution safe.

Under this model:

- Two different ``Space`` instances may be stepped or queried concurrently.
- One thread may not call ``step()``, query APIs, or mutating APIs on the same
  ``Space`` while another thread is doing so.
- Callbacks invoked during ``step()`` run while the same space lock is held and
  must be able to re-enter safe APIs on that same ``Space``.

Why this model
--------------

Pymunk currently relies on the GIL for safety around mutable Python state such
as:

- ``Space._handlers``
- ``Space._post_step_callbacks``
- ``Space._shapes`` / ``_bodies`` / ``_constraints``
- ``Space._add_later`` / ``_remove_later``
- ``Space._locked``
- object weakref links and ownership maps on ``Body``, ``Shape``, and
  ``Constraint``

In a free-threaded build, these become shared mutable structures accessed from
multiple threads without implicit serialization.

Chipmunk's internal threaded solver does not remove this problem. It helps with
solver work inside one step, but it does not make Pymunk's Python object graph
safe for concurrent access.

The safest minimal architecture is therefore to:

1. Serialize all access to each ``Space``.
2. Route space-owned object mutation through that space's lock.
3. Keep callback semantics unchanged.

Concurrency contract to document
--------------------------------

The user-facing threading contract should be documented explicitly.

Proposed contract:

- Pymunk supports Python 3.14 free-threaded builds.
- Operations on different ``Space`` objects may run concurrently.
- Operations involving the same ``Space`` are serialized internally.
- A callback invoked by a ``Space`` may safely call back into that same space
  through supported public APIs.
- Sharing ``Body``, ``Shape``, or ``Constraint`` objects across threads is only
  supported to the extent that access is synchronized through their owning
  ``Space``.
- Collection properties such as ``Space.shapes``, ``Space.bodies``,
  ``Space.constraints``, ``Body.shapes``, and ``Body.constraints`` keep their
  current live-view API shape rather than changing to snapshot-returning
  methods.
- APIs that operate on more than one object graph, such as
  ``Shape.shapes_collide(other)``, remain supported even when the two objects
  are attached to different spaces.
- Objects not attached to a ``Space`` are not guaranteed to be safe for
  arbitrary concurrent mutation from multiple threads.

This keeps the supported behavior precise and achievable.

Implementation strategy
-----------------------

Phase 1: Build and packaging baseline
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Ensure Pymunk builds correctly for free-threaded CPython.
- Ensure the generated CFFI extension is built with a CFFI version that
  supports free-threaded Python.

Changes:

- Raise the minimum supported CFFI version to ``>=2.0.0``.
- Verify ``cibuildwheel`` configuration includes and tests ``cp314t`` builds.
- Verify wheel naming and artifacts for free-threaded builds on supported
  platforms.
- Add CI jobs that run the test suite under both regular 3.14 and free-threaded
  3.14.

Files likely touched:

- ``pyproject.toml``
- CI workflow files

Exit criteria:

- Pymunk builds on ``cp314t``.
- Importing Pymunk under ``cp314t`` does not warn about re-enabling the GIL.

Phase 2: Introduce explicit per-space locking
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Add a lock that protects all mutable state owned by a ``Space``.
- Ensure re-entrant callback behavior remains valid.

Design:

- Add a per-``Space`` re-entrant lock, likely ``threading.RLock``.
- Treat the space lock as protecting both:

  - Python-side state owned by the space
  - calls into Chipmunk that operate on that space

- The lock must be re-entrant because collision callbacks, body callbacks,
  constraint callbacks, and post-step callbacks may re-enter space methods.

Suggested ``Space`` invariant:

- If code reads or writes space-owned Python state or calls a Chipmunk function
  on ``self._space``, it must hold ``self._lock``.

Initial lock-protected members:

- ``_handlers``
- ``_post_step_callbacks``
- ``_removed_shapes``
- ``_shapes``
- ``_bodies``
- ``_constraints``
- ``_static_body`` initialization
- ``_add_later``
- ``_remove_later``
- ``_bodies_to_check``
- ``_locked``
- lifetime-sensitive teardown in ``spacefree``

Files likely touched:

- ``pymunk/space.py``

Key methods to wrap under the space lock:

- ``__init__`` final setup for lock-owned fields
- ``add``
- ``remove``
- ``_remove``
- ``_add_shape``
- ``_add_body``
- ``_add_constraint``
- ``_remove_shape``
- ``_remove_body``
- ``_remove_constraint``
- ``reindex_shape``
- ``reindex_shapes_for_body``
- ``reindex_static``
- ``use_spatial_hash``
- ``step``
- ``on_collision``
- ``add_post_step_callback``
- all query methods that operate on the space
- stateful copy/pickle helpers that iterate handlers, bodies, shapes, or
  constraints

Important detail:

- ``step()`` should hold the space lock across the full step and the immediate
  callback-driven follow-up work that mutates space-owned state. This preserves
  consistency between Chipmunk and Python bookkeeping.

Live views without public API changes:

- ``Space.shapes``, ``Space.bodies``, and ``Space.constraints`` currently
  return live ``KeysView`` objects backed directly by mutable dictionaries.
- ``Body.shapes`` and ``Body.constraints`` do the same, with
  ``Body.constraints`` using a custom ``WeakKeysView``.
- Returning those raw views is not safe under free-threading because iteration,
  membership checks, and length checks can race with concurrent mutation after
  the property getter has returned.
- Do not change these APIs to return plain snapshots. That would change the
  documented and tested behavior that an already-held view object reflects later
  additions and removals.
- Instead, replace the raw exposed views with small synchronized live-view
  wrapper types.

Required behavior for synchronized live views:

- Preserve the existing public property names and general usage pattern.
- Preserve live-view behavior for repeated ``len(view)``, ``x in view``,
  ``repr(view)``, and fresh iteration started from the same held view object.
- Each operation on the view should acquire the owning lock before touching the
  underlying mapping.
- ``__iter__`` should acquire the owning lock, snapshot the current keys into a
  tuple/list, and return an iterator over that snapshot.
- This means the view object remains live across calls, while each individual
  iterator is a point-in-time snapshot. That is the safest way to keep the
  existing API shape without exposing a mutable mapping view across threads.
- For ``Body`` views, if the body is attached to a ``Space``, use that space's
  lock. If unattached, access can proceed without a space lock.

Cross-space operations and lock ordering:

- Most operations should take at most one ``Space`` lock.
- A small number of APIs naturally involve more than one ownership graph and
  must remain supported without changing the public API.
- The clearest example is ``Shape.shapes_collide(other)``, which currently
  works independently of whether either shape is attached to a space.
- Any API that can touch two attached objects from different spaces must use a
  stable two-space lock ordering.

Required rule for multi-space locking:

- Resolve the set of distinct owning spaces involved in the operation.
- If no owning space exists, execute directly.
- If exactly one owning space exists, lock only that space.
- If two owning spaces exist, acquire both locks in a stable global order.
- The ordering key should be simple and process-local, such as ``id(space)``.
- Never acquire multiple space locks in call-site-specific or data-dependent
  order.
- Do not introduce a general need for three-or-more-space locking. If such a
  path appears, stop and reassess the design.

Initial APIs to treat as multi-space candidates:

- ``Shape.shapes_collide(other)``
- ``Shape.body`` setter when moving a shape between bodies associated with
  different spaces
- any future helper that inspects or mutates relationships spanning two
  attached objects with different owning spaces

Files likely touched:

- ``pymunk/space.py``
- ``pymunk/body.py``
- ``pymunk/_weakkeysview.py``
- possibly a tiny internal shared helper module for synchronized view wrappers
- possibly a tiny shared helper for ordered acquisition of one or two owning
  space locks

Exit criteria:

- No public ``Space`` operation can race with another operation on the same
  space.
- Re-entrant calls from callbacks on the same thread do not deadlock.
- Collection view properties remain live at the API level while no longer
  exposing unsynchronized raw dict views.

Phase 3: Route attached object mutation through the owning space
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Ensure ``Body``, ``Shape``, and ``Constraint`` methods that touch attached
  objects are synchronized with their owning ``Space``.

Problem:

- Many mutating methods live on objects other than ``Space``.
- Once attached, those methods still affect space-owned Chipmunk state and
  Python bookkeeping.
- Under free-threading, calling such methods concurrently with ``Space.step()``
  or queries would otherwise race.

Design:

- Add a small internal helper to detect whether an object is attached to a
  ``Space`` and, if so, execute the operation under that space's lock.
- If unattached, perform the operation without a space lock.
- Prefer a minimal shared helper over duplicating lock-selection logic in every
  setter.

Suggested helper behavior:

- For ``Body``, ``Shape``, and ``Constraint``, resolve the owning space if one
  exists.
- If there is an owning space, enter ``space._lock``.
- Otherwise, execute directly.

Extended helper rule for cross-space operations:

- For methods that can involve two attached objects, resolve both owning
  spaces.
- If both objects belong to the same space, acquire that lock once.
- If they belong to different spaces, acquire both in the global lock order
  defined above.
- If one object is unattached, only the attached object's space lock is needed.

Files likely touched:

- ``pymunk/body.py``
- ``pymunk/shapes.py``
- ``pymunk/constraints.py``
- possibly a small helper in ``pymunk/_util.py``

Methods to audit first in ``Body``:

- position/velocity/force/angle setters
- activation and sleep-related methods
- custom velocity/position callback setters
- methods that iterate or mutate attached shapes/constraints

Methods to audit first in ``Shape``:

- ``body`` setter
- filter / collision / friction / elasticity / sensor / surface velocity setters
- shape update and cache methods that touch Chipmunk state

Methods to audit first in ``Constraint``:

- pre/post solve callback setters
- spring/torque callback setters
- core property setters that update Chipmunk constraint state

Important boundary:

- For objects attached to a space, the space lock is the source of truth.
- Do not introduce separate per-object locks for attached objects unless a clear
  need is found, as that increases deadlock risk and complexity.
- Multi-space operations are the exception, not the baseline. Keep them explicit
  and limited to APIs that already span multiple ownership graphs.

Exit criteria:

- Mutating an attached body, shape, or constraint cannot race with operations on
  its owning space.

Phase 4: Make callback paths lock-aware and documented
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Ensure Python callbacks behave correctly under the new locking model.
- Preserve existing semantics.

Observations:

- Pymunk uses many ``ffi.def_extern()`` callbacks.
- These callbacks commonly look up Python objects via ``ffi.from_handle`` and
  then mutate Python-side state.
- Current callback behavior relies on same-thread re-entry and implicit GIL
  serialization.

Plan:

- Keep callback execution inside the space lock established by the public entry
  point that triggered the callback.
- Do not add extra callback-level locking unless a callback can be invoked
  outside an already-locked path.
- Audit callbacks that may run during object finalization or teardown, where the
  lock may need to be acquired explicitly.

Files to audit:

- ``pymunk/_callbacks.py``
- ``pymunk/_collision_handler.py``
- teardown helpers inside ``space.py``, ``body.py``, ``shapes.py``, and
  ``constraints.py``

Specific concerns to verify:

- collision callbacks calling ``Space.add`` or ``Space.remove``
- separate callbacks that temporarily manipulate ``space._locked``
- query callbacks that append into Python lists during active queries
- debug draw callbacks that resolve Python shapes and options
- body and constraint callback setters that expose Python functions to Chipmunk

Required rule:

- No callback path should acquire locks in an order that can invert against a
  normal public API path.

Exit criteria:

- Existing callback tests pass unchanged.
- Re-entrant callback behavior remains correct on free-threaded Python.

Phase 5: Teardown, finalizers, and GC safety
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Make destruction and finalization safe when reference drops may happen on any
  Python thread.

Problem:

- ``ffi.gc`` finalizers currently perform Chipmunk removal and Python-side state
  changes with no explicit synchronization.
- In a free-threaded runtime, finalizer timing and thread choice become more
  important.

Plan:

- Audit ``ffi.gc`` destructors in:

  - ``Space``
  - ``Body``
  - ``Shape``
  - ``Constraint``

- Ensure that if a finalizer operates on an attached object, it uses the owning
  space lock before mutating space-owned state or calling into the space.
- Avoid lock-order cycles between finalizers of nested objects.
- Keep finalizer work minimal and defensive, especially during interpreter
  shutdown.

Open implementation question:

- If some finalizer paths prove too fragile to synchronize safely, convert those
  paths to a more defensive best-effort cleanup that avoids touching Python-side
  bookkeeping late in interpreter shutdown. This should only be done where
  necessary and documented clearly.

Exit criteria:

- Attached object destruction cannot race with active work on the same space.
- Existing shutdown and GC behavior remains stable.

Phase 6: Tests for correctness under free-threading
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Add tests that prove safety properties, not just importability.

Test categories:

1. Build and import

- import on regular 3.14
- import on free-threaded 3.14
- wheel smoke tests for supported platforms

2. Same-space serialization

- two threads repeatedly calling ``space.step()`` on the same space
- ``space.step()`` racing with ``space.add()`` / ``space.remove()``
- ``space.step()`` racing with point/segment/bb/shape queries
- ``space.step()`` racing with collision handler registration
- ``space.step()`` racing with post-step callback registration

3. Cross-space concurrency

- two spaces stepped concurrently by different threads
- one space queried while another is stepped
- per-space callbacks active in both threads

4. Attached object mutation

- mutate ``Body.position`` while owning space is stepping
- mutate ``Shape.filter`` / ``collision_type`` while owning space is stepping
- mutate constraint properties while owning space is stepping

5. Callback re-entry

- collision callback calls ``space.add``
- collision callback calls ``space.remove``
- callback reads and mutates attached objects
- body custom velocity/position callback touches owning space or related objects

6. Destruction and lifetime

- remove objects from another thread while a space is active
- let attached objects be GC'd while concurrent operations run
- repeat create/step/destroy loops under thread pressure

7. ``threaded=True`` interaction

- free-threaded Python with ``Space(threaded=True)`` and ``threads=2``
- verify correctness with collision callbacks and custom body/constraint
  callbacks enabled

Test style recommendations:

- Prefer deterministic barriers and events over sleep-based races.
- Add a stress test mode that loops many times to widen race windows.
- Mark the heaviest tests separately if runtime becomes significant.

Likely files:

- ``pymunk/tests/test_space.py``
- ``pymunk/tests/test_body.py``
- ``pymunk/tests/test_shape.py``
- ``pymunk/tests/test_constraint.py``
- possibly a new ``pymunk/tests/test_free_threading.py``

Exit criteria:

- New concurrency tests pass on free-threaded Python.
- No intermittent crashes, deadlocks, or bookkeeping corruption are observed in
  repeated CI runs.

Phase 7: Documentation and supported guarantees
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Objectives:

- Document what is now safe and what is intentionally serialized.

Documentation additions:

- mention free-threaded Python support in installation/release notes
- document the per-space serialization model
- document that different spaces may run concurrently
- document that attached object mutation is synchronized through the owning
  space
- document any remaining unsupported cases explicitly

Suggested user-facing wording:

- Pymunk supports Python 3.14 free-threaded builds.
- Thread safety is provided at the ``Space`` level.
- Work involving the same ``Space`` is serialized internally.
- Work involving different spaces may proceed concurrently.

Risks and mitigations
---------------------

Risk: deadlocks from nested locking
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Mitigation:

- Use one lock per space.
- Use re-entrant locking for callback re-entry.
- Avoid adding independent locks to attached objects.
- Define and document lock ordering for any exceptional cases.

Risk: missed object methods that mutate attached state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Mitigation:

- Do a method-by-method audit of ``Body``, ``Shape``, and ``Constraint``.
- Add regression tests that exercise representative setters while stepping.

Risk: fragile finalizer behavior
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Mitigation:

- Minimize finalizer work.
- Synchronize attached-object cleanup through the owning space lock.
- Add repeated lifecycle stress tests.

Risk: performance regressions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Mitigation:

- Keep lock scope at the space level instead of adding many fine-grained locks.
- Preserve cross-space concurrency.
- Benchmark common single-thread use cases before and after.

Recommended implementation order
--------------------------------

1. Raise the CFFI minimum and enable ``cp314t`` CI.
2. Add per-space ``RLock`` and protect all ``Space`` APIs.
3. Audit and update attached ``Body`` / ``Shape`` / ``Constraint`` mutation.
4. Audit callback paths and teardown/finalizers.
5. Add concurrency tests and stress tests.
6. Document the supported threading model.

Definition of done
------------------

The work should be considered complete when all of the following are true:

- Pymunk builds and imports on Python 3.14 free-threaded.
- Import does not cause CPython to re-enable the GIL.
- Concurrent operations on the same ``Space`` are safe because they are
  serialized internally.
- Concurrent operations on different spaces work correctly.
- Attached body/shape/constraint mutation is synchronized through the owning
  space.
- Existing callback semantics remain intact.
- New concurrency and lifecycle tests pass reliably in CI.

Recommendation
--------------

Proceed with the per-space locking design described above. It is the smallest
design that plausibly provides real free-threaded safety, preserves the public
API, and avoids the larger callback redesign.

Execution checklist
-------------------

This section is intended to be used directly while implementing the feature.
The items are grouped by file and split into:

- implement first: methods that should be changed in the first locking pass
- audit next: methods that must be checked after the first pass

Checklist: ``pymunk/space.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Additions and helpers:

- Add ``import threading``.
- Add ``self._lock = threading.RLock()`` during ``Space.__init__``.
- Add a small internal context helper if useful, but keep it minimal. Avoid a
  large abstraction layer if plain ``with self._lock:`` is clearer.

Implement first:

- ``__init__``
  - initialize the lock before shared mutable state is exposed
  - ensure finalizer closures can safely reference the lock strategy
- ``_setup_static_body``
- ``static_body``
- ``add``
- ``remove``
- ``_remove``
- ``_add_shape``
- ``_add_body``
- ``_add_constraint``
- ``_remove_shape``
- ``_remove_body``
- ``_remove_constraint``
- ``step``
  - hold the space lock for the full method
  - keep callback-driven follow-up work under the same lock
  - keep ``_locked`` as simulation-state bookkeeping, not as the thread-safety
    primitive
- ``on_collision``
- ``add_post_step_callback``
- ``point_query``
- ``point_query_nearest``
- ``segment_query``
- ``segment_query_first``
- ``bb_query``
- ``shape_query``
- ``debug_draw``
- ``_get_arbiters``
- ``__getstate__``
- ``__setstate__``

Audit next:

- property getters/setters that call Chipmunk on ``self._space``
  - ``iterations``
  - ``gravity``
  - ``damping``
  - ``idle_speed_threshold``
  - ``sleep_time_threshold``
  - ``collision_slop``
  - ``collision_bias``
  - ``collision_persistence``
  - ``current_time_step``
  - ``threads``
- space configuration helpers
  - ``reindex_shape``
  - ``reindex_shapes_for_body``
  - ``reindex_static``
  - ``use_spatial_hash``
- view-returning properties
  - ``shapes``
  - ``bodies``
  - ``constraints``
  - keep live-view semantics at the property level
  - do not return raw ``dict_keys`` views after free-threading changes
  - return synchronized wrapper views where each operation locks and each new
    iterator snapshots under lock

Special notes for ``Space``:

- ``spacefree`` inside ``__init__`` needs a careful audit. It iterates and
  removes shapes, constraints, and bodies. Make sure it does not race with
  normal operations on the same space.
- ``__getstate__`` and ``__setstate__`` touch handler dictionaries and object
  collections and must not observe partially-mutated state.
- The current ``_locked`` flag should remain about simulation mutation rules,
  not become the lock substitute.

Checklist: ``pymunk/body.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Additions and helpers:

- Add a small internal helper to run an operation under the owning space lock
  when ``self.space`` is not ``None``.
- Keep this helper local and minimal. The point is only to centralize the
  attach-to-space case.

Implement first:

- ``velocity_func`` setter
- ``position_func`` setter
- ``apply_force_at_world_point``
- ``apply_force_at_local_point``
- ``apply_impulse_at_world_point``
- ``apply_impulse_at_local_point``
- ``activate``
- ``sleep``
- ``sleep_with_group``
- ``body_type`` setter
- ``each_arbiter``

Implement early if attached-object races show up in tests:

- ``mass`` setter
- ``moment`` setter
- ``position`` setter
- ``center_of_gravity`` setter
- ``velocity`` setter
- ``force`` setter
- ``angle`` setter
- ``angular_velocity`` setter
- ``torque`` setter

Audit next:

- read-only getters that call Chipmunk on an attached body
  - these may still need the space lock for consistent reads during active step
- ``constraints`` and ``shapes`` view-returning properties
  - preserve live-view behavior for held views
  - replace raw ``dict`` / ``WeakKeyDictionary`` views with synchronized
    wrappers
- coordinate transform helpers
  - ``local_to_world``
  - ``world_to_local``
  - ``velocity_at_world_point``
  - ``velocity_at_local_point``
- ``__getstate__`` / ``__setstate__``

Special notes for ``Body``:

- The constructor finalizer ``freebody`` removes shapes and constraints from the
  current space. This path must be audited together with ``Space`` finalization.
- ``cpBodySetUserData`` and callback lookup paths depend on body lifetime.
  Recheck assumptions after locking is introduced.

Checklist: ``pymunk/shapes.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Additions and helpers:

- Add the same style of minimal helper used for ``Body`` so attached shapes run
  under the owning space lock.

Implement first:

- ``body`` setter
- ``sensor`` setter
- ``collision_type`` setter
- ``filter`` setter
- ``elasticity`` setter
- ``friction`` setter
- ``surface_velocity`` setter
- ``update``
- ``cache_bb``
- ``point_query``
- ``segment_query``
- ``shapes_collide``

Implement early for shape subclasses:

- ``Circle.unsafe_set_radius``
- ``Circle.unsafe_set_offset``
- ``Segment.unsafe_set_endpoints``
- ``Segment.unsafe_set_radius``
- ``Segment.set_neighbors``
- ``Poly.unsafe_set_radius``
- ``Poly.unsafe_set_vertices``

Audit next:

- ``mass`` setter
- ``density`` setter
- read-only getters that call Chipmunk on attached shapes
- ``bb`` property
- ``space`` property
- ``_hashid`` property access
- ``__getstate__``
- subclass constructors for any ordering assumptions around body linkage and
  user data

Special notes for ``Shape``:

- ``shapefree`` in ``_init`` calls ``cpSpaceRemoveShape`` if attached. This is
  a teardown race point and must be audited with the new lock rules.
- ``Shape._from_cp_shape`` resolves Python objects from Chipmunk user data.
  Keep object lifetime assumptions in mind when testing callback-heavy paths.
- ``shapes_collide`` should remain supported for unattached shapes, same-space
  shapes, and shapes attached to different spaces. Use the explicit multi-space
  lock ordering rule rather than restricting this API.

Checklist: ``pymunk/constraints.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Additions and helpers:

- Add a small helper to execute operations under the owning space lock, using
  the space from either constrained body if attached.

Implement first:

- base-class property setters
  - ``max_force``
  - ``error_bias``
  - ``max_bias``
  - ``collide_bodies``
- ``activate_bodies``
- ``pre_solve`` setter
- ``post_solve`` setter
- ``_set_bodies``
- ``__getstate__`` / ``__setstate__``

Implement next on subclasses:

- ``PinJoint``
  - ``anchor_a`` setter
  - ``anchor_b`` setter
  - ``distance`` setter
- ``SlideJoint``
  - ``anchor_a`` setter
  - ``anchor_b`` setter
  - ``min`` setter
  - ``max`` setter
- ``PivotJoint``
  - ``anchor_a`` setter
  - ``anchor_b`` setter
- ``GrooveJoint``
  - ``anchor_b`` setter
  - ``groove_a`` setter
  - ``groove_b`` setter
- ``DampedSpring``
  - ``anchor_a`` setter
  - ``anchor_b`` setter
  - ``rest_length`` setter
  - ``stiffness`` setter
  - ``damping`` setter
  - ``force_func`` setter
- ``DampedRotarySpring``
  - ``rest_angle`` setter
  - ``stiffness`` setter
  - ``damping`` setter
  - ``torque_func`` setter
- ``RotaryLimitJoint``
  - ``min`` setter
  - ``max`` setter
- ``RatchetJoint``
  - ``angle`` setter
  - ``phase`` setter
  - ``ratchet`` setter
- ``GearJoint``
  - ``phase`` setter
  - ``ratio`` setter
- ``SimpleMotor``
  - ``rate`` setter

Audit next:

- read-only getters that call Chipmunk on attached constraints
- subclass constructors and body-link setup order
- finalizer ``constraintfree`` in ``_init``

Special notes for ``Constraint``:

- Constraint callback setters are part of the callback surface and should be
  covered by callback re-entry tests early.

Checklist: ``pymunk/_callbacks.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Audit and verify first:

- collision callbacks
  - ``ext_cpCollisionBeginFunc``
  - ``ext_cpCollisionPreSolveFunc``
  - ``ext_cpCollisionPostSolveFunc``
  - ``ext_cpCollisionSeparateFunc``
- body callbacks
  - ``ext_cpBodyPositionFunc``
  - ``ext_cpBodyVelocityFunc``
  - ``ext_cpBodyArbiterIteratorFunc``
- constraint callbacks
  - ``ext_cpConstraintPreSolveFunc``
  - ``ext_cpConstraintPostSolveFunc``
  - ``ext_cpDampedSpringForceFunc``
  - ``ext_cpDampedRotarySpringTorqueFunc``
- query callbacks
  - point / segment / bb / shape query callbacks
- debug draw callbacks

What to check:

- confirm each callback runs under the expected owning space lock
- identify any callback that may run outside an already-locked public entry path
- confirm callbacks that append to Python containers do not escape the expected
  space lock discipline
- confirm ``ext_cpCollisionSeparateFunc`` still behaves correctly with the new
  thread-safety lock and existing ``space._locked`` bookkeeping

Checklist: ``pymunk/_collision_handler.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Audit:

- ``CollisionHandler.__init__`` user data lifetime assumptions
- callback setter paths for ``begin``, ``pre_solve``, ``post_solve``, and
  ``separate``
- any interaction with ``Space.on_collision`` while the space lock is held

Checklist: teardown and finalizer paths
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

These should be checked as a dedicated pass after the initial locking changes.

- ``Space.__init__`` nested ``spacefree``
- ``Body.__init__`` nested ``freebody``
- ``Shape._init`` nested ``shapefree``
- ``Constraint._init`` nested ``constraintfree``

For each finalizer:

- identify whether it can run on an attached object
- identify the owning space, if any
- ensure the finalizer either uses the owning space lock or has a documented
  shutdown-safe fallback path
- verify no lock-order inversion with normal API paths

Checklist: tests to add first
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Add these before broad cleanup so the implementation can iterate against them.

- same-space concurrent ``step`` vs ``step``
- same-space concurrent ``step`` vs ``add``
- same-space concurrent ``step`` vs ``remove``
- same-space concurrent ``step`` vs point query
- same-space concurrent ``step`` vs attached ``Body.position`` setter
- same-space concurrent ``step`` vs attached ``Shape.filter`` setter
- same-space concurrent ``step`` vs attached constraint property setter
- same-space callback re-entry calling ``space.add``
- same-space callback re-entry calling ``space.remove``
- cross-space concurrent stepping
- attached object destruction under thread pressure

Suggested implementation sequence inside the codebase
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. Change ``Space`` first and get same-space API serialization working.
2. Change ``Body`` / ``Shape`` / ``Constraint`` setters that obviously mutate
   attached state.
3. Add the first concurrency tests and get them green.
4. Audit callback paths and fix re-entry edge cases.
5. Audit finalizers and lifecycle races.
6. Expand tests to cross-space concurrency and teardown stress.

Suggested patch series
----------------------

The work should be split into a small patch series so each step stays
reviewable and bisectable.

Patch 1: Build and CI baseline
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- raise CFFI minimum to ``>=2.0.0``
- add or enable ``cp314t`` test jobs in CI
- add a minimal smoke test job that imports Pymunk on free-threaded Python

Files likely touched:

- ``pyproject.toml``
- CI workflow files

Why first:

- establishes the free-threaded build target before runtime changes begin
- makes regressions visible early

Recommended checks:

- build wheel / install
- ``python -c "import pymunk"`` on ``cp314t``

Patch 2: Space lock skeleton and first same-space serialization tests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- add ``Space._lock`` as ``threading.RLock``
- wrap the highest-risk ``Space`` methods under the lock
- add the first concurrency tests focused on same-space serialization

Methods to include immediately:

- ``add``
- ``remove``
- ``_remove``
- ``_add_shape``
- ``_add_body``
- ``_add_constraint``
- ``_remove_shape``
- ``_remove_body``
- ``_remove_constraint``
- ``step``
- ``point_query``
- ``segment_query``
- ``bb_query``
- ``shape_query``
- ``on_collision``
- ``add_post_step_callback``

Tests to include immediately:

- same-space ``step`` vs ``step``
- same-space ``step`` vs ``add``
- same-space ``step`` vs ``remove``
- same-space ``step`` vs query

Why second:

- ``Space`` is the central synchronization boundary
- most later changes depend on this lock existing

Patch 3: Finish ``Space`` audit and stabilize view/state operations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- wrap the remaining ``Space`` property getters/setters and helpers that call
  Chipmunk or inspect shared state
- preserve live-view behavior for ``shapes`` / ``bodies`` / ``constraints``
  using synchronized wrappers instead of raw mapping views
- audit ``__getstate__`` / ``__setstate__`` and ``debug_draw``

Methods to include:

- all remaining configuration properties
- ``reindex_shape``
- ``reindex_shapes_for_body``
- ``reindex_static``
- ``use_spatial_hash``
- ``debug_draw``
- ``_get_arbiters``
- ``__getstate__``
- ``__setstate__``

Why separate from Patch 2:

- keeps the initial lock introduction focused on correctness-critical paths
- avoids mixing API semantics changes into the first concurrency patch

Patch 4: Body synchronization through owning space
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- add the minimal helper for executing attached-body operations under the owning
  space lock
- wrap body mutators and key read paths
- add body-focused race tests

Methods to include first:

- ``velocity_func`` setter
- ``position_func`` setter
- ``apply_force_at_world_point``
- ``apply_force_at_local_point``
- ``apply_impulse_at_world_point``
- ``apply_impulse_at_local_point``
- ``activate``
- ``sleep``
- ``sleep_with_group``
- ``body_type`` setter
- direct state setters if tests show races

Tests to include:

- same-space ``step`` vs attached ``Body.position`` setter
- same-space ``step`` vs body force/impulse application
- callback path using custom body velocity/position functions

Why separate:

- attached object mutation is the next major race surface after ``Space``

Patch 5: Shape synchronization through owning space
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- add the minimal helper for attached-shape synchronization
- wrap key shape mutators and query methods
- add shape-focused race tests

Methods to include first:

- ``body`` setter
- ``sensor`` setter
- ``collision_type`` setter
- ``filter`` setter
- ``elasticity`` setter
- ``friction`` setter
- ``surface_velocity`` setter
- ``update``
- ``cache_bb``
- ``point_query``
- ``segment_query``
- ``shapes_collide``
- subclass unsafe mutators

Tests to include:

- same-space ``step`` vs attached ``Shape.filter`` setter
- same-space ``step`` vs attached shape query/caching
- shape mutation inside a callback

Patch 6: Constraint synchronization through owning space
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- add the minimal helper for attached-constraint synchronization
- wrap constraint property setters and callback setters
- add constraint-focused race tests

Methods to include first:

- base setters: ``max_force``, ``error_bias``, ``max_bias``,
  ``collide_bodies``
- callback setters: ``pre_solve``, ``post_solve``
- subclass property setters in the order listed above

Tests to include:

- same-space ``step`` vs constraint property setter
- same-space callback re-entry through constraint callbacks
- spring/torque callback setter coverage

Why separate from shapes:

- constraint callbacks are a distinct risk area and easier to reason about in a
  dedicated patch

Patch 7: Callback audit and re-entry hardening
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- audit ``_callbacks.py`` and ``_collision_handler.py`` with the new locking in
  place
- fix any paths that run outside the intended owning-space lock
- add callback re-entry tests

Tests to include:

- collision callback calling ``space.add``
- collision callback calling ``space.remove``
- callback mutating attached body/shape/constraint state
- ``threaded=True`` with callbacks enabled

Why after patches 4-6:

- callback safety depends on the underlying object locking already existing

Patch 8: Finalizers, teardown, and lifecycle stress
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- audit and update ``spacefree``, ``freebody``, ``shapefree``, and
  ``constraintfree``
- add lifecycle stress tests and repeated create/step/destroy loops
- validate no deadlocks or races during teardown

Tests to include:

- attached object destruction while another thread is active on the same space
- repeated create/step/destroy under thread pressure
- interpreter-shutdown-adjacent smoke coverage where practical

Why near the end:

- teardown logic depends on the final lock model already being in place

Patch 9: Documentation and supported guarantees
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scope:

- document supported free-threading behavior
- add release-note/changelog entries
- document the per-space serialization model and cross-space concurrency

Files likely touched:

- user docs under ``docs/src``
- changelog/news files

Acceptance gates for the series
-------------------------------

Each patch should meet a clear gate before the next one starts.

Patch 1 gate:

- ``cp314t`` build and import path is green in CI

Patch 2 gate:

- no deadlocks in same-space ``step``/``add``/``remove``/query tests

Patch 3 gate:

- no remaining unguarded ``Space`` methods that call Chipmunk on ``self._space``

Patch 4 gate:

- attached ``Body`` mutation races covered by tests are green

Patch 5 gate:

- attached ``Shape`` mutation races covered by tests are green

Patch 6 gate:

- attached ``Constraint`` mutation races covered by tests are green

Patch 7 gate:

- callback re-entry tests are green on free-threaded Python

Patch 8 gate:

- lifecycle stress tests pass repeatedly without deadlocks or crashes

Patch 9 gate:

- documented guarantees match the implemented behavior

Practical review guidance
-------------------------

To keep reviews effective:

- do not mix broad formatting cleanups into concurrency patches
- add tests in the same patch as the behavior they cover whenever possible
- prefer a few minimal internal helpers over a new locking framework
- keep lock ownership obvious at the call site
- stop and reassess if a patch starts needing more than one lock per attached
  object
