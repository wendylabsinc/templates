"""Motor-clock arm reference; network packets only replace the destination.

Velocity and acceleration bounds apply during ordinary tracking. A HOLD,
position limit or measured-position leash takes precedence over smoothing.
This generates commands; it does not model or guarantee physical tracking.
"""
import numpy as np


class ArmReference:
    # Operator-requested fast profile: 10x the previous 3.5 rad/s smoothing
    # ceiling. Acceleration remains independently bounded, so a late packet
    # cannot turn this larger steady-state ceiling into an instant jump.
    max_velocity = 35.0
    max_acceleration = 16.0
    # The old 20 rad/s bandwidth never reached even the old velocity ceiling
    # inside the 10-degree tracking leash. Doubling it makes the higher speed
    # ceiling observable while the unchanged acceleration cap still bounds
    # every ordinary tick.
    frequency = 40.0

    def __init__(self, measured):
        self.reset(measured)
        self.guard_hits = 0

    def reset(self, measured):
        self.q = np.array(measured, dtype=float, copy=True)
        self.velocity = np.zeros_like(self.q)

    def advance(self, target, measured, dt, limits=None, tracking_error=np.deg2rad(10)):
        # Never turn a scheduling pause into a catch-up jump.
        dt = min(.02, max(0., float(dt)))
        if dt == 0:
            return self.q.copy()
        target = np.asarray(target, dtype=float)
        measured = np.asarray(measured, dtype=float)
        lo, hi = measured - tracking_error, measured + tracking_error
        if limits is not None:
            lo = np.maximum(lo, limits[:, 0])
            hi = np.minimum(hi, limits[:, 1])
            # When measurement is outside a calibrated range, the position
            # bound wins. Never widen that bound to satisfy the tracking leash.
            lo = np.minimum(lo, limits[:, 1])
            hi = np.maximum(hi, limits[:, 0])
        destination = np.clip(target, lo, hi)
        # Critically damped second-order reference, with bounded acceleration.
        acceleration = self.frequency**2 * (destination - self.q) - 2*self.frequency*self.velocity
        acceleration = np.clip(acceleration, -self.max_acceleration, self.max_acceleration)
        self.velocity = np.clip(self.velocity + acceleration*dt,
                                -self.max_velocity, self.max_velocity)
        proposed = self.q + self.velocity*dt
        bounded = np.clip(proposed, lo, hi)
        hit = bounded != proposed
        self.guard_hits += int(np.count_nonzero(hit))
        self.velocity[hit] = 0.
        self.q = bounded
        return self.q.copy()


def validate_motion_limits(values):
    """Validate the complete physical pair; never silently clamp a request."""
    bounds = {"max_velocity_rad_s": (.25, 50.), "max_acceleration_rad_s2": (1., 30.)}
    if not isinstance(values, dict) or set(values) != set(bounds):
        raise ValueError("expected max_velocity_rad_s and max_acceleration_rad_s2")
    result = {}
    for name, (lo, hi) in bounds.items():
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{name} must be finite and between {lo:g} and {hi:g}")
        result[name] = float(value)
    return result
