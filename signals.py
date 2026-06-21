from technical_indicators import safe_pos


def _num(x):
    """Return x if it is a usable number, otherwise None."""
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


SUPPRESSED_CHIP_REASON_PREFIXES = (
    '籌碼震盪（',
    '籌碼混亂（',
)

SUPPRESSED_CHIP_REASON_TEXTS = {
    '籌碼震盪',
    '籌碼震盪，方向未定',
    '籌碼震盪，方向未定。',
    '籌碼震盪：籌碼震盪，方向未定。',
    '籌碼混亂：主力買賣互抵且價格無明確方向。',
}


def _clean_reason_text(reason):
    """Normalize one reason and suppress noisy neutral chip fallback text."""
    text = str(reason or '').strip()
    if not text:
        return ''

    # 這兩種是籌碼情境分類的保守 fallback。以前每次都 append，
    # 導致幾乎每檔都出現「籌碼震盪（中性）：籌碼震盪，方向未定。」。
    if text in SUPPRESSED_CHIP_REASON_TEXTS:
        return ''
    if any(text.startswith(prefix) for prefix in SUPPRESSED_CHIP_REASON_PREFIXES):
        return ''

    return text


def _dedupe_reasons(reasons):
    cleaned = []
    seen = set()
    for reason in reasons or []:
        text = _clean_reason_text(reason)
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _join_reasons(reasons):
    values = _dedupe_reasons(reasons)
    return ' / '.join(values) if values else '訊號尚未明確'


def _format_signal_sections(tech_reasons, chip_reasons):
    """Build message text in two sections for downstream display."""
    tech_text = _join_reasons(tech_reasons)
    chip_text = _join_reasons(chip_reasons)
    return f"技術面：{tech_text}\n籌碼面：{chip_text}"


def _calc_chip_scenario(
    main_buy_days=None,
    main_sell_days=None,
    main_net_3d=None,
    price_up=False,
    price_down=False,
    price_flat=False,
    volume_up=False,
    price_volume_state=None,
    position_zone=None,
    close_position=None,
):
    """Classify 3-day main-force chip scenario.

    Returns scenario / bias / description. The wording avoids trade-action
    conclusions and is intended for the B)籌碼面段落.
    """
    try:
        main_buy_days = int(main_buy_days) if main_buy_days is not None else 0
    except Exception:
        main_buy_days = 0
    try:
        main_sell_days = int(main_sell_days) if main_sell_days is not None else 0
    except Exception:
        main_sell_days = 0

    main_net_3d = _num(main_net_3d)
    price_volume_state = price_volume_state or ''
    zone_text = str(close_position or position_zone or '')

    high_zone = ('高檔' in zone_text) or ('頂部' in zone_text)
    low_zone = ('低檔' in zone_text) or ('底部' in zone_text)
    range_or_flat = bool(price_flat) or price_volume_state in ('價平量增', '價平量縮', '價量中性')
    price_up_or_high_range = bool(price_up) or (high_zone and range_or_flat)
    volume_expanding = bool(volume_up) or price_volume_state in ('價漲量增', '價跌量增', '價平量增')

    if high_zone and main_sell_days >= 2:
        return {
            'scenario': '高檔主力賣',
            'bias': '偏空',
            'description': '高檔區搭配主力連續賣超，籌碼風險較高。',
        }
    if low_zone and main_buy_days >= 2:
        return {
            'scenario': '低檔主力買',
            'bias': '偏多',
            'description': '低檔區搭配主力連續買超，後續有轉強機會。',
        }
    if main_buy_days >= 2 and price_up and volume_expanding:
        return {
            'scenario': '主力進場攻擊',
            'bias': '偏多',
            'description': '主力連續買超，股價上漲且成交量放大，短線有續攻機會。',
        }
    if main_buy_days >= 2 and (price_down or range_or_flat):
        return {
            'scenario': '主力低接吸籌',
            'bias': '偏多',
            'description': '主力連續買超，但股價小跌或盤整，可能在壓盤吃貨。',
        }
    if main_sell_days >= 1 and price_up_or_high_range:
        return {
            'scenario': '主力拉高出貨',
            'bias': '偏空',
            'description': '主力轉賣，但股價仍上漲或高檔震盪，留意隔日反轉風險。',
        }
    if main_sell_days >= 2 and price_down and volume_expanding:
        return {
            'scenario': '主力倒貨下殺',
            'bias': '偏空',
            'description': '主力連續賣超，股價下跌且成交量放大，短線弱勢。',
        }
    if price_up and main_net_3d is not None and main_net_3d < 0:
        return {
            'scenario': '價漲籌碼不跟',
            'bias': '中性偏空',
            'description': '股價上漲但主力淨賣超，可能是散戶追價。',
        }
    if price_down and main_net_3d is not None and main_net_3d > 0:
        return {
            'scenario': '價跌主力買',
            'bias': '中性偏多',
            'description': '股價下跌但主力淨買超，可能是洗盤或承接。',
        }
    if (main_net_3d is None or abs(main_net_3d) == 0 or (main_buy_days > 0 and main_sell_days > 0)) and range_or_flat:
        return {
            'scenario': '籌碼混亂',
            'bias': '中性',
            'description': '主力買賣互抵且價格無明確方向。',
        }
    return {
        'scenario': '籌碼震盪',
        'bias': '中性',
        'description': '籌碼震盪，方向未定。',
    }


