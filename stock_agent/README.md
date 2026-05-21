# Stock Agent — VN30 Monitor

Long-term-hold helper for the HOSE market. Two modes in one script.

## Setup (one-time, ~5 minutes)

```bash
cd /Users/mac/Projects/AI-Agents/stock_agent
python3 -m venv .venv             # OR reuse /Users/mac/Projects/.venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then edit .env with your Gmail App Password
```

Gmail App Password setup: https://myaccount.google.com/apppasswords
(Requires 2-Step Verification enabled on your Google account.)

## Usage

### Phase 1 — Pick a stock (run during your analysis month)

```bash
python vn30_monitor.py screen
```

Ranks all 30 VN30 tickers by a 4-factor composite score
(value 40% / quality 30% / growth 20% / momentum 10%) and writes a CSV
to `reports/screen_YYYYMMDD.csv`. Use the top 5–10 as your manual
research shortlist; **do not blindly buy the #1 ranked stock.**

Restrict to a subset:
```bash
python vn30_monitor.py screen --symbols FPT MWG VNM TCB
```

### Phase 2 — Daily monitor (run every weekday after market close)

```bash
python vn30_monitor.py monitor                          # full WATCHLIST, email + local
python vn30_monitor.py monitor --symbols FPT --no-email # just one ticker, local only
```

Generates an HTML digest at `reports/digest_YYYYMMDD.html`. If SMTP creds
are set in `.env`, the digest is also emailed to `EMAIL_TO`. The subject
line shows how many alerts were triggered, so you can scan your inbox
without opening every email.

Trim `WATCHLIST` in `vn30_monitor.py` to your held names once you've
committed capital — fewer tickers = less noise.

## Rate limits & speed

vnstock's free **Guest** tier allows only ~20 API requests per minute and
**terminates the process** if you exceed it. The script throttles itself to
stay under that cap, so a full 30-ticker run is paced, not instant:

- `screen`  — roughly **7 minutes** on the Guest tier
- `monitor` — roughly **10 minutes** on the Guest tier

You'll see `⏳ Rate-limit guard: pausing …` lines while it waits — that's
normal, just let it run. To go faster, register a **free Community API key**
at https://vnstocks.com/login and raise the budget in `.env`:

```
VNSTOCK_MAX_RPM=50      # default is 16 (Guest-safe)
```

That cuts a screen run to ~2 minutes. For a quick test, screen a few names
at a time: `python vn30_monitor.py screen --symbols FPT MWG VCB`.

The deprecation banners and "INSIDERS PROGRAM" ads vnstock prints are
harmless noise — ignore them.

## Scheduling

HOSE closes at 15:00 ICT. The launchd template runs the script daily at
16:00 ICT (= 09:00 UTC = your laptop's local time if you're in Vietnam).

```bash
# Install the schedule
cp com.nam.vn30monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.nam.vn30monitor.plist

# Run a one-off to confirm
launchctl start com.nam.vn30monitor

# Stop it
launchctl unload ~/Library/LaunchAgents/com.nam.vn30monitor.plist
```

If your laptop is asleep at 16:00 ICT, launchd will run the job at next
wake. For 24/7 reliability, run the script on a cheap VPS instead.

## Tuning

All thresholds live at the top of `vn30_monitor.py` in `ALERT_THRESHOLDS`.
Adjust to your risk tolerance — for a 1–2 year hold, the defaults are
intentionally generous (you don't want to be paged for every -2% day).

`NEWS_RED_FLAGS` is the Vietnamese keyword list for headline triage.
Add domain terms as you discover them.

## What this script does NOT do

- It does **not** place trades. Read the alerts, then make your own decision.
- It does **not** include a Bayesian fair-value model (your MCMC research
  belongs in a separate notebook — keep production monitoring boring).
- It does **not** evaluate fundamental deterioration quarter-by-quarter.
  You opted out of that trigger; fundamentals are checked once in `screen`
  mode. Re-run `screen` quarterly to catch slow-burn deterioration.
