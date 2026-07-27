import os
import yfinance as yf
import pandas as pd
import numpy as np
import asyncio
from telegram import Bot
from datetime import datetime, timedelta
import time
import requests
import pytz

# --- Configuration ---
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ASSETS = {
    "GOLD": "GC=F",
    "BITCOIN": "BTCUSDT",
    "ETHEREUM": "ETHUSDT",
    "SOLANA": "SOLUSDT",
    "EURUSD": "EURUSD=X",
    "AUDUSD": "AUDUSD=X",
    "USDJPY": "USDJPY=X",
    "NZDUSD": "NZDUSD=X",
    "UKBRENT": "BZ=F"
}

ASSET_SETTINGS = {
    "GOLD": {"is_crypto": False},
    "BITCOIN": {"is_crypto": True},
    "ETHEREUM": {"is_crypto": True},
    "SOLANA": {"is_crypto": True},
    "EURUSD": {"is_crypto": False},
    "AUDUSD": {"is_crypto": False},
    "USDJPY": {"is_crypto": False},
    "NZDUSD": {"is_crypto": False},
    "UKBRENT": {"is_crypto": False}
}

cooldowns = {asset: {} for asset in ASSETS}

async def send_telegram_message(message):
    bot = Bot(token=TOKEN)
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        print(f"[Telegram Error] {e}", flush=True)

def is_in_cooldown(asset, strategy, direction):
    now = datetime.now()
    if strategy in cooldowns[asset]:
        last_info = cooldowns[asset][strategy]
        if last_info["direction"] == direction and now < last_info["time"] + timedelta(minutes=15):
            return True
    return False

def update_cooldown(asset, strategy, direction):
    cooldowns[asset][strategy] = {"time": datetime.now(), "direction": direction}

def get_mt5_time():
    # MetaTrader server time is usually UTC+3
    mt5_tz = pytz.FixedOffset(180) 
    return datetime.now(pytz.UTC).astimezone(mt5_tz).strftime('%H:%M:%S')

# --- Custom Indicators to match MT5 ---

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def mt5_stochastic(df, k_window):
    low_min = df['Low'].rolling(window=k_window).min()
    high_max = df['High'].rolling(window=k_window).max()
    k = 100 * (df['Close'] - low_min) / (high_max - low_min)
    return k

# --- Data Fetching ---

