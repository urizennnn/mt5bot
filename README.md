# MT5 Telegram Trading Bot

This repository provides a simple trading bot that listens to Telegram messages and executes trades through the MetaTrader 5 (MT5) API.

## Features

- Parses trading signals such as `buy vix 75 s` or `sell vol 10`.
- Connects to MT5 (requires the MetaTrader5 Python package and a running MT5 terminal).
- Calculates trade lot size based on current account balance.
- Automatically moves stop loss to break even after minimal profit.
- Closes trades if price reverses against the opened position.

## Usage

1. Install dependencies (Python 3.10+ recommended):
   ```bash
   pip install MetaTrader5 python-telegram-bot
   ```

2. Set your Telegram bot token in the `TELEGRAM_BOT_TOKEN` environment variable.
   ```bash
   export TELEGRAM_BOT_TOKEN="<your token>"
   ```

3. Set MT5 credentials (if needed):
   ```bash
   export MT5_LOGIN=<account>
   export MT5_PASSWORD=<password>
   export MT5_SERVER=<broker server>
   ```

4. Run the bot:
   ```bash
   python bot.py
   ```

The bot will start polling Telegram for new messages. When a supported trading signal is received, it will attempt to place a trade via MT5.

**Note:** Actual trading requires a configured MT5 terminal with login credentials and symbols enabled. The break-even and reversal thresholds in this bot are simple examples and should be adapted to your trading strategy.
