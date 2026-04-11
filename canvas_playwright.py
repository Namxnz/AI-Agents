"""
JHU Canvas Study Agent — Playwright Downloader
================================================
Uses your real browser session (no API token needed).
You log in once via JHU SSO + Duo MFA, session is saved,
and all future runs download silently in the background.

Setup:
    pip install playwright python-dotenv
    playwright install chromium

Usage:
    python canvas_playwright.py               # interactive, pick course + module
    python canvas_playwright.py --refresh     # force re-login (if session expired)
    python canvas_playwright.py --list        # list your courses and exit
"""

import os
import re
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

CANVAS_URL   = "https://canvas.jhu.edu"
OUTPUT_DIR   = Path(os.getenv("STUDY_DIR",   "./study_materials"))
SESSION_FILE = Path(os.getenv("SESSION_FILE", "./canvas_session.json"))

SKIP_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".zip"}
LOGIN_TIMEOUT   = 120_000   # 2 min for manual SSO + MFA
NAV_TIMEOUT     = 30_000


# ─── Helpers ───────────────────────────────────────────────────────────────────

def sanitise(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:120]


def print_banner(text: str):
    bar = "━" * 50
    print(f"\n{bar}\n  {text}\n{bar}")


# ─── Session management ────────────────────────────────────────────────────────

def save_session(context):
    """Persist browser cookies + storage to disk."""
    state = context.storage_state()
    SESSION_FILE.write_text(json.dumps(state, indent=2))
    print(f"  Session saved → {SESSION_FILE}")


def session_exists() -> bool:
    return SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 100


def load_session(playwright, headless=True):
    """Return a browser context with saved session loaded."""
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    return browser, context


# ─── Login flow ────────────────────────────────────────────────────────────────

def do_login(playwright) -> bool:
    """
    Opens a visible browser window so you can log in via JHU SSO + Duo MFA.
    Waits until Canvas dashboard is fully loaded, then saves the session.
    """
    print("\n  A browser window will open for you to log in.")
    print("  Complete your JHED login and Duo MFA as normal.")
    print("  The window will close automatically once you're in.\n")

    browser = playwright.chromium.launch(headless=False, slow_mo=100)
    context = browser.new_context()
    page    = context.new_page()

    page.goto(f"{CANVAS_URL}/login/saml", timeout=NAV_TIMEOUT)

    print("  Waiting for you to complete login", end="", flush=True)

    # Wait until we land on the Canvas dashboard (URL no longer has /login)
    try:
        page.wait_for_url(
            lambda url: "canvas.jhu.edu" in url and "/login" not in url,
            timeout=LOGIN_TIMEOUT
        )
    except PWTimeout:
        print("\n  Login timed out. Please try again.")
        browser.close()
        return False

    # Extra wait to make sure all cookies are fully set
    print(" done.")
    page.wait_for_timeout(2000)
    save_session(context)
    browser.close()
    return True


def verify_session(playwright) -> bool:
    """Check if the saved session is still valid by loading the dashboard."""
    browser, context = load_session(playwright, headless=True)
    page = context.new_page()
    try:
        page.goto(f"{CANVAS_URL}/", timeout=NAV_TIMEOUT)
        page.wait_for_timeout(2000)
        # If we're redirected to /login, session has expired
        is_valid = "/login" not in page.url
        return is_valid
    except Exception:
        return False
    finally:
        browser.close()


# ─── Canvas data fetching (via API using session cookies) ─────────────────────

def get_cookies_dict(playwright) -> dict:
    """Extract cookies from the saved session as a requests-compatible dict."""
    state = json.loads(SESSION_FILE.read_text())
    return {c["name"]: c["value"] for c in state.get("cookies", [])}