def fetch_binance_data(symbol, interval="15m", limit=500):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url).json()
        df = pd.DataFrame(res, columns=['Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'CloseTime', 'QuoteAssetVolume', 'Trades', 'TakerBuyBase', 'TakerBuyQuote', 'Ignore'])
        df['Time'] = pd.to_datetime(df['Time'], unit='ms')
        df.set_index('Time', inplace=True)
        for col in ['Open', 'High', 'Low', 'Close']:
            df[col] = df[col].astype(float)
        return df
    except:
        return pd.DataFrame()

async def check_asset(name, ticker):
    try:
        now_dt = datetime.now()
        if not ASSET_SETTINGS[name]["is_crypto"]:
            if now_dt.weekday() == 5 or (now_dt.weekday() == 6 and now_dt.hour < 22):
                return
            df_15m = yf.download(ticker, period="7d", interval="15m", progress=False)
            df_1m = yf.download(ticker, period="1d", interval="1m", progress=False)
        else:
            df_15m = fetch_binance_data(ticker, "15m", limit=500)
            df_1m = fetch_binance_data(ticker, "1m", limit=100)

        if df_15m.empty: return
        if isinstance(df_15m.columns, pd.MultiIndex): df_15m.columns = df_15m.columns.get_level_values(0)
        
        # --- Indicators ---
        df_15m['stoch_240'] = mt5_stochastic(df_15m, 240)
        df_15m['stoch_60'] = mt5_stochastic(df_15m, 60)
        df_15m['stoch_15'] = mt5_stochastic(df_15m, 15)
        df_15m['ema_15'] = calculate_ema(df_15m['Close'], 15)
        
        stoch_240_1m = None
        if not df_1m.empty:
            df_1m['stoch_240'] = mt5_stochastic(df_1m, 240)
            stoch_240_1m = df_1m['stoch_240'].iloc[-1]

        latest_idx = df_15m.index[-1]
        prev_idx = df_15m.index[-2]
        latest = df_15m.loc[latest_idx]
        prev = df_15m.loc[prev_idx]
        mt_time = get_mt5_time()

        # --- 1. Perforation ---
        perf_signal = None
        if (latest['stoch_240'] < 50 and latest['stoch_60'] > 50 and 
            latest['stoch_15'] < latest['stoch_240'] and prev['stoch_15'] >= prev['stoch_240']):
            if stoch_240_1m is None or stoch_240_1m > 50: perf_signal = 'BUY'
        elif (latest['stoch_240'] > 50 and latest['stoch_60'] < 50 and 
              latest['stoch_15'] > latest['stoch_240'] and prev['stoch_15'] <= prev['stoch_240']):
            if stoch_240_1m is None or stoch_240_1m < 50: perf_signal = 'SELL'

        if perf_signal and not is_in_cooldown(name, "perforation", perf_signal):
            await send_telegram_message(f"⚡️ سیگنال Perforation ({name})\n{'📈' if perf_signal == 'BUY' else '📉'} نوع: {perf_signal}\n🕒 زمان (MT5): {mt_time}")
            update_cooldown(name, "perforation", perf_signal)

        # --- 2. Stoch-Hidden ---
        hidden_signal = None
        if latest['stoch_240'] > 50 and latest['stoch_60'] > 50 and latest['stoch_15'] < 20:
            hidden_signal = 'BUY'
        elif latest['stoch_240'] < 50 and latest['stoch_60'] < 50 and latest['stoch_15'] > 80:
            hidden_signal = 'SELL'
            
        if hidden_signal and not is_in_cooldown(name, "hidden", hidden_signal):
            await send_telegram_message(f"🔍 سیگنال هیدن ({name})\n{'📈' if hidden_signal == 'BUY' else '📉'} نوع: {hidden_signal}\n🕒 زمان (MT5): {mt_time}")
            update_cooldown(name, "hidden", hidden_signal)

        # --- 3. Mismatch (Normal & Strong) ---
        if latest['High'] >= latest['ema_15'] and latest['Low'] <= latest['ema_15']:
            confirmed = False
            if stoch_240_1m is not None:
                if latest['Close'] > latest['ema_15'] and stoch_240_1m > 50: confirmed = True
                elif latest['Close'] < latest['ema_15'] and stoch_240_1m < 50: confirmed = True
            else:
                confirmed = True
            
            if confirmed:
                strong_signal = None
                if latest['Close'] > latest['ema_15'] and latest['stoch_15'] > 80: strong_signal = 'BUY'
                elif latest['Close'] < latest['ema_15'] and latest['stoch_15'] < 20: strong_signal = 'SELL'
                
                if strong_signal and not is_in_cooldown(name, "mismatch_strong", strong_signal):
                    await send_telegram_message(f"🔥 سیگنال عدم تناسب قوی ({name})\n{'📈' if strong_signal == 'BUY' else '📉'} نوع: {strong_signal}\n🕒 زمان (MT5): {mt_time}")
                    update_cooldown(name, "mismatch_strong", strong_signal)
                    update_cooldown(name, "mismatch_normal", strong_signal)
                
                elif not strong_signal:
                    normal_signal = None
                    if latest['Close'] > latest['ema_15'] and latest['stoch_15'] > 50: normal_signal = 'BUY'
                    elif latest['Close'] < latest['ema_15'] and latest['stoch_15'] < 50: normal_signal = 'SELL'
                    
                    if normal_signal and not is_in_cooldown(name, "mismatch_normal", normal_signal):
                        await send_telegram_message(f"⚠️ سیگنال عدم تناسب عادی ({name})\n{'📈' if normal_signal == 'BUY' else '📉'} نوع: {normal_signal}\n🕒 زمان (MT5): {mt_time}")
                        update_cooldown(name, "mismatch_normal", normal_signal)

    except Exception as e:
        print(f"[Error {name}] {e}", flush=True)

async def main_loop():
    print(f"[{datetime.now()}] Bot version 18 started (Simplified Messages + MT5 Time)...", flush=True)
    while True:
        tasks = [check_asset(name, ticker) for name, ticker in ASSETS.items()]
        await asyncio.gather(*tasks)
        print(f"[{datetime.now()}] Cycle complete. Waiting 60s...", flush=True)
        await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main_loop())