def _calc_price_volume_state(chgPct, amp, volume, prev_volume, prev2_volume, volume_ok=None):
    """
    價量關係：
    - 價漲量增：偏多，底部/上漲途中較有利
    - 價漲量縮：可能反彈或追價力道不足
    - 價跌量增：偏空，頂部/下跌途中較危險
    - 價跌量縮：跌勢趨緩，底部區可觀察是否止穩
    - 價平量增：多空換手，需看位階
    - 價平量縮：盤整
    """
    chgPct = _num(chgPct)
    amp = _num(amp)
    volume = _num(volume)
    prev_volume = _num(prev_volume)
    prev2_volume = _num(prev2_volume)

    price_up = chgPct is not None and chgPct > 0.5
    price_down = chgPct is not None and chgPct < -0.5
    price_flat = chgPct is not None and abs(chgPct) <= 0.5

    volume_2day_up = False
    volume_up = False
    volume_down = False
    volume_shrink = False
    volume_not_bad = False
    volume_spike = False

    if None not in (volume, prev_volume):
        volume_up = volume > prev_volume * 1.05
        volume_down = volume < prev_volume * 0.95
        volume_shrink = volume < prev_volume * 0.85
        volume_not_bad = volume >= prev_volume * 0.9
        volume_spike = volume >= prev_volume * 1.5

    if None not in (volume, prev_volume, prev2_volume):
        volume_2day_up = volume > prev_volume > prev2_volume
    elif volume_ok is not None:
        volume_2day_up = bool(volume_ok)
        volume_up = bool(volume_ok)
        volume_not_bad = bool(volume_ok)

    if price_up and volume_up:
        state = '價漲量增'
    elif price_up and volume_down:
        state = '價漲量縮'
    elif price_down and volume_up:
        state = '價跌量增'
    elif price_down and volume_down:
        state = '價跌量縮'
    elif price_flat and volume_up:
        state = '價平量增'
    elif price_flat and volume_down:
        state = '價平量縮'
    else:
        state = '價量中性'

    return {
        'state': state,
        'price_up': price_up,
        'price_down': price_down,
        'price_flat': price_flat,
        'volume_2day_up': volume_2day_up,
        'volume_up': volume_up,
        'volume_down': volume_down,
        'volume_shrink': volume_shrink,
        'volume_not_bad': volume_not_bad,
        'volume_spike': volume_spike,
    }


