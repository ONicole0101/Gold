import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

import data_sources
import index_data


def _df(rows):
    df = pd.DataFrame(
        rows, columns=["date", "open", "close", "max", "min", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _today_text():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


class StockPriceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_dir = data_sources.STOCK_PRICE_CACHE_DIR
        self._old_enabled = data_sources.STOCK_PRICE_CACHE_ENABLED
        data_sources.STOCK_PRICE_CACHE_DIR = self.tmp.name
        data_sources.STOCK_PRICE_CACHE_ENABLED = True
        self.addCleanup(setattr, data_sources,
                        "STOCK_PRICE_CACHE_DIR", self._old_dir)
        self.addCleanup(setattr, data_sources,
                        "STOCK_PRICE_CACHE_ENABLED", self._old_enabled)

    def _write_cache(self, stock_id, df, fetch_date, volume_scale=1000):
        csv_path, meta_path = data_sources._stock_price_cache_paths(stock_id)
        out = df.copy()
        out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
        out.to_csv(csv_path, index=False, encoding="utf-8-sig")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "stock_id": str(stock_id),
                "fetch_date": fetch_date,
                "last_data_date": out["date"].max(),
                "volume_scale": volume_scale,
            }, f)

    def test_same_day_incremental_supplement(self):
        stock_id = "2330"
        cached = _df([
            ("2026-11-09", 100, 110, 115, 95, 1000),
            ("2026-11-10", 110, 120, 125, 105, 1100),
        ])
        self._write_cache(stock_id, cached, fetch_date=_today_text())

        # 遠端回傳已沿用快取 volume_scale=1000 正規化的增補資料（單位：張）
        increment = _df([
            ("2026-11-10", 110, 121, 126, 105, 1150),
            ("2026-11-11", 121, 130, 135, 115, 2000),
        ])
        with patch.object(data_sources, "_fetch_stock_price_remote",
                          return_value=(increment, 1000, True)) as mock_fetch:
            result = data_sources.get_stock_data(stock_id)

        # 同日：自最後資料日起增補，並沿用快取量綱，而非全量重取
        mock_fetch.assert_called_once_with(
            stock_id, "2026-11-10", volume_scale=1000)
        self.assertEqual(len(result), 3)
        by_date = {d.strftime("%Y-%m-%d"): r for d,
                   r in result.set_index("date").iterrows()}
        # 舊列保留、當日列以增補資料更新
        self.assertEqual(by_date["2026-11-09"]["close"], 110)
        self.assertEqual(by_date["2026-11-10"]["close"], 121)
        self.assertEqual(by_date["2026-11-11"]["volume"], 2000)

        _, meta_path = data_sources._stock_price_cache_paths(stock_id)
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["last_data_date"], "2026-11-11")
        self.assertEqual(meta["fetch_date"], _today_text())

    def test_cross_day_full_refetch(self):
        stock_id = "2330"
        cached = _df([("2026-11-09", 100, 110, 115, 95, 1000)])
        yesterday = (datetime.strptime(_today_text(), "%Y-%m-%d")
                     - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write_cache(stock_id, cached, fetch_date=yesterday)

        full = _df([
            ("2026-11-09", 100, 110, 115, 95, 1000),
            ("2026-11-10", 110, 120, 125, 105, 1100),
        ])
        with patch.object(data_sources, "_fetch_stock_price_remote",
                          return_value=(full, 1000, True)) as mock_fetch:
            result = data_sources.get_stock_data(stock_id)

        # 跨日：全量重取
        mock_fetch.assert_called_once_with(
            stock_id, data_sources.STOCK_PRICE_START_DATE)
        self.assertEqual(len(result), 2)

    def test_increment_failure_falls_back_to_cache(self):
        stock_id = "2330"
        cached = _df([("2026-11-10", 110, 120, 125, 105, 1100)])
        self._write_cache(stock_id, cached, fetch_date=_today_text())

        with patch.object(data_sources, "_fetch_stock_price_remote",
                          return_value=(pd.DataFrame(), 1, False)):
            result = data_sources.get_stock_data(stock_id)

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result.iloc[0]["date"].strftime("%Y-%m-%d"), "2026-11-10")

    def test_cache_disabled_always_full_fetch(self):
        data_sources.STOCK_PRICE_CACHE_ENABLED = False
        full = _df([("2026-11-10", 110, 120, 125, 105, 1100)])
        with patch.object(data_sources, "_fetch_stock_price_remote",
                          return_value=(full, 1000, True)) as mock_fetch:
            result = data_sources.get_stock_data("2330")

        mock_fetch.assert_called_once_with(
            "2330", data_sources.STOCK_PRICE_START_DATE)
        self.assertEqual(len(result), 1)
        self.assertEqual(os.listdir(self.tmp.name), [])


