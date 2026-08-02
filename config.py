"""Runtime configuration for report/static generation.

Use the same code in multiple repositories by setting GitHub repository variables
or environment variables instead of editing Python source files.
"""
import os


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


REPORT_TYPE = _env("REPORT_TYPE", "持股")
CSV_FILE = _env("CSV_FILE", "stocks.csv")
REPORT_TITLE = _env("REPORT_TITLE", "(持股)技術分析報告")
OUTPUT_FILE = _env("OUTPUT_FILE", "持股")
REPORT_SUBTITLE = _env("REPORT_SUBTITLE", "")

STATIC_OUTPUT_FILE = _env("STATIC_OUTPUT_FILE", _env(
    "STATIC_CSV_FILE", "AllStatic.csv"))
STATIC_CSV_FILE = STATIC_OUTPUT_FILE

STATIC_VALUATION_OUTPUT_FILE = _env(
    "STATIC_VALUATION_OUTPUT_FILE",
    _env("STATIC_VALUATION_FILE", _env(
        "STATIC_VALUATION_CSV", "AllStatic_Valuation.csv")),
)
STATIC_VALUATION_FILE = STATIC_VALUATION_OUTPUT_FILE
STATIC_VALUATION_CSV = STATIC_VALUATION_OUTPUT_FILE

ALLSTATIC_NEWS_OUTPUT_FILE = _env(
    "ALLSTATIC_NEWS_OUTPUT_FILE",
    _env("ALLSTATIC_NEWS_FILE", _env("ALLSTATIC_NEWS_CSV", "AllStatic_news.csv")),
)
ALLSTATIC_NEWS_FILE = ALLSTATIC_NEWS_OUTPUT_FILE
ALLSTATIC_NEWS_CSV = ALLSTATIC_NEWS_OUTPUT_FILE

NEWS_DAYS = int(_env("NEWS_DAYS", "10"))
NEWS_SLEEP_SEC = float(_env("NEWS_SLEEP_SEC", "0.6"))

STATIC_CHIP_OUTPUT_FILE = _env(
    "STATIC_CHIP_OUTPUT_FILE",
    _env("STATIC_CHIPS_FILE", "AllStatic_Chips.csv"),
)
# Backward-compatible alias for older code.
STATIC_CHIPS_OUTPUT_FILE = STATIC_CHIP_OUTPUT_FILE
STATIC_CHIPS_FILE = STATIC_CHIP_OUTPUT_FILE

CHIP_TREND_DAYS = int(_env("CHIP_TREND_DAYS", "3"))
CHIP_CONCENTRATION_THRESHOLD = float(
    _env("CHIP_CONCENTRATION_THRESHOLD", "15"))
CHIP_LOOKBACK_DAYS = int(_env("CHIP_LOOKBACK_DAYS", "21"))
CHIP_WORKERS = int(_env("CHIP_WORKERS", "6"))
CHIP_SLEEP_SEC = float(_env("CHIP_SLEEP_SEC", "0.05"))
