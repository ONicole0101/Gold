import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
import requests

import data_sources as sources
import financial_analysis as analysis
import generate_static_csv as static


def complete_row():
    row = {col: 1.0 for col in static.DATA_COLS}
    row.update({
        "stock_id": "2330",
        "name": "Test stock",
        "eps_Y_quarters": 2,
        "static_updated_at": static.now_utc_str(),
        "static_status": "ok",
        "static_reason": "",
    })
    for group in static.GROUPS:
        static.set_group_status(row, group, "ok")
    return row


class RefreshSelectionTests(unittest.TestCase):
    def should_update(self, row, **kwargs):
        options = dict(
            retry_errors=False, retry_no_data=False, force=False,
            refresh_hours=24,
        )
        options.update(kwargs)
        return static.should_update(row, **options)

    def test_fresh_complete_row_is_reused(self):
        self.assertFalse(self.should_update(complete_row()))

    def test_ok_status_does_not_hide_missing_eps_metadata(self):
        for value in (None, "", float("nan")):
            with self.subTest(value=value):
                row = complete_row()
                row["eps_Y_quarters"] = value
                self.assertTrue(self.should_update(row))

    def test_optional_derived_fields_do_not_force_refresh(self):
        row = complete_row()
        for col in ("eps_yoy", "per_ttm", "rev_yoy"):
            row[col] = None
        self.assertFalse(self.should_update(row))

    def test_stale_completed_rows_are_refreshed(self):
        for status in ("ok", "partial_ok"):
            with self.subTest(status=status):
                row = complete_row()
                row["static_status"] = status
                row["static_updated_at"] = (
                    datetime.utcnow() - timedelta(hours=25)
                ).strftime("%Y-%m-%d %H:%M:%S")
                self.assertTrue(self.should_update(row))
                self.assertFalse(self.should_update(row, refresh_hours=0))

    def test_confirmed_no_data_waits_until_refresh_or_explicit_retry(self):
        row = complete_row()
        row["static_status"] = "partial_ok"
        static.set_group_status(row, "eps", "no_data", "empty")
        for col in static.GROUPS["eps"]:
            row[col] = None
        self.assertFalse(self.should_update(row))
        self.assertTrue(self.should_update(row, retry_no_data=True))

    def test_error_retry_flag_is_respected(self):
        row = complete_row()
        row["static_status"] = "error"
        static.set_group_status(row, "eps", "error", "timeout")
        self.assertFalse(self.should_update(row))
        self.assertTrue(self.should_update(row, retry_errors=True))
        self.assertTrue(self.should_update(row, force=True))

    def test_limited_or_pending_sources_are_retried(self):
        for status in ("api_limited", "pending", "incomplete"):
            with self.subTest(status=status):
                row = complete_row()
                row["static_status"] = "incomplete"
                static.set_group_status(row, "eps", status)
                self.assertTrue(self.should_update(row))

    def test_finalize_marks_missing_quarters_incomplete(self):
        row = complete_row()
        row["eps_Y_quarters"] = None
        with patch.object(static, "apply_finmind_usage_to_row", side_effect=lambda r: r):
            result = static.finalize_static_status(row)
        self.assertEqual(result["eps_status"], "incomplete")
        self.assertEqual(result["static_status"], "incomplete")


