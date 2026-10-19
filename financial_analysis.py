from datetime import datetime

import pandas as pd

from data_sources import (
    get_balance_sheet_raw,
    get_eps_raw,
    get_profit_ratio as get_profit_ratio_raw,
    get_revenue_raw,
    get_shareholding_raw,
)


def safe_margin(num, denom):
    if num is None or denom is None or denom <= 0:
        return None
    return round(num / denom * 100, 2)


def calc_diff(a, b):
    if a is None or b is None:
        return None
    return round(a - b, 2)


def fmt(v):
    return '-' if v is None else v


def build_output(result):
    cur = result['current']
    prev = result['prev']
    yoy = result['yoy']
    qoq = result['qoq']
    yoy_diff = result['yoy_diff']

    return {
        'gross_margin': cur['gross'],
        'gross_margin_prev': prev['gross'],
        'gross_margin_yoy': yoy['gross'],
        'gross_margin_qoq': qoq['gross'],
        'gross_margin_yoy_diff': yoy_diff['gross'],
        'gross_margin_combined': f"{fmt(cur['gross'])} / {fmt(prev['gross'])} / {fmt(yoy['gross'])}",
        'operating_margin': cur['op'],
        'operating_margin_prev': prev['op'],
        'operating_margin_yoy': yoy['op'],
        'operating_margin_qoq': qoq['op'],
        'operating_margin_yoy_diff': yoy_diff['op'],
        'operating_margin_combined': f"{fmt(cur['op'])} / {fmt(prev['op'])} / {fmt(yoy['op'])}",
        'net_margin': cur['net'],
        'net_margin_prev': prev['net'],
        'net_margin_yoy': yoy['net'],
        'net_margin_qoq': qoq['net'],
        'net_margin_yoy_diff': yoy_diff['net'],
        'net_margin_combined': f"{fmt(cur['net'])} / {fmt(prev['net'])} / {fmt(yoy['net'])}",
    }


def _normalize_metric_name(value) -> str:
    """Normalize FinMind financial-statement metric labels for exact matching."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    replacements = {
        "（": "(",
        "）": ")",
        "％": "%",
        " ": "",
        "\u3000": "",
        "－": "-",
        "—": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _metric_name_mask(df: pd.DataFrame, names: list[str]):
    """Exact-match FinMind metric by type/name/origin_name after normalization.

    Do not use contains(). Labels such as 營業收入, 毛利, 毛利率, 淨利 and
    淨利率 overlap; substring matching can select the wrong row and explode
    EPS/margins/QoQ/YoY.
    """
    mask = pd.Series(False, index=df.index)
    clean_names = {_normalize_metric_name(name) for name in names}
    for col in ("type", "name", "origin_name"):
        if col in df.columns:
            s = df[col].map(_normalize_metric_name)
            mask = mask | s.isin(clean_names)
    return mask


def _standardize_financial_df(data) -> pd.DataFrame:
    df = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data or [])
    if df.empty:
        return pd.DataFrame()
    if not {"date", "value"}.issubset(df.columns):
        raise ValueError("financial statements: missing date/value columns")
    if not {"type", "name", "origin_name"}.intersection(df.columns):
        raise ValueError("financial statements: missing metric columns")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    if df[["date", "value"]].isna().any().any():
        raise ValueError("financial statements: invalid date/value")
    return df.sort_values("date", kind="stable")


def _series_by_metric(df: pd.DataFrame, aliases: list[str]) -> pd.Series:
    """Return one value per statement date for a strictly matched metric."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    m = df.loc[_metric_name_mask(df, aliases), ["date", "value"]].copy()
    if m.empty:
        return pd.Series(dtype="float64")
    if "type" in df.columns:
        source_types = df.loc[m.index, "type"].map(_normalize_metric_name)
        m = m.loc[~source_types.str.endswith("_per", na=False)]
        if m.empty:
            return pd.Series(dtype="float64")
    m["value"] = pd.to_numeric(m["value"], errors="coerce")
    m = m.dropna(subset=["date", "value"]).sort_values("date")
    if m.empty:
        return pd.Series(dtype="float64")
    # Prefer canonical type aliases over overlapping translated labels.
    m["priority"] = len(aliases)
    for priority, alias in enumerate(aliases):
        mask = _metric_name_mask(df.loc[m.index], [alias])
        m.loc[mask, "priority"] = m.loc[mask, "priority"].clip(upper=priority)
    m["period"] = m["date"].dt.to_period("Q")
    return (
        m.sort_values("priority", ascending=False, kind="stable")
         .drop_duplicates("period", keep="last")
         .set_index("date")["value"]
         .sort_index()
         .astype(float)
    )