def api_get(endpoint: str, cookies: dict) -> list | dict:
    """Canvas API GET with pagination, using session cookies for auth."""
    url     = f"{CANVAS_URL}/api/v1{endpoint}"
    params  = {"per_page": 100}
    results = []

    while url:
        resp = requests.get(url, cookies=cookies, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            results.extend(data)
        else:
            return data

        # Follow Canvas Link-header pagination
        url    = None
        params = {}
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break

    return results


def get_courses(cookies: dict) -> list[dict]:
    courses = api_get("/courses?enrollment_state=active&include[]=term", cookies)
    return [c for c in courses if c.get("name") and not c.get("access_restricted_by_date")]


def get_modules(course_id: int, cookies: dict) -> list[dict]:
    return api_get(f"/courses/{course_id}/modules", cookies)


def get_module_items(course_id: int, module_id: int, cookies: dict) -> list[dict]:
    return api_get(f"/courses/{course_id}/modules/{module_id}/items", cookies)


def get_assignments(course_id: int, cookies: dict) -> list[dict]:
    return api_get(f"/courses/{course_id}/assignments?order_by=due_at", cookies)


# ─── File downloader ───────────────────────────────────────────────────────────

def download_file_via_browser(playwright, cookies: dict, file_url: str,
                               dest: Path, filename: str) -> Path | None:
    """
    Download a Canvas file using the browser session.
    Canvas files need an authenticated redirect to S3 — we resolve the
    real download URL via the API, then stream it with requests + cookies.
    """
    ext = Path(filename).suffix.lower()
    if ext in SKIP_EXTENSIONS:
        print(f"      ⏭  Skipping: {filename}")
        return None

    dest.mkdir(parents=True, exist_ok=True)
    filepath = dest / sanitise(filename)

    if filepath.exists():
        print(f"      ✓  Already exists: {filename}")
        return filepath

    try:
        # Step 1: resolve the Canvas file metadata to get the pre-signed S3 URL
        meta_resp = requests.get(file_url, cookies=cookies, timeout=15,
                                 allow_redirects=True)
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        download_url = meta.get("url")  # pre-signed S3 URL (no auth needed)

        if not download_url:
            print(f"      ✗  No download URL for: {filename}")
            return None

        # Step 2: stream-download from S3
        with requests.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        print(f"      ↓  Downloaded: {filename}")
        return filepath

    except Exception as e:
        print(f"      ✗  Failed ({filename}): {e}")
        return None


# ─── Interactive pickers ───────────────────────────────────────────────────────

def pick_course(courses: list[dict]) -> dict:
    print("\n  Your active courses:\n")
    for i, c in enumerate(courses, 1):
        term = c.get("term", {}).get("name", "")
        print(f"  [{i:2}]  {c['name']:<55} {term}")

    while True:
        raw = input("\n  Enter course number (or 'q' to quit): ").strip()
        if raw.lower() == "q":
            sys.exit(0)
        if raw.isdigit() and 1 <= int(raw) <= len(courses):
            return courses[int(raw) - 1]
        print("      Invalid — try again.")


def pick_modules(modules: list[dict]) -> list[dict]:
    if not modules:
        print("  No modules found for this course.")
        return []

    print("\n  Modules in this course:\n")
    for i, m in enumerate(modules, 1):
        count = m.get("items_count", "?")
        print(f"  [{i:2}]  {m['name']:<55} ({count} items)")
    print(f"  [ A]  Download ALL modules")

    raw = input("\n  Enter module number(s) separated by commas, or 'A' for all: ").strip()

    if raw.upper() == "A":
        return modules

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(modules):
            selected.append(modules[int(part) - 1])
    return selected


# ─── Obsidian index writer ─────────────────────────────────────────────────────

def write_index(course_dir: Path, course: dict, modules_data: list[dict],
                assignments: list[dict], downloaded: list[Path]) -> Path:
    today      = datetime.now().strftime("%Y-%m-%d")
    index_path = course_dir / "_index.md"
    course_url = f"{CANVAS_URL}/courses/{course['id']}"

    lines = [
        f"# {course['name']}",
        f"",
        f"> Downloaded: {today}  ",
        f"> Source: [Canvas]({course_url})",
        f"",
        f"---",
        f"",
        f"## Modules downloaded",
        f"",
    ]

    for mod in modules_data:
        lines.append(f"### {mod['name']}")
        for item in mod.get("items", []):
            label = item.get("title", "Untitled")
            itype = item.get("type", "")
            link  = item.get("html_url", "#")
            lines.append(f"- [{label}]({link}) `{itype}`")
        lines.append("")

    if assignments:
        lines += ["---", "", "## Upcoming assignments", ""]
        now = datetime.now().isoformat()
        upcoming = [a for a in assignments if (a.get("due_at") or "9999") > now]
        for a in upcoming[:10]:
            due = (a.get("due_at") or "No due date")[:10]
            lines.append(f"- **{a['name']}** — due {due}")
        lines.append("")

    if downloaded:
        lines += ["---", "", "## Files saved", ""]
        for f in downloaded:
            try:
                rel = f.relative_to(course_dir)
                lines.append(f"- [[{rel}]]")
            except ValueError:
                lines.append(f"- {f.name}")
        lines.append("")

    lines += [
        "---",
        "",
        "## My study notes",
        "",
        "> _Paste Claude's summary here after your study session_",
        "",
        "### Key concepts",
        "",
        "### Definitions & formulas",
        "",
        "### Worked examples",
        "",
        "### Questions / gaps",
        "",
    ]

    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


# ─── Module processor ──────────────────────────────────────────────────────────

def process_module(playwright, cookies: dict, course_id: int,
                   module: dict, course_dir: Path) -> tuple[dict, list[Path]]:
    print(f"\n   Module: {module['name']}")
    items      = get_module_items(course_id, module["id"], cookies)
    module     = {**module, "items": items}
    downloaded = []
    mod_dir    = course_dir / sanitise(module["name"])

    for item in items:
        itype = item.get("type")
        title = item.get("title", "Untitled")

        if itype == "File":
            file_api_url = item.get("url")
            if not file_api_url:
                continue
            filename = title
            path = download_file_via_browser(
                playwright, cookies, file_api_url, mod_dir, filename
            )
            if path:
                downloaded.append(path)

        elif itype == "Assignment":
            assignment_url = item.get("url", "")
            if assignment_url:
                try:
                    endpoint = assignment_url.replace(f"{CANVAS_URL}/api/v1", "")
                    a = api_get(endpoint, cookies)
                    stub = mod_dir / sanitise(f"{title}.md")
                    mod_dir.mkdir(parents=True, exist_ok=True)
                    stub.write_text(
                        f"# {a.get('name', title)}\n\n"
                        f"**Due:** {(a.get('due_at') or 'TBD')[:10]}\n\n"
                        f"**Points:** {a.get('points_possible', 'N/A')}\n\n"
                        f"## Description\n\n"
                        f"{a.get('description') or '_No description provided_'}\n\n"
                        f"## My approach\n\n"
                        f"> _Outline your solution strategy here_\n\n"
                        f"## LaTeX draft\n\n"
                        f"> _Paste from Overleaf when complete_\n",
                        encoding="utf-8"
                    )
                    print(f"      ✎  Assignment stub: {title}.md")
                    downloaded.append(stub)
                except Exception as e:
                    print(f"      ✗  Could not fetch assignment: {e}")

        elif itype in ("Page", "ExternalUrl", "SubHeader"):
            print(f"      ℹ  Skipping {itype}: {title}")

    return module, downloaded


# ─── Main ──────────────────────────────────────────────────────────────────────

def run(force_login=False, list_only=False):
    print_banner("JHU Canvas Study Agent — Playwright")

    with sync_playwright() as pw:

        # ── Session handling ──────────────────────────────────────────────────
        if force_login or not session_exists():
            print("\n  No saved session found — opening browser for login...")
            ok = do_login(pw)
            if not ok:
                return
        else:
            print("\n  Checking saved session...")
            if not verify_session(pw):
                print("  Session expired — opening browser for re-login...")
                ok = do_login(pw)
                if not ok:
                    return
            else:
                print("  Session valid ✓")

        cookies = get_cookies_dict(pw)

        # ── Course list ───────────────────────────────────────────────────────
        print("\n  Fetching your courses...")
        courses = get_courses(cookies)

        if not courses:
            print("  No active courses found. Try --refresh to re-login.")
            return

        if list_only:
            print("\n  Your active courses:\n")
            for c in courses:
                term = c.get("term", {}).get("name", "")
                print(f"  {c['id']:>10}  {c['name']:<55} {term}")
            return

        # ── Pick course ───────────────────────────────────────────────────────
        course = pick_course(courses)
        print(f"\n  Selected: {course['name']}")

        # ── Pick modules ──────────────────────────────────────────────────────
        print("  Fetching modules...")
        modules = get_modules(course["id"], cookies)
        selected = pick_modules(modules)

        if not selected:
            print("  Nothing selected.")
            return

        # ── Assignments ───────────────────────────────────────────────────────
        print("  Fetching assignments...")
        assignments = get_assignments(course["id"], cookies)

        # ── Download ──────────────────────────────────────────────────────────
        course_dir      = OUTPUT_DIR / sanitise(course["name"])
        all_downloaded  = []
        enriched_mods   = []

        for mod in selected:
            enriched, files = process_module(
                pw, cookies, course["id"], mod, course_dir
            )
            enriched_mods.append(enriched)
            all_downloaded.extend(files)

        # ── Obsidian index ────────────────────────────────────────────────────
        index = write_index(
            course_dir, course, enriched_mods, assignments, all_downloaded
        )

        # ── Summary ───────────────────────────────────────────────────────────
        print_banner("Done!")
        print(f"  Files downloaded : {len(all_downloaded)}")
        print(f"  Saved to         : {course_dir.resolve()}")
        print(f"  Obsidian index   : {index.resolve()}")
        print()
        print("  Next step — open your Claude Project and paste:")
        print()
        print('  "Read all attached files. Explain the key concepts,')
        print('   define all important terms, work through any examples,')
        print('   and format your output as Obsidian markdown."')
        print()


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="JHU Canvas Study Agent — Playwright downloader"
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Force re-login even if a session exists"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List your courses and exit"
    )
    parser.add_argument(
        "--output", type=str,
        help="Override output directory"
    )
    args = parser.parse_args()

    if args.output:
        OUTPUT_DIR = Path(args.output)

    run(force_login=args.refresh, list_only=args.list)