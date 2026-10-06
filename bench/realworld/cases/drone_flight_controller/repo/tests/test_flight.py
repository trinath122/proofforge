import math
import unittest

from flight import FlightController, PlanarQuad, State, fly

DT = 0.005


def hover_quad(**kw):
    return PlanarQuad(state=State(z=1.0), **kw)


def run(quad, target, seconds, controller=None):
    ctl = controller or FlightController(t_max=quad.t_max)
    commands = []
    while quad.state.t < seconds - 1e-9:
        left, right = ctl.update(quad.state, target, DT)
        commands.append((left, right))
        quad.step(left, right, DT)
    return quad.state, commands


class TestFlight(unittest.TestCase):
    def test_hover_holds_position(self):
        s, _ = run(hover_quad(), (0.0, 1.0), 3.0)
        self.assertAlmostEqual(s.z, 1.0, delta=0.05)
        self.assertAlmostEqual(s.x, 0.0, delta=0.05)

    def test_climb_settles(self):
        s, _ = run(hover_quad(), (0.0, 2.0), 6.0)
        self.assertAlmostEqual(s.z, 2.0, delta=0.1)

    def test_lateral_move_arrives(self):
        s, _ = run(hover_quad(), (2.0, 1.0), 8.0)
        self.assertAlmostEqual(s.x, 2.0, delta=0.1)
        self.assertAlmostEqual(s.z, 1.0, delta=0.3)
        self.assertLess(abs(s.theta), math.radians(5))

    def test_motor_commands_stay_in_range(self):
        quad = hover_quad()
        _, commands = run(quad, (2.0, 2.0), 4.0)
        for left, right in commands:
            self.assertTrue(0.0 <= left <= quad.t_max and 0.0 <= right <= quad.t_max)

    def test_mission_visits_waypoints(self):
        log = fly([(0.0, 1.5), (2.0, 1.5)])
        self.assertTrue(log.completed, log.aborted)
        self.assertEqual(len(log.reached), 2)
        end = log.trajectory[-1]
        self.assertAlmostEqual(end.x, 2.0, delta=0.2)
        self.assertAlmostEqual(end.z, 1.5, delta=0.2)


if __name__ == "__main__":
    unittest.main()
