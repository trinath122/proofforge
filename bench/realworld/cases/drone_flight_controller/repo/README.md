# flight

Flight stack for the warehouse inspection drone, tested against a planar quadrotor
simulator. `flight/sim.py` is the physics plant and is not part of the flight software.

```python
from flight import FlightController, PlanarQuad, State, fly

quad = PlanarQuad(state=State(z=1.0))
ctl = FlightController(t_max=quad.t_max)
left, right = ctl.update(quad.state, target=(2.0, 1.5), dt=0.005)
quad.step(left, right, 0.005)

log = fly([(0.0, 1.5), (3.0, 2.0)])   # MissionLog(reached=[...], trajectory=[...], aborted=None)
```

Conventions: x is horizontal, z is altitude (m); pitch `theta` > 0 tilts thrust toward +x;
the left rotor is at -x, so `left > right` pitches toward +x. All times are seconds.
