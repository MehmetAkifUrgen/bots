# Binance Futures Scanner & Paper Trading Bot

Gelişmiş Binance USDT-M perpetual futures tarayıcı ve paper trading botu. Stablecoin tabanlı pariteleri dışarıda bırakıp tüm uygun sembolleri tarar ve her coin için şu sorulara cevap verir:

- Trend devam ediyor mu?
- Long mu, short mu, yoksa bekle mi?
- Giriş bölgesi neresi?
- Stop ve iki kademe hedef nerede?

Bot gerçek emir açmaz. Paper trade mantığıyla en iyi setup'i seçer, pozisyonu takip eder ve sadece giriş/çıkış olduğunda Telegram mesajı gönderir.

## 🆕 Yeni Özellikler (v2.0)

### ✨ Eklenen Özellikler:

1. **Backtest Modülü** (`backtest.py`)
   - Geçmiş verilerle strateji testi
   - Detaylı performans metrikleri
   - Strategy breakdown analizi

2. **Dinamik Pozisyon Boyutlandırma**
   - Yüzde bazlı risk yönetimi
   - Risk/getiri oranlı sizing
   - Maksimum risk limiti koruması

3. **Trailing Stop Loss**
   - ATR bazlı dinamik stop
   - Kar koruma mekanizması
   - Pozisyon lehine otomatik güncelleme

4. **Piyasa Rejimi Tespiti**
   - TRENDING_UP / TRENDING_DOWN / RANGING tespiti
   - Rejim gücü skoru
   - Strateji optimizasyonu için rejim bilgisi

5. **Open Interest Entegrasyonu**
   - OI trend takibi
   - Funding rate analizi
   - Fiyat-OI divergence tespiti

6. **Gemini AI Yorumları** (Opsiyonel)
   - Teknik sinyallere doğal dil yorumu
   - Risk değerlendirmesi
   - Alternatif senaryo analizi

7. **Kapsamlı Analitik**
   - Sharpe, Sortino, Calmar ratio
   - Maksimum drawdown takibi
   - Saat/gün bazlı performans
   - Strateji ve sembol kırılımı

8. **Geliştirilmiş Hata Yönetimi**
   - Otomatik retry mekanizması
   - Exponential backoff
   - Detaylı hata logları

## Nasıl Çalışılır

Her dongude bot:

1. Binance Futures'tan aktif USDT perpetual sembolleri alir.
2. Stablecoin bazli sembolleri eleyip hacim filtresinden gecen tum uygun coinleri tarar.
3. Her coin icin `15m`, `1h` ve `4h` mumlarini ceker.
4. EMA, RSI, ATR, MACD histogram ve ADX hesaplar.
5. Her coin icin tum uygun strateji adaylarini cikarir:
   - `LONG`: trend devam setup'i
   - `SHORT`: trend short veya asiri sisme sonrasi exhaustion short
   - `SCALP`: kisa vadeli momentum devam setup'i
   - `WAIT`: coin hareketli ama giris kalitesi zayif
6. Gecmis paper trade performansina bakip en basarili strateji tipine hafif agirlik verir.
7. Tek aktif pozisyon acar ve yalnizca stop veya hedefe gelince kapatir. Istersen sure bazli kapama tekrar acilabilir.
8. Telegram'a sadece pozisyon giris ve cikis mesajlarini gonderir.

## Neden Gemini Ilk Asamada Sart Degil

Gemini istersen sonra eklenebilir, ama sinyal motorunun kendisi kuralli kalmali. LLM katmani daha cok su isler icin mantikli:

- Mesajlari daha insan gibi yorumlamak
- Ek aciklama veya ozet yazmak
- Teknik sinyalin yanina risk notu dusmek

Karar mekanizmasini LLM'e birakmak tutarsizlik yaratir. O yuzden v1 kuralli, deterministik ve test edilebilir.

## Gereksinimler

- Python 3.11+
- Telegram mesaji istiyorsan bot token + chat id

Kurulum:

```bash
pip install -r requirements.txt
```

## Ortam Degiskenleri

Zorunlu:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Opsiyonel:

