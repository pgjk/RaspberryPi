import time


class RoomIntegralCorrection:
    def __init__(self, gain_per_hour: float, correction_limit: float):
        self.gain_per_hour = gain_per_hour
        self.correction_limit = correction_limit
        self.correction = 0.0

    def reset(self) -> None:
        self.correction = 0.0

    def update(
        self,
        room_error: float,
        dt: float,
        base_target: float,
        target_min: float,
        target_max: float,
    ) -> float:
        proposed_correction = self.correction + (
            self.gain_per_hour * room_error * dt / 3600.0
        )
        proposed_correction = max(
            -self.correction_limit,
            min(self.correction_limit, proposed_correction),
        )
        proposed_target = base_target + proposed_correction

        if (
            proposed_target > target_max and room_error > 0
        ) or (
            proposed_target < target_min and room_error < 0
        ):
            proposed_correction = self.correction

        self.correction = proposed_correction
        return max(target_min, min(target_max, base_target + self.correction))


class PIDController:
    def __init__(
        self,
        kp: float = 2.0,
        ki: float = 0.5,
        kd: float = 1.0,
        setpoint: float = 40.0,
        output_min: float = 0.0,
        output_max: float = 10.0,
        integral_limit: float = 100.0,
    ):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint
        self.output_min = output_min
        self.output_max = output_max
        self.integral_limit = integral_limit

        self._last_error = 0.0
        self._integral = 0.0
        self._last_time = time.time()

    def reset(self) -> None:
        self._last_error = 0.0
        self._integral = 0.0
        self._last_time = time.time()

    def update(self, measured_value: float) -> float:
        now = time.time()
        dt = max(now - self._last_time, 1e-6)
        self._last_time = now

        error = self.setpoint - measured_value
        self._integral += error * dt
        self._integral = max(min(self._integral, self.integral_limit), -self.integral_limit)

        derivative = (error - self._last_error) / dt
        self._last_error = error

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        output = max(self.output_min, min(self.output_max, output))
        return output
