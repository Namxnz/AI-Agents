"""
Google Docs integration for the Research Agent.

Setup:
    1. Go to https://console.cloud.google.com
    2. Create a project and enable the Google Docs API and Google Drive API.
    3. Create OAuth 2.0 Desktop credentials and download as credentials.json.
    4. On first run, a browser window will open for authorization.
       Subsequent runs use the cached token.json.
"""

import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_google_credentials(
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
) -> Credentials:
    """Obtain (and cache) Google OAuth2 credentials."""
    creds: Credentials | None = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"Google credentials file not found: {credentials_file}\n"
                    "Download it from https://console.cloud.google.com → "
                    "APIs & Services → Credentials → OAuth 2.0 Client IDs."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Markdown → Google Docs requests
# ---------------------------------------------------------------------------

def _utf16_len(text: str) -> int:
    """Return the number of UTF-16 code units in text (Google Docs uses these for indices)."""
    return len(text.encode("utf-16-le")) // 2


def _strip_inline_markdown(text: str) -> str:
    """Remove common inline markdown so it doesn't appear verbatim in the doc."""
    # Bold / italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"___(.+?)___", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Inline code
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Links: [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}$", "", text)
    return text


def markdown_to_requests(text: str) -> list[dict]:
    """
    Convert a markdown string into a list of Google Docs batchUpdate requests.

    Handles:
      - # ## ### #### headings → HEADING_1 … HEADING_4
      - - / * / + bullet lines → bulleted list
      - Numbered list lines (1. 2. …) → numbered list
      - Code blocks (``` fences) → NORMAL_TEXT with monospace font
      - Everything else → NORMAL_TEXT
    """
    requests: list[dict] = []
    pos = 1  # Google Docs body content index starts at 1

    lines = text.split("\n")
    in_code_block = False

    for raw_line in lines:
        # Toggle code fence
        if raw_line.strip().startswith("```"):
            in_code_block = not in_code_block
            # Insert a blank line for visual separation
            requests.append({"insertText": {"location": {"index": pos}, "text": "\n"}})
            pos += 1
            continue

        style = "NORMAL_TEXT"
        is_bullet = False
        is_numbered = False

        if in_code_block:
            # Render code lines in Courier New
            clean = raw_line
            insert_text = clean + "\n"
            char_count = _utf16_len(insert_text)
            end = pos + char_count

            requests.append({"insertText": {"location": {"index": pos}, "text": insert_text}})
            # Apply monospace font to the code text (excluding trailing newline)
            if char_count > 1:
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": pos, "endIndex": end - 1},
                        "textStyle": {"weightedFontFamily": {"fontFamily": "Courier New"}},
                        "fields": "weightedFontFamily",
                    }
                })
            pos = end
            continue

        line = raw_line

        # Determine heading / list type
        if line.startswith("#### "):
            style, clean = "HEADING_4", line[5:]
        elif line.startswith("### "):
            style, clean = "HEADING_3", line[4:]
        elif line.startswith("## "):
            style, clean = "HEADING_2", line[3:]
        elif line.startswith("# "):
            style, clean = "HEADING_1", line[2:]
        elif re.match(r"^[-*+] ", line):
            is_bullet = True
            clean = line[2:]
        elif re.match(r"^\d+\. ", line):
            is_numbered = True
            clean = re.sub(r"^\d+\. ", "", line)
        else:
            clean = line

        clean = _strip_inline_markdown(clean)
        insert_text = clean + "\n"
        char_count = _utf16_len(insert_text)
        end = pos + char_count

        # 1. Insert text
        requests.append({
            "insertText": {
                "location": {"index": pos},
                "text": insert_text,
            }
        })

        # 2. Paragraph style (headings)
        if style != "NORMAL_TEXT" and char_count > 1:
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": pos, "endIndex": end - 1},
                    "paragraphStyle": {"namedStyleType": style},
                    "fields": "namedStyleType",
                }
            })

        # 3. Bullet / numbered list
        if (is_bullet or is_numbered) and char_count > 1:
            preset = (
                "BULLET_DISC_CIRCLE_SQUARE" if is_bullet
                else "NUMBERED_DECIMAL_ALPHA_ROMAN"
            )
            requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": pos, "endIndex": end - 1},
                    "bulletPreset": preset,
                }
            })

        pos = end

    return requests


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_google_doc(
    title: str,
    markdown_content: str,
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
) -> str:
    """
    Create a Google Doc from markdown content.

    Returns the edit URL for the created document.
    """
    creds = get_google_credentials(credentials_file, token_file)
    docs_svc = build("docs", "v1", credentials=creds)

    # Create an empty document with the given title
    doc = docs_svc.documents().create(body={"title": title}).execute()
    doc_id: str = doc["documentId"]
    print(f"  Created document: {doc_id}")

    # Convert markdown to batchUpdate requests and apply in chunks
    # (Google Docs API limit: 50,000 requests per call)
    all_requests = markdown_to_requests(markdown_content)
    chunk_size = 400  # conservative chunk to stay well under limits

    for i in range(0, len(all_requests), chunk_size):
        chunk = all_requests[i : i + chunk_size]
        docs_svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": chunk},
        ).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"