class FinancialDownloadTests(unittest.TestCase):
    def setUp(self):
        self.record = patch.object(sources, "_record_finmind_request").start()
        self.get = patch.object(sources.requests, "get").start()
        self.addCleanup(patch.stopall)

    def response(self, body, http_status=200):
        response = Mock(status_code=http_status)
        response.json.return_value = body
        self.get.return_value = response
        return response

    def test_successful_empty_response_is_no_data(self):
        self.response({"status": 200, "data": []})
        self.assertEqual(sources.get_eps_raw("2330"), [])

    def test_http_and_api_errors_are_not_empty_data(self):
        for http, api in ((429, 429), (500, 500), (200, 429), (200, 400)):
            with self.subTest(http=http, api=api):
                self.response({"status": api, "msg": "failed", "data": []}, http)
                with self.assertRaises(RuntimeError):
                    sources.get_eps_raw("2330")

    def test_malformed_responses_raise(self):
        for body in ([], {}, {"status": 200}, {"status": 200, "data": None}):
            with self.subTest(body=body):
                self.response(body)
                with self.assertRaises(ValueError):
                    sources.get_eps_raw("2330")
        self.response({}).json.side_effect = ValueError("invalid")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            sources.get_eps_raw("2330")

    def test_transport_errors_propagate_for_all_fundamental_downloads(self):
        self.get.side_effect = requests.Timeout("timeout")
        for fetch in (
            sources.get_eps_raw, sources.get_profit_ratio,
            sources.get_balance_sheet_raw, sources.get_revenue_raw,
        ):
            with self.subTest(fetch=fetch.__name__):
                with self.assertRaises(requests.Timeout):
                    fetch("2330")

    def test_runtime_auth_is_resolved_before_query_parameters(self):
        self.response({"status": 200, "data": []})
        with patch.object(sources, "FINMIND_token", "old"):
            self.record.side_effect = lambda *args: setattr(
                sources, "FINMIND_token", "new"
            )
            sources.get_eps_raw("2330")
        self.assertEqual(self.get.call_args.kwargs["params"]["token"], "new")


class FinancialAnalysisTests(unittest.TestCase):
    def test_static_analysis_can_propagate_download_errors(self):
        cases = (
            (analysis.get_eps_analysis, "get_eps_raw"),
            (analysis.get_roe_analysis, "get_profit_ratio_raw"),
            (analysis.get_profit_ratio, "get_profit_ratio_raw"),
        )
        for analyze, raw_name in cases:
            with self.subTest(analyze=analyze.__name__):
                with patch.object(analysis, raw_name, side_effect=RuntimeError("429")):
                    with self.assertRaisesRegex(RuntimeError, "429"):
                        analyze("2330", raise_errors=True)
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        analyze("2330")
                    self.assertIn("429", output.getvalue())

    def test_downloaded_third_quarter_updates_eps_and_quarter_count(self):
        year = datetime.now().year
        rows = [
            {"date": f"{year - 1}-12-31", "type": "EPS", "value": 4},
            {"date": f"{year}-03-31", "type": "EPS", "value": 1},
            {"date": f"{year}-06-30", "type": "EPS", "value": 2},
            {"date": f"{year}-09-30", "type": "EPS", "value": 3},
        ]
        with patch.object(analysis, "get_eps_raw", return_value=rows):
            with contextlib.redirect_stdout(io.StringIO()):
                row = static.apply_eps_fields({}, "2330")
        self.assertEqual(row["eps_Y"], 6)
        self.assertEqual(row["eps_Y_quarters"], 3)
        self.assertEqual(row["eps_ttm"], 10)
        self.assertEqual(row["eps_status"], "ok")


