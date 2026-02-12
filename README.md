# 🚀 Signal Copier Bot

A high-performance trading signal copier built for automated execution of external trading signals directly into your exchange account.

This bot is designed for speed, reliability, and controlled risk — ideal for futures traders who want automated execution without manual delays.

---

## 📌 Features

- ⚡ Real-time signal parsing  
- 🔁 Automatic trade execution  
- 📊 Futures trading support  
- 🛑 Custom Stop Loss & Take Profit handling  
- 📈 Risk-based position sizing  
- 📝 Trade logging  
- 🔐 Secure API key configuration  
- 🧠 Expandable architecture (hybrid / grid / AI ready)  

---

## 🏗️ Architecture Overview

Signal Source → Signal Parser → Risk Engine → Exchange API → Trade Execution → Logger

### Components

- **Signal Source** – Telegram / Discord / Webhook / JSON input  
- **Signal Parser** – Extracts symbol, entry, SL, TP  
- **Risk Engine** – Calculates position size based on account risk %  
- **Execution Engine** – Sends orders via exchange API  
- **Logger** – Records trade data and performance  

---

## ⚙️ Supported Exchange

- MetaTrader 5 via account from ByBit

---

## 🔧 Installation

Clone the repository:

```bash
git clone https://github.com/yourusername/signal-copier.git
cd signal-copier
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## 🔐 Configuration

Put in API key and phone number, API HASH, and ID, in `signal-copier-4.py` file at line 58-60:

⚠️ Never commit API keys to GitHub.  
For production use, environment variables are recommended.

---

## 📊 Risk Management Logic

Position size is calculated using:

Position Size = (Account Balance × Risk %) / Stop Loss Distance

This ensures:

- Controlled drawdown  
- No martingale  
- Consistent risk exposure  
- Long-term survivability  

Default risk is 1%

---

## 📈 Example Signal Format

```json
{
  "symbol": "XAUUSD",
  "side": "Buy",
  "entry": 50000,
  "stop_loss": 49500,
  "take_profit": 52000
}
```

The signal copier supports 7 channels

---

## ▶️ Running the Bot

```bash
python signal-copier-4.py
```

Before running:

- Verify API keys 
- Double-check risk settings  

---

## 🧪 Recommended Deployment

- Run on a VPS for 24/7 uptime   
- Start with small risk (0.5–1%)  

---

## 🧠 Roadmap

- [ ] Web dashboard  
- [ ] Multiple signal providers  
- [ ] AI-based signal filtering  
- [ ] Dynamic grid integration  
- [ ] Backtesting module  
- [ ] Performance analytics  

---

## ⚠️ Disclaimer

This software is for educational purposes only.

Trading cryptocurrencies involves substantial risk.  
You are fully responsible for your own financial decisions and use of this software.

---

## 📜 License

GPLv3 License

---

⭐ If you find this project useful, consider starring the repository.
