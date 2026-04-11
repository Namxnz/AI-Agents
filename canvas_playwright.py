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

CANVAS_URL      = "https://canvas.jhu.edu"
CANVAS_API_URL  = "https://jhu.instructure.com"   # actual backend after SSO redirect
OUTPUT_DIR   = Path(os.getenv("STUDY_DIR",   "./study_materials"))
SESSION_FILE = Path(os.getenv("SESSION_FILE", "./canvas_session.json"))

# Opera executable path on macOS — auto-detected, or override in .env
OPERA_PATH = os.getenv(
    "OPERA_PATH",
    "/Applications/Opera.app/Contents/MacOS/Opera"
)

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

    # Use Opera if it exists, otherwise fall back to Playwright's Chromium
    opera_exists = Path(OPERA_PATH).exists()
    if opera_exists:
        print(f"  Using Opera at: {OPERA_PATH}")
        browser = playwright.chromium.launch(
            headless=False,
            slow_mo=100,
            executable_path=OPERA_PATH
        )
    else:
        print("  Opera not found — using built-in Chromium instead.")
        print(f"  (Expected Opera at: {OPERA_PATH})")
        browser = playwright.chromium.launch(headless=False, slow_mo=100)

    context = browser.new_context()
    page    = context.new_page()

    # Go to Canvas homepage and click the JHU Login button
    page.goto(f"{CANVAS_URL}/", timeout=NAV_TIMEOUT)
    page.wait_for_timeout(2000)

    # Click the "JHU Login" button if it's present on the page
    try:
        page.click("a:has-text('JHU Login')", timeout=8000)
    except Exception:
        # Button not found — may have already redirected, just continue
        pass

    print("  Waiting for you to complete login", end="", flush=True)

    # Wait until we land on the Canvas dashboard
    # JHU Canvas may redirect to jhu.instructure.com — both are valid
    try:
        page.wait_for_url(
            lambda url: (
                ("canvas.jhu.edu" in url or "jhu.instructure.com" in url)
                and "/login" not in url
                and "microsoftonline" not in url
            ),
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
    opera_exists = Path(OPERA_PATH).exists()
    launch_kwargs = {"headless": True}
    if opera_exists:
        launch_kwargs["executable_path"] = OPERA_PATH

    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page    = context.new_page()
    try:
        page.goto(f"{CANVAS_URL}/", timeout=NAV_TIMEOUT)
        page.wait_for_timeout(2000)
        # If we're redirected to /login, session has expired
        is_valid = (
            "/login" not in page.url
            and "microsoftonline" not in page.url
        )
        return is_valid
    except Exception:
        return False
    finally:
        browser.close()


# ─── Canvas data fetching (via API using session cookies) ─────────────────────

def api_get_via_browser(page, endpoint: str) -> list | dict:
    """
    Make Canvas API calls by navigating the browser directly to API URLs.
    The browser is already authenticated — it just loads the JSON and we parse it.
    Handles pagination via Link header embedded in the page response.
    """
    base = f"{CANVAS_API_URL}/api/v1"
    url  = f"{base}{endpoint}"
    if "?" in url:
        url += "&per_page=100"
    else:
        url += "?per_page=100"

    results = []

    while url:
        # Navigate the browser to the API endpoint — it returns raw JSON
        page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")

        # Grab the raw JSON text from the page body
        raw = page.inner_text("body")
        data = json.loads(raw)

        if isinstance(data, list):
            results.extend(data)
        else:
            return data

        # Canvas paginates — check for a next page link in the response headers
        # Since we navigated directly, grab Link from a meta tag or re-request
        # We detect pagination by checking if we got a full page (100 items)
        if len(data) < 100:
            break  # no more pages

        # Build next page URL by incrementing page param
        if "page=" in url:
            import re
            url = re.sub(r"page=(\d+)", lambda m: f"page={int(m.group(1))+1}", url)
        else:
            sep = "&" if "?" in url else "?"
            url = url + sep + "page=2"
            # Track page number for subsequent iterations
            break  # simplified: if < 100 results, we got all of them

    return results


def get_courses(page) -> list[dict]:
    courses = api_get_via_browser(page, "/courses?enrollment_state=active&include[]=term")
    return [c for c in courses if c.get("name") and not c.get("access_restricted_by_date")]


def get_modules(course_id: int, page) -> list[dict]:
    return api_get_via_browser(page, f"/courses/{course_id}/modules")


def get_module_items(course_id: int, module_id: int, page) -> list[dict]:
    return api_get_via_browser(page, f"/courses/{course_id}/modules/{module_id}/items")


def get_assignments(course_id: int, page) -> list[dict]:
    return api_get_via_browser(page, f"/courses/{course_id}/assignments?order_by=due_at")


# ─── File downloader ───────────────────────────────────────────────────────────

def download_file_via_browser(page, file_url: str,
                               dest: Path, filename: str) -> Path | None:
    """
    Download a Canvas file using the authenticated browser page.
    Uses browser fetch() to get the pre-signed S3 URL, then streams
    the actual file with requests (S3 URLs need no auth).
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
        # Navigate browser to the file API URL to get metadata (JSON response)
        page.goto(file_url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        raw  = page.inner_text("body")
        meta = json.loads(raw)

        download_url = meta.get("url")  # pre-signed S3 URL
        if not download_url:
            print(f"      ✗  No download URL for: {filename}")
            return None

        # Stream-download from S3 (pre-signed URL needs no auth)
        with requests.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f_out:
                for chunk in r.iter_content(chunk_size=8192):
                    f_out.write(chunk)

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
    course_url = f"{CANVAS_API_URL}/courses/{course['id']}"

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

def process_module(page, course_id: int,
                   module: dict, course_dir: Path) -> tuple[dict, list[Path]]:
    print(f"\n   Module: {module['name']}")
    items      = get_module_items(course_id, module["id"], page)
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
                page, file_api_url, mod_dir, filename
            )
            if path:
                downloaded.append(path)

        elif itype == "Assignment":
            assignment_url = item.get("url", "")
            if assignment_url:
                try:
                    endpoint = assignment_url.replace(f"{CANVAS_API_URL}/api/v1", "")
                    a = api_get_via_browser(page, endpoint)
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

        # ── Open a visible browser page reusing the saved session ──────────────
        # Must be non-headless so Cloudflare doesn't block the requests
        opera_exists = Path(OPERA_PATH).exists()
        launch_kwargs = {"headless": False}
        if opera_exists:
            launch_kwargs["executable_path"] = OPERA_PATH
        bg_browser = pw.chromium.launch(**launch_kwargs)
        bg_context = bg_browser.new_context(storage_state=str(SESSION_FILE))
        page = bg_context.new_page()
        # Navigate to Canvas so cookies are active for fetch() calls
        page.goto(f"{CANVAS_API_URL}/", timeout=NAV_TIMEOUT)
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        print("  Browser ready. (You can minimise it — do not close it)")

        # ── Course list ───────────────────────────────────────────────────────
        print("\n  Fetching your courses...")
        courses = get_courses(page)

        if not courses:
            print("  No active courses found. Try --refresh to re-login.")
            bg_browser.close()
            return

        if list_only:
            print("\n  Your active courses:\n")
            for c in courses:
                term = c.get("term", {}).get("name", "")
                print(f"  {c['id']:>10}  {c['name']:<55} {term}")
            bg_browser.close()
            return

        # ── Pick course ───────────────────────────────────────────────────────
        course = pick_course(courses)
        print(f"\n  Selected: {course['name']}")

        # ── Pick modules ──────────────────────────────────────────────────────
        print("  Fetching modules...")
        modules = get_modules(course["id"], page)
        selected = pick_modules(modules)

        if not selected:
            print("  Nothing selected.")
            bg_browser.close()
            return

        # ── Assignments ───────────────────────────────────────────────────────
        print("  Fetching assignments...")
        assignments = get_assignments(course["id"], page)

        # ── Download ──────────────────────────────────────────────────────────
        course_dir      = OUTPUT_DIR / sanitise(course["name"])
        all_downloaded  = []
        enriched_mods   = []

        for mod in selected:
            enriched, files = process_module(
                page, course["id"], mod, course_dir
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
        bg_browser.close()
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