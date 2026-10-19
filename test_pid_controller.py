import unittest

from pid_controller import RoomIntegralCorrection


class RoomIntegralCorrectionTests(unittest.TestCase):
    def test_integrates_room_error_over_elapsed_hours(self):
        correction = RoomIntegralCorrection(gain_per_hour=2.0, correction_limit=8.0)

        target = correction.update(
            room_error=1.0,
            dt=1800.0,
            base_target=26.0,
            target_min=15.0,
            target_max=44.0,
        )

        self.assertEqual(target, 27.0)
        self.assertEqual(correction.correction, 1.0)

    def test_limits_room_correction(self):
        correction = RoomIntegralCorrection(gain_per_hour=2.0, correction_limit=3.0)

        target = correction.update(
            room_error=2.0,
            dt=3600.0,
            base_target=26.0,
            target_min=15.0,
            target_max=44.0,
        )

        self.assertEqual(target, 29.0)
        self.assertEqual(correction.correction, 3.0)

    def test_does_not_integrate_further_past_maximum_target(self):
        correction = RoomIntegralCorrection(gain_per_hour=2.0, correction_limit=8.0)

        target = correction.update(
            room_error=1.0,
            dt=3600.0,
            base_target=43.5,
            target_min=15.0,
            target_max=44.0,
        )

        self.assertEqual(target, 43.5)
        self.assertEqual(correction.correction, 0.0)

    def test_clamps_target_and_unwinds_correction(self):
        correction = RoomIntegralCorrection(gain_per_hour=2.0, correction_limit=8.0)
        correction.correction = 4.0

        target = correction.update(
            room_error=-1.0,
            dt=3600.0,
            base_target=15.0,
            target_min=15.0,
            target_max=44.0,
        )

        self.assertEqual(target, 17.0)
        self.assertEqual(correction.correction, 2.0)


if __name__ == "__main__":
    unittest.main()
