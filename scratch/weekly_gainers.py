import requests
base = "https://fapi.binance.com"
info = requests.get(f"{base}/fapi/v1/exchangeInfo").json()
symbols = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT' and not s['symbol'].startswith('USDC')]

results = []
for sym in symbols:
    try:
        res = requests.get(f"{base}/fapi/v1/klines", params={"symbol":sym, "interval":"1d", "limit":8}).json()
        if len(res) >= 8:
            close_7d_ago = float(res[0][4])
            close_now = float(res[-1][4])
            if close_7d_ago > 0:
                pct_change = (close_now - close_7d_ago) / close_7d_ago * 100
                results.append((sym, pct_change))
    except:
        pass

results.sort(key=lambda x: x[1], reverse=True)
print("TOP 10 WEEKLY GAINERS:")
for r in results[:10]:
    print(f"{r[0]}: +{r[1]:.2f}%")
