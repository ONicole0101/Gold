import pandas as pd


def clean_ohlc_data(df):
    """Remove impossible OHLC rows before technical indicator calculations."""
    if df is None or df.empty:
        return df

    data = df.copy()
    price_cols = [col for col in ("open", "close", "max", "min") if col in data.columns]
    for col in price_cols:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    required = [col for col in ("close", "max", "min") if col in data.columns]
    if required:
        valid = data[required].notna().all(axis=1)
        valid &= data[required].gt(0).all(axis=1)
    else:
        valid = pd.Series(True, index=data.index)

    if {"max", "min"}.issubset(data.columns):
        valid &= data["max"] >= data["min"]
    if {"close", "max", "min"}.issubset(data.columns):
        valid &= data["close"].between(data["min"], data["max"])
    if {"open", "max", "min"}.issubset(data.columns):
        valid &= data["open"].between(data["min"], data["max"])

    return data.loc[valid].copy()


def calculate_kd(rsv, initial_value=50.0):
    """Calculate KD with the common Taiwan-market 2/3 + 1/3 recursion."""
    k_values = []
    d_values = []
    prev_k = float(initial_value)
    prev_d = float(initial_value)

    for value in pd.to_numeric(rsv, errors="coerce"):
        if pd.isna(value):
            k_values.append(float("nan"))
            d_values.append(float("nan"))
            continue

        k = (prev_k * 2 + float(value)) / 3
        d = (prev_d * 2 + k) / 3
        k_values.append(k)
        d_values.append(d)
        prev_k = k
        prev_d = d

    return (
        pd.Series(k_values, index=rsv.index, dtype="float64"),
        pd.Series(d_values, index=rsv.index, dtype="float64"),
    )


def add_indicators(df):
    try:
        df = clean_ohlc_data(df)
        if df is None or df.empty:
            return df

        low_min = df['min'].rolling(9).min()
        high_max = df['max'].rolling(9).max()
        denom = (high_max - low_min).mask(lambda s: s == 0)
        rsv = (df['close'] - low_min) / denom * 100
        rsv = pd.to_numeric(rsv, errors="coerce").ffill()
        df['K'], df['D'] = calculate_kd(rsv)

        df['MA5'] = df['close'].rolling(5).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['MA60'] = df['close'].rolling(60).mean()

        # MACD: 用於判斷主升段動能是否翻正、改善或降溫。
        # DIF = EMA12 - EMA26, DEA = DIF 的 9 日 EMA, HIST = DIF - DEA。
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD_DIF'] = ema12 - ema26
        df['MACD_DEA'] = df['MACD_DIF'].ewm(span=9, adjust=False).mean()
        df['MACD_HIST'] = df['MACD_DIF'] - df['MACD_DEA']

        std = df['close'].rolling(20).std()
        df['BB_upper'] = df['MA20'] + 2 * std
        df['BB_lower'] = df['MA20'] - 2 * std

        df['BIAS5'] = (df['close'] - df['MA5']) / df['MA5'] * 100
        df['BIAS20'] = (df['close'] - df['MA20']) / df['MA20'] * 100
        df['BIAS60'] = (df['close'] - df['MA60']) / df['MA60'] * 100

        df['BIAS5_60D_HIGH'] = df['BIAS5'].rolling(60, min_periods=30).max()
        df['BIAS5_60D_LOW'] = df['BIAS5'].rolling(60, min_periods=30).min()
        df['BIAS20_60D_HIGH'] = df['BIAS20'].rolling(60, min_periods=30).max()
        df['BIAS20_60D_LOW'] = df['BIAS20'].rolling(60, min_periods=30).min()
        df['BIAS60_60D_HIGH'] = df['BIAS60'].rolling(60, min_periods=30).max()
        df['BIAS60_60D_LOW'] = df['BIAS60'].rolling(60, min_periods=30).min()

        return df
    except Exception as e:
        print(f'❌ indicator error: {e}')
        return df


