import threading
import unittest

import pymunk as p


class UnitTestFreeThreading(unittest.TestCase):
    def _make_blocked_space(
        self,
    ) -> tuple[p.Space, p.Body, p.Shape, threading.Event, threading.Event]:
        space = p.Space()
        body = p.Body(1, 1)
        body.velocity = (1, 0)
        shape = p.Circle(body, 1)
        entered = threading.Event()
        release = threading.Event()
        blocked_once = threading.Event()

        def velocity(body: p.Body, gravity: p.Vec2d, damping: float, dt: float) -> None:
            if not blocked_once.is_set():
                blocked_once.set()
                entered.set()
                self.assertTrue(release.wait(2))
            p.Body.update_velocity(body, gravity, damping, dt)

        body.velocity_func = velocity
        space.add(body, shape)
        return space, body, shape, entered, release

    def test_same_space_step_vs_step_is_serialized(self) -> None:
        space, body, shape, entered, release = self._make_blocked_space()
        first_done = threading.Event()
        second_done = threading.Event()

        t1 = threading.Thread(target=lambda: (space.step(0.01), first_done.set()))
        t2 = threading.Thread(target=lambda: (space.step(0.01), second_done.set()))

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(second_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(first_done.is_set())
        self.assertTrue(second_done.is_set())
        self.assertIn(body, space.bodies)
        self.assertIn(shape, space.shapes)

    def test_same_space_step_vs_add_is_serialized(self) -> None:
        space, _, _, entered, release = self._make_blocked_space()
        added_body = p.Body(1, 1)
        add_done = threading.Event()

        t1 = threading.Thread(target=space.step, args=(0.01,))
        t2 = threading.Thread(target=lambda: (space.add(added_body), add_done.set()))

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(add_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(add_done.is_set())
        self.assertIn(added_body, space.bodies)

    def test_same_space_step_vs_remove_is_serialized(self) -> None:
        space, body, shape, entered, release = self._make_blocked_space()
        remove_done = threading.Event()

        t1 = threading.Thread(target=space.step, args=(0.01,))
        t2 = threading.Thread(
            target=lambda: (space.remove(shape, body), remove_done.set())
        )

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(remove_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(remove_done.is_set())
        self.assertNotIn(body, space.bodies)
        self.assertNotIn(shape, space.shapes)

    def test_same_space_step_vs_point_query_is_serialized(self) -> None:
        space, _, _, entered, release = self._make_blocked_space()
        query_done = threading.Event()

        t1 = threading.Thread(target=space.step, args=(0.01,))
        t2 = threading.Thread(
            target=lambda: (
                space.point_query((0, 0), 10, p.ShapeFilter()),
                query_done.set(),
            )
        )

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(query_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(query_done.is_set())

    def test_same_space_step_vs_attached_body_setter_is_serialized(self) -> None:
        space, body, _, entered, release = self._make_blocked_space()
        setter_done = threading.Event()

        t1 = threading.Thread(target=space.step, args=(0.01,))
        t2 = threading.Thread(
            target=lambda: (setattr(body, "position", (10, 20)), setter_done.set())
        )

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(setter_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(setter_done.is_set())
        self.assertEqual(body.position, (10, 20))

    def test_same_space_step_vs_attached_shape_setter_is_serialized(self) -> None:
        space, _, shape, entered, release = self._make_blocked_space()
        setter_done = threading.Event()
        new_filter = p.ShapeFilter(1, 2, 3)

        t1 = threading.Thread(target=space.step, args=(0.01,))
        t2 = threading.Thread(
            target=lambda: (setattr(shape, "filter", new_filter), setter_done.set())
        )

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(setter_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(setter_done.is_set())
        self.assertEqual(shape.filter, new_filter)

    def test_same_space_step_vs_attached_constraint_setter_is_serialized(self) -> None:
        space, body, _, entered, release = self._make_blocked_space()
        joint = p.PinJoint(space.static_body, body, (0, 0), (0, 0))
        space.add(joint)
        setter_done = threading.Event()

        t1 = threading.Thread(target=space.step, args=(0.01,))
        t2 = threading.Thread(
            target=lambda: (setattr(joint, "max_force", 123), setter_done.set())
        )

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertFalse(setter_done.wait(0.1))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(setter_done.is_set())
        self.assertEqual(joint.max_force, 123)

    def test_cross_space_steps_can_run_concurrently(self) -> None:
        space1, _, _, entered, release = self._make_blocked_space()
        space2 = p.Space()
        body2 = p.Body(1, 1)
        shape2 = p.Circle(body2, 1)
        space2.add(body2, shape2)
        second_done = threading.Event()

        t1 = threading.Thread(target=space1.step, args=(0.01,))
        t2 = threading.Thread(target=lambda: (space2.step(0.01), second_done.set()))

        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        self.assertTrue(second_done.wait(2))

        release.set()
        t1.join(2)
        t2.join(2)

        self.assertTrue(second_done.is_set())

    def test_collision_callback_can_reenter_space_add(self) -> None:
        space = p.Space()
        body1 = p.Body(1, 1)
        body2 = p.Body(1, 1)
        body2.position = (1, 0)
        shape1 = p.Circle(body1, 1)
        shape2 = p.Circle(body2, 1)
        extra_body = p.Body(1, 1)
        extra_shape = p.Circle(extra_body, 1)

        def begin(arbiter: p.Arbiter, space: p.Space, data: object) -> bool:
            space.add(extra_body, extra_shape)
            return True

        space.on_collision(begin=begin)
        space.add(body1, body2, shape1, shape2)
        space.step(0.01)

        self.assertIn(extra_body, space.bodies)
        self.assertIn(extra_shape, space.shapes)

    def test_collision_callback_can_reenter_space_remove(self) -> None:
        space = p.Space()
        body1 = p.Body(1, 1)
        body2 = p.Body(1, 1)
        body2.position = (1, 0)
        shape1 = p.Circle(body1, 1)
        shape2 = p.Circle(body2, 1)

        def begin(arbiter: p.Arbiter, space: p.Space, data: object) -> bool:
            space.remove(shape2, body2)
            return True

        space.on_collision(begin=begin)
        space.add(body1, body2, shape1, shape2)
        space.step(0.01)

        self.assertNotIn(body2, space.bodies)
        self.assertNotIn(shape2, space.shapes)
