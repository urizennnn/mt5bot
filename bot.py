import logging
import os
import re
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Set, Tuple, Union
import argparse

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import MetaTrader5 as mt5
except ImportError:  # environment might not have MetaTrader5
    mt5 = None  # type: ignore

try:
    from telethon import TelegramClient, events
    from telethon.tl.types import User
except ImportError:
    TelegramClient = None  # type: ignore
    events = None  # type: ignore
    User = None  # type: ignore


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if load_dotenv is not None:
    load_dotenv()

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

ALLOWED_CHANNELS: List[str] = [
    c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()
]

def _parse_allowed(raw: List[str]) -> Tuple[Set[int], Set[str]]:
    ids: Set[int] = set()
    names: Set[str] = set()
    for item in raw:
        if re.fullmatch(r"-?\d+", item):
            ids.add(int(item))
        else:
            names.add(item.lower())
    return ids, names


ALLOWED_ID_SET, ALLOWED_NAME_SET = _parse_allowed(ALLOWED_CHANNELS)


def _to_input(value: str) -> Union[int, str]:
    """Return int for numeric identifiers or the original string."""
    return int(value) if re.fullmatch(r"-?\d+", value) else value

@dataclass
class TradeSignal:
    action: str
    symbol: str
    timeframe: str


def parse_signal(text: str) -> Optional[TradeSignal]:
    """Parse a Telegram message into a trading signal."""
    pattern = re.compile(
        r"(?P<action>buy|sell)\s+(?P<symbol>\S+)(?:\s+(?P<timeframe>\S+))?",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None

    action = match.group("action").lower()
    symbol = match.group("symbol").upper()
    timeframe = match.group("timeframe") or "1s"
    return TradeSignal(action=action, symbol=symbol, timeframe=timeframe)


def connect_mt5() -> bool:
    """Initialize connection to MT5 using credentials from environment."""
    if mt5 is None:
        logger.error("MetaTrader5 package is not installed.")
        return False

    path = os.getenv("MT5_PATH", "")
    kwargs = {"path": path} if path else {}

    if mt5.initialize(**kwargs):
        return True
    else:
        code, msg = mt5.last_error()
        logger.warning("Initial MT5 initialize failed: %s (%s)", code, msg)

    login = int(os.getenv("MT5_LOGIN", "0"))
    password = os.getenv("MT5_PASSWORD", "")
    server = os.getenv("MT5_SERVER", "")
    if login and password and server:
        kwargs.update({"login": login, "password": password, "server": server})
        if mt5.initialize(**kwargs):
            return True
        else:
            code, msg = mt5.last_error()
            logger.warning("MT5 initialize with credentials failed: %s (%s)", code, msg)

    logger.error("Failed to initialize MT5")
    return False


def calculate_lot(balance: float, risk_percent: float = 1.0) -> float:
    """Return lot size based on account balance and risk."""
    risk_amount = balance * risk_percent / 100.0
    lot = max(round(risk_amount / 100.0, 2), 0.01)
    return lot


def has_open_positions() -> bool:
    """Return True if there are any open MT5 positions."""
    if mt5 is None:
        return False
    positions = mt5.positions_get()
    return bool(positions)


def place_order(signal: TradeSignal):
    if mt5 is None:
        logger.error("MT5 not available")
        return

    account_info = mt5.account_info()
    if account_info is None:
        logger.error("Unable to get account info")
        return

    balance = account_info.balance
    lot = calculate_lot(balance, RISK_PERCENT)

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


async def test_last_messages():
    """Fetch and display the last two messages from allowed channels."""
    if TelegramClient is None:
        logger.error("telethon package not installed")
        return
    if not API_ID or not API_HASH:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
        return

    if not ALLOWED_CHANNELS:
        logger.error("No TELEGRAM_CHANNELS configured for testing")
        return

    client = TelegramClient("mt5bot", API_ID, API_HASH)
    await client.start()
    for channel in ALLOWED_CHANNELS:
        try:
            messages = await client.get_messages(_to_input(channel), limit=2)
        except Exception as exc:
            logger.error("Failed to fetch messages from %s: %s", channel, exc)
            continue

        logger.info("Last 2 messages from %s", channel)
        for msg in reversed(messages):
            text = msg.message or ""
            logger.info(text)
            signal = parse_signal(text)
            if signal:
                logger.info("Parsed signal: %s", signal)
    await client.disconnect()


async def list_chats():
    """List the channels, groups and chats the account can access."""
    if TelegramClient is None:
        logger.error("telethon package not installed")
        return
    if not API_ID or not API_HASH:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
        return

    client = TelegramClient("mt5bot", API_ID, API_HASH)
    await client.start()

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        name = dialog.name
        if getattr(entity, "megagroup", False):
            chat_type = "Group"
        elif getattr(entity, "broadcast", False):
            chat_type = "Channel"
        elif isinstance(entity, User):
            chat_type = "Chat"
        else:
            chat_type = "Unknown"
        logger.info("%s - %s (%s)", dialog.id, name, chat_type)

    await client.disconnect()


def place_test_trade():
    """Place a sample trade on the VIX 25 index."""
    if not connect_mt5():
        return
    signal = TradeSignal(action="buy", symbol="VIX25", timeframe="1s")
    place_order(signal)
    if mt5 is not None:
        mt5.shutdown()


async def run_client():
    if TelegramClient is None:
        logger.error("telethon package not installed")
        return
    if not API_ID or not API_HASH:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
        return

    if not connect_mt5():
        return

    client = TelegramClient("mt5bot", API_ID, API_HASH)

    @client.on(events.NewMessage)
    async def handle_event(event):
        chat = event.chat
        chat_id = event.chat_id
        username = getattr(chat, "username", None) if chat else None
        if (ALLOWED_ID_SET or ALLOWED_NAME_SET) and (
            chat_id not in ALLOWED_ID_SET and (username or "").lower() not in ALLOWED_NAME_SET
        ):
            return

        text = event.message.message
        signal = parse_signal(text)
        if signal and connect_mt5():
            if has_open_positions():
                logger.warning("Open position detected, skipping new trade")
                return
            ticket = place_order(signal)
            if ticket:
                asyncio.create_task(monitor_trade(ticket, signal))
        else:
            logger.debug("No valid signal found in message: %s", text)

    await client.start()
    logger.info("Client started")
    await client.run_until_disconnected()


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 Telegram Trading Assistant")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Fetch last two messages from allowed channels and exit",
    )
    parser.add_argument(
        "--trade",
        nargs="?",
        const="default",
        help="Place a test VIX 25 order and exit",
    )
    parser.add_argument(
        "--groups",
        action="store_true",
        help="List available channels, groups and chats and exit",
    )
    args = parser.parse_args()

    connect_mt5()

    if args.trade is not None:
        place_test_trade()
    elif args.test:
        asyncio.run(test_last_messages())
    elif args.groups:
        asyncio.run(list_chats())
    else:
        asyncio.run(run_client())


if __name__ == "__main__":
    main()