class FetchStockPriceRemoteScaleTests(unittest.TestCase):
    """volume_scale 傳入時沿用該量綱，未傳入時以啟發式判定。"""

    PAYLOAD = {"data": [
        {"date": "2026-11-10", "open": 110, "close": 120, "max": 125,
         "min": 105, "Trading_Volume": 2000000},
    ]}

    class _Res:
        status_code = 200

        def json(self):
            return FetchStockPriceRemoteScaleTests.PAYLOAD

    def _fetch(self, volume_scale):
        with patch.object(data_sources, "_record_finmind_request"), \
             patch.object(data_sources.requests, "get",
                          return_value=self._Res()):
            return data_sources._fetch_stock_price_remote(
                "2330", "2026-11-10", volume_scale=volume_scale)

    def test_forced_scale_1000_divides(self):
        df, scale, ok = self._fetch(1000)
        self.assertTrue(ok)
        self.assertEqual(scale, 1000)
        self.assertEqual(df.iloc[0]["volume"], 2000)

    def test_forced_scale_1_keeps_raw(self):
        df, scale, ok = self._fetch(1)
        self.assertTrue(ok)
        self.assertEqual(scale, 1)
        self.assertEqual(df.iloc[0]["volume"], 2000000)

    def test_heuristic_without_forced_scale(self):
        df, scale, ok = self._fetch(None)
        self.assertTrue(ok)
        self.assertEqual(scale, 1000)
        self.assertEqual(df.iloc[0]["volume"], 2000)


class IndexMaintenanceTests(unittest.TestCase):
    SERIES = [("2026-11-10", 167.99), ("2026-11-09", 162.46),
              ("2026-11-06", 151.93)]

    def test_empty_parsed_still_emits_maintenance(self):
        fields = index_data._build_index_chip_fields([], self.SERIES)
        self.assertEqual(fields["margin_maintenance"], 167.99)
        self.assertEqual(fields["margin_maintenance_t0"], 167.99)
        self.assertEqual(fields["margin_maintenance_ma5"],
                         round((167.99 + 162.46 + 151.93) / 3, 2))

    def test_empty_parsed_and_series_returns_empty(self):
        self.assertEqual(index_data._build_index_chip_fields([], []), {})

    def test_on_or_before_uses_latest_available(self):
        parsed = [{"date": "2026-11-10"}, {"date": "2026-11-09"}]
        fields = index_data._build_index_chip_fields(parsed, self.SERIES)
        self.assertEqual(fields["margin_maintenance_t0"], 167.99)
        self.assertEqual(fields["margin_maintenance_t1"], 162.46)

    def test_target_before_all_dates_falls_back_to_nearest(self):
        parsed = [{"date": "2026-11-01"}]
        fields = index_data._build_index_chip_fields(parsed, self.SERIES)
        # 目標日早於所有可得資料時，取時間上最接近的一筆而非空值
        self.assertEqual(fields["margin_maintenance"], 151.93)
        self.assertIsNotNone(fields["margin_maintenance_ma5"])

    def test_fetch_maintenance_series_fallback_parse(self):
        payload = {"data": [
            {"date": "2026-11-10", "TotalExchangeMarginMaintenance": "167.99"},
            {"date": "2026-11-09", "TotalExchangeMarginMaintenance": "162.46"},
        ]}

        class _Res:
            status_code = 200

            @staticmethod
            def json():
                return payload

        with patch.object(index_data, "_refresh_finmind_runtime_auth",
                          return_value=("token", {})), \
             patch.object(index_data, "_record_finmind_request"), \
             patch.object(index_data.requests, "get", return_value=_Res()):
            series = index_data._fetch_maintenance_series()

        self.assertEqual(series, [("2026-11-10", 167.99),
                                  ("2026-11-09", 162.46)])


if __name__ == "__main__":
    unittest.main()
