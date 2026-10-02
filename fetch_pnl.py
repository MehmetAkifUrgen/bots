import os, sys, hmac, hashlib, urllib.parse, time, requests, json, datetime, traceback
from dotenv import load_dotenv

OUT_FILE = "/Users/a./flutterProjects/bots/pnl_result.txt"
lines = []
def log(s):
    print(s, flush=True)
    lines.append(s)

load_dotenv()

API_KEY    = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
PAPI_BASE  = os.getenv("BINANCE_PAPI_BASE", "https://papi.binance.com")
PROXY_URL  = os.getenv("FIXIE_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

log(f"API KEY: {'var' if API_KEY else 'YOK'} | SECRET: {'var' if API_SECRET else 'YOK'}")

def get_proxies():
    if PROXY_URL and "@" in PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None

def signed(method, path, params=None):
    if params is None: params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 15000
    query = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{PAPI_BASE}{path}?{query}&signature={sig}"
    hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "X-MBX-APIKEY": API_KEY}
    proxies = get_proxies()
    opts = {"timeout": 20}
    if proxies: opts["proxies"] = proxies
    r = requests.get(url, headers=hdrs, **opts)
    try:
        return r.json()
    except:
        return {"_raw": r.text[:500]}

def get_price(sym):
    try:
        r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}", timeout=10)
        return float(r.json()["price"])
    except:
        return None

if not API_KEY or not API_SECRET:
    log("ERROR: API anahtarlari yok")
    sys.exit(1)

log("")
log("="*55)
log("  BINANCE HESAP DURUMU (CANLI - API uzerinden)")
log("="*55)

try:
    acc = signed("GET", "/papi/v1/account")
    if isinstance(acc, dict) and "accountEquity" in acc:
        equity = float(acc.get("accountEquity", 0))
        free_margin = float(acc.get("totalAvailableBalance", 0))
        log(f"  Toplam Varlik (Equity):   ${equity:.2f} USDT")
        log(f"  Serbest Teminat:          ${free_margin:.2f} USDT")
    else:
        log(f"  Hesap dondu: {str(acc)[:400]}")
except Exception as e:
    log(f"Hesap HATA: {e}")
    traceback.print_exc()

log("")
log("  ACIK POZISYONLAR:")
toplam_unreal = 0.0
try:
    poses = signed("GET", "/papi/v1/um/positionRisk")
    found = False
    if isinstance(poses, list):
        for p in poses:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                found = True
                sym = p["symbol"]
                ep = float(p.get("entryPrice", 0))
                upnl = float(p.get("unRealizedProfit", 0))
                toplam_unreal += upnl
                last = get_price(sym)
                log("")
                log(f"    >> {sym} <<")
                log(f"      Miktar:     {amt}")
                log(f"      Giris:      ${ep:.6f}")
                if last:
                    log(f"      Anlik:      ${last:.6f}")
                log(f"      Unrealized: ${upnl:+.2f}")
                log(f"      Leverage:   {p.get('leverage','?')}x")
    else:
        log(f"  Pozisyonlar hatali: {str(poses)[:400]}")
    if not found:
        log("    (hic acik pozisyon yok - RATS dahil degil)")
except Exception as e:
    log(f"Pozisyon HATA: {e}")
    traceback.print_exc()
log(f"  > Toplam Unrealized PnL: ${toplam_unreal:+.2f}")

log("")
log("  ISLEMLER (25 Agu 00:00'dan beri - userTrades):")
rpnl_total = 0.0
fee_total = 0.0
tr_list = []
try:
    dundeki = int(datetime.datetime(2026,8,25,0,0,0).timestamp() * 1000)
    trades = signed("GET", "/papi/v1/um/userTrades", {"limit": 1000})
    if isinstance(trades, list):
        for t in trades:
            tm = int(t.get("time", 0))
            rp = float(t.get("realizedPnl", 0))
            fee = float(t.get("commission", 0))
            if tm >= dundeki:
                rpnl_total += rp
                fee_total += fee
                if abs(rp) > 0.001 or fee > 0.001:
                    dt = datetime.datetime.utcfromtimestamp(tm/1000).strftime("%m-%d %H:%M")
                    net = rp - fee
                    tr_list.append((dt, t['symbol'], rp, fee, net))
    else:
        log(f"  userTrades hatali: {str(trades)[:500]}")
    tr_list.sort(key=lambda x: x[0])
    if tr_list:
        for l in tr_list:
            log(f"    {l[0]}  {l[1]:15s}  PnL: ${l[2]:+.2f}  Komisyon: -${l[3]:.2f}  Net: ${l[4]:+.2f}")
    else:
        log("    (islem bulunamadi / filtre disi)")
except Exception as e:
    log(f"Trades HATA: {e}")
    traceback.print_exc()

log("")
log(f"  GERCEKLESEN PnL (brut):   ${rpnl_total:+.2f}")
log(f"  TOPLAM KOMISYON:          -${fee_total:.2f}")
net_real = rpnl_total - fee_total
log(f"  NET GERCEKLESEN PnL:      ${net_real:+.2f}")
log("")
log("="*55)
log("  GENEL DURUM (Dunden beri):")
log(f"    Kapali (Realized): ${net_real:+.2f}")
log(f"    Acik   (Unrealized): ${toplam_unreal:+.2f}")
log(f"    =========================")
genel = net_real + toplam_unreal
log(f"    TOPLAM:            ${genel:+.2f}")
if genel > 0:
    log(f"    DURUM:             >>> KAZANC <<<")
elif genel < 0:
    log(f"    DURUM:             >>> KAYIP <<<")
else:
    log(f"    DURUM:             BASA BAS")
log("="*55)

try:
    with open(OUT_FILE, "w") as f:
        f.write("\n".join(lines))
    log(f"\nDosyaya yazildi: {OUT_FILE}")
except Exception as e:
    log(f"Dosya yazma HATA: {e}")
