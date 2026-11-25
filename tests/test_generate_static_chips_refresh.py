import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_static_chips_v3 as static_chips
import pandas as pd


class DailyValuationRefreshTests(unittest.TestCase):
    def test_full_valuation_refresh_writes_latest_per_fields(self):
        usage = {
            "login_status": "ok",
            "token_present": True,
            "token_source": "FINMIND_TOKEN",
            "token_masked": "masked",
            "user_count": 1,
            "api_request_limit": 1000,
            "remain": 999,
        }
        per_results = [
            {"per": 18.5, "per_120d_high": 22, "per_120d_low": 15,
             "per_is_prev": False},
            {"per": None, "per_120d_high": None, "per_120d_low": None,
             "per_is_prev": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "valuation.csv")
            with patch.object(
                static_chips, "get_per_120d_stats", side_effect=per_results,
            ) as get_per, patch.object(static_chips, "log_finmind_static_event"):
                result = static_chips.build_daily_valuation(
                    [
                        {"stock_id": "2330", "name": "TSMC"},
                        {"stock_id": "2317", "name": "Hon Hai"},
                    ],
                    output,
                    usage_info=usage,
                )

            persisted = pd.read_csv(output, dtype=str).set_index("stock_id")

        self.assertEqual(get_per.call_args_list[0].args, ("2330",))
        self.assertEqual(get_per.call_args_list[1].args, ("2317",))
        self.assertEqual(len(result), 2)
        self.assertEqual(persisted.loc["2330", "per_latest"], "18.5")
        self.assertEqual(persisted.loc["2330", "valuation_status"], "ok")
        self.assertEqual(persisted.loc["2317", "valuation_status"], "no_data")

    def test_full_chip_run_refreshes_valuation_for_complete_stock_list(self):
        stocks = [{"stock_id": "2330"}, {"stock_id": "2317"}]
        with patch.object(sys, "argv", ["generate_static_chips_v3.py"]), \
                patch.object(static_chips, "load_stock_list", return_value=stocks), \
                patch.object(static_chips, "build_static_chips"), \
                patch.object(static_chips, "build_daily_valuation") as build_valuation:
            static_chips.main()

        build_valuation.assert_called_once()
        self.assertIs(build_valuation.call_args.kwargs["stock_list"], stocks)

    def test_single_stock_chip_run_does_not_rebuild_full_valuation(self):
        stocks = [{"stock_id": "2330"}, {"stock_id": "2317"}]
        with patch.object(
            sys, "argv", ["generate_static_chips_v3.py", "--stock-id", "2330"],
        ), patch.object(
            static_chips, "load_stock_list", return_value=stocks,
        ), patch.object(
            static_chips, "build_static_chips",
        ) as build_chips, patch.object(
            static_chips, "build_daily_valuation",
        ) as build_valuation:
            static_chips.main()

        self.assertEqual(
            [row["stock_id"] for row in build_chips.call_args.kwargs["stock_list"]],
            ["2330"],
        )
        build_valuation.assert_not_called()


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
