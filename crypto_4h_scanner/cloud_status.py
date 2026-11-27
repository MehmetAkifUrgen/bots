"""Read live Railway paper wallet state & daily PnL report directly via Telegram pinned dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import requests
from dotenv import dotenv_values

from .telegram_notifier import TelegramNotifier


def fetch_live_cloud_report() -> dict:
    root = Path(__file__).resolve().parent.parent
    env = dotenv_values(root / ".env")
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = [c.strip() for c in (env.get("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not token or not chat_ids:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID bulunamadı.")

    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getChat",
        params={"chat_id": chat_ids[0]},
        timeout=10.0,
    )
    resp.raise_for_status()
    chat_data = resp.json().get("result") or {}
    pinned = chat_data.get("pinned_message")
    if not pinned:
        return {"status": "no_pinned_dashboard_yet"}

    payload = TelegramNotifier.unpack_cloud_payload(pinned)
    tz_tr = timezone(timedelta(hours=3))
    today_str = datetime.now(tz_tr).strftime("%Y-%m-%d")

    out: dict = {
        "status": "ok",
        "today_tr": today_str,
        "dashboard_text": pinned.get("text", ""),
        "wallets": {},
    }
    if payload and isinstance(payload.get("wallets"), dict):
        for wname, snap in payload["wallets"].items():
            hist = snap.get("closed_history") or []
            today_rows = [r for r in hist if r.get("date_tr") == today_str]
            wins = [r for r in today_rows if float(r.get("pnl_usd", 0.0)) > 0]
            losses = [r for r in today_rows if float(r.get("pnl_usd", 0.0)) <= 0]
            stops = [r for r in today_rows if bool(r.get("is_stop")) or float(r.get("pnl_usd", 0.0)) < 0]
            p_win = sum(float(r.get("pnl_usd", 0.0)) for r in wins)
            p_loss = sum(float(r.get("pnl_usd", 0.0)) for r in losses)
            p_fees = sum(float(r.get("fee_usd", 0.0)) for r in today_rows)
            out["wallets"][wname] = {
                "balance": snap.get("paper_balance"),
                "initial_balance": snap.get("paper_initial_balance"),
                "total_trades": snap.get("paper_total_trades"),
                "total_wins": snap.get("paper_win_trades"),
                "total_fees": snap.get("paper_total_fees"),
                "total_realized_pnl": snap.get("paper_realized_pnl"),
                "today_trades": len(today_rows),
                "today_wins": len(wins),
                "today_stops": len(stops),
                "today_profit_usd": round(p_win, 2),
                "today_loss_usd": round(p_loss, 2),
                "today_net_usd": round(p_win + p_loss, 2),
                "today_fees_usd": round(p_fees, 3),
                "open_positions": snap.get("open_positions", {}),
                "recent_closed_trades": hist,
            }
    return out


if __name__ == "__main__":
    print(json.dumps(fetch_live_cloud_report(), ensure_ascii=False, indent=2))
