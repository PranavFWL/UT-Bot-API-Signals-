import time
import threading
from fastapi import FastAPI
from datetime import datetime
import pandas as pd
import numpy as np
import requests
from collections import deque
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import os

load_dotenv()  # Load from .env file if it exists

USERNAME = os.getenv("MSTOCK_USERNAME", "")
PASSWORD = os.getenv("MSTOCK_PASSWORD", "")
API_KEY = os.getenv("MSTOCK_API_KEY", "")


# ---------------------------------------------------------------------------
# GLOBALS
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")

latest_signal = None
all_signals = []
current_price = None
running = True
auth_token = None
auth_status = "Not started"  # "Not started", "Waiting for OTP", "Authenticated", "Failed"
request_token = None

# ---------------------------------------------------------------------------
# UTBot Indicator (Cleaned wrapper)
# ---------------------------------------------------------------------------

class UTBotLive:
    def __init__(self, key_value=1, atr_period=1):
        self.key_value = key_value
        self.atr_period = atr_period

        self.raw_data = deque(maxlen=50000)
        self.refined_1s = deque(maxlen=10000)
        self.last_1sec = None
        self.last_1min = None

        self.signals = []
        self.min_data = atr_period + 5

    # ----------- RAW → 1 SECOND ------------
    def process_tick(self, price, timestamp):
        self.raw_data.append({"datetime": timestamp, "ltp": price})
        self._refine_to_1s()

        # check for new 1-min candle
        current_minute = timestamp.replace(second=0, microsecond=0)

        if self.last_1min is None:
            self.last_1min = current_minute
            return None

        if current_minute > self.last_1min:
            df_1m = self._convert_to_1m()

            if df_1m is not None and len(df_1m) >= self.min_data:
                self._run_utbot(df_1m)

            self.last_1min = current_minute

    # ----------- REFINE RAW TO 1s ----------
    def _refine_to_1s(self):
        if len(self.raw_data) < 10:
            return

        df = pd.DataFrame(list(self.raw_data)[-1000:])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df['ts'] = df['datetime'].dt.floor("s")

        grouped = df.groupby("ts").agg({"ltp": "last"}).reset_index()

        for _, row in grouped.iterrows():
            ts = row["ts"]
            if self.last_1sec is None or ts > self.last_1sec:
                self.refined_1s.append({"datetime": ts, "ltp": row["ltp"]})
                self.last_1sec = ts

    # ----------- 1s → 1min OHLC ------------
    def _convert_to_1m(self):
        if len(self.refined_1s) < 20:
            return None

        df = pd.DataFrame(self.refined_1s)
        df = df.set_index("datetime")

        df['ltp'] = pd.to_numeric(df['ltp'], errors='coerce')

        ohlc = df['ltp'].resample("1min").agg(
            Open="first",
            High="max",
            Low="min",
            Close="last"
        ).dropna()

        if len(ohlc) == 0:
            return None

        return ohlc.reset_index()

    # ----------- RUN UTBOT -----------------
    def _run_utbot(self, df):
        close = df['Close']
        high = df['High']
        low = df['Low']

        atr_period = self.atr_period
        key_value = self.key_value

        # True Range
        tr1 = high - low
        tr2 = np.abs(high - close.shift(1))
        tr3 = np.abs(low - close.shift(1))
        tr = np.maximum(tr1, np.maximum(tr2, tr3))

        # ATR
        atr = tr.rolling(window=atr_period, min_periods=1).mean()
        nLoss = key_value * atr

        ts = pd.Series(index=close.index, dtype=float)
        ts.iloc[0] = close.iloc[0]

        for i in range(1, len(close)):
            pc = close.iloc[i-1]
            cc = close.iloc[i]
            prev_stop = ts.iloc[i-1]
            loss = nLoss.iloc[i]

            if cc > prev_stop and pc > prev_stop:
                ts.iloc[i] = max(prev_stop, cc - loss)
            elif cc < prev_stop and pc < prev_stop:
                ts.iloc[i] = min(prev_stop, cc + loss)
            elif cc > prev_stop:
                ts.iloc[i] = cc - loss
            else:
                ts.iloc[i] = cc + loss

        # Crossover logic
        ema = close.copy()
        above = (ema > ts) & (ema.shift(1) <= ts.shift(1))
        below = (ts > ema) & (ts.shift(1) <= ema.shift(1))

        barbuy = close > ts
        barsell = close < ts

        ut_buy = barbuy & above
        ut_sell = barsell & below

        last = df.iloc[-1]

        if ut_buy.iloc[-1]:
            signal = {
                "date": last["datetime"].strftime("%Y-%m-%d"),
                "time": last["datetime"].strftime("%H:%M:%S"),
                "signal": "LONG",
                "type": "UT_Buy"
            }
            self.signals.append(signal)
            return signal

        if ut_sell.iloc[-1]:
            signal = {
                "date": last["datetime"].strftime("%Y-%m-%d"),
                "time": last["datetime"].strftime("%H:%M:%S"),
                "signal": "SHORT",
                "type": "UT_Sell"
            }
            self.signals.append(signal)
            return signal

        return None


# ---------------------------------------------------------------------------
# AUTHENTICATION (USES .env VARIABLES)
# ---------------------------------------------------------------------------