def get_kd_trend(df):
    if 'K' not in df.columns or 'D' not in df.columns:
        return {"kd_3d_up": None, "kd_trend": None}
    try:
        last3 = df.tail(3)

        # 資料不足
        if len(last3) < 3:
            return {
                "kd_3d_up": None,
                "kd_trend": None
            }

        k_vals = last3['K'].values
        d_vals = last3['D'].values

        # 避免 NaN
        if pd.isna(k_vals).any() or pd.isna(d_vals).any():
            return {
                "kd_3d_up": None,
                "kd_trend": None
            }

        # === K 三日趨勢 ===
        # Sample === k_vals[0]   # [0]三天前
        # Sample === k_vals[1]   # [1]前一天
        # Sample === k_vals[2]   # [2]最新一天
        k_up = k_vals[2] > k_vals[1] > k_vals[0]
        k_down = k_vals[2] < k_vals[1] < k_vals[0]
        k_up = k_vals[2] > k_vals[1] > k_vals[0]
        k_down = k_vals[2] < k_vals[1] < k_vals[0]

        # === KD 交叉（最重要）===
        cross_up = (k_vals[1] <= d_vals[1]) and (
            k_vals[2] > d_vals[2])     # 黃金交叉
        cross_down = (k_vals[1] >= d_vals[1]) and (
            k_vals[2] < d_vals[2])   # 死亡交叉

        # === 趨勢判斷 ===
        if cross_up:
            trend = "↑"       # 強烈買訊
        elif cross_down:
            trend = "↓"       # 強烈賣訊
        elif k_up:
            trend = "↗"
        elif k_down:
            trend = "↘"
        else:
            trend = "→"

        return {
            "kd_3d_up": k_up if k_up is not None else None,
            "kd_trend": trend,
        }

    except Exception as e:
        print(f"❌ KD trend error: {e}")
        return {
            "kd_3d_up": None,
            "kd_trend": None
        }


def get_MABias(df):
    if len(df) < 60:
        return {
            'ma5': None, 'ma20': None, 'ma60': None,
            'bias5': None, 'bias20': None, 'bias60': None,
            'bias5_min': None, 'bias5_max': None,
            'bias20_min': None, 'bias20_max': None,
            'bias60_min': None, 'bias60_max': None,
        }

    periods = [5, 20, 60]
    stats = {}

    for p in periods:
        ma_series = df['close'].rolling(p).mean()
        ma_value = ma_series.iloc[-1]
        stats[f'ma{p}'] = round(ma_value, 2) if pd.notna(ma_value) else None

        if ma_value == 0 or pd.isna(ma_value):
            stats[f'bias{p}'] = None
            stats[f'bias{p}_min'] = None
            stats[f'bias{p}_max'] = None
            continue

        bias_series = (df['close'] - ma_series) / ma_series * 100
        latest_bias = bias_series.iloc[-1]
        bias_60 = bias_series.iloc[-60:]

        stats[f'bias{p}'] = round(
            latest_bias, 2) if pd.notna(latest_bias) else None
        stats[f'bias{p}_min'] = round(
            bias_60.min(), 2) if bias_60.notna().any() else None
        stats[f'bias{p}_max'] = round(
            bias_60.max(), 2) if bias_60.notna().any() else None

    return stats


def get_bb_trend(df):
    if 'BB_upper' not in df.columns or 'BB_lower' not in df.columns:
        return {"bb_3d_up": None, "bb_trend": None, "bb_score": None}

    last3 = df.tail(3)

    if len(last3) < 3:
        return {"bb_3d_up": None, "bb_trend": None, "bb_score": None}

    def calc_pct(row):
        if pd.notna(row['BB_upper']) and pd.notna(row['BB_lower']) and row['BB_upper'] != row['BB_lower']:
            return (row['close'] - row['BB_lower']) / (row['BB_upper'] - row['BB_lower']) * 100
        return None

    pcts = last3.apply(calc_pct, axis=1).values

    if pd.isna(pcts).any():
        return {"bb_3d_up": None, "bb_trend": None, "bb_score": None}

    up = pcts[2] > pcts[1] > pcts[0]
    down = pcts[2] < pcts[1] < pcts[0]

    if up:
        trend = "↗"
        score = 1
    elif down:
        trend = "↘"
        score = -1
    else:
        trend = "→"
        score = 0

    return {
        "bb_3d_up": up,
        "bb_trend": trend,
        "bb_score": score
    }


def safe_pos(value, low, high):
    if value is None or low is None or high is None or high == low:
        return None
    return (value - low) / (high - low)