def _calc_position_zone(
    close,
    bb_pct,
    bias_low_zone,
    bias_high_zone,
    kd_low,
    kd_high,
    above_ma18,
    below_ma18,
    ma18_break,
    ma18_fall_break,
    kd_turn_strong,
    kd_turn_weak,
    k_trend_up,
    k_trend_down,
    ma18_up=False,
    ma50_up=False,
    above_ma50=False,
    below_ma50=False,
):
    """
    股價位階粗分：
    - 底部區域：布林低檔 / 乖離低檔 / KD低檔
    - 上漲途中：站上月線且動能偏強，或剛突破月線
    - 頂部區域：布林高檔 / 乖離高檔 / KD高檔
    - 下跌途中：跌破月線或在月線下且動能偏弱

    聯發科 2454 檢討修正：
    - 剛站回月線、KD轉強、價量轉強時，優先視為起漲/上漲途中。
    - KD高檔不等於頂部；必須搭配乖離/布林過熱或趨勢跌破才視為頂部。
    """
    bb_pct = _num(bb_pct)

    bb_low = bb_pct is not None and bb_pct < 20
    bb_mid_low = bb_pct is not None and 20 <= bb_pct <= 50
    bb_mid = bb_pct is not None and 35 <= bb_pct <= 80
    bb_high = bb_pct is not None and bb_pct > 80
    bb_overheat = bb_pct is not None and bb_pct > 95

    early_uptrend = ma18_break and (kd_turn_strong or k_trend_up)
    trend_supported = above_ma18 and (ma18_up or ma50_up or above_ma50)

    # 下跌途中優先於底部，避免「跌破後還沒止穩」被誤判為低接。
    if ma18_fall_break or (below_ma18 and (kd_turn_weak or k_trend_down)):
        zone = '下跌途中'
    # 起漲與上漲途中優先於 KD 高檔，避免聯發科 4 月剛啟動時被太早調節。
    elif early_uptrend or (above_ma18 and (k_trend_up or kd_turn_strong) and not bb_overheat):
        zone = '上漲途中'
    elif trend_supported and not (bb_overheat and bias_high_zone):
        zone = '上漲途中'
    elif bb_overheat or (bias_high_zone and kd_high):
        zone = '頂部區域'
    elif bb_low or bias_low_zone or kd_low:
        zone = '底部區域'
    elif above_ma18:
        zone = '上漲途中'
    elif below_ma18 or below_ma50:
        zone = '下跌途中'
    else:
        zone = '盤整區域'

    return {
        'zone': zone,
        'bb_low': bb_low,
        'bb_mid_low': bb_mid_low,
        'bb_mid': bb_mid,
        'bb_high': bb_high,
        'bb_overheat': bb_overheat,
    }


