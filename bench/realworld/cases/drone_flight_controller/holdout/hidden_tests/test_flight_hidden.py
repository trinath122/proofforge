import math
import unittest

from flight import FlightController, PlanarQuad, State, fly

DT = 0.005
TILT_LIMIT = math.radians(25)
TILT_SLACK = math.radians(5)


def run(quad, target, seconds, controller=None):
    ctl = controller or FlightController(t_max=quad.t_max)
    trajectory = []
    while quad.state.t < seconds - 1e-9:
        left, right = ctl.update(quad.state, target, DT)
        trajectory.append(quad.step(left, right, DT))
    return trajectory, ctl


class TestControllerContract(unittest.TestCase):
    def test_dt_must_be_positive_seconds(self):
        ctl = FlightController()
        for bad in (0.0, -0.005):
            with self.assertRaises(ValueError):
                ctl.update(State(z=1.0), (0.0, 1.0), bad)

    def test_setpoint_step_causes_no_derivative_kick(self):
        quad = PlanarQuad(state=State(z=1.0))
        _, ctl = run(quad, (0.0, 1.0), 2.0)  # settled hover
        stepped = ctl.update(quad.state, (3.0, 2.0), DT)
        fresh = FlightController(t_max=quad.t_max).update(quad.state, (3.0, 2.0), DT)
        for a, b in zip(stepped, fresh):
            self.assertAlmostEqual(a, b, places=6)

    def test_reset_forgets_integrated_error(self):
        quad = PlanarQuad(state=State(z=1.0), wind=(1.5, 0.0))
        _, ctl = run(quad, (0.0, 1.0), 6.0)
        ctl.reset()
        probe = State(x=0.3, z=1.2, vx=0.1)
        fresh = FlightController(t_max=quad.t_max)
        for a, b in zip(ctl.update(probe, (0.0, 1.0), DT), fresh.update(probe, (0.0, 1.0), DT)):
            self.assertAlmostEqual(a, b, places=6)

    def test_commands_respect_motor_limits(self):
        quad = PlanarQuad(state=State(z=1.0))
        ctl = FlightController(t_max=quad.t_max)
        for target in ((10.0, 10.0), (-10.0, 0.5), (0.0, 30.0)):
            left, right = ctl.update(quad.state, target, DT)
            self.assertTrue(0.0 <= left <= quad.t_max and 0.0 <= right <= quad.t_max)


class TestFlightEnvelope(unittest.TestCase):
    def test_tilt_stays_within_limit_on_long_dash(self):
        trajectory, _ = run(PlanarQuad(state=State(z=2.0)), (15.0, 2.0), 10.0)
        self.assertLess(max(abs(s.theta) for s in trajectory), TILT_LIMIT + TILT_SLACK)
        self.assertAlmostEqual(trajectory[-1].x, 15.0, delta=0.2)

    def test_altitude_held_while_translating(self):
        trajectory, _ = run(PlanarQuad(state=State(z=1.0)), (4.0, 1.0), 8.0)
        self.assertLess(max(abs(s.z - 1.0) for s in trajectory), 0.05)

    def test_long_climb_does_not_wind_up(self):
        trajectory, _ = run(PlanarQuad(state=State(z=1.0)), (0.0, 11.0), 15.0)
        self.assertLess(max(s.z for s in trajectory), 11.4)
        self.assertAlmostEqual(trajectory[-1].z, 11.0, delta=0.1)

    def test_speed_limit(self):
        trajectory, _ = run(PlanarQuad(state=State(z=1.0)), (20.0, 15.0), 6.0)
        self.assertLess(max(abs(s.vx) for s in trajectory), 3.0 * 1.15)
        self.assertLess(max(abs(s.vz) for s in trajectory), 3.0 * 1.15)

    def test_weak_motors_keep_attitude_authority(self):
        # Thrust-to-weight 1.2: collective saturates, torque must not be lost.
        quad = PlanarQuad(state=State(z=1.0), t_max=6.0)
        trajectory, _ = run(quad, (6.0, 4.0), 14.0)
        self.assertLess(max(abs(s.theta) for s in trajectory), TILT_LIMIT + TILT_SLACK)
        self.assertGreater(min(s.z for s in trajectory), 0.8)
        self.assertAlmostEqual(trajectory[-1].x, 6.0, delta=0.2)
        self.assertAlmostEqual(trajectory[-1].z, 4.0, delta=0.2)

    def test_steady_wind_is_rejected(self):
        trajectory, _ = run(PlanarQuad(state=State(z=1.0), wind=(1.5, -0.5)), (0.0, 1.0), 10.0)
        self.assertLess(abs(trajectory[-1].x), 0.05)
        self.assertLess(abs(trajectory[-1].z - 1.0), 0.05)


class TestMissions(unittest.TestCase):
    def test_waypoint_needs_position_and_low_speed(self):
        waypoints = [(0.0, 3.0), (4.0, 3.0), (4.0, 1.0), (-3.0, 2.0)]
        log = fly(waypoints)
        self.assertTrue(log.completed, log.aborted)
        self.assertEqual(len(log.reached), len(waypoints))
        by_time = {round(s.t, 9): s for s in log.trajectory}
        for (wx, wz), t in zip(waypoints, log.reached):
            s = by_time[round(t, 9)]
            self.assertLessEqual(math.hypot(wx - s.x, wz - s.z), 0.2 + 1e-9)
            self.assertLessEqual(s.speed, 0.5 + 1e-9)
        self.assertGreater(log.reached[0], 0.5, "x already matched; altitude did not")

    def test_unreachable_waypoint_aborts_on_timeout(self):
        log = fly([(0.0, 1.5), (0.0, 80.0), (1.0, 1.0)], timeout=4.0)
        self.assertFalse(log.completed)
        self.assertEqual(log.aborted, "timeout at waypoint 1")
        self.assertEqual(len(log.reached), 1)
        self.assertLess(log.trajectory[-1].t - log.reached[0], 4.0 + 0.05)

    def test_mission_uses_the_quads_own_limits(self):
        log = fly([(3.0, 3.0)], quad=PlanarQuad(state=State(z=1.0), t_max=6.0))
        self.assertTrue(log.completed, log.aborted)

    def test_missions_are_deterministic(self):
        a = fly([(1.0, 2.0), (-1.0, 1.0)])
        b = fly([(1.0, 2.0), (-1.0, 1.0)])
        self.assertEqual(a.reached, b.reached)
        self.assertEqual(a.trajectory[-1], b.trajectory[-1])


if __name__ == "__main__":
    unittest.main()
