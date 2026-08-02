from data_sources import API_URL, _refresh_finmind_runtime_auth, _safe_response_json
import requests

token, headers = _refresh_finmind_runtime_auth()
tests = [
    ('TaiwanStockPrice', 'TPEX', '2024-01-01'),
    ('TaiwanStockPrice', 'TPEX01', '2024-01-01'),
    ('TaiwanStockPrice', 'Y9999', '2024-01-01'),
    ('TaiwanStockPrice', 'OTC', '2024-01-01'),
    ('TaiwanStockPrice', '9999', '2024-01-01'),
    ('TaiwanOTCStockPrice', 'TPEX', '2024-01-01'),
    ('TaiwanOTCStockPrice', None, '2024-01-01'),
    ('TaiwanStockMarketIndex', None, '2024-01-01'),
    ('TaiwanStockMarketIndex', 'OTC', '2024-01-01'),
    ('TaiwanOTCStockMarketIndex', None, '2024-01-01'),
    ('TaiwanVariousIndexTotal', None, '2024-01-01'),
    ('TaiwanVariousIndexTotal', 'TPEX', '2024-01-01'),
    ('TaiwanVariousIndexTotal', 'OTC', '2024-01-01'),
    ('TaiwanStockTotalReturnIndex', 'TPEX', '2020-01-01'),
    ('TaiwanStockTotalReturnIndex', 'OTC', '2020-01-01'),
    ('TaiwanStockTotalReturnIndex', 'TWO', '2020-01-01'),
]
for ds, did, sd in tests:
    params = {'dataset': ds, 'start_date': sd, 'token': token}
    if did:
        params['data_id'] = did
    r = requests.get(API_URL, params=params, headers=headers, timeout=30)
    d = _safe_response_json(r)
    rows = d.get('data') or []
    tag = 'OK ' if rows else '   '
    info = f"rows={len(rows)} cols={list(rows[0].keys())}" if rows else f"status={r.status_code} msg={d.get('msg', '')[:60]}"
    print(f"{tag} {ds}[{did}]: {info}")
    if rows:
        print("     last:", rows[-1])