def initiate_login():
    global auth_status, request_token
    
    print("Initiating login...")
    print(f"Username: {USERNAME[:3]}*** (length: {len(USERNAME)})")
    print(f"Password: {'*' * len(PASSWORD)} (length: {len(PASSWORD)})")
    print(f"API_KEY: {API_KEY[:10]}*** (length: {len(API_KEY)})")
    
    auth_status = "Initiating"

    login_data = {"username": USERNAME, "password": PASSWORD}
    headers = {'X-Api-Key': API_KEY, 'X-Mirae-Version': '1'}

    try:
        resp = requests.post(
            'https://api.mstock.trade/openapi/typea/connect/login',
            data=login_data,
            headers=headers,
            timeout=10
        )

        print(f"Login Response Status: {resp.status_code}")
        print(f"Login Response: {resp.text}")

        resp_json = resp.json()
        
        if resp.status_code != 200 or resp_json.get('status') != 'success':
            error_msg = resp_json.get('message', 'Unknown error')
            print(f"Login failed! Error: {error_msg}")
            auth_status = f"Login Failed: {error_msg}"
            return False

        auth_status = "Waiting for OTP"
        print("Login successful. Waiting for OTP...")
        return True
        
    except requests.exceptions.Timeout:
        print("Login request timed out!")
        auth_status = "Timeout"
        return False
    except requests.exceptions.RequestException as e:
        print(f"Network error: {e}")
        auth_status = f"Network Error: {str(e)}"
        return False
    except Exception as e:
        print(f"Login error: {e}")
        auth_status = f"Error: {str(e)}"
        return False


def verify_otp(otp):
    global auth_token, auth_status
    
    headers = {'X-Api-Key': API_KEY, 'X-Mirae-Version': '1'}
    
    token_req = {
        "api_key": API_KEY,
        "request_token": otp,
        "checksum": "1"
    }

    try:
        resp = requests.post(
            'https://api.mstock.trade/openapi/typea/session/token',
            data=token_req,
            headers=headers
        )

        if resp.status_code != 200 or resp.json().get('status') != 'success':
            print("OTP verification failed!")
            auth_status = "OTP Failed"
            return False

        auth_token = resp.json()['data']['access_token']
        auth_status = "Authenticated"
        print("Authenticated successfully!")
        return True
    except Exception as e:
        print(f"OTP verification error: {e}")
        auth_status = f"Error: {str(e)}"
        return False


# ---------------------------------------------------------------------------
# LTP FETCH (LIVE)
# ---------------------------------------------------------------------------

def fetch_ltp(auth_token):
    headers = {
        'X-Mirae-Version': '1',
        'Authorization': f'token {API_KEY}:{auth_token}'
    }

    symbol = "NSE:NIFTY 50"

    resp = requests.get(
        'https://api.mstock.trade/openapi/typea/instruments/quote/ltp',
        headers=headers,
        params={'i': [symbol]},
        timeout=3
    )

    js = resp.json()

    if resp.status_code == 200 and js.get("status") == "success":
        return js["data"][symbol]["last_price"]

    return None


# ---------------------------------------------------------------------------
# BACKGROUND THREAD
# ---------------------------------------------------------------------------

def live_loop():
    global latest_signal, current_price, all_signals, auth_token

    utbot = UTBotLive()

    # Wait for authentication
    print("Waiting for authentication...")
    while running and auth_token is None:
        time.sleep(1)
    
    if not running:
        return

    print("Starting live data collection...")

    while running:
        try:
            ltp = fetch_ltp(auth_token)
            if ltp is None:
                continue

            now = datetime.now(IST)
            utbot.process_tick(ltp, now)

            # update shared state
            if len(utbot.refined_1s) > 0:
                current_price = utbot.refined_1s[-1]

            if len(utbot.signals) > 0:
                latest_signal = utbot.signals[-1]
                all_signals.append(latest_signal)

            time.sleep(0.1)
        except Exception as e:
            print("ERR:", e)


# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------

app = FastAPI(title="Live UTBot API")


@app.on_event("startup")
def start_background():
    # Start background thread (it will wait for authentication)
    t = threading.Thread(target=live_loop)
    t.daemon = True
    t.start()
    
    # Auto-initiate login on startup
    initiate_login()


@app.get("/")
def home():
    return {
        "status": "running",
        "latest_signal": latest_signal
    }


@app.get("/latest-signal")
def api_signal():
    return latest_signal


@app.get("/all-signals")
def api_all():
    return all_signals


@app.get("/current-price")
def api_price():
    return current_price

@app.get("/auth-status")
def get_auth_status():
    return {
        "status": auth_status,
        "authenticated": auth_token is not None
    }


@app.post("/login")
def login():
    success = initiate_login()
    return {
        "success": success,
        "status": auth_status,
        "message": "OTP sent. Use /submit-otp endpoint to verify." if success else "Login failed"
    }


@app.post("/submit-otp")
@app.get("/submit-otp")
def submit_otp(otp: str):
    if auth_status != "Waiting for OTP":
        return {
            "success": False,
            "message": f"Cannot submit OTP. Current status: {auth_status}"
        }
    
    success = verify_otp(otp)
    return {
        "success": success,
        "status": auth_status,
        "message": "Authentication successful!" if success else "OTP verification failed"
    }
