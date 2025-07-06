# MT5 Telegram Trading Bot

This repository provides a simple trading bot that listens to Telegram messages and executes trades through the MetaTrader 5 (MT5) API.

## Features

- Parses trading signals such as `buy EURUSD H1` or `sell BTCUSD m5`.
- Connects to MT5 (requires the MetaTrader5 Python package and a running MT5 terminal).
- Calculates trade lot size based on current account balance.
- Automatically moves stop loss to break even after minimal profit.
- Closes trades if price reverses against the opened position.

## Usage

1. Install dependencies (Python 3.10+ recommended):
   ```bash
   pip install MetaTrader5 python-telegram-bot python-dotenv
   ```

2. Create a `.env` file with your bot and MT5 credentials:
   ```
   TELEGRAM_BOT_TOKEN=<your token>
   MT5_LOGIN=<account>
   MT5_PASSWORD=<password>
   MT5_SERVER=<broker server>
   RISK_PERCENT=1.0
   ```

3. Run the bot:
   ```bash
   python bot.py
   ```

The bot will start polling Telegram for new messages. When a supported trading signal is received, it will attempt to place a trade via MT5.

**Note:** Actual trading requires a configured MT5 terminal with login credentials and symbols enabled. The break-even and reversal thresholds in this bot are simple examples and should be adapted to your trading strategy.
