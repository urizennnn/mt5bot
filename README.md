# MT5 Telegram Trading Assistant

This repository provides a simple trading assistant that listens to messages from your personal Telegram account and executes trades through the MetaTrader 5 (MT5) API.

## Features

- Parses trading signals such as `buy EURUSD H1` or `sell BTCUSD m5`.
- Connects to MT5 (requires the MetaTrader5 Python package and a running MT5 terminal).
- Calculates trade lot size based on current account balance.
- Automatically moves stop loss to break even after minimal profit.
- Closes trades if price reverses against the opened position.
- Works with channels, groups and direct chats.

## Usage

1. Install dependencies (Python 3.10+ recommended):
    ```bash
    pip install MetaTrader5 telethon python-dotenv
    ```

2. Create a `.env` file with your Telegram and MT5 credentials:
    ```
    TELEGRAM_API_ID=<api id>
    TELEGRAM_API_HASH=<api hash>
    TELEGRAM_CHANNELS=<comma separated ids or usernames>
    MT5_LOGIN=<account>
    MT5_PASSWORD=<password>
    MT5_SERVER=<broker server>
    RISK_PERCENT=1.0
    ```

3. Run the script:
    ```bash
    python bot.py
    ```

    To test your configuration and fetch the last two messages from each
    configured channel, run:

    ```bash
    python bot.py --test
    ```

    To list the chats, channels and groups your account can access,
    run:

    ```bash
    python bot.py --groups
    ```

The script will start listening to the specified channels using your personal account. When a supported trading signal is received, it will attempt to place a trade via MT5. If a trade is already open, new signals are ignored.

**Note:** Actual trading requires a configured MT5 terminal with login credentials and symbols enabled. The break-even and reversal thresholds in this bot are simple examples and should be adapted to your trading strategy.