def get_share_turnover(stock_id, trading_volumes):
    """Calculate turnover from TaiwanStockShareholding.NumberOfSharesIssued."""
    try:
        volumes = list(trading_volumes) if isinstance(
            trading_volumes, (list, tuple)) else [trading_volumes]
        df = get_shareholding_raw(stock_id)
        if df is None or df.empty:
            return {
                "turnover_pct_t0": None,
                "turnover_pct_t1": None,
                "turnover_pct_t2": None,
                "shares_outstanding": None,
                "treasury_shares": None,
                "circulating_shares": None,
            }

        if "NumberOfSharesIssued" in df.columns:
            issued = df[[
                c for c in ("date", "NumberOfSharesIssued") if c in df.columns
            ]].copy()
            issued = issued.rename(columns={"NumberOfSharesIssued": "value"})
        elif "type" in df.columns and "value" in df.columns:
            issued = df.loc[
                df["type"].astype(str).str.strip().eq("NumberOfSharesIssued")
            ].copy()
        else:
            issued = pd.DataFrame()
        if issued.empty:
            print(
                f"⚠️ TaiwanStockShareholding {stock_id} lacks NumberOfSharesIssued; "
                f"columns={list(df.columns)}"
            )
            return {
                "turnover_pct_t0": None,
                "turnover_pct_t1": None,
                "turnover_pct_t2": None,
                "shares_outstanding": None,
                "treasury_shares": None,
                "circulating_shares": None,
            }
        if "date" in issued.columns:
            issued["date"] = pd.to_datetime(issued["date"], errors="coerce")
            issued = issued.sort_values("date")
        shares_outstanding = pd.to_numeric(
            issued.iloc[-1]["value"], errors="coerce")
        if pd.isna(shares_outstanding) or shares_outstanding <= 0:
            shares_outstanding = None
        circulating_shares = float(
            shares_outstanding) if shares_outstanding is not None else None
        treasury_shares = 0.0
        turnover_pct = []
        for volume in volumes[:3]:
            if volume is None or pd.isna(volume) or circulating_shares in (None, 0):
                turnover_pct.append(None)
            else:
                turnover_pct.append(
                    round(float(volume) * 1000 / circulating_shares * 100, 2))
        turnover_pct += [None] * (3 - len(turnover_pct))

        return {
            "turnover_pct_t0": turnover_pct[0],
            "turnover_pct_t1": turnover_pct[1],
            "turnover_pct_t2": turnover_pct[2],
            "shares_outstanding": shares_outstanding,
            "treasury_shares": treasury_shares,
            "circulating_shares": circulating_shares,
        }
    except Exception as e:
        print(f"❌ share turnover error {stock_id}: {e}")
        return {
            "turnover_pct_t0": None,
            "turnover_pct_t1": None,
            "turnover_pct_t2": None,
            "shares_outstanding": None,
            "treasury_shares": None,
            "circulating_shares": None,
        }


def _normalize_percent_series(s: pd.Series, max_abs: float = 200.0) -> pd.Series:
    """Normalize percentage values and remove impossible polluted rows."""
    if s is None or s.empty:
        return pd.Series(dtype="float64")
    s = pd.to_numeric(s, errors="coerce").dropna().astype(float)
    if s.empty:
        return s

    # Amount-derived margins and FinMind ratio aliases are percentage points.
    s = s.round(2)
    return s[s.between(-max_abs, max_abs)]


