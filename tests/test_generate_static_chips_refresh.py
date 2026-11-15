import unittest
from unittest.mock import patch

import generate_static_chips_v3 as static_chips


class MarginMaintenanceRefreshTests(unittest.TestCase):
    def existing_row(self, **overrides):
        row = {
            "chips_status": "ok",
            "chip_signal_state": "ok",
            "chip_available_days": "3",
            "chip_latest_date": "2026-11-14",
            "margin_cost_status": "ok",
            "margin_cost_line_t0": "100",
            "margin_maintenance_status": "ok",
            "margin_maintenance_t0": "160",
            "margin_maintenance_120d_status": "ok",
            "margin_maintenance_120d_low": "140",
            "margin_maintenance_120d_high": "190",
        }
        row.update(overrides)
        return row

    def test_legacy_row_without_120d_status_is_refreshed(self):
        row = self.existing_row(margin_maintenance_120d_status="")

        self.assertTrue(static_chips.should_refresh_stock(row, "2026-11-14"))

    def test_completed_empty_120d_attempt_is_not_repeated(self):
        row = self.existing_row(
            margin_maintenance_120d_status="empty",
            margin_maintenance_120d_low=None,
            margin_maintenance_120d_high=None,
        )

        self.assertFalse(static_chips.should_refresh_stock(row, "2026-11-14"))

    def test_completed_120d_range_is_reused(self):
        row = self.existing_row()

        self.assertFalse(static_chips.should_refresh_stock(row, "2026-11-14"))

    def test_build_marks_120d_range_attempt_as_ok_or_empty(self):
        for range_values, expected_status in (
            ({"margin_maintenance_120d_low": 140,
              "margin_maintenance_120d_high": 190}, "ok"),
            ({"margin_maintenance_120d_low": None,
              "margin_maintenance_120d_high": None}, "empty"),
        ):
            with self.subTest(expected_status=expected_status):
                with patch.object(
                    static_chips, "call_get_chip_analysis",
                    return_value=range_values,
                ), patch.object(
                    static_chips, "apply_finmind_usage_to_row",
                    side_effect=lambda row: row,
                ):
                    row = static_chips.build_chip_row(
                        {"stock_id": "2330", "name": "Test"},
                        trend_days=3,
                        concentration_threshold=15,
                    )

                self.assertEqual(
                    row["margin_maintenance_120d_status"], expected_status
                )


if __name__ == "__main__":
    unittest.main()
