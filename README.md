# Binance Futures Top Gainers Analyzer

Bu proje Binance USDT-M perpetual futures tarafinda 24 saatlik en cok yukselen 10 coini tarar ve her coin icin su sorulara cevap verir:

- Trend devam ediyor mu?
- Long mu, short mu, yoksa bekle mi?
- Giris bolgesi neresi?
- Stop ve iki kademe hedef nerede?

Bot emir acmaz. Ilk surumun amaci temiz bir analiz akisi kurmak ve Telegram'a kullanilabilir trade setup'lari dusurmektir.

## Nasil Calisir

Her dongude bot:

1. Binance Futures'tan aktif USDT perpetual sembolleri alir.
2. 24 saatlik en cok yukselen coinleri hacim filtresiyle siralar.
3. Her coin icin `15m`, `1h` ve `4h` mumlarini ceker.
4. EMA, RSI, ATR, MACD histogram ve ADX hesaplar.
5. Her coin icin su kararlardan birini uretir:
   - `LONG`: trend devam setup'i
   - `SHORT`: trend short veya asiri sisme sonrasi exhaustion short
   - `WAIT`: coin hareketli ama giris kalitesi zayif
6. Rapor degistiyse Telegram'a tek mesaj halinde gonderir.

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
- `SCAN_EVERY_SECONDS` varsayilan `120`
- `TOP_GAINERS_LIMIT` varsayilan `10`
- `MIN_QUOTE_VOLUME_USD` varsayilan `15000000`
- `MAX_QUOTE_VOLUME_USD` varsayilan `5000000000`
- `LOOKBACK_BARS` varsayilan `260`
- `MIN_READY_CONFIDENCE` varsayilan `78`
- `SEND_WAIT_SETUPS` varsayilan `true`
- `MAX_WAIT_SETUPS` varsayilan `3`
- `ANALYSIS_STATE_FILE` varsayilan `analysis_state.json`

Telegram ayari yoksa bot yine calisir ama raporu konsola yazar.

## Calistirma

```bash
python bot.py
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

Bot artik raporu ikiye ayirir:

- `Isleme Uygunlar`: `MIN_READY_CONFIDENCE` esigini gecen setup'lar
- `Izleme Listesi`: opsiyonel olarak gonderilen, henuz temiz olmayan coinler

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

Deploy sonrasi loglarda `Top gainers` ve `Telegram'a rapor gonderildi` satirlarini goruyorsan akis calisiyor demektir.

## Sonraki Asama

Istersen bir sonraki iterasyonda bunlardan birini ekleyebiliriz:

1. Gemini ile teknik rapora dogal dil yorumu.
2. Open interest ve liquidation verisi.
3. Backtest modu.
4. Sadece `READY` setup'lari atan sessiz mod.

## Uyari

Bu proje yatirim tavsiyesi degildir. Canli emir baglamadan once forward test ve risk kontrolu yap.
