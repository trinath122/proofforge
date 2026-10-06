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
    controller = controller or FlightController(t_max=quad.t_max)
    log = MissionLog(trajectory=[quad.state])
    for wp in waypoints:
        for _ in range(int(timeout / dt)):
            if abs(wp[0] - quad.state.x) <= accept_radius:
                break
            left, right = controller.update(quad.state, wp, dt * 1000)  # controller works in ms
            log.trajectory.append(quad.step(left, right, dt))
        log.reached.append(quad.state.t)
    return log
