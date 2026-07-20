import datetime
import pandas as pd
from FinMind.data import DataLoader

# 1. 初始化 FinMind 實體 (若有 Token 可以填入，沒有的話免費版有流量限制)
dl = DataLoader()
# dl.login(token="你的FinMindToken") # 選填

# 2. 設定觀察設定
stock_id = "2330"  # 你想觀察的股票代碼
today = datetime.date.today()
start_date = (today - datetime.timedelta(days=15)
              ).strftime("%Y-%m-%d")  # 抓取過去15天確保有3個交易日

print(f"正在分析 {stock_id} 近期的籌碼變化...")

try:
    # 3. 抓取「券商分點買賣超」數據 (TaiwanStockBrokerBS)
    # 註：此 API 免費版可能每日有調用限制，或需要傳入精確日期
    df = dl.taiwan_stock_broker_bs(
        stock_id=stock_id,
        start_date=start_date
    )

    if df.empty:
        print("未抓取到數據，可能非交易日或超出免費版權限。")
    else:
        # 4. 資料前處理：計算每日的「主力買賣超」與「買賣家數差」
        # FinMind 欄位說明：date(日期), broker(券商), buy(買進張數), sell(賣出張數)
        df['net_buy'] = df['buy'] - df['sell']  # 計算每家券商的淨買超

        def top_15_main_force(group):
            buy_group = group[group['net_buy'] > 0].sort_values(
                by=['net_buy', 'broker'], ascending=[False, True]
            ).head(15)
            sell_group = group[group['net_buy'] < 0].sort_values(
                by=['net_buy', 'broker'], ascending=[True, True]
            ).head(15)
            top_15_buy = buy_group['net_buy'].sum()
            top_15_sell = sell_group['net_buy'].sum()
            return top_15_buy + top_15_sell

        daily_report = []

        # 依日期分組計算
        for date, group in df.groupby('date'):
            # 計算買賣家數差：有買的券商家數 - 有賣的券商家數
            active_buyers = group[group['buy'] > 0]['broker'].nunique()
            active_sellers = group[group['sell'] > 0]['broker'].nunique()
            broker_diff = active_buyers - active_sellers

            # 計算主力買賣超（買超前 15 名合計 - 賣超前 15 名合計）
            main_force_net = top_15_main_force(group)

            daily_report.append({
                'Date': date,
                '主力買賣超': int(main_force_net),
                '買賣家數差': int(broker_diff)
            })

        report_df = pd.DataFrame(daily_report).sort_values(
            by='Date', ascending=False)

        # 5. 取出最近的 3 個交易日
        recent_3_days = report_df.head(3)
        print("\n【最近 3 個交易日籌碼數據】")
        print(recent_3_days.to_string(index=False))
        print("-" * 40)

        # 6. 產出可丟進 signals.get_tech_signal(...) 的三日籌碼欄位
        main_buy_days = int((recent_3_days['主力買賣超'] > 0).sum())
        main_sell_days = int((recent_3_days['主力買賣超'] < 0).sum())
        main_net_3d = int(recent_3_days['主力買賣超'].sum())
        broker_diff_score = int(recent_3_days['買賣家數差'].sum())

        chip_context = {
            'main_buy_days': main_buy_days,
            'main_sell_days': main_sell_days,
            'main_net_3d': main_net_3d,
            'broker_diff_score': broker_diff_score,
            # 下面欄位請由價格/量能模組補入後一起傳給 get_tech_signal
            'price_change_3d': None,
            'volume_change_3d': None,
            'close_position': None,
            'repeat_buy_brokers': None,
            'repeat_sell_brokers': None,
        }

        print("\n【可傳入 signals.get_tech_signal 的三日籌碼欄位】")
        print(chip_context)

        if main_buy_days >= 2 and broker_diff_score < 0:
            print(f"🔥 籌碼判斷：股票 {stock_id} 主力連續買超且買賣家數差收斂，籌碼偏多。")
        elif main_buy_days >= 2:
            print(f"⚠️ 籌碼判斷：股票 {stock_id} 主力連續買超，但買賣家數差尚未明顯收斂。")
        elif main_sell_days >= 2 and broker_diff_score > 0:
            print(f"🚨 籌碼判斷：股票 {stock_id} 主力連續賣超且買賣家數差擴散，籌碼偏空。")
        else:
            print("籌碼判斷：籌碼處於震盪洗盤階段，方向未定。")

except Exception as e:
    print(f"程式執行出錯: {e}")
    print("提示：FinMind 的分點數據 (TaiwanStockBrokerBS) 資料量較大，若是免費版帳號，建議縮短 start_date 的範圍，或確認 API Token 是否設定。")