def get_support_resistance_levels(
    df,
    lookback_days=None,
    pivot_window=5,
    tolerance_pct=1.2,
    min_distance_pct=0.2,
    price_bands_pct=(8, 15, 25, 40, None),
):
    """
    Use FinMind TaiwanStockPrice OHLCV data to estimate nearby resistance/support.

    Logic:
    - Use all available OHLCV rows by default instead of a fixed day window.
    - Find swing highs as resistance candidates and swing lows as support candidates.
    - Merge nearby prices into clusters by tolerance_pct so repeated tests count as one zone.
    - Report the actual traded high/low from the latest touch in that zone, not a cluster average.
    - Search from the current price zone outward, then prefer the most recent valid cluster.

    Returned prices are rounded to 2 decimals and safe for JSON/template rendering.
    """
    empty = {
        "resistance_price": None,
        "support_price": None,
        "resistance_date": None,
        "support_date": None,
        "resistance_distance_pct": None,
        "support_distance_pct": None,
        "resistance_touch_count": None,
        "support_touch_count": None,
    }

    def _safe_float(value):
        try:
            if pd.isna(value):
                return None
            return float(value)
        except Exception:
            return None

    def _round_or_none(value, ndigits=2):
        value = _safe_float(value)
        if value is None:
            return None
        return round(value, ndigits)

    def _date_text(value):
        try:
            if value is None or pd.isna(value):
                return None
            return pd.Timestamp(value).strftime("%Y-%m-%d")
        except Exception:
            return None

    def _date_rank(value):
        try:
            if value is None or pd.isna(value):
                return 0
            return pd.Timestamp(value).timestamp()
        except Exception:
            return 0

    def _cluster_levels(levels, tolerance):
        """Cluster nearby prices, but keep an actual traded price as the representative."""
        if not levels:
            return []

        levels = sorted(levels, key=lambda x: x["price"])
        clusters = []

        for item in levels:
            price = item["price"]
            weight = max(float(item.get("weight") or 1), 1.0)
            normalized = {
                **item,
                "price": price,
                "weight": weight,
            }
            if not clusters:
                clusters.append({"items": [normalized]})
                continue

            cluster = clusters[-1]
            items = cluster["items"]
            weight_sum = sum(i["weight"] for i in items)
            avg = sum(i["price"] * i["weight"] for i in items) / weight_sum
            if avg and abs(price - avg) / avg <= tolerance:
                items.append(normalized)
            else:
                clusters.append({"items": [normalized]})

        result = []
        for cluster in clusters:
            items = cluster["items"]
            weight_sum = sum(i["weight"] for i in items)
            avg_price = sum(i["price"] * i["weight"] for i in items) / weight_sum
            representative = max(
                items,
                key=lambda i: (
                    _date_rank(i.get("date")),
                    float(i.get("weight") or 0),
                ),
            )
            result.append({
                "price": representative["price"],
                "avg_price": avg_price,
                "touch_count": len(items),
                "weight": weight_sum,
                "last_date": representative.get("date"),
            })
        return result

    def _select_nearest_cluster(candidates, side, latest_close, tolerance):
        side_candidates = []
        min_distance = max(float(min_distance_pct or 0), 0) / 100

        for item in candidates:
            price = _safe_float(item.get("price"))
            if price is None or price <= 0:
                continue
            if side == "resistance":
                distance = (price - latest_close) / latest_close
            else:
                distance = (latest_close - price) / latest_close
            if distance < min_distance:
                continue
            enriched = dict(item)
            enriched["distance_pct"] = distance * 100
            side_candidates.append(enriched)

        if not side_candidates:
            return None

        for band in price_bands_pct:
            if band is None:
                band_candidates = side_candidates
            else:
                band_candidates = [
                    item for item in side_candidates
                    if item["distance_pct"] <= float(band)
                ]
            if not band_candidates:
                continue

            clusters = _cluster_levels(band_candidates, tolerance)
            valid_clusters = []
            for cluster in clusters:
                price = _safe_float(cluster.get("price"))
                if price is None or price <= 0:
                    continue
                if side == "resistance":
                    distance = (price - latest_close) / latest_close
                else:
                    distance = (latest_close - price) / latest_close
                if distance < min_distance:
                    continue
                distance_pct = distance * 100
                if band is not None and distance_pct > float(band):
                    continue
                cluster["distance_pct"] = distance_pct
                valid_clusters.append(cluster)

            if valid_clusters:
                return min(
                    valid_clusters,
                    key=lambda c: (
                        -_date_rank(c.get("last_date")),
                        c["distance_pct"],
                        -int(c.get("touch_count") or 0),
                    ),
                )

        return None

    try:
        if df is None or df.empty:
            return empty
        required = {"close", "max", "min"}
        if not required.issubset(df.columns):
            return empty

        data = clean_ohlc_data(df)
        if data is None or data.empty:
            return empty
        if "date" in data.columns:
            data["date"] = pd.to_datetime(data["date"], errors="coerce")
            data = data.sort_values("date")

        for col in ["close", "max", "min", "volume"]:
            if col in data.columns:
                data[col] = pd.to_numeric(data[col], errors="coerce")

        data = data.dropna(subset=["close", "max", "min"])
        if data.empty:
            return empty

        window = data.copy()
        if lookback_days:
            window = data.tail(max(int(lookback_days), 20)).copy()
        if window.empty:
            return empty

        latest_close = _safe_float(data["close"].iloc[-1])
        if latest_close is None or latest_close <= 0:
            return empty

        pivot_window = max(int(pivot_window or 5), 3)
        if pivot_window % 2 == 0:
            pivot_window += 1
        tolerance = max(float(tolerance_pct or 1.2), 0.1) / 100

        high_roll = window["max"].rolling(pivot_window, center=True, min_periods=3).max()
        low_roll = window["min"].rolling(pivot_window, center=True, min_periods=3).min()
        pivot_highs = window[window["max"].eq(high_roll)].copy()
        pivot_lows = window[window["min"].eq(low_roll)].copy()

        resistance_candidates = []
        support_candidates = []
        resistance_seen = set()
        support_seen = set()

        def _append_candidate(target, seen, row, price_col):
            price = _safe_float(row.get(price_col))
            if price is None or price <= 0:
                return
            volume = _safe_float(row.get("volume")) if "volume" in row.index else None
            date_value = row.get("date") if "date" in row.index else None
            key = (round(price, 4), str(date_value)[:10], price_col)
            if key in seen:
                return
            seen.add(key)
            target.append({
                "price": price,
                "weight": volume if volume and volume > 0 else 1,
                "date": date_value,
            })

        for _, row in pivot_highs.iterrows():
            _append_candidate(resistance_candidates, resistance_seen, row, "max")

        for _, row in pivot_lows.iterrows():
            _append_candidate(support_candidates, support_seen, row, "min")

        def _append_extreme(target, seen, frame, price_col, pick):
            if frame.empty or price_col not in frame.columns:
                return
            series = pd.to_numeric(frame[price_col], errors="coerce").dropna()
            if series.empty:
                return
            idx = series.idxmax() if pick == "max" else series.idxmin()
            _append_candidate(target, seen, frame.loc[idx], price_col)

        for span in (20, 60, 120, None):
            frame = window if span is None else window.tail(min(int(span), len(window)))
            _append_extreme(resistance_candidates, resistance_seen, frame, "max", "max")
            _append_extreme(support_candidates, support_seen, frame, "min", "min")

        resistance = _select_nearest_cluster(resistance_candidates, "resistance", latest_close, tolerance)
        support = _select_nearest_cluster(support_candidates, "support", latest_close, tolerance)

        result = empty.copy()
        if resistance:
            rp = resistance["price"]
            result.update({
                "resistance_price": _round_or_none(rp),
                "resistance_date": _date_text(resistance.get("last_date")),
                "resistance_distance_pct": _round_or_none((rp - latest_close) / latest_close * 100),
                "resistance_touch_count": int(resistance.get("touch_count") or 0),
            })
        if support:
            sp = support["price"]
            result.update({
                "support_price": _round_or_none(sp),
                "support_date": _date_text(support.get("last_date")),
                "support_distance_pct": _round_or_none((latest_close - sp) / latest_close * 100),
                "support_touch_count": int(support.get("touch_count") or 0),
            })
        return result

    except Exception as e:
        print(f"❌ support/resistance error: {e}")
        return empty