def _calc_current_qoq_yoy(s: pd.Series, latest_statement_date=None) -> dict:
    """Calculate current value, QoQ and YoY as percentage-point differences."""
    s = _normalize_percent_series(s)
    if s.empty:
        return {"current": None, "prev": None, "yoy": None, "qoq": None, "yoy_diff": None, "is_prev": False}

    current_date = s.index.max()
    current = float(s.loc[current_date])

    quarters = s.copy()
    quarters.index = quarters.index.to_period("Q")
    current_period = current_date.to_period("Q")
    prev = quarters.get(current_period - 1)
    yoy = quarters.get(current_period - 4)

    if latest_statement_date is None:
        latest_statement_date = current_date

    return {
        "current": round(current, 2),
        "prev": round(prev, 2) if prev is not None else None,
        "yoy": round(yoy, 2) if yoy is not None else None,
        "qoq": calc_diff(current, prev),
        "yoy_diff": calc_diff(current, yoy),
        "is_prev": bool(current_period < latest_statement_date.to_period("Q")),
    }


def get_profit_ratio(stock_id, *, raise_errors=False):
    """
    Return latest gross/operating/net margins.

    Project rule:
    1. The displayed margins are percentage points, for example 23.32.
    2. QoQ/YoY are percentage-point differences, for example +3.75.
    3. Prefer amount-account calculation from FinMind financial statements:
       Revenue / GrossProfit / OperatingIncome / IncomeAfterTaxes.
    4. Use precomputed ratio rows only as fallback.
    5. Never substring-match metric names.
    """
    try:
        df = _standardize_financial_df(get_profit_ratio_raw(stock_id))
        if df.empty:
            return None

        latest_statement_date = df["date"].max()

        amount_aliases = {
            "Revenue": [
                "Revenue",
                "營業收入",
                "營業收入合計",
                "營業收入淨額",
                "營業收益",
                "收益",
                "收入",
            ],
            "GrossProfit": [
                "GrossProfit",
                "營業毛利",
                "營業毛利(毛損)",
                "營業毛利（毛損）",
            ],
            "OperatingIncome": [
                "OperatingIncome",
                "營業利益",
                "營業利益(損失)",
                "營業利益（損失）",
            ],
            "IncomeAfterTaxes": [
                "IncomeAfterTaxes",
                "IncomeAfterTax",
                "NetIncome",
                "ProfitLoss",
                "本期淨利",
                "本期淨利(淨損)",
                "本期淨利（淨損）",
                "稅後淨利",
                "繼續營業單位稅後淨利(淨損)",
                "繼續營業單位稅後淨利（淨損）",
            ],
        }

        amount_series = {k: _series_by_metric(
            df, v) for k, v in amount_aliases.items()}
        revenue = amount_series["Revenue"]

        def calc_margin_from_amount(numerator_key: str) -> pd.Series:
            numerator = amount_series.get(
                numerator_key, pd.Series(dtype="float64"))
            if revenue.empty or numerator.empty:
                return pd.Series(dtype="float64")

            merged = pd.concat(
                [revenue.rename("revenue"), numerator.rename("numerator")],
                axis=1,
                join="inner",
            ).dropna()
            if merged.empty:
                return pd.Series(dtype="float64")

            merged = merged[merged["revenue"] > 0]
            if merged.empty:
                return pd.Series(dtype="float64")

            s = (merged["numerator"] / merged["revenue"] * 100).round(2)
            return s[s.between(-200.0, 200.0)]

        # Amount-based calculation is the primary path. Units cancel out.
        gross_s = calc_margin_from_amount("GrossProfit")
        op_s = calc_margin_from_amount("OperatingIncome")
        net_s = calc_margin_from_amount("IncomeAfterTaxes")

        # Fill missing quarters per metric; amount-derived values take precedence.
        ratio_aliases = {
            "gross": [
                "GrossMargin",
                "gross_margin",
                "毛利率",
                "營業毛利率",
            ],
            "op": [
                "OperatingMargin",
                "operating_margin",
                "營業利益率",
                "營益率",
                "營業利益率(%)",
                "營益率(%)",
            ],
            "net": [
                "NetMargin",
                "net_margin",
                "稅後淨利率",
                "淨利率",
                "稅後淨利率(%)",
                "淨利率(%)",
            ],
        }

        def fill_ratio_periods(amounts, aliases):
            ratios = _series_by_metric(df, aliases)
            amounts = amounts.copy()
            amounts.index = pd.DatetimeIndex(amounts.index).to_period("Q")
            ratios.index = pd.DatetimeIndex(ratios.index).to_period("Q")
            combined = amounts.combine_first(ratios).sort_index()
            combined.index = combined.index.to_timestamp(how="end").normalize()
            return combined

        gross_s = fill_ratio_periods(gross_s, ratio_aliases["gross"])
        op_s = fill_ratio_periods(op_s, ratio_aliases["op"])
        net_s = fill_ratio_periods(net_s, ratio_aliases["net"])

        gross = _calc_current_qoq_yoy(gross_s, latest_statement_date)
        op = _calc_current_qoq_yoy(op_s, latest_statement_date)
        net = _calc_current_qoq_yoy(net_s, latest_statement_date)

        return {
            "current": {"gross": gross["current"], "op": op["current"], "net": net["current"]},
            "prev": {"gross": gross["prev"], "op": op["prev"], "net": net["prev"]},
            "yoy": {"gross": gross["yoy"], "op": op["yoy"], "net": net["yoy"]},
            "qoq": {"gross": gross["qoq"], "op": op["qoq"], "net": net["qoq"]},
            "yoy_diff": {"gross": gross["yoy_diff"], "op": op["yoy_diff"], "net": net["yoy_diff"]},
            "is_prev": {"gross": gross["is_prev"], "op": op["is_prev"], "net": net["is_prev"]},
        }
    except Exception as e:
        if raise_errors:
            raise
        print(f'❌ profit error {stock_id}: {e}')
        return None


