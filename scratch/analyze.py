import requests
import datetime
base = "https://fapi.binance.com"
sym = "HYPERUSDT"
res = requests.get(f"{base}/fapi/v1/klines", params={"symbol":sym, "interval":"1d", "limit":10}).json()
print(f"\n--- {sym} Daily Klines (Last 10 Days) ---")
if type(res) == list and len(res) > 0:
    for k in res:
        date = datetime.datetime.fromtimestamp(k[0]/1000).strftime('%Y-%m-%d')
        print(f"Date: {date}, O:{k[1]}, H:{k[2]}, L:{k[3]}, C:{k[4]}, VolUSD: {float(k[7])/1000000:.1f}M")
else:
    print(f"Could not fetch data for {sym}. Response: {res}")
