import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config.settings import Settings
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from storage.trade_log import close_trade, fetch_closed_trades_since, fetch_open_trades, fetch_recent_closed_trades

logger = logging.getLogger("TelegramBot")


def _price_matches(a: float, b: float) -> bool:
    return abs(a - b) / max(abs(b), 1e-9) < 0.001


@dataclass
class BotState:
    paused: bool = False


class TelegramBot:
    def __init__(self, settings: Settings, info: Info, exchange: Exchange, bot_state: BotState) -> None:
        self._settings = settings
        self._info = info
        self._exchange = exchange
        self._bot_state = bot_state
        self._app = Application.builder().token(settings.telegram_bot_token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        for name, handler in [
            ("status", self._cmd_status),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("history", self._cmd_history),
            ("position", self._cmd_position),
            ("close", self._cmd_close),
            ("stats", self._cmd_stats),
            ("help", self._cmd_help),
        ]:
            self._app.add_handler(CommandHandler(name, handler))

    def _is_authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != self._settings.telegram_authorized_user_id:
            logger.warning(f"Unauthorized Telegram access | user_id={user_id}")
            return False
        return True

    async def send(self, text: str) -> None:
        try:
            await self._app.bot.send_message(
                chat_id=self._settings.telegram_authorized_user_id,
                text=text,
            )
        except Exception as error:
            logger.error(f"Telegram send failed: {error}")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_state = await asyncio.to_thread(self._info.user_state, self._settings.hl_account_address)
        open_positions = [
            p["position"] for p in user_state.get("assetPositions", [])
            if float(p.get("position", {}).get("szi", 0)) != 0
        ]

        if not open_positions:
            withdrawable = float(user_state.get("withdrawable", 0))
            if withdrawable == 0:
                spot_state = await asyncio.to_thread(
                    self._info.spot_user_state, self._settings.hl_account_address
                )
                for b in spot_state.get("balances", []):
                    if b["coin"] == "USDC":
                        withdrawable = float(b["total"])
                        break
            await update.message.reply_text(
                f"No open positions.\n\n"
                f"💰 Available: ${withdrawable:,.2f}"
            )
            return

        sections = []
        for pos in open_positions:
            szi = float(pos["szi"])
            side = "LONG" if szi > 0 else "SHORT"
            direction_emoji = "🟢" if szi > 0 else "🔴"
            size = abs(szi)
            position_value = float(pos.get("positionValue", 0))
            avg_entry = float(pos.get("entryPx", 0))
            raw_liq = pos.get("liquidationPx")
            liq_str = f"${float(raw_liq):,.2f}" if raw_liq else "N/A"
            upnl = float(pos.get("unrealizedPnl", 0))
            upnl_sign = "+" if upnl >= 0 else ""
            pnl_pct_str = "N/A"
            if avg_entry > 0 and size > 0:
                pnl_pct = (upnl / (avg_entry * size)) * 100
                pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
            sections.append(
                f"{direction_emoji} {pos['coin']} {side}\n\n"
                f"📐 Size: {size} (${position_value:,.2f})\n"
                f"💵 Avg Entry: ${avg_entry:,.2f}\n"
                f"💀 Liq: {liq_str}\n"
                f"📈 uPnL: {upnl_sign}${upnl:,.2f} ({pnl_pct_str})"
            )
        await update.message.reply_text("\n\n".join(sections))

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        self._bot_state.paused = True
        await update.message.reply_text("Bot paused. Signals will be ignored.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        self._bot_state.paused = False
        await update.message.reply_text("Bot resumed. Listening for signals.")

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        trades = await fetch_recent_closed_trades(10)
        if not trades:
            await update.message.reply_text("No closed trades yet.")
            return
        lines = []
        for t in trades:
            pnl = t.get("pnl")
            pnl_str = f"+${pnl:,.2f}" if pnl and pnl >= 0 else f"-${abs(pnl):,.2f}" if pnl else "N/A"
            lines.append(f"{t['coin']} {t['side']} | Entry ${t['entry_px']:,.2f} | {t['status']} | PnL {pnl_str}")
        await update.message.reply_text("Recent trades:\n" + "\n".join(lines))

    async def _cmd_position(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        trades = await fetch_open_trades()
        if not trades:
            await update.message.reply_text("No open trades.")
            return

        user_state, all_mids, spot_state = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, self._settings.hl_account_address),
            asyncio.to_thread(self._info.all_mids),
            asyncio.to_thread(self._info.spot_user_state, self._settings.hl_account_address),
        )

        hl_positions = {}
        for ap in user_state.get("assetPositions", []):
            pos = ap.get("position", {})
            coin = pos.get("coin", "")
            if coin and float(pos.get("szi", 0)) != 0:
                hl_positions[coin] = pos

        margin = user_state.get("marginSummary", {})
        margin_used = float(margin.get("totalMarginUsed", 0))
        spot_usdc = next(
            (float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"),
            0.0,
        )
        account_value = spot_usdc
        available = spot_usdc - margin_used
        margin_pct = (margin_used / account_value * 100) if account_value > 0 else 0

        trades_by_coin: dict[str, list] = {}
        for trade in trades:
            trades_by_coin.setdefault(trade["coin"], []).append(trade)

        sections = []
        for coin, coin_trades in trades_by_coin.items():
            side = coin_trades[0]["side"]
            direction_emoji = "🟢" if side == "LONG" else "🔴"
            hl_pos = hl_positions.get(coin, {})

            total_size = abs(float(hl_pos["szi"])) if hl_pos else sum(float(t["size"]) for t in coin_trades)
            mark_px = float(all_mids.get(coin, 0))
            position_value = total_size * mark_px if mark_px else 0
            avg_entry = float(hl_pos.get("entryPx", 0)) if hl_pos else 0
            raw_liq = hl_pos.get("liquidationPx") if hl_pos else None
            liq_str = f"${float(raw_liq):,.2f}" if raw_liq else "N/A"

            leverage_val = hl_pos.get("leverage", {}).get("value") if hl_pos else None
            leverage_str = f"{leverage_val}x" if leverage_val else "N/A"

            cum_funding = hl_pos.get("cumFunding", {}) if hl_pos else {}
            funding = float(cum_funding.get("sinceOpen", 0))
            funding_str = f"{'+' if funding >= 0 else ''}${funding:,.4f}"

            trade_rows = []
            for trade in coin_trades:
                idx = trades.index(trade) + 1
                trade_rows.append(
                    f"  #{idx} — {float(trade['size'])} @ ${float(trade['entry_px']):,.2f}"
                    f" | ✅ ${float(trade['tp_px']):,.2f} | ⛔ ${float(trade['sl_px']):,.2f}"
                )

            sections.append(
                f"{direction_emoji} {coin} {side} — {len(coin_trades)} trade{'s' if len(coin_trades) > 1 else ''}\n\n"
                f"📐 Total Size: {total_size} (${position_value:,.2f})\n"
                f"💵 Avg Entry: ${avg_entry:,.2f}\n"
                f"💀 Liq: {liq_str}\n"
                f"⚡ Leverage: {leverage_str}\n"
                f"🔛 Funding: {funding_str}\n"
                + "\n".join(trade_rows)
            )

        total_upnl = sum(float(pos.get("unrealizedPnl", 0)) for pos in hl_positions.values())
        total_cost = sum(
            abs(float(pos.get("szi", 0))) * float(pos.get("entryPx", 0))
            for pos in hl_positions.values()
        )
        upnl_sign = "+" if total_upnl >= 0 else ""
        upnl_pct_str = f" ({upnl_sign}{total_upnl / total_cost * 100:.2f}%)" if total_cost > 0 else ""

        footer = (
            f"\n\n📈 uPnL: {upnl_sign}${total_upnl:,.2f}{upnl_pct_str}\n\n"
            f"🏦 Account Value: ${account_value:,.2f}\n"
            f"🎢 Margin Used: ${margin_used:,.2f} ({margin_pct:.1f}%)\n"
            f"💰 Available: ${available:,.2f}"
        )
        await update.message.reply_text("\n\n".join(sections) + footer)

    async def _cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return

        args = context.args or []
        if len(args) != 1 or (args[0] != "all" and not args[0].isdigit()):
            await update.message.reply_text("Usage: /close <number> or /close all")
            return

        open_trades = await fetch_open_trades()
        if not open_trades:
            await update.message.reply_text("No open trades.")
            return

        if args[0] == "all":
            await self._close_all_trades(update, open_trades)
        else:
            index = int(args[0]) - 1
            if index < 0 or index >= len(open_trades):
                await update.message.reply_text(f"Invalid number. Valid range: 1–{len(open_trades)}")
                return
            await self._close_single_trade(update, open_trades[index])

    async def _fetch_tpsl_oids_by_price(self, coin: str, tp_px: float, sl_px: float) -> list[int]:
        try:
            orders = await asyncio.to_thread(
                self._info.frontend_open_orders, self._settings.hl_account_address
            )
        except Exception as error:
            logger.error(f"Failed to fetch open orders for {coin}: {error}")
            return []
        return [
            o["oid"] for o in orders
            if o.get("coin") == coin
            and o.get("isTrigger")
            and (_price_matches(float(o.get("triggerPx", 0)), tp_px)
                 or _price_matches(float(o.get("triggerPx", 0)), sl_px))
        ]

    async def _fetch_all_trigger_oids(self, coin: str) -> list[int]:
        try:
            orders = await asyncio.to_thread(
                self._info.frontend_open_orders, self._settings.hl_account_address
            )
        except Exception as error:
            logger.error(f"Failed to fetch open orders for {coin}: {error}")
            return []
        return [o["oid"] for o in orders if o.get("coin") == coin and o.get("isTrigger")]

    async def _cancel_oids(self, coin: str, oids: list[int]) -> None:
        if not oids:
            logger.warning(f"No open TP/SL orders found to cancel for {coin}")
            return
        try:
            result = await asyncio.to_thread(
                self._exchange.bulk_cancel,
                [{"coin": coin, "oid": oid} for oid in oids],
            )
            if result.get("status") == "ok":
                logger.info(f"Cancelled {len(oids)} TP/SL order(s) for {coin} | oids={oids}")
            else:
                logger.error(f"bulk_cancel failed for {coin}: {result}")
        except Exception as error:
            logger.error(f"Failed to cancel orders for {coin}: {error}")

    async def _close_single_trade(self, update: Update, trade: dict) -> None:
        coin = trade["coin"]
        size = float(trade["size"])
        entry_px = float(trade["entry_px"])
        side = trade["side"]

        try:
            result = await asyncio.to_thread(
                self._exchange.market_close, coin, sz=size, slippage=0.02
            )
            fill_px = float(result["response"]["data"]["statuses"][0]["filled"]["avgPx"])
        except Exception as error:
            logger.error(f"Close order failed for trade {trade['id']} ({coin}): {error}")
            await update.message.reply_text(f"Close order placed but could not confirm fill price for {coin}.")
            return

        pnl = (fill_px - entry_px) * size if side == "LONG" else (entry_px - fill_px) * size
        await close_trade(trade["id"], pnl, "MANUAL")

        tp_px = float(trade["tp_px"])
        sl_px = float(trade["sl_px"])
        oids = await self._fetch_tpsl_oids_by_price(coin, tp_px, sl_px)
        await self._cancel_oids(coin, oids)

        direction_emoji = "🟢" if side == "LONG" else "🔴"
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_pct = (pnl / (entry_px * size)) * 100 if entry_px > 0 and size > 0 else 0
        pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
        await update.message.reply_text(
            f"{direction_emoji} {coin} {side} CLOSED — MANUAL @ ${fill_px:,.2f}\n\n"
            f"📐 Size: {size} (${size * fill_px:,.2f})\n"
            f"💵 Entry: ${entry_px:,.2f} → Exit: ${fill_px:,.2f}\n"
            f"📈 PnL: {pnl_sign}${pnl:,.2f} ({pnl_pct_str})"
        )

    async def _close_all_trades(self, update: Update, open_trades: list) -> None:
        coins_seen = {}
        for trade in open_trades:
            coins_seen.setdefault(trade["coin"], []).append(trade)

        lines = []
        total_pnl = 0.0
        total_cost = 0.0
        for coin, trades in coins_seen.items():
            try:
                result = await asyncio.to_thread(
                    self._exchange.market_close, coin, slippage=0.02
                )
                fill_px = float(result["response"]["data"]["statuses"][0]["filled"]["avgPx"])
            except Exception as error:
                logger.error(f"Close all failed for {coin}: {error}")
                lines.append(f"{coin} — close failed")
                continue

            for trade in trades:
                entry_px = float(trade["entry_px"])
                size = float(trade["size"])
                side = trade["side"]
                pnl = (fill_px - entry_px) * size if side == "LONG" else (entry_px - fill_px) * size
                total_pnl += pnl
                total_cost += entry_px * size
                await close_trade(trade["id"], pnl, "MANUAL")
                direction_emoji = "🟢" if side == "LONG" else "🔴"
                pnl_sign = "+" if pnl >= 0 else ""
                pnl_pct = (pnl / (entry_px * size)) * 100 if entry_px > 0 and size > 0 else 0
                pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
                lines.append(
                    f"{direction_emoji} {coin} {side} CLOSED — MANUAL @ ${fill_px:,.2f}\n"
                    f"📐 Size: {size} (${size * fill_px:,.2f})\n"
                    f"💵 Entry: ${entry_px:,.2f} → Exit: ${fill_px:,.2f}\n"
                    f"📈 PnL: {pnl_sign}${pnl:,.2f} ({pnl_pct_str})"
                )

            oids = await self._fetch_all_trigger_oids(coin)
            await self._cancel_oids(coin, oids)

        total_sign = "+" if total_pnl >= 0 else ""
        total_pct_str = f" ({total_sign}{total_pnl / total_cost * 100:.2f}%)" if total_cost > 0 else ""
        lines.append(f"💰 Total PnL: {total_sign}${total_pnl:,.2f}{total_pct_str}")
        await update.message.reply_text("\n\n".join(lines))

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return

        args = context.args or []
        period = args[0].lower() if args else None

        if period == "week":
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            label = "Last 7 Days"
        elif period == "month":
            since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            label = "Last 30 Days"
        elif period is None:
            since = None
            label = "All Time"
        else:
            await update.message.reply_text("Usage: /stats or /stats week or /stats month")
            return

        trades = await fetch_closed_trades_since(since)
        if not trades:
            await update.message.reply_text(f"No closed trades — {label}.")
            return

        total = len(trades)
        wins = [t for t in trades if (t["pnl"] or 0) >= 0]
        losses = [t for t in trades if (t["pnl"] or 0) < 0]
        win_rate = len(wins) / total * 100

        total_pnl = sum(t["pnl"] or 0 for t in trades)
        total_cost = sum(float(t["entry_px"]) * float(t["size"]) for t in trades)
        total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

        avg_win = sum(t["pnl"] or 0 for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] or 0 for t in losses) / len(losses) if losses else 0

        best = max(trades, key=lambda t: t["pnl"] or 0)
        worst = min(trades, key=lambda t: t["pnl"] or 0)

        durations = []
        for t in trades:
            if t["opened_at"] and t["closed_at"]:
                opened = datetime.fromisoformat(t["opened_at"])
                closed = datetime.fromisoformat(t["closed_at"])
                durations.append((closed - opened).total_seconds())
        avg_hold_secs = sum(durations) / len(durations) if durations else 0
        hours = int(avg_hold_secs // 3600)
        minutes = int((avg_hold_secs % 3600) // 60)
        hold_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

        tp_count = sum(1 for t in trades if t["status"] == "TP")
        sl_count = sum(1 for t in trades if t["status"] == "SL")
        manual_count = sum(1 for t in trades if t["status"] == "MANUAL")

        pnl_sign = "+" if total_pnl >= 0 else ""
        pct_sign = "+" if total_pct >= 0 else ""

        await update.message.reply_text(
            f"📊 Performance — {label}\n\n"
            f"🏁 Trades: {total} | Win Rate: {win_rate:.1f}%\n"
            f"💰 Total PnL: {pnl_sign}${total_pnl:,.2f} ({pct_sign}{total_pct:.2f}%)\n"
            f"📈 Avg Win: +${avg_win:,.2f} | 📉 Avg Loss: -${abs(avg_loss):,.2f}\n"
            f"🏆 Best: +${best['pnl'] or 0:,.2f} ({best['coin']} {best['side']})\n"
            f"💀 Worst: -${abs(worst['pnl'] or 0):,.2f} ({worst['coin']} {worst['side']})\n"
            f"⏱ Avg Hold: {hold_str}\n\n"
            f"Close Reasons:\n"
            f"  ✅ TP: {tp_count} | ⛔ SL: {sl_count} | 🔧 Manual: {manual_count}"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            "/status — open positions or available balance\n"
            "/position — individual trade records with funding\n"
            "/pause — stop processing signals\n"
            "/resume — resume signals\n"
            "/history — last 10 closed trades\n"
            "/close <number|all> — close a specific trade or all positions\n"
            "/stats — performance dashboard (or /stats week, /stats month)\n"
            "/help — this message"
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        async with self._app:
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started.")
            await stop_event.wait()
            await self._app.updater.stop()
            await self._app.stop()
        logger.info("Telegram bot stopped.")