def _calc_roe(net_income: float | None, equity_start: float | None, equity_end: float | None):
    if net_income is None or equity_start is None or equity_end is None:
        return None
    avg_equity = (float(equity_start) + float(equity_end)) / 2
    if avg_equity <= 0:
        return None
    return round(float(net_income) / avg_equity * 100, 2)


def _value_in_quarter(s: pd.Series, period):
    if s is None or s.empty or period is None:
        return None
    values = s.loc[s.index.to_period("Q") == period]
    if values.empty:
        return None
    return float(values.iloc[-1])


def _latest_complete_statement_year(net_income: pd.Series, latest_statement_date) -> int | None:
    if net_income is None or net_income.empty:
        return None

    counts = net_income.groupby(net_income.index.year).count()
    complete_years = sorted(int(year)
                            for year, count in counts.items() if int(count) >= 4)
    if not complete_years:
        return None

    latest_year = int(latest_statement_date.year)
    latest_quarter = int(latest_statement_date.quarter)
    target_year = latest_year if latest_quarter == 4 else latest_year - 1
    if target_year in complete_years:
        return target_year

    older = [year for year in complete_years if year <= target_year]
    return max(older) if older else max(complete_years)


def get_roe_analysis(stock_id, *, raise_errors=False):
    """
    Return last complete year ROE and trailing-twelve-month ROE.

    ROE is calculated from financial-statement amount accounts:
    net income / average equity * 100. Units cancel out as long as both accounts
    come from FinMind statement rows using the same unit scale.
    """
    try:
        income_df = _standardize_financial_df(get_profit_ratio_raw(stock_id))
        equity_df = _standardize_financial_df(get_balance_sheet_raw(stock_id))
        if income_df.empty or equity_df.empty:
            return {"roe_last_year": None, "roe_ttm": None}

        latest_statement_date = income_df["date"].max()

        total_income_aliases = [
            "IncomeAfterTaxes",
            "IncomeAfterTax",
            "NetIncome",
            "ProfitLoss",
            "本期淨利",
            "本期淨利(淨損)",
            "本期淨利（淨損）",
            "稅後淨利",
            "本期稅後淨利",
            "本期稅後淨利(淨損)",
            "本期稅後淨利（淨損）",
        ]
        parent_income_aliases = [
            "EquityAttributableToOwnersOfParent",
            "ProfitLossAttributableToOwnersOfParent",
            "母公司業主淨利",
            "歸屬於母公司業主之淨利",
            "歸屬於母公司業主之淨利(損)",
            "歸屬於母公司業主之淨利（損）",
            "本期淨利(淨損)歸屬於母公司業主",
            "本期淨利（淨損）歸屬於母公司業主",
        ]
        parent_equity_aliases = [
            "EquityAttributableToOwnersOfParent",
            "TotalEquityAttributableToOwnersOfParent",
            "母公司業主權益合計",
            "歸屬於母公司業主之權益",
            "歸屬於母公司業主之權益合計",
            "權益總計歸屬於母公司業主",
            "權益總額歸屬於母公司業主",
        ]
        total_equity_aliases = [
            "Equity",
            "TotalEquity",
            "權益總計",
            "權益總額",
            "權益合計",
            "股東權益總計",
            "股東權益總額",
            "股東權益合計",
        ]

        net_income = _series_by_metric(income_df, parent_income_aliases)
        equity = _series_by_metric(equity_df, parent_equity_aliases)
        if net_income.empty or equity.empty:
            net_income = _series_by_metric(income_df, total_income_aliases)
            equity = _series_by_metric(equity_df, total_equity_aliases)
        if net_income.empty or equity.empty:
            return {"roe_last_year": None, "roe_ttm": None}

        net_income = net_income.dropna().sort_index()
        equity = equity.dropna().sort_index()

        target_year = _latest_complete_statement_year(
            net_income, latest_statement_date)
        roe_last_year = None
        if target_year is not None:
            yearly_income = net_income.loc[net_income.index.year == target_year]
            if len(yearly_income) >= 4:
                income_sum = float(yearly_income.sum())
                year_end = yearly_income.index.max()
                equity_start = _value_in_quarter(
                    equity, pd.Period(f"{target_year - 1}Q4")
                )
                equity_end = _value_in_quarter(equity, year_end.to_period("Q"))
                roe_last_year = _calc_roe(income_sum, equity_start, equity_end)

        roe_ttm = None
        latest4 = net_income.tail(4)
        latest_period = latest_statement_date.to_period("Q")
        expected_periods = pd.period_range(end=latest_period, periods=4, freq="Q")
        if list(latest4.index.to_period("Q")) == list(expected_periods):
            ttm_income = float(latest4.sum())
            equity_start = _value_in_quarter(equity, expected_periods[0] - 1)
            equity_end = _value_in_quarter(equity, latest_period)
            roe_ttm = _calc_roe(ttm_income, equity_start, equity_end)

        return {
            "roe_last_year": roe_last_year,
            "roe_ttm": roe_ttm,
        }

    except Exception as e:
        if raise_errors:
            raise
        print(f"❌ ROE error {stock_id}: {e}")
        return {"roe_last_year": None, "roe_ttm": None}


