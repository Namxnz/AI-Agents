---
title: Stock Agent — Session Log (2026-05-22)
aliases: [VN30 Monitor session log, rate-limit fix]
tags: [project/stock-agent, vietnam-stocks, vn30, changelog, session-log]
created: 2026-05-22
status: done-pending-push
---

# Stock Agent — Session Log (2026-05-22)

> [!abstract] TL;DR
> The `screen` command crashed because vnstock's free tier rate-limits us.
> Fixed it with a self-throttling rate limiter, added a `.gitignore`, and
> committed everything as `96cd490`. **One step left: push from my own Mac.**

---

## 1. What broke

Running `python vn30_monitor.py screen` died after only 6 tickers
(ACB → FPT) with:

```
⚠️  GIỚI HẠN API ĐÃ ĐẠT TỐI ĐA (Rate Limit Exceeded)
   • Gói hiện tại: Khách (Guest)
   • Giới hạn: 20 requests/phút
 Process terminated.
```

> [!danger] Root cause
> vnstock's free **Guest tier = ~20 API requests/minute**, and the library
> **hard-kills the process** when you exceed it — there is no exception to
> catch. The script fired ~3 requests per ticker with no pacing, so it blew
> past the cap in ~20 seconds.

---

## 2. What was fixed (in `vn30_monitor.py`)

- Added a sliding-window **`RateLimiter`** that paces every vnstock call so
  the trailing 60-second window never exceeds the cap. When the window is
  full it sleeps and logs `⏳ Rate-limit guard: pausing Ns …`, then resumes.
- Budget is one env knob — **`VNSTOCK_MAX_RPM`** (default **16**, Guest-safe).
- **Cached** the `Vnstock()` client + per-symbol stock objects so repeated
  calls for the same ticker stop wasting requests.
- Added a startup log line estimating runtime.
- Documented the knob in `.env.example` and a new "Rate limits & speed"
  section in `README.md`.

> [!tip] Speed
> Full 30-ticker `screen` ≈ **7 min** on Guest, `monitor` ≈ **10 min**.
> Registering a **free Community key** at https://vnstocks.com/login lets me
> raise `VNSTOCK_MAX_RPM` to ~50 → screen in ~2 min.
> The vnstock deprecation banners and "INSIDERS PROGRAM" ads are harmless.

---

## 3. The git cleanup

> [!warning] What went wrong in the repo
> A broad `git add` had staged **10,795 files** — the entire `.venv/` folder
> *and* `stock_agent/.env` (which holds my Gmail App Password). Nothing was
> committed yet, so nothing leaked — but it had to be unstaged first.

Actions taken:

- Created a repo-root **`.gitignore`** → `.venv/`, `__pycache__/`, `*.pyc`,
  `.env`, `reports/`, `*.log`, plus `credentials.json` / `token.json` /
  `*.pem` / `*.key`.
- `git reset` to unstage the 10,795 files; stopped tracking a stray `.pyc`.
- Committed only the intended 6 files.

> [!note] Sandbox quirk (why git was noisy)
> The Cowork file mount blocks file *deletion* inside `.git/`, so every git
> write left stale 0-byte `.lock` files behind. Renaming works, so the locks
> were quarantined into `.git/_purged_locks/`. The repo itself is healthy.

---

## 4. The commit

```
96cd490  stock_agent: throttle vnstock requests, add .gitignore
```

Files: `.gitignore`, `stock_agent/vn30_monitor.py`, `README.md`,
`.env.example`, `Stock Agent - Obsidian Recall.md`, minus a stray `.pyc`.
**Verified: no `.env`, no `.venv` in the commit.** `.env` never entered git
history and is now ignored.

---

## 5. Open / next steps

- [ ] **Push from my Mac** — the sandbox can't auth over SSH:
  ```bash
  cd /Users/mac/Projects/AI-Agents
  git push origin main          # currently 1 commit ahead of origin
  ```
- [ ] Tidy the leftover sandbox clutter in `.git/`:
  ```bash
  rm -rf .git/_purged_locks && git gc
  ```
- [ ] Re-run the screener once pushed:
  `python vn30_monitor.py screen --symbols FPT MWG VCB TCB VNM` (quick test),
  then the full `python vn30_monitor.py screen`.
- [ ] Optional: register a free vnstock Community key and bump
  `VNSTOCK_MAX_RPM` to ~50 in `.env`.

---

## 6. Things to remember

- vnstock Guest tier = 20 req/min and **kills the process** on breach —
  the script now throttles itself; just let it run.
- The git remote is SSH (`git@github.com:Namxnz/AI-Agents.git`) — pushes
  must come from my Mac, not the assistant sandbox.
- One past commit was authored as `namxnz@gmnail.com` (typo of `gmail.com`)
  → check `git config --global user.email` so future commits link to my
  GitHub account.
- `.gitignore` now prevents the 10k-file `git add` mistake from recurring.

## Related notes

- [[Stock Agent - Obsidian Recall]]
- [[VN30 watchlist]]
