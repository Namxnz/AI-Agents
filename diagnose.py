"""
Canvas session diagnostic — run this to see exactly what cookies were saved
and test which ones work for API authentication.

Run: python diagnose.py
"""

import json
import requests
from pathlib import Path

SESSION_FILE = Path("./canvas_session.json")

if not SESSION_FILE.exists():
    print("ERROR: canvas_session.json not found. Run canvas_playwright.py first.")
    exit(1)

state = json.loads(SESSION_FILE.read_text())
all_cookies = state.get("cookies", [])

print(f"\nTotal cookies in session: {len(all_cookies)}\n")
print(f"{'Domain':<40} {'Name':<30} {'Value (first 20 chars)'}")
print("-" * 95)
for c in all_cookies:
    val_preview = str(c.get('value', ''))[:20]
    print(f"  {c.get('domain','?'):<38} {c.get('name','?'):<30} {val_preview}")

# Try API with ALL cookies (no domain filter)
print("\n\n--- Testing API with ALL cookies ---")
all_cookie_dict = {c["name"]: c["value"] for c in all_cookies}
resp = requests.get(
    "https://jhu.instructure.com/api/v1/courses?enrollment_state=active&per_page=5",
    cookies=all_cookie_dict
)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    data = resp.json()
    print(f"SUCCESS! Found {len(data)} courses:")
    for c in data:
        print(f"  - {c.get('name', '?')}")
else:
    print(f"Failed: {resp.text[:300]}")

# Also try with canvas.jhu.edu domain
print("\n--- Testing API with canvas.jhu.edu domain ---")
resp2 = requests.get(
    "https://canvas.jhu.edu/api/v1/courses?enrollment_state=active&per_page=5",
    cookies=all_cookie_dict
)
print(f"Status: {resp2.status_code}")
if resp2.status_code == 200:
    data2 = resp2.json()
    print(f"SUCCESS via canvas.jhu.edu! Found {len(data2)} courses:")
    for c in data2:
        print(f"  - {c.get('name', '?')}")
else:
    print(f"Failed: {resp2.text[:300]}")