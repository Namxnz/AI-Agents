---
title: Stock Agent — VN30 Monitor (Project Recall)
aliases: [VN30 Monitor, stock_agent, Vietnam Stock Agent]
tags: [project/stock-agent, vietnam-stocks, vn30, hose, investing, python]
created: 2026-05-21
status: in-progress
---

# Stock Agent — VN30 Monitor

> [!abstract] One-line summary
> A two-mode Python tool to (1) screen the VN30 universe and shortlist one HOSE stock to buy in the next month and hold 1–2 years, and (2) monitor that holding daily and email me when something goes wrong.

---

## Goal

- Pick **one** stock from VN30 (or a potential VN30 entrant) within the next month.
- Hold it for **1–2 years** (long-term position).
- Get **automatic news + price alerts** when the stock "goes wrong" so I don't have to watch it daily.

---

## Project location

```
/Users/mac/Projects/AI-Agents/
├── research_agent/        # earlier work, moved here
└── stock_agent/           # THIS project
    ├── vn30_monitor.py        # main script (~520 lines, 27 KB)
    ├── requirements.txt       # vnstock>=3.2.0, pandas>=2.0, numpy>=1.24
    ├── .env.example           # SMTP template (copy to .env)
    ├── README.md              # setup / usage / scheduling / tuning
    ├── com.nam.vn30monitor.plist  # launchd schedule (weekdays 16:00 ICT)
    └── introduction.md        # my own file
```

---

## How the tool works

`vn30_monitor.py` is a single file with two CLI subcommands.

### Phase 1 — `screen` (run during my analysis month)

```bash
python vn30_monitor.py screen
python vn30_monitor.py screen --symbols FPT MWG VNM TCB   # subset
```

- Ranks all 30 VN30 tickers by a **4-factor composite z-score**:
  value **40%** / quality **30%** / growth **20%** / momentum **10%**.
- Writes `reports/screen_YYYYMMDD.csv`.

> [!warning] Use it as a shortlist, not a verdict
> Take the **top 5–10** as a manual research shortlist. **Do NOT blindly buy the #1 ranked stock.** Re-run `screen` quarterly to catch slow fundamental deterioration.

### Phase 2 — `monitor` (run every weekday after market close)

```bash
python vn30_monitor.py monitor                           # full watchlist, email + local
python vn30_monitor.py monitor --symbols FPT --no-email   # one ticker, local only
```

- Builds an HTML digest at `reports/digest_YYYYMMDD.html`.
- Emails it to `EMAIL_TO` if SMTP creds are set; subject line shows the alert count.
- After I commit capital, trim `WATCHLIST` in the script to just my held names (less noise).

---

## Alert triggers (what counts as "going wrong")

> [!note] Three trigger groups — fundamentals deliberately excluded as a daily trigger
> Fundamentals are checked once in `screen`; re-run `screen` quarterly instead.

| Group | Signals |
|---|---|
| Price / technical | daily drop, weekly drop, RSI oversold/overbought, volume spike, price below SMA200 |
| News / insider | Vietnamese red-flag keyword scan of company headlines |
| Foreign flow / structure | foreign net selling (1-day and 5-day) |

### Default thresholds (`ALERT_THRESHOLDS` at top of script)

| Threshold | Value |
|---|---|
| daily_drop_pct | -4.0% |
| weekly_drop_pct | -8.0% |
| rsi_oversold | 30 |
| rsi_overbought | 75 |
| volume_spike_x | 2.5× |
| foreign_net_sell_billion | -50.0 |
| foreign_net_sell_5d_billion | -150.0 |
| below_sma200 | True |

Defaults are intentionally generous for a 1–2 year hold — I don't want to be paged for every -2% day. Tune them in the script.

---

## Setup checklist

- [ ] `cd /Users/mac/Projects/AI-Agents/stock_agent`
- [ ] Create/activate venv (or reuse `/Users/mac/Projects/.venv`)
- [ ] `pip install -r requirements.txt`
- [ ] `cp .env.example .env` and add Gmail **App Password** (needs 2-Step Verification)
- [ ] Smoke test: `python vn30_monitor.py monitor --symbols FPT --no-email`
- [ ] Run first real screen: `python vn30_monitor.py screen`
- [ ] Install schedule: copy `.plist` to `~/Library/LaunchAgents/`, then `launchctl load ...`
- [ ] Commit & push `stock_agent/` to the AI-Agents repo

---

## Open / pending tasks

- [ ] Smoke-test the script and confirm `vnstock` returns data
- [ ] Run `screen` and review the top 5–10 shortlist
- [ ] Do manual research on shortlisted names, then pick ONE
- [ ] Trim `WATCHLIST` to the chosen holding once capital is committed
- [ ] Commit & push `stock_agent/`

> [!danger] Security — credentials leak (must close out)
> A `credentials.json` with a real **Google OAuth Client ID + Secret** was committed to git history.
> - [ ] Revoke the credentials at **Google Cloud Console**
> - [ ] Scrub history: `git filter-repo --path credentials.json --invert-paths --force`
> - [ ] Add `credentials.json`, `token.json`, `.env`, `*.pem`, `*.key` to `.gitignore`
> - [ ] Force-push the cleaned history
> - Never use GitHub's "allow secret" bypass URLs for real credentials.

---

## MCMC research — critique (separate from this tool)

> [!tip] Keep production monitoring boring
> My Bayesian/MCMC intrinsic-value research stays in a **separate notebook** — it is NOT part of `vn30_monitor.py`.

- **Flaw found:** the model defined `IntrinsicValue = EPS × PE` and then regressed intrinsic value on EPS — that is **circular** (the target is built from the predictor).
- **Recommended fix:** a **hierarchical residual-income (Ohlson) valuation** — model residual income with priors, pool across tickers, derive value from book value + discounted abnormal earnings rather than from a PE identity.

---

## Key facts to remember

- HOSE market closes **15:00 ICT (UTC+7)**; settlement is **T+2.5**.
- The `launchd` job runs **weekdays 16:00 local time** — assumes the Mac is on Asia/Ho_Chi_Minh time. If laptop is asleep, the job runs on next wake; a cheap VPS is more reliable.
- Data source: **`vnstock`** library (v3.x API).
- Alerts go by **email** to `namxnz@gmail.com` via Gmail SMTP.
- `.env` can live in either `stock_agent/.env` **or** the repo root — the script checks both.
- The script **does not place trades** — it surfaces alerts; I make the decision.

---

## Related notes

- [[VN30 watchlist]]
- [[MCMC stock valuation research]]
- [[Vietnam stock investing — strategy]]
