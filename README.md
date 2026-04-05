# Binance Signal Notifier Bot (Telegram)

Bu bot Binance spot verilerini teknik analiz ile tarar ve sinyal bulursa Telegram'a mesaj atar.

Ek olarak paper trade (sanal al-sat) modulu ile gercek para kullanmadan stratejinin performansini takip eder.

Kullanılan ana filtreler:

- EMA 20 / EMA 50 trend yönü
- RSI(14)
- ATR(14) tabanlı stop-loss
- Fibonacci retracement (0.5 - 0.618 bölgesi)
- Basit hacim filtresi (volume > 20 ortalama)
- Maksimum fiyat filtresi (`MAX_PRICE_USD`) ile pahali coinleri eleme
- Dinamik sembol secimi: 2021+ listelenme ve dusuk hacim araligi

Paper trade ozellikleri:

- Sinyal geldiginde sanal pozisyon acma
- Mum yuksek/dusuk verisine gore TP/SL tetikleme
- Net PnL, win rate, max drawdown takibi
- Kapanan islemleri `paper_trades.csv` dosyasina loglama

## Kurulum

1. Python 3.11+ önerilir.
2. Bağımlılıkları kur:

```bash
pip install -r requirements.txt
```

3. Ortam değişkenlerini hazırla:

```bash
cp .env.example .env
```

4. `.env` içini doldur:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SYMBOLS` (virgülle)
- `INTERVAL` (örn: `15m`, `1h`)
- `MAX_PRICE_USD` (sadece bu fiyat ve altindaki coinleri tara)
- `USE_DYNAMIC_SYMBOLS` (`true` ise Binance'den otomatik sembol secer)
- `MIN_LISTING_YEAR` (ornek: `2021`)
- `MIN_QUOTE_VOLUME_USD` (24s minimum quote hacim)
- `MAX_QUOTE_VOLUME_USD` (24s maksimum quote hacim)
- `DYNAMIC_SYMBOL_LIMIT` (taranacak sembol limiti)
- `PAPER_TRADE_ENABLED` (`true`/`false`)
- `PAPER_INITIAL_BALANCE` (baslangic sanal bakiye)
- `PAPER_RISK_PER_TRADE_PCT` (islem basi risk yuzdesi)
- `PAPER_FEE_RATE` (komisyon orani, varsayilan `0.0004`)
- `PAPER_LOG_FILE` (islem log dosyasi)

Not: Dinamik secim acikken bot sadece su kosullari saglayan USDT paritelerini alir:

- Binance spotta `TRADING` durumda olmasi
- `MIN_LISTING_YEAR` ve sonrasi listelenmis olmasi
- Son fiyatin `MAX_PRICE_USD` ve altinda olmasi
- 24s quote hacminin `MIN_QUOTE_VOLUME_USD` ile `MAX_QUOTE_VOLUME_USD` araliginda olmasi

## Çalıştırma

```bash
python bot.py
```

## Railway Deploy

1. Projeyi GitHub'a push et.
2. Railway'de `New Project` -> `Deploy from GitHub Repo` ile bu repoyu sec.
3. Servis ayarlarinda Start Command gerekirse `python -u bot.py` yaz.
4. Railway `Variables` bolumune `.env` icindeki degiskenleri ekle.
5. Deploy sonrasi loglarda `Tarama basliyor...` satirini goruyorsan bot calisiyor demektir.

Oneri:

- Railway tarafinda localdeki `.env` dosyasini yukleme; degerleri `Variables` icine tek tek ekle.
- Local testte `USE_DYNAMIC_SYMBOLS=true` ile basla; cok az sembol gelirse hacim limitlerini genislet.

Paper trade aciksa bot sinyal mesajina ek olarak:

- "Paper Trade Acildi" mesaji yollar
- TP/SL oldugunda "Paper Trade Kapandi" mesaji yollar
- Islem sonucunu `paper_trades.csv` dosyasina yazar

## Önemli Not

Bu bot yatırım tavsiyesi vermez. Sinyaller örnek/otomasyon amaçlıdır; canlı işlem öncesi mutlaka ileri test ve risk kontrolü yap.
