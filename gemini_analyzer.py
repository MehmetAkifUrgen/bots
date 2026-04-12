"""
gemini_analyzer.py — Teknik analiz raporlarina dogal dil yorumu ekler.
Google Gemini API kullanarak teknik sinyalleri aciklar ve risk notlari ekler.

Kurulum:
  pip install google-generativeai
  GEMINI_API_KEY ortam degiskenini ayarla

Kullanim:
  python gemini_analyzer.py --test
"""

import os
import json
from dataclasses import dataclass
from typing import Optional

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


@dataclass
class GeminiCommentary:
    market_outlook: str  # Genel piyasa yorumu
    risk_assessment: str  # Risk degerlendirmesi
    setup_explanation: str  # Setup aciklamasi
    confidence_factors: str  # Guven artırıcı/azaltıcı faktörler
    alternative_scenario: str  # Alternatif senaryo


def configure_gemini(api_key: Optional[str] = None) -> bool:
    """Gemini API'yi yapilandir."""
    if not GEMINI_AVAILABLE:
        print("⚠️  google-generativeai yuklu degil. 'pip install google-generativeai' ile yukle.")
        return False
    
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        print("⚠️  GEMINI_API_KEY ortam degiskeni bulunamadi.")
        return False
    
    genai.configure(api_key=key)
    return True


def generate_commentary(
    symbol: str,
    decision: str,
    setup_type: str,
    confidence: int,
    price_change_pct: float,
    funding_rate: Optional[float],
    oi_trend: Optional[str],
    oi_strength: Optional[int],
    technical_summary: str,
    regime: str = "UNKNOWN",
    regime_strength: int = 0,
) -> Optional[GeminiCommentary]:
    """
    Teknik verilerden dogal dil yorumu uret.
    
    Args:
        symbol: Sembol (örn: BTCUSDT)
        decision: LONG, SHORT, veya WAIT
        setup_type: Setup tipi
        confidence: Guven skoru (0-100)
        price_change_pct: 24s fiyat degisimi
        funding_rate: Funding rate percentage
        oi_trend: Open Interest trendi (INCREASING/DECREASING/STABLE)
        oi_strength: OI trend gucu (0-100)
        technical_summary: Teknik indikatorler ozeti
        regime: Piyasa kosulu (TRENDING_UP/TRENDING_DOWN/RANGING)
        regime_strength: Piyasa kosulu gucu
        
    Returns:
        GeminiCommentary veya None
    """
    if not GEMINI_AVAILABLE or not genai:
        return None
    
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        
        # Prompt olustur - Türkce yanit istiyoruz
        prompt = f"""
Sen profesyonel bir kripto para analistsin. Binance Futures icin teknik analiz yorumu yapacaksin.

VERILENLER:
- Sembol: {symbol}
- Karar: {decision}
- Setup Tipi: {setup_type}
- Guven Skoru: {confidence}/100
- 24s Fiyat Degisimi: {price_change_pct:.2f}%
- Funding Rate: {funding_rate if funding_rate is not None else 'Bilinmiyor'}%
- Open Interest Trendi: {oi_trend or 'Bilinmiyor'} (Guc: {oi_strength or 0}/100)
- Piyasa Kosulu: {regime} (Guc: {regime_strength}/100)
- Teknik Ozet: {technical_summary}

ISTENENLER:
Lutfen asagidaki 5 soruya kisa, net ve profesyonel bir sekilde yanit ver. Her yanit maksimum 2-3 cumle olmali.

1. Market Outlook: Bu coin icin genel piyasa gorunumu nedir? Trend devam eder mi?
2. Risk Assessment: Bu pozisyonun riskleri neler? Nelere dikkat edilmeli?
3. Setup Explanation: Bu setup neden guclu/zayif? Hangi faktörler etkili?
4. Confidence Factors: Guven skorunu artiran veya azaltan faktorler neler?
5. Alternative Scenario: Ana senaryo tutmazsa ne olur? Alternatif nedir?

Yaniti JSON formatinda ver. Ornegin:
{{
  "market_outlook": "...",
  "risk_assessment": "...",
  "setup_explanation": "...",
  "confidence_factors": "...",
  "alternative_scenario": "..."
}}

ONCELIK: Kisa, net, ve isleme yarayan bilgiler ver. Genel goruslerden kacin.
"""
        
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # JSON parse et
        try:
            # Markdown kod blogu varsa temizle
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            
            data = json.loads(text.strip())
            
            return GeminiCommentary(
                market_outlook=data.get("market_outlook", "Yorum yok."),
                risk_assessment=data.get("risk_assessment", "Risk degerlendirmesi yok."),
                setup_explanation=data.get("setup_explanation", "Setup aciklamasi yok."),
                confidence_factors=data.get("confidence_factors", "Faktorler belirtilmemis."),
                alternative_scenario=data.get("alternative_scenario", "Alternatif senaryo yok."),
            )
        except json.JSONDecodeError as e:
            print(f"  [Gemini JSON Parse Hatasi]: {e}")
            print(f"  Ham yanit: {text[:200]}")
            return None
            
    except Exception as e:
        print(f"  [Gemini Commentary Hatasi]: {e}")
        return None


def format_commentary_for_telegram(commentary: GeminiCommentary) -> str:
    """Gemini yorumunu Telegram mesaji formatina cevir."""
    return (
        f"🧠 *Gemini Analizi*\n\n"
        f"📊 *Piyasa*: {commentary.market_outlook}\n\n"
        f"⚠️ *Risk*: {commentary.risk_assessment}\n\n"
        f"🎯 *Setup*: {commentary.setup_explanation}\n\n"
        f"💪 *Guven*: {commentary.confidence_factors}\n\n"
        f"🔄 *Alternatif*: {commentary.alternative_scenario}"
    )


def test_gemini():
    """Gemini entegrasyonunu test et."""
    if not configure_gemini():
        print("❌ Gemini yapilandirilamadi. GEMINI_API_KEY kontrol et.")
        return
    
    print("✅ Gemini basariyla yapilandirildi!")
    print("\n🧪 Test yorumu olusturuluyor...\n")
    
    commentary = generate_commentary(
        symbol="BTCUSDT",
        decision="LONG",
        setup_type="trend continuation",
        confidence=85,
        price_change_pct=3.5,
        funding_rate=0.01,
        oi_trend="INCREASING",
        oi_strength=75,
        technical_summary="4h ve 1h trend yukari, 15m geri cekilme sonrasi EMA20 destegi, hacim artisi",
        regime="TRENDING_UP",
        regime_strength=80,
    )
    
    if commentary:
        print("✅ Test yorumu basarili!")
        print("\n" + "="*60)
        print(format_commentary_for_telegram(commentary))
        print("="*60)
    else:
        print("❌ Test yorumu basarisiz oldu.")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Gemini Analiz Modulu Test")
    parser.add_argument("--test", action="store_true", help="Test modu")
    
    args = parser.parse_args()
    
    if args.test:
        test_gemini()
    else:
        print("Kullanim: python gemini_analyzer.py --test")
        print("\nBu modül teknik analiz raporlarina dogal dil yorumu ekler.")
        print("Detaylar icin README.md'ye bak.")
