"""Waypoint missions: fly the quad through waypoints with the flight controller."""

from __future__ import annotations

from dataclasses import dataclass, field

from flight.controller import FlightController
from flight.sim import PlanarQuad, State


@dataclass
class MissionLog:
    reached: list[float] = field(default_factory=list)  # time each waypoint was reached
    trajectory: list[State] = field(default_factory=list)
    aborted: str | None = None

    @property
    def completed(self) -> bool:
        return self.aborted is None


def fly(
    waypoints: list[tuple[float, float]],
    *,
    quad: PlanarQuad | None = None,
    controller: FlightController | None = None,
    dt: float = 0.005,
    accept_radius: float = 0.2,
    accept_speed: float = 0.5,
    timeout: float = 15.0,
) -> MissionLog:
    quad = quad or PlanarQuad()
    controller = controller or FlightController(
        mass=quad.mass, inertia=quad.inertia, arm=quad.arm, t_max=quad.t_max
    )
    log = MissionLog(trajectory=[quad.state])
    for i, wp in enumerate(waypoints):
        started = quad.state.t
        while True:
            s = quad.state
            if (
                (wp[0] - s.x) ** 2 + (wp[1] - s.z) ** 2 <= accept_radius**2
                and s.speed <= accept_speed
            ):
                log.reached.append(s.t)
                break
            if s.t - started > timeout:
                log.aborted = f"timeout at waypoint {i}"
                return log
            left, right = controller.update(s, wp, dt)
            log.trajectory.append(quad.step(left, right, dt))
    return log
