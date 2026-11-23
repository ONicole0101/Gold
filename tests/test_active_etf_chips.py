import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import generate_static_chips_v3 as chips
from stock_service import _build_chip_fields


class ActiveEtfChipsTests(unittest.TestCase):
    def response(self, data, status=200):
        response = unittest.mock.Mock()
        response.json.return_value = {"status": status, "data": data, "msg": "error"}
        return response

    def test_build_aggregates_all_etfs_by_stock_and_chip_date(self):
        dates = ["2026-11-20", "2026-11-19", "2026-11-18"]
        rows = {
            "00980A": [
                {"date": d, "stock_id": "00980A", "component_stock_id": "2330",
                 "buy": buy, "sell": sell}
                for d, buy, sell in zip(dates, [1200, 0, 10], [0, 300, 10])
            ],
            "00981A": [
                {"date": d, "stock_id": "00981A", "component_stock_id": "2330",
                 "buy": buy, "sell": sell}
                for d, buy, sell in zip(dates, [0, 100, 0], [200, 0, 5])
            ],
        }

        def get(url, headers, params, timeout):
            self.assertEqual(headers, {"Authorization": "Bearer test-token"})
            if params["dataset"] == "TaiwanStockActiveETFInfo":
                return self.response([{"stock_id": key} for key in rows])
            self.assertEqual(params["start_date"], dates[-1])
            self.assertEqual(params["end_date"], dates[0])
            return self.response(rows[params["data_id"]])

        stock_row = {
            "stock_id": "2330", "chip_date_t0": dates[0],
            "chip_date_t1": dates[1], "chip_date_t2": dates[2],
            "chips_status": "ok", "chip_signal_state": "neutral",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "chips.csv")
            with patch.dict("os.environ", {"FINMIND_TOKEN": "test-token"}), \
                    patch.object(chips.requests, "get", side_effect=get) as request, \
                    patch.object(chips, "get_finmind_usage"), \
                    patch.object(chips, "build_chip_row", return_value=stock_row.copy()):
                result = chips.build_static_chips(
                    [{"stock_id": "2330"}], path, 3, 15, incremental=False,
                )
            persisted = chips.read_existing_chips(path).iloc[0].to_dict()
        self.assertEqual(request.call_count, 3)
        self.assertEqual([result.iloc[0][f"active_etf_net_t{i}"] for i in range(3)],
                         [1000, -200, -5])
        fields = _build_chip_fields(persisted)
        self.assertEqual([fields[f"active_etf_net_t{i}"] for i in range(3)],
                         [1000, -200, -5])
        self.assertEqual([fields[f"active_etf_partial_t{i}"] for i in range(3)],
                         [0, 0, 0])

    def test_missing_etf_day_shows_available_total_with_warning(self):
        dates = {"2026-11-20", "2026-11-19"}
        def get(url, headers, params, timeout):
            if params["dataset"] == "TaiwanStockActiveETFInfo":
                return self.response([{"stock_id": "A"}, {"stock_id": "B"}])
            days = dates if params["data_id"] == "A" else {"2026-11-19"}
            return self.response([
                {"date": day, "stock_id": params["data_id"],
                 "component_stock_id": "2330", "buy": 100, "sell": 0}
                for day in days
            ])

        rows = [
            {"stock_id": "2330", "chip_date_t0": "2026-11-20", "chip_date_t1": "2026-11-19"},
            {"stock_id": "2317", "chip_date_t0": "2026-11-20", "chip_date_t1": "2026-11-19"},
        ]
        with patch.dict("os.environ", {"FINMIND_TOKEN": "test-token"}), \
                patch.object(chips.requests, "get", side_effect=get):
            chips.apply_active_etf_nets(rows)
        self.assertEqual(rows[0]["active_etf_net_t0"], 100)
        self.assertEqual(rows[0]["active_etf_partial_t0"], 1)
        self.assertEqual(rows[0]["active_etf_net_t1"], 200)
        self.assertEqual(rows[0]["active_etf_partial_t1"], 0)
        self.assertEqual(rows[1]["active_etf_net_t0"], 0)
        self.assertEqual(rows[1]["active_etf_partial_t0"], 1)
        self.assertEqual(rows[1]["active_etf_net_t1"], 0)
        self.assertIsNone(rows[0]["active_etf_net_t2"])

    def test_all_etfs_without_records_leaves_day_unavailable(self):
        row = {"stock_id": "2330", "chip_date_t0": "2026-11-20"}
        with patch.dict("os.environ", {"FINMIND_TOKEN": "test-token"}), \
                patch.object(chips.requests, "get", side_effect=[
                    self.response([{"stock_id": "A"}, {"stock_id": "B"}]),
                    self.response([]), self.response([]),
                ]):
            chips.apply_active_etf_nets([row])
        self.assertIsNone(row["active_etf_net_t0"])
        self.assertIsNone(row["active_etf_partial_t0"])

    def test_failure_is_logged_and_does_not_keep_stale_totals(self):
        row = {"stock_id": "2330", "chip_date_t0": "2026-11-20", "active_etf_net_t0": 123}
        with patch.object(chips, "fetch_active_etf_nets", side_effect=requests.Timeout("timeout")), \
                patch("builtins.print") as log:
            chips.apply_active_etf_nets([row])
        self.assertIsNone(row["active_etf_net_t0"])
        self.assertIn("timeout", str(log.call_args))

    def test_latest_date_is_used_for_legacy_rows(self):
        row = {"stock_id": "2330", "chip_latest_date": "2026-11-20"}
        with patch.object(chips, "fetch_active_etf_nets",
                          return_value=({("2026-11-20", "2330"): 25}, {"2026-11-20"}, {"2026-11-20"})) as fetch:
            chips.apply_active_etf_nets([row])
        fetch.assert_called_once_with({"2026-11-20"})
        self.assertEqual(row["active_etf_net_t0"], 25)

    def test_api_error_is_not_treated_as_empty_data(self):
        with patch.dict("os.environ", {"FINMIND_TOKEN": "test-token"}), \
                patch.object(chips.requests, "get", return_value=self.response([], 402)):
            with self.assertRaisesRegex(RuntimeError, "FinMind TaiwanStockActiveETFInfo"):
                chips.fetch_active_etf_nets({"2026-11-20"})


if __name__ == "__main__":
    unittest.main()