- `BINANCE_API_FUTURES_BASE` varsayilan `https://fapi.binance.com`
- `SCAN_EVERY_SECONDS` varsayilan `900`
- `MIN_SCAN_INTERVAL_SECONDS` varsayilan `900`
- `TOP_GAINERS_LIMIT` varsayilan `0` ve `0` tum uygun coinleri tarar
- `MIN_QUOTE_VOLUME_USD` varsayilan `15000000`
- `MAX_QUOTE_VOLUME_USD` varsayilan `5000000000`
- `LOOKBACK_BARS` varsayilan `260`
- `MIN_READY_CONFIDENCE` varsayilan `78`
- `SEND_WAIT_SETUPS` varsayilan `true`
- `MAX_WAIT_SETUPS` varsayilan `3`
- `ANALYSIS_STATE_FILE` varsayilan `analysis_state.json`
- `PAPER_TRADES_FILE` varsayilan `paper_trades.csv`
- `POSITION_SIZE_USD` varsayilan `100`
- `POSITION_SIZE_PCT` varsayilan `2` (bakiyenin %2'si risk)
- `MAX_RISK_PCT` varsayilan `5` (maksimum pozisyon büyüklüğü)
- `TRAILING_STOP_ATR_MULT` varsayilan `1.5` (trailing stop ATR çarpanı)
- `MAX_DRAWDOWN_PCT` varsayilan `20` (maksimum drawdown limiti)
- `MAX_POSITION_HOLD_HOURS` varsayilan `0` ve `0` sure bazli zorunlu cikisi kapatir
- `STARTING_BALANCE_USD` varsayilan `1000`
- `GEMINI_API_KEY` Gemini AI yorumları için (opsiyonel)

Telegram ayari yoksa bot yine calisir ama giris/cikis mesajlarini konsola yazar.

## Calistirma

### Ana Bot

```bash
python bot.py
```

### Backtest

```bash
# Tek sembol backtest
python backtest.py --symbol BTCUSDT --days 90 --interval 1h

# Tüm top gainers backtest
python backtest.py --all-symbols --days 60 --interval 1h --top 20

# Sonuçları kaydet
python backtest.py --symbol ETHUSDT --days 90 --interval 1h --output backtest_results.json
```

### Analitik Raporu

```bash
# Kapsamlı analiz
python analytics.py --file paper_trades.csv --balance 1000

# JSON çıktısı
python analytics.py --file paper_trades.csv --balance 1000 --output analytics_report.json
```

### Gemini AI Test

```bash
python gemini_analyzer.py --test
```

### Alarm Botu

```bash
python alarm.py
```

## Telegram Mesaji Nasil Okunur

Her coin icin raporda su alanlar gelir:

- `READY LONG`, `READY SHORT` veya `WAIT`
- Guven skoru
- 24s fiyat degisimi
- Entry zone
- Stop
- TP1 ve TP2
- Kararin nedenleri

`WAIT` demek coin kotu degil, sadece su an kovalanacak kadar temiz setup vermiyor demek.

Bot artik surekli watchlist spam'i atmaz. Mesajlar sunlarla sinirlidir:

- `PAPER ENTRY`: secilen en iyi setup ile acilan pozisyon
- `PAPER EXIT`: stop veya hedef ile kapanan pozisyon. Mesaj icinde toplu basari orani da vardir.

## Railway Deploy

Railway start command zaten hazir:

```bash
python -u bot.py
```

`Variables` kismina en az su degerleri gir:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SCAN_EVERY_SECONDS`
- `TOP_GAINERS_LIMIT`
- `MIN_QUOTE_VOLUME_USD`
- `MAX_QUOTE_VOLUME_USD`
- `MIN_READY_CONFIDENCE`
- `SEND_WAIT_SETUPS`
- `MAX_WAIT_SETUPS`
- `POSITION_SIZE_PCT`
- `TRAILING_STOP_ATR_MULT`
- `GEMINI_API_KEY` (opsiyonel)

Deploy sonrasi loglarda `Tarama evreni` ve `Pozisyon acildi` satirlarini goruyorsan akis calisiyor demektir.

## Gelecek Gelistirmeler

Istersen bir sonraki iterasyonda bunlardan birini ekleyebiliriz:

1. Multi-position portfolio yönetimi (aynı anda birden fazla pozisyon)
2. Liquidation data entegrasyonu
3. Order book depth analizi
4. Machine learning model training (geçmiş verilere dayalı)
5. Web dashboard (gerçek zamanlı pozisyon takibi)
6. Discord webhook desteği

## Uyari

Bu proje yatirim tavsiyesi degildir. Canli emir baglamadan once forward test ve risk kontrolu yap.

## Backtest Önemli Notlar

Botu canlıya almadan önce mutlaka backtest yapın:

```bash
# 1. Önce backtest yap
python backtest.py --all-symbols --days 90 --interval 1h --top 30 --output results.json

# 2. Sonuçları incele (win rate > 50%, profit factor > 1.2 olmalı)

# 3. Paper trading ile en az 1-2 hafta test et

# 4. Analitik raporu oluştur
python analytics.py --file paper_trades.csv --balance 1000 --output final_report.json
```

**Asla backtest yapmadan canlıya başlama!**