class StaticBuildErrorTests(unittest.TestCase):
    def setUp(self):
        patch.object(
            static, "apply_finmind_usage_to_row", side_effect=lambda row: row
        ).start()
        patch.object(static, "get_roe_analysis", return_value={
            "roe_last_year": 10, "roe_ttm": 12,
        }).start()
        patch.object(static, "get_revenue_trend", return_value=({"rev": 1}, "")).start()
        patch.object(static, "get_profit_ratio", return_value={
            "current": {"gross": 30, "op": 20, "net": 10},
        }).start()
        patch.object(static, "get_per_120d_stats", return_value={"per": 20}).start()
        patch.object(static, "get_disposition_securities_period", return_value={}).start()
        self.addCleanup(patch.stopall)

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return static.build_static_row({"stock_id": "2330", "name": "Test"})

    def test_eps_limit_is_not_no_data_and_other_groups_stay_pending(self):
        with patch.object(analysis, "get_eps_raw", side_effect=RuntimeError("429")):
            row = self.build()
        self.assertEqual(row["eps_status"], "api_limited")
        self.assertEqual(row["static_status"], "api_limited")
        self.assertEqual(row["roe_status"], "pending")

    def test_eps_timeout_is_recorded_without_blocking_other_sources(self):
        with patch.object(analysis, "get_eps_raw", side_effect=requests.Timeout("timeout")):
            row = self.build()
        self.assertEqual(row["eps_status"], "error")
        self.assertEqual(row["static_status"], "error")
        self.assertEqual(row["roe_status"], "ok")
        self.assertEqual(row["revenue_status"], "ok")

    def test_empty_eps_is_no_data_not_an_error(self):
        with patch.object(analysis, "get_eps_raw", return_value=[]):
            row = self.build()
        self.assertEqual(row["eps_status"], "no_data")
        self.assertEqual(row["static_status"], "partial_ok")

    def test_revenue_and_profit_errors_are_not_no_data(self):
        for group, function in (
            ("revenue", "get_revenue_trend"), ("profit", "get_profit_ratio"),
        ):
            with self.subTest(group=group):
                with patch.object(analysis, "get_eps_raw", return_value=[]):
                    with patch.object(static, function, side_effect=RuntimeError("download failed")):
                        row = self.build()
                self.assertEqual(row[f"{group}_status"], "error")
                self.assertEqual(row["static_status"], "error")

    def test_old_margin_quarter_requires_backfill(self):
        result = {
            "current": {"gross": 30, "op": 20, "net": 10},
            "is_prev": {"gross": True, "op": False, "net": False},
        }
        with patch.object(analysis, "get_eps_raw", return_value=[]):
            with patch.object(static, "get_profit_ratio", return_value=result):
                row = self.build()
        self.assertEqual(row["gross_margin"], 30)
        self.assertEqual(row["gross_margin_is_prev"], "True")
        self.assertEqual(row["profit_status"], "incomplete")


class RevenuePeriodTests(unittest.TestCase):
    def trend(self, rows):
        with patch.object(static, "get_revenue_raw", return_value=rows):
            return static.get_revenue_trend("2330")[0]

    def test_comparisons_use_actual_month_not_row_offsets(self):
        rows = [
            {"date": "2025-10-01", "revenue": 1e9, "revenue_year": 2025, "revenue_month": 9},
            {"date": "2026-07-01", "revenue": 2e9, "revenue_year": 2026, "revenue_month": 6},
            {"date": "2026-09-01", "revenue": 3e9, "revenue_year": 2026, "revenue_month": 8},
            {"date": "2026-10-01", "revenue": 4e9, "revenue_year": 2026, "revenue_month": 9},
        ]
        result = self.trend(rows)
        self.assertEqual(result["rev"], 4)
        self.assertEqual(result["mom"], 33.33)
        self.assertEqual(result["qoq"], 100)
        self.assertEqual(result["yoy"], 300)

    def test_missing_month_is_not_replaced_by_previous_available_row(self):
        result = self.trend([
            {"date": "2026-07-01", "revenue": 1e9},
            {"date": "2026-09-01", "revenue": 2e9},
        ])
        self.assertIsNone(result["mom"])
        self.assertIsNone(result["qoq"])
        self.assertIsNone(result["yoy"])

    def test_duplicate_month_uses_latest_revision_and_does_not_shift_offsets(self):
        result = self.trend([
            {"date": "2026-09-01", "revenue": 2e9, "create_time": "2026-09-10"},
            {"date": "2026-10-01", "revenue": 3e9, "create_time": "2026-10-10"},
            {"date": "2026-09-01", "revenue": 1e9, "create_time": "2026-09-09"},
        ])
        self.assertEqual(result["mom"], 50)

    def test_business_period_takes_precedence_over_publication_date(self):
        result = self.trend([
            {"date": "2026-10-15", "revenue": 1e9, "revenue_year": 2026, "revenue_month": 8},
            {"date": "2026-10-01", "revenue": 2e9, "revenue_year": 2026, "revenue_month": 9},
        ])
        self.assertEqual(result["rev"], 2)
        self.assertEqual(result["mom"], 100)

    def test_value_alias_and_zero_denominator(self):
        result = self.trend([
            {"date": "2026-08-01", "value": 0},
            {"date": "2026-09-01", "value": 1e9},
        ])
        self.assertEqual(result["rev"], 1)
        self.assertIsNone(result["mom"])

    def test_invalid_nonempty_revenue_is_an_error(self):
        for row in (
            {"date": "2026-09-01"},
            {"revenue": 1e9},
            {"date": "invalid", "revenue": 1e9},
            {"date": "2026-09-01", "revenue": "invalid"},
            {"date": "2026-09-01", "revenue": 1e9, "revenue_month": 9},
            {"date": "2026-09-01", "revenue": 1e9, "revenue_year": 2026, "revenue_month": 13},
        ):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    self.trend([row])


