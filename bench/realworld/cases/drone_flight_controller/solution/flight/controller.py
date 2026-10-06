"""Cascaded flight controller for the planar quadrotor: position -> attitude -> motors."""

from __future__ import annotations

import math

from flight.sim import G, State


class _Axis:
    """One translational axis: position error -> speed command -> acceleration command.

    The speed command is limited to `v_max`. The integral of the position error removes
    steady offsets (wind); it is frozen while any command is limited (anti-windup).
    Damping acts on the measured velocity, never on the derivative of the error.
    """

    def __init__(self, kp: float, kv: float, ki: float, i_limit: float, v_max: float) -> None:
        self.kp, self.kv, self.ki, self.i_limit, self.v_max = kp, kv, ki, i_limit, v_max
        self.integral = 0.0
        self.limited = False

    def output(self, error: float, velocity: float) -> float:
        raw = self.kp * error
        v_cmd = min(max(raw, -self.v_max), self.v_max)
        self.limited = v_cmd != raw
        return self.kv * (v_cmd - velocity) + self.ki * self.integral

    def integrate(self, error: float, dt: float) -> None:
        if not self.limited:
            self.integral = min(max(self.integral + error * dt, -self.i_limit), self.i_limit)


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
        self.max_tilt = max_tilt
        self.x_axis = _Axis(kp=1.5, kv=3.0, ki=1.2, i_limit=3.0, v_max=v_max)
        self.z_axis = _Axis(kp=2.0, kv=4.0, ki=1.5, i_limit=3.0, v_max=v_max)
        self.kp_att, self.kd_att = 120.0, 14.0
        self.saturated = False

    def reset(self) -> None:
        self.x_axis.integral = self.z_axis.integral = 0.0
        self.saturated = False

    def update(self, state: State, target: tuple[float, float], dt: float) -> tuple[float, float]:
        """Motor thrust commands (left, right) in newtons for one control step of dt seconds."""
        if not dt > 0:
            raise ValueError("dt must be > 0 seconds")
        ex, ez = target[0] - state.x, target[1] - state.z
        ax = self.x_axis.output(ex, state.vx)
        az = self.z_axis.output(ez, state.vz)

        # Desired tilt from desired accelerations, clamped to the tilt limit.
        raw_tilt = math.atan2(ax, G + az)
        tilt = min(max(raw_tilt, -self.max_tilt), self.max_tilt)
        tilt_clamped = tilt != raw_tilt

        # Collective: hold the vertical component while tilted.
        cos_t = max(math.cos(state.theta), math.cos(self.max_tilt))
        collective = self.mass * (G + az) / cos_t

        # Attitude loop: derivative on the measured rate.
        alpha = self.kp_att * (tilt - state.theta) - self.kd_att * state.omega
        diff = self.inertia * alpha / (2 * self.arm)  # left - right = 2 * diff

        left, right, saturated = self._mix(collective, diff)
        self.saturated = saturated

        # Anti-windup: integrate only while the outputs are not saturated.
        if not saturated:
            self.z_axis.integrate(ez, dt)
            if not tilt_clamped:
                self.x_axis.integrate(ex, dt)
        return left, right

    def _mix(self, collective: float, diff: float) -> tuple[float, float, bool]:
        """Split into motors; under saturation keep the torque and give up collective."""
        half = self.t_max / 2
        diff_c = min(max(diff, -half), half)
        base = collective / 2
        base_c = min(max(base, abs(diff_c)), self.t_max - abs(diff_c))
        return base_c + diff_c, base_c - diff_c, (base_c != base or diff_c != diff)
