# MidasBot — Full Squad, Single Brain

A sophisticated multi-phase cryptocurrency trading bot designed for Kraken and Binance US exchanges.

## Overview

MidasBot operates with **multiple trading phases** controlled by a single intelligent system:
- **SCOUT** - Market analysis and entry detection
- **LUNCHBOX** - Mean-reversion strategy
- **REGULAR** - Grid trading
- **AFTERBURNER** - Momentum trading
- **DIP** - Pullback DCA (Dollar Cost Averaging)

## Features

- **Multi-Exchange Support**: Kraken and Binance US via ccxt
- **Paper Trading Mode**: Safe simulation mode (default)
- **Live Trading**: Optional live trading with safety confirmations
- **Budget Management**: Budget, fee, and minimum notional aware
- **Post-Only Orders**: Price padding to avoid taker fees
- **Trade Logging**: Automatic CSV logging to `family_trades.csv`
- **Technical Indicators**: EMA and RSI calculations built-in
- **YAML Configuration**: Flexible configuration via YAML files

## Installation

```bash
pip install ccxt python-dotenv pyyaml
```

## Quick Start

### Paper Trading (Safe Mode)
```bash
python MidasBot_Full.py --exchange kraken --pair BTC/USD --budget 50
```

### Live Trading (Advanced)
```bash
python MidasBot_Full.py --exchange kraken --pair BTC/USD --budget 50 --live --confirm I-UNDERSTAND
```

**Warning**: Live trading requires API keys and confirmation flag.

## Configuration

### Environment Variables (.env)
```env
BINANCEUS_API_KEY=your_key_here
BINANCEUS_SECRET=your_secret_here
KRAKEN_API_KEY=your_key_here
KRAKEN_SECRET=your_secret_here
MIDAS_LOG=family_trades.csv
```

### Command Line Parameters

| Flag | Description | Default |
|------|-------------|---------|
| `--exchange` | kraken or binanceus | - |
| `--pair` | Trading pair (e.g., BTC/USD) | BTC/USD |
| `--budget` | USD budget cap | 50 |
| `--grids` | Grid levels | 8 |
| `--spacing` | Spacing between levels (fraction) | 0.005 (0.5%) |
| `--min-net` | Minimum net step after fees | 0.002 (0.20%) |
| `--tick` | Loop interval in seconds | 15 |
| `--paper` | Paper trading mode | true |
| `--live` | Live trading mode | false |
| `--confirm` | Required with --live | - |
| `--config` | Path to YAML config file | - |
| `--dryrun` | Simulate one cycle and exit | false |

### YAML Configuration

Create a YAML file to override defaults:

```yaml
exchange: kraken
pair: BTC/USD
budget: 100
grids: 10
spacing: 0.005
min_net: 0.002
tick: 15
mode: paper
fees:
  manual_maker: 0.0016
  manual_taker: 0.0026
```

Then run:
```bash
python MidasBot_Full.py --config config.yaml
```

## Trading Phases

### SCOUT Phase
Analyzes market conditions and identifies optimal entry points.

### LUNCHBOX Phase (Mean-Reversion)
Capitalizes on price returning to mean after deviation.

### REGULAR Phase (Grid Trading)
Places multiple buy/sell orders at defined price intervals.

### AFTERBURNER Phase (Momentum)
Exploits strong trending moves with momentum-based entries.

### DIP Phase (Pullback DCA)
Dollar-cost averaging during price pullbacks.

## Trade Logging

All trades are automatically logged to CSV with:
- Timestamp
- Exchange
- Pair
- Side (buy/sell)
- Amount
- Price
- Phase
- Mode (paper/live)

## Safety Features

- **Paper Mode Default**: All trading is simulated unless explicitly enabled
- **Confirmation Required**: Live trading requires `--confirm I-UNDERSTAND` flag
- **Budget Caps**: Enforced spending limits
- **Post-Only Orders**: Attempts to avoid taker fees
- **Fee Awareness**: Calculates profitability after fees
- **Minimum Notional**: Respects exchange minimums

## Testing

Dry run mode for testing configuration:
```bash
python MidasBot_Full.py --config config.yaml --dryrun
```

## Example Workflows

### Conservative Grid Bot on Kraken
```bash
python MidasBot_Full.py \
  --exchange kraken \
  --pair BTC/USD \
  --budget 100 \
  --grids 12 \
  --spacing 0.003 \
  --paper
```

### Live Trading on Binance US (Advanced)
```bash
python MidasBot_Full.py \
  --exchange binanceus \
  --pair BTC/USDT \
  --budget 500 \
  --live \
  --confirm I-UNDERSTAND
```

## Indicators

Built-in technical analysis:
- **EMA (Exponential Moving Average)**: Trend detection
- **RSI (Relative Strength Index)**: Overbought/oversold conditions

## Requirements

- Python 3.7+
- ccxt
- python-dotenv
- pyyaml

## ⚠️ Risk Disclaimer

Cryptocurrency trading carries significant risk. This bot is provided for educational purposes. Always:
- Test in paper mode first
- Start with small amounts
- Understand the strategies
- Never invest more than you can afford to lose
- Monitor the bot regularly
- Keep API keys secure

## License

Provided as-is for personal use and learning.

---

**Built for mobile-first development with Pydroid3**
