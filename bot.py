import logging
import os
import re
import asyncio
import json
from dataclasses import dataclass
from typing import Optional, List, Set, Tuple, Union, Dict
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
POSITIONS_PER_SIGNAL = int(os.getenv("POSITIONS_PER_SIGNAL", "2"))
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

SYMBOLS_FILE = "available_symbols.json"
AVAILABLE_SYMBOLS: Set[str] = set()


MESSAGE_POSITIONS: Dict[int, List[int]] = {}
OPEN_POSITIONS_FILE = "open_positions.json"
OPEN_POSITIONS: Dict[int, Dict[str, Union[str, float, int]]] = {}


def _to_input(value: str) -> Union[int, str]:
    """Return int for numeric identifiers or the original string."""
    return int(value) if re.fullmatch(r"-?\d+", value) else value


def load_open_positions() -> None:
    """Load saved open positions from disk."""
    global OPEN_POSITIONS
    if os.path.exists(OPEN_POSITIONS_FILE):
        try:
            with open(OPEN_POSITIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            OPEN_POSITIONS = {int(k): v for k, v in data.items()}
        except Exception as exc:
            logger.warning("Failed to load %s: %s", OPEN_POSITIONS_FILE, exc)
            OPEN_POSITIONS = {}
    else:
        OPEN_POSITIONS = {}


def save_open_positions() -> None:
    """Persist open positions to disk."""
    try:
        with open(OPEN_POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(OPEN_POSITIONS, f)
    except Exception as exc:
        logger.warning("Failed to save %s: %s", OPEN_POSITIONS_FILE, exc)


def refresh_open_positions() -> None:
    """Fetch current MT5 positions and persist them."""
    if mt5 is None:
        OPEN_POSITIONS.clear()
        return
    positions = mt5.positions_get() or []
    OPEN_POSITIONS.clear()
    for pos in positions:
        OPEN_POSITIONS[pos.ticket] = {
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": pos.type,
        }
    save_open_positions()


def load_symbols() -> None:
    """Load available symbols from disk."""
    global AVAILABLE_SYMBOLS
    if os.path.exists(SYMBOLS_FILE):
        try:
            with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                AVAILABLE_SYMBOLS = {s.upper() for s in data}
            else:
                AVAILABLE_SYMBOLS = set()
        except Exception as exc:
            logger.warning("Failed to load %s: %s", SYMBOLS_FILE, exc)
            AVAILABLE_SYMBOLS = set()
    else:
        AVAILABLE_SYMBOLS = set()


def save_symbols() -> None:
    """Persist available symbols to disk."""
    try:
        with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(AVAILABLE_SYMBOLS), f)
    except Exception as exc:
        logger.warning("Failed to save %s: %s", SYMBOLS_FILE, exc)


def refresh_symbols() -> None:
    """Fetch available MT5 symbols and persist them."""
    if mt5 is None:
        AVAILABLE_SYMBOLS.clear()
        return
    symbols = mt5.symbols_get() or []
    AVAILABLE_SYMBOLS.clear()
    for sym in symbols:
        name = getattr(sym, "name", None)
        if isinstance(name, str):
            AVAILABLE_SYMBOLS.add(name.upper())
    save_symbols()

@dataclass
class TradeSignal:
    action: str
    symbol: str
    timeframe: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


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
    synonyms = {"VOL": "Volatility", "VIX": "Volatility"}
    symbol = synonyms.get(symbol, symbol)
    if AVAILABLE_SYMBOLS and symbol not in AVAILABLE_SYMBOLS:
        logger.error("Symbol %s not found", symbol)
        return None
    timeframe = match.group("timeframe") or "1s"

    sl_match = re.search(r"sl[:\s]*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    tp_match = re.search(r"tp[:\s]*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    stop_loss = float(sl_match.group(1)) if sl_match else None
    take_profit = float(tp_match.group(1)) if tp_match else None

    return TradeSignal(
        action=action,
        symbol=symbol,
        timeframe=timeframe,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


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


def can_open_trade() -> bool:
    """Check if there is enough free margin to open a new trade."""
    if mt5 is None:
        return False

    account = mt5.account_info()
    if account is None:
        return False

    free_margin = account.equity - account.margin
    required_margin = account.equity * RISK_PERCENT / 100.0
    return free_margin > required_margin


def place_order(signal: TradeSignal):
    if mt5 is None:
        logger.error("MT5 not available")
        return

    account_info = mt5.account_info()
    if account_info is None:
        logger.error("Unable to get account info")
        return

    equity = account_info.equity
    lot = calculate_lot(equity, RISK_PERCENT)

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
    if signal.stop_loss is not None:
        request["sl"] = signal.stop_loss
    if signal.take_profit is not None:
        request["tp"] = signal.take_profit
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


def update_sl_tp(ticket: int, sl: float, tp: float):
    """Update stop loss and take profit for an open position."""
    if mt5 is None:
        return

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": sl,
        "tp": tp,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("Failed to update SL/TP: %s", result)
    else:
        logger.info("SL/TP updated for ticket %s", ticket)


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
        OPEN_POSITIONS.pop(ticket, None)
        save_open_positions()


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
            OPEN_POSITIONS.pop(ticket, None)
            save_open_positions()
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
    refresh_open_positions()
    refresh_symbols()
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
    refresh_open_positions()
    refresh_symbols()

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
            if any(p.get("symbol") == signal.symbol for p in OPEN_POSITIONS.values()):
                logger.info("Position already open for %s", signal.symbol)
                return
            tickets: List[int] = []
            for _ in range(POSITIONS_PER_SIGNAL):
                if not can_open_trade():
                    logger.warning("Insufficient equity for additional position")
                    break
                ticket = place_order(signal)
                if ticket:
                    tickets.append(ticket)
                    OPEN_POSITIONS[ticket] = {"symbol": signal.symbol}
                    save_open_positions()
                    asyncio.create_task(monitor_trade(ticket, signal))
            if tickets:
                MESSAGE_POSITIONS[event.message.id] = tickets
        else:
            logger.debug("No valid signal found in message: %s", text)

    @client.on(events.MessageEdited)
    async def handle_edit(event):
        tickets = MESSAGE_POSITIONS.get(event.message.id)
        if not tickets:
            return
        text = event.message.message
        signal = parse_signal(text)
        if not signal or signal.stop_loss is None or signal.take_profit is None:
            return
        for ticket in tickets:
            update_sl_tp(ticket, signal.stop_loss, signal.take_profit)

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

    load_open_positions()
    load_symbols()
    if connect_mt5():
        refresh_open_positions()
        refresh_symbols()

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
