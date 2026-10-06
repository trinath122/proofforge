"""Cascaded flight controller for the planar quadrotor: position -> attitude -> motors.

Ported from the bench-test notebook. Gains were tuned on the old simulator.
"""

from __future__ import annotations

import math

from flight.sim import G, State


class PID:
    def __init__(self, kp: float, ki: float, kd: float) -> None:
        self.kp, self.ki, self.kd = kp, ki, kd
        self.integral = 0.0
        self.prev_error: float | None = None

    def step(self, error: float, dt: float) -> float:
        self.integral += error * dt
        deriv = 0.0 if self.prev_error is None else (error - self.prev_error) / dt
        self.prev_error = error
        return self.kp * error + self.ki * self.integral + self.kd * deriv


class FlightController:
    def __init__(
        self,
        *,
        mass: float = 1.0,
        inertia: float = 0.02,
        arm: float = 0.2,
        t_max: float = 10.0,
        max_tilt: float = math.radians(25),
        v_max: float = 3.0,
    ) -> None:
        self.mass, self.inertia, self.arm, self.t_max = mass, inertia, arm, t_max
        self.max_tilt, self.v_max = max_tilt, v_max
        self.x_pid = PID(4.5, 1.2, 3.0)
        self.z_pid = PID(8.0, 1.5, 4.0)
        self.att_pid = PID(120.0, 0.0, 14.0)

    def reset(self) -> None:
        self.x_pid.integral = self.z_pid.integral = 0.0

    def update(self, state: State, target: tuple[float, float], dt: float) -> tuple[float, float]:
        ax = self.x_pid.step(target[0] - state.x, dt)
        az = self.z_pid.step(target[1] - state.z, dt)
        tilt = math.atan2(ax, G + az)
        collective = self.mass * (G + az)
        alpha = self.att_pid.step(tilt - state.theta, dt)
        diff = self.inertia * alpha / (2 * self.arm)
        left = collective / 2 - diff
        right = collective / 2 + diff
        clip = lambda v: min(max(v, 0.0), self.t_max)  # noqa: E731
        return clip(left), clip(right)
