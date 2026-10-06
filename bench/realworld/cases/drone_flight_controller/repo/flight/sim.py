"""Planar quadrotor physics (frozen: this is the plant, not the code under repair).

The drone flies in the x-z plane. Two rotors sit at +-`arm` metres from the centre:
`left` at -x and `right` at +x. Pitch `theta` (radians) tilts the thrust vector toward +x
when positive, so with total thrust T:

    m * ax = T * sin(theta) - drag * vx + wind_x
    m * az = T * cos(theta) - m * g - drag * vz + wind_z
    I * alpha = arm * (left - right)

Motors are first-order lags (`motor_tau` seconds) and physically limited to
[0, t_max] newtons each. The ground is z = 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

G = 9.81


@dataclass(frozen=True)
class State:
    x: float = 0.0
    z: float = 0.0
    theta: float = 0.0
    vx: float = 0.0
    vz: float = 0.0
    omega: float = 0.0
    t: float = 0.0

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vz)


class PlanarQuad:
    def __init__(
        self,
        *,
        mass: float = 1.0,
        inertia: float = 0.02,
        arm: float = 0.2,
        t_max: float = 10.0,
        drag: float = 0.1,
        motor_tau: float = 0.02,
        wind: tuple[float, float] = (0.0, 0.0),
        state: State | None = None,
    ) -> None:
        self.mass, self.inertia, self.arm, self.t_max = mass, inertia, arm, t_max
        self.drag, self.motor_tau, self.wind = drag, motor_tau, wind
        self.state = state or State()
        hover = mass * G / 2 if self.state.z > 0 else 0.0
        self.thrust = [hover, hover]  # actual rotor thrusts (left, right)

    def step(self, left: float, right: float, dt: float) -> State:
        """Advance the simulation by dt seconds with the given motor commands (newtons)."""
        if not dt > 0:
            raise ValueError("dt must be > 0 seconds")
        k = min(1.0, dt / self.motor_tau)
        for i, cmd in enumerate((left, right)):
            cmd = min(max(cmd, 0.0), self.t_max)
            self.thrust[i] += (cmd - self.thrust[i]) * k
        tl, tr = self.thrust
        s = self.state
        total = tl + tr
        ax = (total * math.sin(s.theta) - self.drag * s.vx + self.wind[0]) / self.mass
        az = (total * math.cos(s.theta) - self.drag * s.vz + self.wind[1]) / self.mass - G
        alpha = self.arm * (tl - tr) / self.inertia
        omega = s.omega + alpha * dt
        vx, vz = s.vx + ax * dt, s.vz + az * dt
        theta = s.theta + omega * dt
        x, z = s.x + vx * dt, s.z + vz * dt
        if z <= 0.0:  # resting on the ground
            z, vz = 0.0, max(vz, 0.0)
            vx, omega, theta = 0.0, 0.0, 0.0
        self.state = replace(s, x=x, z=z, theta=theta, vx=vx, vz=vz, omega=omega, t=s.t + dt)
        return self.state