def get_tech_signal(
    close,
    chgPct,
    amp,
    volume_ok=None,
    volume=None,
    prev_volume=None,
    prev2_volume=None,
    k=None,
    d=None,
    prev_k=None,
    prev_d=None,
    bb_pct=None,
    bias6=None,
    bias18=None,
    bias50=None,
    bias6_min=None,
    bias6_max=None,
    bias18_min=None,
    bias18_max=None,
    bias50_min=None,
    bias50_max=None,
    ma18=None,
    prev_ma18=None,
    prev_close=None,
    k_trend=None,
    d_trend=None,
    # 以下為向下相容的選填欄位；呼叫端沒有給也不影響原本功能。
    ma6=None,
    prev_ma6=None,
    ma50=None,
    prev_ma50=None,
    macd_hist=None,
    prev_macd_hist=None,
    chip_signal_state=None,
    chip_signal_text=None,
    chip_concentration_score=None,
    main_force_score=None,
    broker_diff_score=None,
    chip_concentration_pct=None,
    chip_trend_days=None,
    chip_concentration_threshold=None,
    # 三日籌碼情境判斷欄位；沒有傳入時仍維持原本籌碼分數邏輯。
    main_buy_days=None,
    main_sell_days=None,
    main_net_3d=None,
    avg_volume_3d=None,
    price_change_3d=None,
    volume_change_3d=None,
    close_position=None,
    repeat_buy_brokers=None,
    repeat_sell_brokers=None,
):
    """
    技術訊號主邏輯。

    2026 聯發科/廣達檢討改版重點：
    1. 先判斷「位階」：底部區域 / 上漲途中 / 頂部區域 / 下跌途中 / 盤整區域
    2. 再判斷「價量關係」：價漲量增 / 價漲量縮 / 價跌量增 / 價跌量縮 / 價平量增 / 價平量縮
    3. 最後才用 KD、月線、布林、乖離、MACD 輔助確認買賣訊號
    4. 避免「剛進入上漲途中」卻因 KD 高檔或短線拉回而過早調節
    5. 下跌途中低接只給觀察，不在未止跌前直接買進或重壓
    """
    reasons = []
    chip_reasons = []

    close = _num(close)
    chgPct = _num(chgPct)
    amp = _num(amp)
    k = _num(k)
    d = _num(d)
    prev_k = _num(prev_k)
    prev_d = _num(prev_d)
    ma6 = _num(ma6)
    prev_ma6 = _num(prev_ma6)
    ma18 = _num(ma18)
    prev_ma18 = _num(prev_ma18)
    ma50 = _num(ma50)
    prev_ma50 = _num(prev_ma50)
    prev_close = _num(prev_close)
    macd_hist = _num(macd_hist)
    prev_macd_hist = _num(prev_macd_hist)
    chip_concentration_score = _num(chip_concentration_score)
    main_force_score = _num(main_force_score)
    broker_diff_score = _num(broker_diff_score)
    chip_concentration_pct = _num(chip_concentration_pct)
    try:
        chip_trend_days = int(chip_trend_days) if chip_trend_days is not None else None
    except Exception:
        chip_trend_days = None
    chip_concentration_threshold = _num(chip_concentration_threshold)
    main_net_3d = _num(main_net_3d)
    avg_volume_3d = _num(avg_volume_3d)
    price_change_3d = _num(price_change_3d)
    volume_change_3d = _num(volume_change_3d)
    repeat_buy_brokers = _num(repeat_buy_brokers)
    repeat_sell_brokers = _num(repeat_sell_brokers)

    if close is None:
        return {
            'signal': '中性',
            'reason': '缺少收盤價資料',
            'signal_text': '技術面：資料不足\n籌碼面：訊號尚未明確',
        }

    # === KD 判斷 ===
    if None in (k, d, prev_k, prev_d):
        kd_gold_cross = False
        kd_dead_cross = False
    else:
        kd_gold_cross = prev_k <= prev_d and k > d
        kd_dead_cross = prev_k >= prev_d and k < d

    kd_low = (k is not None and d is not None and k < 30 and d < 30)
    kd_high = (k is not None and d is not None and k > 80 and d > 80)

    kd_turn_strong = False
    kd_turn_weak = False
    if prev_k is not None and k is not None:
        kd_turn_strong = k > prev_k
        kd_turn_weak = k < prev_k

    k_trend_up = k_trend in ('↑', '↗', 'up')
    k_trend_down = k_trend in ('↓', '↘', 'down')

    if kd_gold_cross:
        reasons.append('KD黃金交叉')
    if kd_dead_cross:
        reasons.append('KD死亡交叉')
    if kd_low:
        reasons.append('KD位於低檔區')
    if kd_high:
        reasons.append('KD位於高檔區')
    if k_trend_up and not kd_gold_cross:
        reasons.append('KD動能走強')
    if k_trend_down and not kd_dead_cross:
        reasons.append('KD動能轉弱')

    # === 股價 / 趨勢 ===
    price_up_raw = chgPct is not None and chgPct > 0
    price_down_raw = chgPct is not None and chgPct < 0
    price_flat_raw = chgPct is not None and abs(chgPct) < 0.5

    above_ma6 = ma6 is not None and close > ma6
    below_ma6 = ma6 is not None and close < ma6
    above_ma18 = ma18 is not None and close > ma18
    below_ma18 = ma18 is not None and close < ma18
    above_ma50 = ma50 is not None and close > ma50
    below_ma50 = ma50 is not None and close < ma50

    ma6_up = ma6 is not None and prev_ma6 is not None and ma6 > prev_ma6
    ma18_up = ma18 is not None and prev_ma18 is not None and ma18 >= prev_ma18
    ma50_up = ma50 is not None and prev_ma50 is not None and ma50 >= prev_ma50

    ma18_break = (
        ma18 is not None and prev_ma18 is not None and prev_close is not None
        and prev_close <= prev_ma18 and close > ma18
    )

    ma18_fall_break = (
        ma18 is not None and prev_ma18 is not None and prev_close is not None
        and prev_close >= prev_ma18 and close < ma18
    )

    ma6_fall_break = (
        ma6 is not None and prev_ma6 is not None and prev_close is not None
        and prev_close >= prev_ma6 and close < ma6
    )

    if price_up_raw:
        reasons.append('股價上漲')
    elif price_down_raw:
        reasons.append('股價下跌')
    if price_flat_raw:
        reasons.append('股價接近橫盤整理')

    if above_ma18:
        reasons.append('股價位於月線之上')
    elif below_ma18:
        reasons.append('股價位於月線之下')

    if ma18_break:
        reasons.append('股價突破月線')
    if ma18_fall_break:
        reasons.append('股價跌破月線')
    if ma18_up:
        reasons.append('月線走平向上')
    if above_ma50:
        reasons.append('股價位於季線之上')

    # === MACD 輔助 ===
    macd_turn_positive = False
    macd_turn_negative = False
    macd_improving = False
    macd_weakening = False
    if macd_hist is not None and prev_macd_hist is not None:
        macd_turn_positive = prev_macd_hist <= 0 < macd_hist
        macd_turn_negative = prev_macd_hist >= 0 > macd_hist
        macd_improving = macd_hist > prev_macd_hist
        macd_weakening = macd_hist < prev_macd_hist
        if macd_turn_positive:
            reasons.append('MACD柱狀體翻正')
        elif macd_turn_negative:
            reasons.append('MACD柱狀體翻黑')
        elif macd_improving:
            reasons.append('MACD動能改善')
        elif macd_weakening:
            reasons.append('MACD動能降溫')

    # === Bias 輔助 ===
    bias6_pos = safe_pos(bias6, bias6_min, bias6_max)
    bias18_pos = safe_pos(bias18, bias18_min, bias18_max)
    bias50_pos = safe_pos(bias50, bias50_min, bias50_max)

    low_count = 0
    high_count = 0
    for pos in (bias6_pos, bias18_pos, bias50_pos):
        if pos is None:
            continue
        if pos < 0.2:
            low_count += 1
        elif pos > 0.8:
            high_count += 1

    bias_low_zone = low_count >= 2
    bias_high_zone = high_count >= 2

    if bias_low_zone:
        reasons.append('乖離處於相對低檔')
    if bias_high_zone:
        reasons.append('乖離處於相對高檔')

    # === 價量關係 ===
    pv = _calc_price_volume_state(
        chgPct=chgPct,
        amp=amp,
        volume=volume,
        prev_volume=prev_volume,
        prev2_volume=prev2_volume,
        volume_ok=volume_ok,
    )

    volume_2day_up = pv['volume_2day_up']
    volume_up = pv['volume_up']
    volume_down = pv['volume_down']
    volume_not_bad = pv['volume_not_bad']
    volume_shrink = pv['volume_shrink']
    volume_spike = pv['volume_spike']

    price_up = pv['price_up']
    price_down = pv['price_down']
    price_volume_state = pv['state']
    reasons.append(price_volume_state)

    if volume_2day_up:
        reasons.append('成交量連續兩天放大')
    elif volume_up:
        reasons.append('成交量放大')
    elif volume_down:
        reasons.append('成交量縮小')
    elif volume_not_bad:
        reasons.append('成交量維持')
    if volume_spike:
        reasons.append('爆量換手')
    if volume_shrink:
        reasons.append('明顯量縮')

    # === 籌碼判斷 ===
    chip_state = str(chip_signal_state or '').strip()
    chip_days_text = f"{chip_trend_days}天" if chip_trend_days else "多日"
    chip_threshold_text = (
        f"、集中度門檻{chip_concentration_threshold:g}%"
        if chip_concentration_threshold is not None else ""
    )

    chip_bullish_concentrated = chip_state == 'bullish_concentrated'
    chip_bullish_distributed = chip_state == 'bullish_distributed'
    chip_bearish_distributed = chip_state == 'bearish_distributed'
    chip_bearish = chip_state in ('bearish', 'bearish_distributed')

    if chip_signal_text:
        chip_reasons.append(str(chip_signal_text))
    elif chip_bullish_concentrated:
        chip_reasons.append(f'籌碼{chip_days_text}集中偏多{chip_threshold_text}')
    elif chip_bullish_distributed:
        chip_reasons.append(f'主力{chip_days_text}買超但籌碼偏分散')
    elif chip_bearish_distributed:
        chip_reasons.append(f'主力{chip_days_text}賣超且籌碼流向散戶')
    elif chip_bearish:
        chip_reasons.append(f'主力{chip_days_text}賣超')

    if chip_concentration_score is not None and chip_concentration_score > 0:
        chip_reasons.append('籌碼集中趨勢轉強')
    elif chip_concentration_score is not None and chip_concentration_score < 0:
        chip_reasons.append('籌碼集中趨勢轉弱')

    if main_force_score is not None and main_force_score > 0:
        chip_reasons.append('主力買超趨勢偏多')
    elif main_force_score is not None and main_force_score < 0:
        chip_reasons.append('主力買超趨勢偏空')

    if broker_diff_score is not None and broker_diff_score < 0:
        chip_reasons.append('買賣家數差收斂')
    elif broker_diff_score is not None and broker_diff_score > 0:
        chip_reasons.append('買賣家數差擴散')

    # === 位階判斷 ===
    zone_info = _calc_position_zone(
        close=close,
        bb_pct=bb_pct,
        bias_low_zone=bias_low_zone,
        bias_high_zone=bias_high_zone,
        kd_low=kd_low,
        kd_high=kd_high,
        above_ma18=above_ma18,
        below_ma18=below_ma18,
        ma18_break=ma18_break,
        ma18_fall_break=ma18_fall_break,
        kd_turn_strong=kd_turn_strong,
        kd_turn_weak=kd_turn_weak,
        k_trend_up=k_trend_up,
        k_trend_down=k_trend_down,
        ma18_up=ma18_up,
        ma50_up=ma50_up,
        above_ma50=above_ma50,
        below_ma50=below_ma50,
    )

    position_zone = zone_info['zone']
    bb_low = zone_info['bb_low']
    bb_mid = zone_info['bb_mid']
    bb_high = zone_info['bb_high']
    bb_overheat = zone_info['bb_overheat']

    reasons.append(position_zone)

    if bb_low:
        reasons.append('接近布林下緣')
    elif bb_high:
        reasons.append('位於布林高檔區')
    elif bb_mid:
        reasons.append('布林位於中性偏強區')

    if bb_overheat:
        reasons.append('接近布林上緣過熱')

    chip_scenario = _calc_chip_scenario(
        main_buy_days=main_buy_days,
        main_sell_days=main_sell_days,
        main_net_3d=main_net_3d,
        price_up=price_up,
        price_down=price_down,
        price_flat=pv['price_flat'],
        volume_up=volume_up,
        price_volume_state=price_volume_state,
        position_zone=position_zone,
        close_position=close_position,
    )
    # 只補充有明確方向的籌碼情境。
    # 「籌碼震盪 / 籌碼混亂」是保守 fallback，若無條件加入，
    # 會讓每檔股票都出現「籌碼震盪（中性）：籌碼震盪，方向未定。」且與前面的籌碼描述重複。
    chip_scenario_name = str(chip_scenario.get('scenario') or '').strip()
    chip_scenario_description = str(chip_scenario.get('description') or '').strip()
    if chip_scenario_name and chip_scenario_name not in ('籌碼震盪', '籌碼混亂'):
        chip_reasons.append(f"{chip_scenario_name}：{chip_scenario_description}")
    if repeat_buy_brokers is not None and repeat_buy_brokers >= 3:
        chip_reasons.append('三日重複買超券商增加，主力承接連續性較佳')
    if repeat_sell_brokers is not None and repeat_sell_brokers >= 3:
        chip_reasons.append('三日重複賣超券商增加，主力調節連續性較高')

    # === 強弱輔助條件 ===
    kd_strong = kd_gold_cross or kd_turn_strong or k_trend_up
    kd_weak = kd_dead_cross or kd_turn_weak or k_trend_down
    trend_supported = above_ma18 and (ma18_up or ma50_up or above_ma50 or ma18_break)
    early_uptrend = ma18_break and kd_strong and price_volume_state in ('價漲量增', '價平量增', '價量中性')
    main_uptrend = position_zone == '上漲途中' and trend_supported and not ma18_fall_break
    overheat_confirmed = bb_overheat or (bb_high and bias_high_zone) or (bias_high_zone and kd_high)
    trend_break_confirmed = ma18_fall_break or (below_ma18 and kd_weak)

    # ============================================================
    # 規則判斷：位階 × 價量 × 技術確認
    # ============================================================

    # 0) 籌碼優先警示：主力賣、家數擴散，若股價走弱或放量下跌，風險優先。
    if (
        chip_bearish_distributed
        and (price_down or price_volume_state in ('價跌量增', '價平量增'))
        and (kd_weak or trend_break_confirmed or position_zone in ('頂部區域', '下跌途中'))
    ):
        return {
            'signal': '偏空',
            'reason': '主力連續賣超且買賣家數差擴散，搭配股價轉弱，籌碼流向散戶風險高',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 0-1) 主力連買但家數擴散：虛胖型上漲，不追高。
    if (
        chip_bullish_distributed
        and price_up
        and (bb_high or bias_high_zone or volume_spike)
    ):
        return {
            'signal': '偏空' if position_zone == '頂部區域' else '中性',
            'reason': '雖有主力買超，但買賣家數差擴散，屬偏分散的虛胖型上漲，避免追高',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 0-2) 籌碼集中偏多：低調吸籌或起漲初期，給偏多觀察/買進。
    if (
        chip_bullish_concentrated
        and not overheat_confirmed
        and not trend_break_confirmed
        and price_volume_state in ('價漲量增', '價平量增', '價量中性', '價漲量縮')
        and (kd_strong or trend_supported or position_zone in ('底部區域', '上漲途中', '盤整區域'))
    ):
        return {
            'signal': '偏多',
            'reason': '主力買超且買賣家數差收斂，籌碼集中偏多，可跟隨低佈局但避免追高',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 1) 起漲保護：聯發科 2454 類型，剛站回月線 + 價量/KD轉強，不因短線高檔或剛獲利而調節。
    if early_uptrend:
        return {
            'signal': '偏多',
            'reason': '剛突破月線且價量/KD轉強，屬起漲或轉強初期，持股不宜過早調節',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 2) 明確調節：高檔過熱後價跌量增，或已跌破月線，才直接轉空。
    if (
        price_volume_state == '價跌量增'
        and (overheat_confirmed or position_zone == '下跌途中' or trend_break_confirmed)
        and (kd_weak or trend_break_confirmed or macd_turn_negative)
    ):
        return {
            'signal': '偏空',
            'reason': '高檔過熱或下跌途中出現價跌量增，且動能/均線轉弱',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 3) 明確調節：連續量增下跌且跌破月線。
    if (
        volume_2day_up
        and price_down
        and ma18_fall_break
        and (kd_weak or macd_turn_negative)
    ):
        return {
            'signal': '偏空',
            'reason': '連續放量下跌並跌破月線，轉弱訊號明確',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 4) 高檔轉弱：只要尚未跌破月線，不直接出清，以分批留意賣點為主。
    if (
        position_zone == '頂部區域'
        and overheat_confirmed
        and (kd_weak or macd_weakening or ma6_fall_break)
        and not ma18_fall_break
    ):
        return {
            'signal': '偏空',
            'reason': '高檔過熱且動能降溫，但尚未跌破月線，宜分批停利而非一次出清',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 5) 主升段保護：月線上方、趨勢仍受支撐，KD高檔或短線降溫不視為調節。
    if (
        main_uptrend
        and price_volume_state in ('價漲量增', '價漲量縮', '價平量增', '價量中性')
        and not overheat_confirmed
    ):
        return {
            'signal': '偏多' if price_volume_state in ('價漲量增', '價平量增') and kd_strong else '中性',
            'reason': '股價仍在上漲途中且月線趨勢未破，持股以續抱觀察為主，不因KD高檔過早調節',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 6) 上漲途中放量下跌：提高警戒，但未跌破月線前不直接轉空。
    if (
        main_uptrend
        and price_volume_state == '價跌量增'
        and (kd_weak or macd_weakening)
        and not ma18_fall_break
    ):
        return {
            'signal': '偏空',
            'reason': '上漲途中出現價跌量增與動能轉弱，若跌破月線應降低持股',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 7) 下跌途中反彈：不急著買，除非重新站回月線且價量/KD同步轉強。
    if (
        position_zone == '下跌途中'
        and not early_uptrend
        and price_volume_state in ('價漲量縮', '價跌量縮', '價平量縮', '價量中性')
    ):
        return {
            'signal': '中性',
            'reason': '仍在下跌途中，反彈或量縮尚不足以確認轉強',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 8) 底部轉強：底部區 + 價漲量增 + KD/MACD改善；偏多但避免一次重壓。
    if (
        position_zone == '底部區域'
        and price_volume_state == '價漲量增'
        and (kd_strong or macd_improving)
        and not ma18_fall_break
    ):
        return {
            'signal': '偏多',
            'reason': '底部區域出現價漲量增與動能改善，可觀察低檔轉強，但宜分批不宜重壓',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 9) 底部止跌：底部區 + 價跌量縮 / 價平量縮，只能當止跌觀察。
    if (
        position_zone == '底部區域'
        and price_volume_state in ('價跌量縮', '價平量縮')
        and (kd_turn_strong or k_trend_up or kd_low or volume_shrink)
    ):
        return {
            'signal': '中性',
            'reason': '底部區域跌勢趨緩，但尚未出現明確價漲量增，先觀察止穩',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 10) 明確買進：突破或站上月線，價漲量增，KD/MACD轉強，且未明顯過熱。
    if (
        price_volume_state == '價漲量增'
        and (kd_strong or macd_turn_positive or macd_improving)
        and (above_ma18 or ma18_break)
        and not bb_overheat
        and not bias_high_zone
    ):
        return {
            'signal': '偏多',
            'reason': '價漲量增，動能轉強，股價站上月線，技術面偏多',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 11) 上漲途中但價漲量縮：不追高，但也不急賣。
    if (
        position_zone == '上漲途中'
        and above_ma18
        and price_volume_state == '價漲量縮'
        and not ma18_fall_break
    ):
        return {
            'signal': '中性',
            'reason': '上漲途中出現價漲量縮，持股可觀察但不宜追高',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 12) 上漲途中轉弱：月線上方先觀察，不因 KD 高檔或短線轉弱過早調節。
    if (
        position_zone == '上漲途中'
        and above_ma18
        and kd_weak
        and price_volume_state in ('價漲量縮', '價平量增', '價量中性')
        and not ma18_fall_break
    ):
        return {
            'signal': '中性',
            'reason': '上漲途中動能轉弱但尚未跌破月線，先觀察不急賣',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 13) 盤整區：價平量縮或訊號混雜。
    if (
        position_zone == '盤整區域'
        or price_volume_state in ('價平量縮', '價量中性')
    ):
        return {
            'signal': '中性',
            'reason': '位階與價量尚未形成明確方向，留意突破或跌破確認',
            'signal_text': _format_signal_sections(reasons, chip_reasons),
        }

    # 14) 保守預設。
    return {
        'signal': '中性',
        'reason': '價格、量能、KD與布林尚未形成明確方向',
        'signal_text': _format_signal_sections(reasons, chip_reasons),
    }