class ProfitPeriodTests(unittest.TestCase):
    def analyze(self, rows):
        with patch.object(analysis, "get_profit_ratio_raw", return_value=rows):
            return analysis.get_profit_ratio("2330", raise_errors=True)

    def test_low_margins_stay_percentage_points(self):
        result = self.analyze([
            {"date": "2026-06-30", "type": "Revenue", "value": 1000},
            {"date": "2026-06-30", "type": "GrossProfit", "value": 10},
            {"date": "2026-06-30", "type": "OperatingIncome", "value": 5},
            {"date": "2026-06-30", "type": "IncomeAfterTaxes", "value": -2},
        ])
        self.assertEqual(result["current"], {"gross": 1, "op": 0.5, "net": -0.2})

    def test_missing_quarter_uses_ratio_fallback_without_overriding_amount(self):
        result = self.analyze([
            {"date": "2026-03-31", "type": "Revenue", "value": 100},
            {"date": "2026-03-31", "type": "GrossProfit", "value": 30},
            {"date": "2026-03-31", "type": "GrossMargin", "value": 99},
            {"date": "2026-06-30", "type": "Revenue", "value": 100},
            {"date": "2026-06-30", "type": "GrossMargin", "value": 35},
        ])
        self.assertEqual(result["current"]["gross"], 35)
        self.assertEqual(result["prev"]["gross"], 30)
        self.assertEqual(result["qoq"]["gross"], 5)
        self.assertFalse(result["is_prev"]["gross"])

    def test_absent_previous_quarter_and_year_do_not_use_arbitrary_rows(self):
        dates = [
            "2024-03-31", "2024-06-30", "2024-09-30",
            "2024-12-31", "2025-03-31", "2026-06-30",
        ]
        result = self.analyze([
            {"date": date, "type": "GrossMargin", "value": 10 + i}
            for i, date in enumerate(dates)
        ])
        self.assertIsNone(result["qoq"]["gross"])
        self.assertIsNone(result["yoy_diff"]["gross"])

    def test_same_quarter_last_year_and_previous_quarter_are_used(self):
        result = self.analyze([
            {"date": "2025-06-30", "type": "GrossMargin", "value": 20},
            {"date": "2026-03-31", "type": "GrossMargin", "value": 30},
            {"date": "2026-06-30", "type": "GrossMargin", "value": 35},
        ])
        self.assertEqual(result["qoq"]["gross"], 5)
        self.assertEqual(result["yoy_diff"]["gross"], 15)

    def test_account_precedence_does_not_depend_on_response_order(self):
        numerator_rows = [
            {"date": "2026-06-30", "type": "IncomeAfterTaxes", "value": 20},
            {"date": "2026-06-30", "type": "OtherIncome", "origin_name": "稅後淨利", "value": 999},
        ]
        for ordered in (numerator_rows, list(reversed(numerator_rows))):
            result = self.analyze([
                {"date": "2026-06-30", "type": "Revenue", "value": 100},
            ] + ordered)
            self.assertEqual(result["current"]["net"], 20)

    def test_financial_sector_singular_income_after_tax_is_supported(self):
        result = self.analyze([
            {"date": "2026-06-30", "type": "Revenue", "value": 100},
            {"date": "2026-06-30", "type": "IncomeAfterTax", "value": 20},
        ])
        self.assertEqual(result["current"]["net"], 20)

    def test_older_metric_is_flagged_when_latest_statement_lacks_it(self):
        result = self.analyze([
            {"date": "2026-03-31", "type": "GrossMargin", "value": 20},
            {"date": "2026-06-30", "type": "Revenue", "value": 100},
        ])
        self.assertEqual(result["current"]["gross"], 20)
        self.assertTrue(result["is_prev"]["gross"])

    def test_malformed_statement_is_not_no_data(self):
        for rows in (
            [{"date": "2026-06-30", "type": "Revenue"}],
            [{"date": "invalid", "type": "Revenue", "value": 100}],
            [{"date": "2026-06-30", "type": "Revenue", "value": "invalid"}],
            [{"date": "2026-06-30", "value": 100}],
        ):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    self.analyze(rows)


class RoeAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.income = []
        self.equity = []
        for date in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"):
            self.income.extend([
                {"date": date, "type": "EquityAttributableToOwnersOfParent", "value": 10},
                {"date": date, "type": "IncomeAfterTaxes", "value": 30},
            ])
        for date in ("2024-12-31", "2025-12-31"):
            self.equity.extend([
                {"date": date, "type": "EquityAttributableToOwnersOfParent", "value": 100},
                {"date": date, "type": "Equity", "value": 200},
            ])

    def analyze(self):
        with patch.object(analysis, "get_profit_ratio_raw", return_value=self.income):
            with patch.object(analysis, "get_balance_sheet_raw", return_value=self.equity):
                return analysis.get_roe_analysis("2330", raise_errors=True)

    def test_parent_income_is_paired_with_parent_equity(self):
        result = self.analyze()
        self.assertEqual(result["roe_last_year"], 40)
        self.assertEqual(result["roe_ttm"], 40)

    def test_total_income_fallback_requires_total_equity(self):
        self.income = [r for r in self.income if r["type"] == "IncomeAfterTaxes"]
        result = self.analyze()
        self.assertEqual(result["roe_last_year"], 60)
        self.assertEqual(result["roe_ttm"], 60)
        self.equity = [r for r in self.equity if r["type"] != "Equity"]
        result = self.analyze()
        self.assertIsNone(result["roe_last_year"])
        self.assertIsNone(result["roe_ttm"])

    def test_singular_total_income_alias_is_supported(self):
        self.income = [
            dict(r, type="IncomeAfterTax")
            for r in self.income if r["type"] == "IncomeAfterTaxes"
        ]
        self.assertEqual(self.analyze()["roe_ttm"], 60)

    def test_missing_ending_equity_does_not_use_older_quarter(self):
        self.equity = [r for r in self.equity if r["date"] != "2025-12-31"]
        result = self.analyze()
        self.assertIsNone(result["roe_last_year"])
        self.assertIsNone(result["roe_ttm"])

    def test_missing_starting_equity_does_not_use_first_quarter(self):
        self.equity = [r for r in self.equity if r["date"] != "2024-12-31"]
        self.equity.append({
            "date": "2025-03-31", "type": "EquityAttributableToOwnersOfParent", "value": 100,
        })
        result = self.analyze()
        self.assertIsNone(result["roe_last_year"])
        self.assertIsNone(result["roe_ttm"])

    def test_ttm_requires_four_consecutive_quarters(self):
        self.income = [r for r in self.income if r["date"] != "2025-06-30"]
        self.income.append({
            "date": "2024-12-31", "type": "EquityAttributableToOwnersOfParent", "value": 10,
        })
        self.assertIsNone(self.analyze()["roe_ttm"])

    def test_ttm_does_not_end_at_an_older_income_quarter(self):
        self.income.append({"date": "2026-03-31", "type": "Revenue", "value": 100})
        self.assertIsNone(self.analyze()["roe_ttm"])


class IncrementalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = os.path.join(self.temp_dir.name, "AllStatic.csv")
        self.stocks = [{"stock_id": "2330", "name": "Test stock"}]
        self.usage = patch.object(static, "get_finmind_usage").start()
        patch.object(static, "get_finmind_token_status", return_value={}).start()
        patch.object(static, "log_finmind_static_event").start()
        self.addCleanup(patch.stopall)
        static.atomic_write_csv(pd.DataFrame([complete_row()]), self.path)

    def run_incremental(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            static.build_incremental(
                self.stocks, self.path, sleep_sec=0, **kwargs
            )
        return static.read_existing_static(self.path).iloc[0].to_dict()

    def test_reusing_a_row_does_not_download_eps(self):
        with patch.object(static, "apply_eps_fields") as eps:
            with patch.object(static, "build_static_row") as build:
                self.run_incremental()
        eps.assert_not_called()
        build.assert_not_called()

    def test_missing_quarters_are_backfilled_and_persisted(self):
        old = complete_row()
        old["eps_Y_quarters"] = None
        static.atomic_write_csv(pd.DataFrame([old]), self.path)
        new = complete_row()
        new.update(eps_Y=6, eps_Y_quarters=3, eps_ttm=10)
        with patch.object(static, "build_static_row", return_value=new):
            row = self.run_incremental()
        self.assertEqual(float(row["eps_Y_quarters"]), 3)
        self.assertEqual(float(row["eps_Y"]), 6)

    def test_failed_refresh_preserves_values_and_persists_failure(self):
        old = complete_row()
        old["eps_Y_is_prev"] = "True"
        static.atomic_write_csv(pd.DataFrame([old]), self.path)
        new = complete_row()
        new.update(eps_Y=None, eps_Y_quarters=None, eps_ttm=None, eps_yoy=None)
        new["static_status"] = "error"
        static.set_group_status(new, "eps", "error", "timeout")
        with patch.object(static, "build_static_row", return_value=new):
            row = self.run_incremental(force=True)
        self.assertEqual(float(row["eps_Y"]), old["eps_Y"])
        self.assertEqual(float(row["eps_Y_quarters"]), old["eps_Y_quarters"])
        self.assertEqual(row["eps_Y_is_prev"], "True")
        self.assertEqual(row["eps_status"], "error")
        self.assertEqual(row["eps_reason"], "timeout")
        self.assertEqual(row["static_status"], "error")

    def test_api_limit_preserves_unattempted_groups(self):
        new = {col: None for col in static.DATA_COLS}
        for group in static.GROUPS:
            static.set_group_status(new, group, "pending")
        static.set_group_status(new, "eps", "api_limited", "429")
        result = static.preserve_failed_group_values(new, complete_row())
        for col in static.DATA_COLS:
            self.assertIsNotNone(result[col], col)
        self.assertEqual(result["eps_status"], "api_limited")
        self.assertEqual(result["roe_status"], "pending")

    def test_max_rows_caps_downloads_including_reused_rows(self):
        self.stocks.extend([
            {"stock_id": "2317", "name": "Second"},
            {"stock_id": "2454", "name": "Third"},
        ])
        new = complete_row()
        new["stock_id"] = "2317"
        with patch.object(static, "apply_eps_fields") as eps:
            with patch.object(static, "build_static_row", return_value=new) as build:
                self.run_incremental(max_rows=1)
        eps.assert_not_called()
        build.assert_called_once_with(self.stocks[1])

    def test_repair_only_does_not_download(self):
        with patch.object(static, "build_static_row") as build:
            with patch.object(static, "apply_eps_fields") as eps:
                self.run_incremental(repair_only=True)
        build.assert_not_called()
        eps.assert_not_called()
        self.usage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
