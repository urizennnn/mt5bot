import logging
import os
import re
import asyncio
from dataclasses import dataclass
from typing import Optional

try:
    import MetaTrader5 as mt5
except ImportError:  # environment might not have MetaTrader5
    mt5 = None  # type: ignore

try:
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
except ImportError:
    Update = None  # type: ignore
    ApplicationBuilder = CommandHandler = ContextTypes = MessageHandler = filters = None  # type: ignore


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str
    symbol: str
    timeframe: str


def parse_signal(text: str) -> Optional[TradeSignal]:
    """Parse a Telegram message into a trading signal."""
    pattern = re.compile(
        r"(?P<action>buy|sell)\s+(?P<instrument>vix|vol|volatility)\s*(?P<symbol>\d+)\s*(?P<timeframe>\w*)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None

    action = match.group("action").lower()
    symbol = f"VOL{match.group('symbol')}"  # convert to standard symbol name
    timeframe = match.group("timeframe") or "1s"
    return TradeSignal(action=action, symbol=symbol, timeframe=timeframe)


def connect_mt5() -> bool:
    """Initialize connection to MT5 using credentials from environment."""
    if mt5 is None:
        logger.error("MetaTrader5 package is not installed.")
        return False

    if mt5.initialize():
        return True

    login = int(os.environ.get("MT5_LOGIN", "0"))
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    if login and password and server:
        if mt5.initialize(login=login, password=password, server=server):
            return True

    logger.error("Failed to initialize MT5")
    return False


def calculate_lot(balance: float, risk_percent: float = 1.0) -> float:
    """Return lot size based on account balance and risk."""
    risk_amount = balance * risk_percent / 100.0
    lot = max(round(risk_amount / 100.0, 2), 0.01)
    return lot


def place_order(signal: TradeSignal):
    if mt5 is None:
        logger.error("MT5 not available")
        return

    account_info = mt5.account_info()
    if account_info is None:
        logger.error("Unable to get account info")
        return

    balance = account_info.balance
    lot = calculate_lot(balance)

    symbol_info = mt5.symbol_info(signal.symbol)
    if symbol_info is None:
        logger.error("Symbol %s not found", signal.symbol)
        return
    if not symbol_info.visible:
        if not mt5.symbol_select(signal.symbol, True):
            logger.error("Failed to select symbol %s", signal.symbol)
            return

    tick = mt5.symbol_info_tick(signal.symbol)
    if tick is None:
        logger.error("No tick data for %s", signal.symbol)
        return

    order_type = mt5.ORDER_TYPE_BUY if signal.action == "buy" else mt5.ORDER_TYPE_SELL
    price = tick.ask if signal.action == "buy" else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": signal.symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 1000,
        "comment": "telegram signal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("Order failed: %s", result)
        return

    logger.info("Order %s placed, ticket %s", signal.action, result.order)
    return result.order


def set_break_even(ticket: int, price: float):
    """Move stop-loss to the entry price."""
    if mt5 is None:
        return

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": price,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("Failed to set break even: %s", result)
    else:
        logger.info("Break even set for ticket %s", ticket)


def check_reversal_and_close(ticket: int, signal: TradeSignal):
    """Close trade if price moves against position beyond threshold."""
    if mt5 is None:
        return

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return
    position = positions[0]
    entry_price = position.price_open
    volume = position.volume

    tick = mt5.symbol_info_tick(signal.symbol)
    if tick is None:
        return

    current_price = tick.bid if signal.action == "buy" else tick.ask
    # close if price reverses by more than 0.5%
    if signal.action == "buy" and current_price < entry_price * 0.995:
        close_type = mt5.ORDER_TYPE_SELL
    elif signal.action == "sell" and current_price > entry_price * 1.005:
        close_type = mt5.ORDER_TYPE_BUY
    else:
        return

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol": signal.symbol,
        "volume": volume,
        "type": close_type,
        "price": current_price,
        "deviation": 20,
        "magic": 1000,
        "comment": "reversal close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("Failed to close position: %s", result)
    else:
        logger.info("Position %s closed", ticket)


async def monitor_trade(ticket: int, signal: TradeSignal):
    """Monitor open position to set break-even and close on reversal."""
    if mt5 is None:
        return

    breakeven_set = False
    while True:
        await asyncio.sleep(5)
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.info("Ticket %s no longer open", ticket)
            break
        position = positions[0]
        entry_price = position.price_open
        tick = mt5.symbol_info_tick(signal.symbol)
        if tick is None:
            continue
        current_price = tick.bid if signal.action == "buy" else tick.ask
        profit_points = current_price - entry_price if signal.action == "buy" else entry_price - current_price
        if not breakeven_set and profit_points > entry_price * 0.002:
            set_break_even(ticket, entry_price)
            breakeven_set = True
        check_reversal_and_close(ticket, signal)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # type: ignore
    text = update.message.text  # type: ignore
    signal = parse_signal(text)
    if signal:
        if connect_mt5():
            ticket = place_order(signal)
            if ticket:
                asyncio.create_task(monitor_trade(ticket, signal))
        await update.message.reply_text(f"Received signal: {signal}")
    else:
        logger.debug("No valid signal found in message: %s", text)


def main(token: str):
    if ApplicationBuilder is None:
        logger.error("telegram package not installed")
        return
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    import os
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        main(telegram_token)
    else:
        logger.error("TELEGRAM_BOT_TOKEN not set")