def extract_metric(res, key):
    if not res:
        return None, None, None
    return (
        res.get('current', {}).get(key),
        res.get('qoq', {}).get(key),
        res.get('yoy_diff', {}).get(key),
    )


def extract_metric_is_prev(res, key):
    if not res:
        return False
    return bool(res.get('is_prev', {}).get(key, False))


def get_eps_analysis(stock_id, current_price=None, *, raise_errors=False):
    """
    EPS from FinMind TaiwanStockFinancialStatements.

    Project rule:
        - eps_Y is the cumulative EPS announced in the current calendar year.
        - eps_Y_quarters records how many quarters have been announced this year.
    - eps_ttm is the latest four quarters total.
    - PER is calculated only when current_price is provided.
    """
    try:
        df = _standardize_financial_df(get_eps_raw(stock_id))
        if df.empty:
            return (None, None, None, None, False, False)

        eps_aliases = [
            "EPS",
            "BasicEPS",
            "basic_eps",
            "基本每股盈餘",
            "基本每股盈餘(元)",
            "基本每股盈餘（元）",
            "每股盈餘",
        ]
        eps_mask = _metric_name_mask(df, eps_aliases)
        eps_df = df.loc[eps_mask, ["date", "value"]].copy()

        # Avoid diluted EPS if both basic and diluted are present.
        for col in ("type", "name", "origin_name"):
            if col in df.columns and not eps_df.empty:
                labels = df.loc[eps_df.index, col].map(_normalize_metric_name)
                diluted_mask = labels.str.contains(
                    "稀釋", na=False) | labels.str.contains("diluted", na=False)
                if diluted_mask.any() and (~diluted_mask).any():
                    eps_df = eps_df.loc[~diluted_mask]

        if eps_df.empty:
            return (None, None, None, None, False, False)

        eps_df["value"] = pd.to_numeric(eps_df["value"], errors="coerce")
        eps_df = eps_df.dropna(subset=["date", "value"]).sort_values("date")
        if eps_df.empty:
            return (None, None, None, None, False, False)

        eps_df["year"] = eps_df["date"].dt.year.astype(int)
        eps_df["season"] = eps_df["date"].dt.quarter.astype(int)
        eps_df = (
            eps_df.drop_duplicates(["year", "season"], keep="last")
                  .sort_values("date")
                  .reset_index(drop=True)
        )
        if eps_df.empty:
            return (None, None, None, None, False, False)

        latest_eps_date = eps_df["date"].max()
        current_year = datetime.now().year
        current_year_eps = eps_df[eps_df["year"] == current_year]
        eps_y_quarters = int(len(current_year_eps))
        eps_year = round(
            float(current_year_eps["value"].sum()), 2) if eps_y_quarters else 0

        latest4 = eps_df.tail(4)
        if len(latest4) >= 4:
            eps_ttm = round(float(latest4["value"].sum()), 2)
            eps_ttm_is_prev = bool(latest4["date"].max() < latest_eps_date)
        else:
            eps_ttm = round(float(latest4["value"].sum()), 2) if len(
                latest4) else 0
            eps_ttm_is_prev = False

        # EPS YoY: current TTM vs. the TTM ending one year (4 quarters) earlier.
        latest8 = eps_df.tail(8)
        eps_yoy = None
        if len(latest8) >= 8:
            ttm_prev = float(latest8.iloc[:4]["value"].sum())
            if ttm_prev != 0:
                eps_yoy = round((eps_ttm - ttm_prev) / abs(ttm_prev) * 100, 2)

        def calc_per(price, eps):
            try:
                price = float(price)
                eps = float(eps)
            except (TypeError, ValueError):
                return None
            return round(price / eps, 2) if price > 0 and eps > 0 else None

        per_y = calc_per(current_price, eps_year)
        per_ttm = calc_per(current_price, eps_ttm)

        return eps_year, eps_ttm, per_y, per_ttm, False, eps_ttm_is_prev, eps_y_quarters, eps_yoy

    except Exception as e:
        if raise_errors:
            raise
        print(f"❌ EPS error {stock_id}: {e}")
        return (None, None, None, None, False, False, None, None)


def calc_margin_score(gross, op, net):
    score = 0
    if gross is not None:
        score += gross * 0.4
    if op is not None:
        score += op * 0.3
    if net is not None:
        score += net * 0.3
    return round(score, 2)


def calc_eps_score(eps_last, eps_ttm):
    if eps_last is None or eps_ttm is None or eps_last <= 0:
        return 0
    growth = (eps_ttm - eps_last) / eps_last * 100
    return round(growth, 2)


def calc_trend_score(qoq_g, yoy_g, qoq_n, yoy_n):
    vals = [qoq_g, yoy_g, qoq_n, yoy_n]
    vals = [v for v in vals if v is not None]
    if not vals:
        return 0
    return round(sum(vals) / len(vals), 2)
