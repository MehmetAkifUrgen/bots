#!/bin/bash
# =====================================================
# FIBONACCI GOLDEN POCKET BOT — ORACLE CLOUD KURULUMU
# =====================================================
# Kullanım: Bu dosyayı Oracle sunucuna atıp  chmod +x setup.sh && sudo ./setup.sh  diye çalıştır

set -e

PROJECT_DIR="/home/ubuntu/fib-pocket-bot"
SERVICE_NAME="fib-pocket-bot"

echo ""
echo "⚡══════════════════════════════════════════════════════════════"
echo "  FIBONACCI GOLDEN POCKET BOT — ORACLE CLOUD KURULUMU"
echo "══════════════════════════════════════════════════════════════"
echo ""

# 1. Paket güncellemeleri
echo "[1/7] 📦 Sistem paketleri güncelleniyor..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq --with-new-pkgs 2>/dev/null || true
apt-get install -y -qq python3 python3-pip python3-venv python3-wheel git curl wget htop jq
echo "       ✅ Tamam"

# 2. Proje klasörü
echo "[2/7] 📁 Proje klasörü hazırlanıyor: $PROJECT_DIR"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
echo "       ✅ Tamam"

# 3. requirements.txt mevcutsa bağımlılıkları kur
echo "[3/7] 🐍 Python bağımlılıkları kuruluyor..."
if [ -f requirements.txt ]; then
    pip3 install --break-system-packages -q -r requirements.txt 2>&1 | tail -5
else
    pip3 install --break-system-packages -q pandas numpy requests python-dotenv 2>&1 | tail -5
fi
echo "       ✅ Tamam"

# 4. Servis dosyasını sistem dizinine kopyala
echo "[4/7] 🔧 Systemd servisi yükleniyor..."
if [ -f fib-pocket-bot.service ]; then
    cp fib-pocket-bot.service /etc/systemd/system/$SERVICE_NAME.service
    systemctl daemon-reload
    systemctl enable $SERVICE_NAME 2>&1 | tail -2
    echo "       ✅ Servis systemd'ye kaydedildi ve enable edildi"
else
    echo "       ⚠️ fib-pocket-bot.service bulunamadı — atlanıyor"
fi

# 5. Güvenlik uyarısı + .env kontrolü
echo "[5/7] 🔐 Çevresel değişkenler (.env) kontrol ediliyor..."
if [ -f .env ]; then
    chmod 600 .env
    chown ubuntu:ubuntu .env
    ENV_OK=$(grep -c "^TELEGRAM_BOT_TOKEN=" .env 2>/dev/null || echo 0)
    if [ "$ENV_OK" -lt 1 ]; then
        echo "       ⚠️ .env dosyası mevcut ama içerik boş / eksik olabilir"
    else
        echo "       ✅ .env dosyası mevcut ve güvenli izinlerde (600)"
    fi
else
    echo "       ⚠️ .env dosyası bulunamadı — lütfen oluştur:  $PROJECT_DIR/.env"
    echo "       İçeriği şu şekilde doldur:"
    echo ""
    echo "       TELEGRAM_BOT_TOKEN=123456789:AAxxxx...."
    echo "       TELEGRAM_CHAT_ID=987654321"
    echo "       BINANCE_API_KEY=xxxxxxxxxxxxxxx"
    echo "       BINANCE_API_SECRET=yyyyyyyyyyyyy"
    echo "       BINANCE_SPOT_BASE=https://api.binance.com"
    echo ""
fi

# 6. trader_bot.py var mı?
echo "[6/7] 🧠 Ana betik kontrol ediliyor..."
if [ -f trader_bot.py ]; then
    python3 -m py_compile trader_bot.py && echo "       ✅ trader_bot.py — syntax TEMİZ"
else
    echo "       ⚠️ trader_bot.py bulunamadı — proje dosyalarını kopyalamadın mı?"
fi

echo "[7/7] 🚀 Kurulum tamamlandı!"
echo ""
echo "─────────────────────────────────────────────────────────────"
echo "  SON ADIMLAR:"
echo ""
echo "  1. .env dosyasını DOLDUR (yoksa):"
echo "     sudo nano $PROJECT_DIR/.env"
echo ""
echo "  2. Tüm dosyaların sahibini ubuntu yap:"
echo "     sudo chown -R ubuntu:ubuntu $PROJECT_DIR"
echo ""
echo "  3. Botu BAŞLAT:"
echo "     sudo systemctl start $SERVICE_NAME"
echo ""
echo "  4. Durum kontrolü:"
echo "     sudo systemctl status $SERVICE_NAME"
echo "     sudo journalctl -u $SERVICE_NAME -f --since '1 min ago'"
echo ""
echo "  5. Durdurmak için:"
echo "     sudo systemctl stop $SERVICE_NAME"
echo ""
echo "  6. Bot güncelledikten sonra (kod değişikliğinde):"
echo "     sudo systemctl restart $SERVICE_NAME"
echo "─────────────────────────────────────────────────────────────"
echo ""
