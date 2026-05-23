"""
vn30_monitor.py — Vietnam (HOSE) long-term investment helper.

Two modes:
  1) screen   : Rank VN30 (or any watchlist) by a multi-factor score
                to help shortlist a 1–2 year hold candidate.
  2) monitor  : Daily check on tickers in WATCHLIST. Flags adverse
                price moves, news/insider events, and foreign-flow
                shifts, then emails a digest.

Author: Built for Nam (namxnz@gmail.com)
Usage:
    python vn30_monitor.py screen
    python vn30_monitor.py monitor
    python vn30_monitor.py monitor --no-email   # write report locally only

Dependencies: see requirements.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

# vnstock is imported lazily inside DataFetcher so the script can still
# `--help` and run unit-like checks even before the package is installed.

# =============================================================================
# CONFIGURATION — tune these to your preferences
# =============================================================================

# Tickers you actively want to track in monitor mode.
# Start with all VN30; trim down to your held / shortlisted names later.
WATCHLIST: list[str] = [
    "ACB", "BCM", "BID", "BVH", "CTG", "FPT", "GAS", "GVR", "HDB", "HPG",
    "MBB", "MSN", "MWG", "PLX", "POW", "SAB", "SHB", "SSB", "SSI", "STB",
    "TCB", "TPB", "VCB", "VHM", "VIB", "VIC", "VJC", "VNM", "VPB", "VRE",
]

# Universe to screen in `screen` mode. Default = VN30; you can add candidates.
SCREEN_UNIVERSE: list[str] = WATCHLIST.copy()

# Alert thresholds — breaches will surface in the email digest.
ALERT_THRESHOLDS: dict = {
    "daily_drop_pct":          -4.0,   # alert if single-day return < -4%
    "weekly_drop_pct":         -8.0,   # alert if 5-day return < -8%
    "rsi_oversold":             30.0,
    "rsi_overbought":           75.0,
    "volume_spike_x":            2.5,  # day volume > 2.5x 20-day avg
    "foreign_net_sell_billion": -50.0, # foreign net sell < -50 bn VND
    "foreign_net_sell_5d_billion": -150.0,
    "below_sma200":             True,  # alert when close drops below SMA200
}

# Vietnamese keywords that should escalate a news headline into an alert.
NEWS_RED_FLAGS: list[str] = [
    # Governance / legal
    "khởi tố", "vi phạm", "phạt", "đình chỉ", "đình chỉ giao dịch",
    "kiểm tra thuế", "truy thu", "thanh tra", "kiểm toán ngoại trừ",
    # Leadership change
    "miễn nhiệm", "từ chức", "bãi nhiệm", "thôi giữ chức",
    # Capital / ownership
    "thoái vốn", "bán giải chấp", "bán ra", "đăng ký bán",
    "phát hành riêng lẻ", "pha loãng",
    # Financial distress
    "lỗ", "thua lỗ", "nợ xấu tăng", "kiểm soát đặc biệt",
    "giảm room", "vượt room",
]

# Where to save daily reports.
REPORT_DIR = Path(__file__).resolve().parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)

# Email config — set in .env. Script falls back to local-only report if missing.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")            # e.g. namxnz@gmail.com
SMTP_PASS = os.getenv("SMTP_PASS", "")            # Gmail App Password (16 chars)
EMAIL_TO  = os.getenv("EMAIL_TO", "namxnz@gmail.com")

# --- vnstock rate limiting --------------------------------------------------
# vnstock's free "Guest" tier allows only ~20 HTTP requests/minute and HARD-
# KILLS the process when you exceed it (no catchable exception). So we pace
# requests ourselves and stay safely under the cap.
#
# VNSTOCK_MAX_RPM = the per-minute HTTP-request budget the limiter enforces.
#   • Guest (no key):        keep at 16  (default — safe under the 20 cap)
#   • Community (free key):  you can raise this to ~50  (60/min cap)
#   Register a free key at https://vnstocks.com/login, then set VNSTOCK_MAX_RPM
#   in your .env. A 30-ticker screen takes ~7 min on Guest, ~2 min on Community.
VNSTOCK_MAX_RPM = int(os.getenv("VNSTOCK_MAX_RPM", "16"))
# Rough number of underlying HTTP requests one DataFetcher method triggers.
# Each fetch call reserves this many slots in the budget (conservative).
REQUESTS_PER_CALL = 2

# =============================================================================
# Logging
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vn30")

# Vietnam local timezone (UTC+7), no DST.
ICT = timezone(timedelta(hours=7))

# =============================================================================
# Rate limiter — keep us under vnstock's per-minute request cap
# =============================================================================

class RateLimiter:
    """Sliding-window limiter. Blocks until a request slot is free.

    vnstock's guest tier terminates the whole process on a rate-limit breach,
    so there is nothing to recover from after the fact — we MUST stay under
    the cap proactively. This limiter records a timestamp per request and
    sleeps whenever the trailing 60-second window is full.
    """

    def __init__(self, max_per_period: int, period: float = 60.0):
        self.max_per_period = max(1, int(max_per_period))
        self.period = float(period)
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, weight: int = 1) -> None:
        """Reserve `weight` request slots, blocking (sleeping) if needed."""
        weight = max(1, min(int(weight), self.max_per_period))
        with self._lock:
            while True:
                now = time.monotonic()
                # Drop timestamps that have aged out of the window.
                while self._stamps and now - self._stamps[0] >= self.period:
                    self._stamps.popleft()
                if len(self._stamps) + weight <= self.max_per_period:
                    self._stamps.extend([now] * weight)
                    return
                # Window is full — wait until the oldest stamp expires.
                wait = self.period - (now - self._stamps[0]) + 0.5
                log.info("⏳ Rate-limit guard: pausing %.0fs to stay under "
                         "%d req/min …", wait, self.max_per_period)
                time.sleep(max(0.5, wait))


# One shared limiter for every vnstock call in the process.
_LIMITER = RateLimiter(VNSTOCK_MAX_RPM)


# =============================================================================
# Data layer — thin wrapper around vnstock
# =============================================================================

class DataFetcher:
    """Wraps vnstock with defensive error handling.

    vnstock's public API has shifted across versions. We isolate it here so
    we only have one place to patch if the upstream library renames things.
    """

    def __init__(self, source: str = "VCI"):
        try:
            from vnstock import Vnstock  # type: ignore
        except ImportError as e:
            raise SystemExit(
                "vnstock is not installed. Run:\n"
                "    pip install -U vnstock python-dotenv pandas numpy\n"
                f"Original error: {e}"
            )
        # One Vnstock() instance for the whole run, so device registration
        # and other one-time setup happen once instead of per ticker.
        self._vn = Vnstock()
        self._source = source
        self._stock_cache: dict = {}

    def _stock(self, symbol: str):
        """Cached stock client per ticker.

        Repeated calls for the same symbol (e.g. price + ratios in `screen`)
        reuse one object instead of re-creating it, which saves requests.
        """
        if symbol not in self._stock_cache:
            self._stock_cache[symbol] = self._vn.stock(
                symbol=symbol, source=self._source
            )
        return self._stock_cache[symbol]

    # ----- Prices -----
    def price_history(self, symbol: str, lookback_days: int = 260) -> pd.DataFrame:
        end = datetime.now(ICT).date()
        start = end - timedelta(days=int(lookback_days * 1.6))  # pad for weekends/holidays
        try:
            _LIMITER.acquire(REQUESTS_PER_CALL)
            df = self._stock(symbol).quote.history(
                start=str(start), end=str(end), interval="1D"
            )
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.rename(columns={c: c.lower() for c in df.columns})
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"])
                df = df.set_index("time").sort_index()
            return df.tail(lookback_days)
        except Exception as e:
            log.warning("price_history(%s) failed: %s", symbol, e)
            return pd.DataFrame()

    # ----- Fundamentals -----
    def financial_ratios(self, symbol: str) -> pd.DataFrame:
        try:
            _LIMITER.acquire(REQUESTS_PER_CALL)
            return self._stock(symbol).finance.ratio(period="year", lang="en")
        except Exception as e:
            log.warning("financial_ratios(%s) failed: %s", symbol, e)
            return pd.DataFrame()

    def company_overview(self, symbol: str) -> dict:
        try:
            _LIMITER.acquire(REQUESTS_PER_CALL)
            df = self._stock(symbol).company.overview()
            if df is None or df.empty:
                return {}
            row = df.iloc[0].to_dict()
            return row
        except Exception as e:
            log.warning("company_overview(%s) failed: %s", symbol, e)
            return {}

    # ----- News -----
    def company_news(self, symbol: str) -> pd.DataFrame:
        try:
            _LIMITER.acquire(REQUESTS_PER_CALL)
            df = self._stock(symbol).company.news()
            if df is None or df.empty:
                return pd.DataFrame()
            # Normalize column names
            df = df.rename(columns={c: c.lower() for c in df.columns})
            for c in ("publishdate", "publish_date", "date"):
                if c in df.columns:
                    df["publish_date"] = pd.to_datetime(df[c], errors="coerce")
                    break
            return df.sort_values("publish_date", ascending=False) if "publish_date" in df.columns else df
        except Exception as e:
            log.warning("company_news(%s) failed: %s", symbol, e)
            return pd.DataFrame()

    # ----- Foreign trade flow -----
    def foreign_trade(self, symbol: str, lookback_days: int = 20) -> pd.DataFrame:
        """Foreign buy/sell daily. Falls back to price-board snapshot if history unavailable."""
        try:
            stock = self._stock(symbol)
            # Newer vnstock exposes foreign trade on trading; method names vary.
            for attr in ("foreign_trading", "trading_stats"):
                fn = getattr(stock.trading, attr, None)
                if callable(fn):
                    _LIMITER.acquire(REQUESTS_PER_CALL)
                    df = fn()
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        df = df.rename(columns={c: c.lower() for c in df.columns})
                        return df.tail(lookback_days)
            # Fallback: price board snapshot today only
            _LIMITER.acquire(REQUESTS_PER_CALL)
            df = stock.trading.price_board(symbols_list=[symbol])
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception as e:
            log.warning("foreign_trade(%s) failed: %s", symbol, e)
            return pd.DataFrame()


# =============================================================================
# Indicators
# =============================================================================

def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up   = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs   = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def pct_return(series: pd.Series, n: int) -> float:
    if len(series) <= n:
        return np.nan
    return float(series.iloc[-1] / series.iloc[-1 - n] - 1.0) * 100.0


# =============================================================================
# Phase 1 — Screener
# =============================================================================

@dataclass
class ScreenRow:
    symbol: str
    last_price: float = np.nan
    pe: float = np.nan
    pb: float = np.nan
    roe: float = np.nan
    roa: float = np.nan
    de: float = np.nan          # debt / equity
    net_margin: float = np.nan
    eps_growth_3y: float = np.nan
    rev_growth_3y: float = np.nan
    mom_6m: float = np.nan
    div_yield: float = np.nan
    score: float = np.nan
    notes: list[str] = field(default_factory=list)


def _first_present(d: dict, keys: Iterable[str], default=np.nan):
    """Match a value from dict d.

    Tries three passes so we handle any vnstock column-naming convention:
      1. Exact match (case-insensitive, after lowercasing both sides)
      2. Normalised match — strip underscores from both key and column name
         (e.g. "price_to_earning" matches "pricetoearning")
      3. Substring match — the search key is contained in the column name
         (e.g. "roe" matches "profitability_roe" or "annualroe")
    """
    d_lower = {k.lower(): v for k, v in d.items()}

    # Pass 1: exact (lowercased)
    for k in keys:
        kl = k.lower()
        if kl in d_lower and pd.notna(d_lower[kl]):
            return d_lower[kl]

    # Pass 2: strip underscores
    d_norm = {k.replace("_", ""): v for k, v in d_lower.items()}
    for k in keys:
        kn = k.lower().replace("_", "")
        if kn in d_norm and pd.notna(d_norm[kn]):
            return d_norm[kn]

    # Pass 3: substring
    for k in keys:
        kn = k.lower().replace("_", "")
        for col, val in d_norm.items():
            if kn in col and pd.notna(val):
                return val

    return default


def screen(fetcher: DataFetcher, universe: list[str]) -> pd.DataFrame:
    """Multi-factor score: value (40%) + quality (30%) + growth (20%) + momentum (10%).

    Lower P/E, lower P/B, higher ROE, higher EPS-growth, positive 6-mo
    momentum all push the score up. Output is sorted descending.
    """
    rows: list[ScreenRow] = []
    for sym in universe:
        log.info("Screening %s …", sym)
        r = ScreenRow(symbol=sym)

        # ----- Price & momentum -----
        prices = fetcher.price_history(sym, lookback_days=180)
        if not prices.empty and "close" in prices.columns:
            r.last_price = float(prices["close"].iloc[-1])
            r.mom_6m = pct_return(prices["close"], n=min(120, len(prices) - 1))

        # ----- Ratios -----
        ratios = fetcher.financial_ratios(sym)
        if not ratios.empty:
            try:
                # Flatten MultiIndex column names first.
                if isinstance(ratios.columns, pd.MultiIndex):
                    raw_cols = ["_".join([str(x) for x in c if x]).lower()
                                for c in ratios.columns]
                else:
                    raw_cols = [str(c).lower() for c in ratios.columns]

                # ── raw diagnostic (first ticker only) ──────────────────────
                if sym == universe[0]:
                    log.info("RAW ratio shape=%s  cols=%s",
                             ratios.shape, raw_cols)
                    log.info("RAW ratio row[0]=%s", list(ratios.values[0]))

                # ── vnstock 4.x long format ──────────────────────────────────
                # Rows = metrics, columns include 'item_id' + year columns.
                # Duplicate column names break pandas selection, so we use
                # positional numpy access throughout.
                long_id_col = next(
                    (c for c in ("item_id", "item_en", "item")
                     if c in raw_cols), None
                )
                year_positions = sorted(
                    [(c, i) for i, c in enumerate(raw_cols)
                     if str(c).lstrip("-").isdigit() and len(str(c)) == 4],
                    reverse=True,   # most-recent year first
                )

                if long_id_col and year_positions:
                    # Long format: build {metric_id: value} using numpy rows.
                    id_pos = raw_cols.index(long_id_col)
                    arr    = ratios.values   # shape (n_metrics, n_cols)

                    # Find the column that holds the most recent year by
                    # reading the 'ratioyearid' or 'year' row's actual values.
                    # All columns may share the same pandas label ('2018') due
                    # to a vnstock naming bug, so we scan the data directly.
                    best_col_pos = year_positions[-1][1]  # default: last col
                    best_year    = -1
                    id_vals = {str(row[id_pos]).strip().lower(): row
                               for row in arr
                               if str(row[id_pos]).strip().lower()
                               not in ("nan", "none", "")}
                    for key in ("ratioyearid", "year"):
                        if key in id_vals:
                            yr_row = id_vals[key]
                            for _, col_pos in year_positions:
                                try:
                                    y = int(float(str(yr_row[col_pos])))
                                    if y > best_year:
                                        best_year    = y
                                        best_col_pos = col_pos
                                except (ValueError, TypeError):
                                    pass
                            break

                    latest = {}
                    for row in arr:
                        k = str(row[id_pos]).strip().lower()
                        if k and k not in ("nan", "none", ""):
                            latest[k] = row[best_col_pos]
                    if sym == universe[0]:
                        log.info("Long-format pivot: best_year=%s col=%s  "
                                 "pe_ratio=%s  roe=%s  div_yield=%s",
                                 best_year, best_col_pos,
                                 latest.get("pe_ratio"), latest.get("roe"),
                                 latest.get("dividend_yield"))

                # ── vnstock 3.x wide format ──────────────────────────────────
                # Rows = years (most-recent first), columns = metric names.
                else:
                    arr = ratios.values
                    latest = {raw_cols[i]: arr[0, i] for i in range(len(raw_cols))}
                    if sym == universe[0]:
                        log.info("Wide-format keys=%s", list(latest.keys()))

            except Exception as e:
                log.warning("ratio parse error for %s: %s", sym, e)
                latest = {}

            # Exact names come from the 'Long-format pivot' diagnostic above.
            r.pe  = _first_present(latest, ["pe_ratio",  "pe", "pricetoearning"])
            r.pb  = _first_present(latest, ["pb_ratio",  "pb", "pricetobook"])
            r.roe = _first_present(latest, ["roe"])
            r.roa = _first_present(latest, ["roa"])
            r.de  = _first_present(latest, ["debt_to_equity", "debtperequity",
                                            "debtonequity", "de"])
            r.net_margin = _first_present(latest, ["net_margin", "netmargin",
                                                   "post_tax_profit_margin",
                                                   "posttaxprofitmargin"])
            r.eps_growth_3y = _first_present(
                latest, ["eps_growth", "epschange", "epsgrowth", "earninggrowth"]
            )
            r.rev_growth_3y = _first_present(
                latest, ["loans_growth", "deposit_growth", "revenuegrowth",
                         "salegrowth", "creditgrowth"]
            )
            r.div_yield = _first_present(latest, ["dividend_yield", "dividendyield"])

        rows.append(r)

    df = pd.DataFrame([r.__dict__ for r in rows])
    if df.empty:
        return df

    # ----- Z-score each factor, invert where lower-is-better -----
    def z(col: pd.Series, lower_is_better: bool = False) -> pd.Series:
        s = col.astype(float)
        z = (s - s.mean()) / s.std(ddof=0)
        return -z if lower_is_better else z

    df["z_value"]    = (z(df["pe"], lower_is_better=True).fillna(0)
                        + z(df["pb"], lower_is_better=True).fillna(0)) / 2
    df["z_quality"]  = (z(df["roe"]).fillna(0)
                        + z(df["roa"]).fillna(0)
                        + z(df["net_margin"]).fillna(0)
                        + z(df["de"], lower_is_better=True).fillna(0)) / 4
    df["z_momentum"] = z(df["mom_6m"]).fillna(0)

    # eps_growth_3y / rev_growth_3y are kept in the CSV for reference but
    # excluded from scoring since vnstock's free tier doesn't provide them.
    df["score"] = (0.50 * df["z_value"]
                   + 0.40 * df["z_quality"]
                   + 0.10 * df["z_momentum"])

    return df.sort_values("score", ascending=False).reset_index(drop=True)


# =============================================================================
# Phase 2 — Daily monitor
# =============================================================================

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower()


def technical_signals(prices: pd.DataFrame) -> dict:
    """Compute the technical indicators we care about for one ticker."""
    if prices.empty or "close" not in prices.columns:
        return {}
    close = prices["close"]
    vol   = prices["volume"] if "volume" in prices.columns else pd.Series(dtype=float)

    out = {
        "last_close":   float(close.iloc[-1]),
        "ret_1d_pct":   pct_return(close, 1),
        "ret_5d_pct":   pct_return(close, 5),
        "ret_20d_pct": pct_return(close, 20),
        "sma50":        float(sma(close, 50).iloc[-1])  if len(close) >= 50  else np.nan,
        "sma200":       float(sma(close, 200).iloc[-1]) if len(close) >= 200 else np.nan,
        "rsi14":        float(rsi(close, 14).iloc[-1])  if len(close) >= 15  else np.nan,
    }
    if not vol.empty and len(vol) >= 21:
        avg_vol = float(vol.iloc[-21:-1].mean())   # 20-day avg excluding today
        out["vol_today"]   = float(vol.iloc[-1])
        out["vol_ratio"]   = out["vol_today"] / avg_vol if avg_vol > 0 else np.nan
    return out


def evaluate_alerts(sig: dict) -> list[str]:
    alerts = []
    t = ALERT_THRESHOLDS
    if sig.get("ret_1d_pct", 0) <= t["daily_drop_pct"]:
        alerts.append(f"Daily drop {sig['ret_1d_pct']:.1f}% ≤ {t['daily_drop_pct']}%")
    if sig.get("ret_5d_pct", 0) <= t["weekly_drop_pct"]:
        alerts.append(f"5-day drop {sig['ret_5d_pct']:.1f}% ≤ {t['weekly_drop_pct']}%")
    rsi_v = sig.get("rsi14", np.nan)
    if pd.notna(rsi_v):
        if rsi_v <= t["rsi_oversold"]:
            alerts.append(f"RSI {rsi_v:.0f} oversold (≤ {t['rsi_oversold']})")
        elif rsi_v >= t["rsi_overbought"]:
            alerts.append(f"RSI {rsi_v:.0f} overbought (≥ {t['rsi_overbought']})")
    vr = sig.get("vol_ratio", np.nan)
    if pd.notna(vr) and vr >= t["volume_spike_x"]:
        alerts.append(f"Volume spike {vr:.1f}× 20-day avg")
    sma200 = sig.get("sma200", np.nan)
    last   = sig.get("last_close", np.nan)
    if t["below_sma200"] and pd.notna(sma200) and pd.notna(last) and last < sma200:
        gap = (last / sma200 - 1) * 100
        alerts.append(f"Below SMA200 by {gap:.1f}%")
    return alerts


def scan_news(news: pd.DataFrame, days: int = 2) -> list[dict]:
    """Return recent headlines, with red-flag matches highlighted."""
    if news.empty:
        return []
    cutoff = datetime.now(ICT) - timedelta(days=days)
    out = []
    title_col = next((c for c in ("title", "name", "headline") if c in news.columns), None)
    if not title_col:
        return []
    for _, row in news.iterrows():
        pub = row.get("publish_date")
        if pd.notna(pub):
            # Normalise to offset-aware for a safe comparison with `cutoff`.
            pub_dt = pub.to_pydatetime()
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=ICT)
            if pub_dt < cutoff:
                continue
            continue
        title = str(row.get(title_col, "")).strip()
        if not title:
            continue
        norm = _strip_accents(title)
        flags = [kw for kw in NEWS_RED_FLAGS if _strip_accents(kw) in norm]
        out.append({"date": pub, "title": title, "flags": flags})
    return out[:15]   # cap


def foreign_flow_summary(ftrade: pd.DataFrame) -> dict:
    """Best-effort summary across vnstock versions."""
    if ftrade.empty:
        return {}
    # Hunt for a net-foreign column.
    candidate_cols = [c for c in ftrade.columns
                      if any(k in c for k in ("foreign", "fr_", "nn"))]
    summary = {"raw_cols": candidate_cols}
    # Many vnstock variants name it foreign_net_value, foreignBuyValue/foreignSellValue,
    # or NN_RoongMua/NN_RoongBan. We compute a net column if both sides exist.
    buy_col  = next((c for c in ftrade.columns
                     if "foreign" in c and "buy"  in c and "value" in c), None)
    sell_col = next((c for c in ftrade.columns
                     if "foreign" in c and "sell" in c and "value" in c), None)
    net_col  = next((c for c in ftrade.columns
                     if "foreign" in c and "net"  in c), None)
    if net_col and not ftrade[net_col].empty:
        net_series = pd.to_numeric(ftrade[net_col], errors="coerce")
    elif buy_col and sell_col:
        net_series = pd.to_numeric(ftrade[buy_col], errors="coerce") \
                   - pd.to_numeric(ftrade[sell_col], errors="coerce")
    else:
        return summary
    net_series = net_series.dropna()
    if net_series.empty:
        return summary
    # vnstock returns VND. Convert to billions for readability.
    today_bn = float(net_series.iloc[-1]) / 1e9
    cum5_bn  = float(net_series.tail(5).sum()) / 1e9
    summary.update({
        "foreign_net_today_bn": today_bn,
        "foreign_net_5d_bn":    cum5_bn,
    })
    return summary


def evaluate_foreign_alerts(fsum: dict) -> list[str]:
    alerts = []
    t = ALERT_THRESHOLDS
    today = fsum.get("foreign_net_today_bn")
    cum5  = fsum.get("foreign_net_5d_bn")
    if today is not None and today <= t["foreign_net_sell_billion"]:
        alerts.append(f"Foreign net sell {today:.0f} bn VND today")
    if cum5 is not None and cum5 <= t["foreign_net_sell_5d_billion"]:
        alerts.append(f"Foreign net sell {cum5:.0f} bn VND over 5 days")
    return alerts


def monitor(fetcher: DataFetcher, watchlist: list[str]) -> dict:
    """Run all daily checks. Returns a structured dict for the reporter."""
    today = datetime.now(ICT).strftime("%Y-%m-%d %H:%M ICT")
    result = {"as_of": today, "tickers": {}}
    for sym in watchlist:
        log.info("Monitoring %s …", sym)
        prices = fetcher.price_history(sym, lookback_days=260)
        sig    = technical_signals(prices)
        news   = fetcher.company_news(sym)
        head   = scan_news(news, days=2)
        ftrade = fetcher.foreign_trade(sym, lookback_days=20)
        fsum   = foreign_flow_summary(ftrade)

        alerts = evaluate_alerts(sig) + evaluate_foreign_alerts(fsum)
        # News alerts
        for h in head:
            if h["flags"]:
                alerts.append(f"News flag ({', '.join(h['flags'])}): {h['title']}")

        result["tickers"][sym] = {
            "signals":      sig,
            "foreign":      fsum,
            "headlines":    head,
            "alerts":       alerts,
        }
    return result


# =============================================================================
# Reporting
# =============================================================================

def build_html_report(payload: dict) -> str:
    css = """
    <style>
      body { font-family: -apple-system, system-ui, sans-serif; color:#111; }
      h1 { font-size: 18px; }
      h2 { font-size: 15px; margin-top: 22px; border-bottom:1px solid #eee; }
      table { border-collapse: collapse; font-size: 12px; }
      td, th { border:1px solid #ddd; padding:4px 8px; text-align:left; }
      .alert { background:#fff4f4; border-left:4px solid #c00; padding:8px 12px;
               margin: 6px 0; }
      .ok    { color:#0a0; }
      .bad   { color:#c00; }
      .muted { color:#777; font-size:11px; }
    </style>
    """
    rows = []
    rows.append(f"<h1>VN30 daily digest — {payload['as_of']}</h1>")

    # Top alerts across all tickers
    all_alerts = []
    for sym, t in payload["tickers"].items():
        for a in t["alerts"]:
            all_alerts.append((sym, a))
    if all_alerts:
        rows.append("<h2>⚠️ Alerts</h2>")
        for sym, a in all_alerts:
            rows.append(f'<div class="alert"><b>{sym}</b> — {a}</div>')
    else:
        rows.append('<h2>✅ No alerts</h2><div class="muted">All tracked tickers within thresholds.</div>')

    # Per-ticker tables
    rows.append("<h2>Per-ticker snapshot</h2>")
    header = (
        "<tr><th>Ticker</th><th>Close</th><th>1D %</th><th>5D %</th>"
        "<th>20D %</th><th>RSI14</th><th>vs SMA200</th><th>Vol×20d</th>"
        "<th>FF today (bn)</th><th>FF 5d (bn)</th></tr>"
    )

    def fmt(v, spec=":.1f", dash="—"):
        return format(v, spec) if pd.notna(v) else dash

    body = []
    for sym, t in payload["tickers"].items():
        s = t["signals"]
        f = t["foreign"]
        close = s.get("last_close", np.nan)
        sma200 = s.get("sma200", np.nan)
        vs200 = (close / sma200 - 1) * 100 if pd.notna(close) and pd.notna(sma200) else np.nan
        ret_1d = s.get("ret_1d_pct", np.nan)
        cls_1d = "bad" if (pd.notna(ret_1d) and ret_1d < 0) else "ok"
        body.append(
            "<tr>"
            f"<td><b>{sym}</b></td>"
            f"<td>{fmt(close, ',.0f')}</td>"
            f"<td class='{cls_1d}'>{fmt(ret_1d, '+.1f')}</td>"
            f"<td>{fmt(s.get('ret_5d_pct', np.nan), '+.1f')}</td>"
            f"<td>{fmt(s.get('ret_20d_pct', np.nan), '+.1f')}</td>"
            f"<td>{fmt(s.get('rsi14', np.nan), '.0f')}</td>"
            f"<td>{fmt(vs200, '+.1f')}%</td>"
            f"<td>{fmt(s.get('vol_ratio', np.nan), '.1f')}</td>"
            f"<td>{fmt(f.get('foreign_net_today_bn', np.nan), '+.0f')}</td>"
            f"<td>{fmt(f.get('foreign_net_5d_bn', np.nan), '+.0f')}</td>"
            "</tr>"
        )
    rows.append(f"<table>{header}{''.join(body)}</table>")

    # Headlines section
    rows.append("<h2>Recent headlines (last 2 days)</h2>")
    any_news = False
    for sym, t in payload["tickers"].items():
        if not t["headlines"]:
            continue
        any_news = True
        rows.append(f"<h3>{sym}</h3><ul>")
        for h in t["headlines"]:
            tag = f" <span class='bad'>[{','.join(h['flags'])}]</span>" if h["flags"] else ""
            date = h["date"].strftime("%Y-%m-%d") if pd.notna(h["date"]) else ""
            rows.append(f"<li>{date} — {h['title']}{tag}</li>")
        rows.append("</ul>")
    if not any_news:
        rows.append('<div class="muted">No recent headlines fetched.</div>')

    rows.append('<p class="muted">Generated by vn30_monitor.py — thresholds editable at top of script.</p>')
    return f"<!doctype html><html><head><meta charset='utf-8'>{css}</head><body>{''.join(rows)}</body></html>"


def save_report(html: str) -> Path:
    fname = REPORT_DIR / f"digest_{datetime.now(ICT).strftime('%Y%m%d')}.html"
    fname.write_text(html, encoding="utf-8")
    log.info("Saved report → %s", fname)
    return fname


# =============================================================================
# Email
# =============================================================================

def send_email(subject: str, html_body: str) -> bool:
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        log.warning("SMTP credentials not set — skipping email.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        log.info("Email sent to %s", EMAIL_TO)
        return True
    except Exception as e:
        log.error("Email failed: %s", e)
        return False


# =============================================================================
# CLI
# =============================================================================

def _load_dotenv():
    """Light .env loader so we don't require python-dotenv.

    Looks first in the script's folder, then walks up one level (so a
    shared .env at the repo root also works).
    """
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def cmd_screen(args) -> int:
    fetcher = DataFetcher()
    universe = SCREEN_UNIVERSE if not args.symbols else args.symbols
    est_min = len(universe) * 2 * REQUESTS_PER_CALL / max(1, VNSTOCK_MAX_RPM)
    log.info("Screening %d tickers, throttled to %d req/min "
             "(~%.0f min). The script pauses itself near the limit — "
             "let it run.", len(universe), VNSTOCK_MAX_RPM, est_min)
    df = screen(fetcher, universe)
    if df.empty:
        log.error("Screening produced no data.")
        return 1
    out_path = REPORT_DIR / f"screen_{datetime.now(ICT).strftime('%Y%m%d')}.csv"
    df.to_csv(out_path, index=False)
    log.info("Screen saved → %s", out_path)
    cols = ["symbol", "last_price", "pe", "pb", "roe", "roa",
            "net_margin", "mom_6m", "score"]
    cols = [c for c in cols if c in df.columns]
    print("\nTop 10 candidates by composite score:\n")
    print(df[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\nFull table: {out_path}")
    return 0


def cmd_monitor(args) -> int:
    fetcher = DataFetcher()
    watch = WATCHLIST if not args.symbols else args.symbols
    est_min = len(watch) * 4 * REQUESTS_PER_CALL / max(1, VNSTOCK_MAX_RPM)
    log.info("Monitoring %d tickers, throttled to %d req/min "
             "(~%.0f min). The script pauses itself near the limit — "
             "let it run.", len(watch), VNSTOCK_MAX_RPM, est_min)
    payload = monitor(fetcher, watch)
    html = build_html_report(payload)
    path = save_report(html)
    if not args.no_email:
        # Subject reflects alert count so it's scannable in inbox
        n_alerts = sum(len(v["alerts"]) for v in payload["tickers"].values())
        subject = f"[VN30] {datetime.now(ICT):%Y-%m-%d} digest — {n_alerts} alert(s)"
        send_email(subject, html)
    print(f"Report: {path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Vietnam (HOSE) long-term investment helper.")
    sub = p.add_subparsers(dest="mode", required=True)

    p_screen = sub.add_parser("screen", help="Rank tickers by multi-factor score.")
    p_screen.add_argument("--symbols", nargs="*", help="Override SCREEN_UNIVERSE")
    p_screen.set_defaults(func=cmd_screen)

    p_mon = sub.add_parser("monitor", help="Run daily checks on WATCHLIST.")
    p_mon.add_argument("--symbols", nargs="*", help="Override WATCHLIST")
    p_mon.add_argument("--no-email", action="store_true", help="Write report locally, do not send email.")
    p_mon.set_defaults(func=cmd_monitor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
