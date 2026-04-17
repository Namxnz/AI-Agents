#!/usr/bin/env python3
"""
Personal Research Agent
-----------------------
Scrapes websites, reads PDFs, and outputs a structured report.

Usage:
    python research_agent.py "AI in healthcare" \
        --urls https://example.com/report1 https://example.com/report2 \
        --pdfs whitepaper.pdf study.pdf \
        --output report.md \
        --google-doc

Requirements:
    pip install -r requirements.txt
    Set ANTHROPIC_API_KEY environment variable.
    For Google Docs: place credentials.json from Google Cloud Console in this directory.
"""

import argparse
import os
import sys
import re
from pathlib import Path

import anthropic

MODEL = "claude-opus-4-6"
MAX_TOKENS = 16000
MAX_CONTINUATIONS = 5  # max pause_turn re-sends


# ---------------------------------------------------------------------------
# PDF upload via Files API
# ---------------------------------------------------------------------------

def upload_pdfs(client: anthropic.Anthropic, pdf_paths: list[str]) -> list[tuple[str, str]]:
    """Upload PDFs to the Anthropic Files API. Returns [(filename, file_id), ...]."""
    results = []
    for path_str in pdf_paths:
        p = Path(path_str)
        if not p.exists():
            print(f"  Warning: PDF not found, skipping: {path_str}", file=sys.stderr)
            continue
        print(f"  Uploading {p.name} ...", end=" ", flush=True)
        with open(p, "rb") as f:
            uploaded = client.beta.files.upload(
                file=(p.name, f, "application/pdf"),
            )
        results.append((p.name, uploaded.id))
        print(f"ok ({uploaded.id})")
    return results


def delete_uploaded_files(client: anthropic.Anthropic, file_ids: list[str]) -> None:
    """Clean up Files API uploads after the run."""
    for fid in file_ids:
        try:
            client.beta.files.delete(fid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    return (
        "You are a thorough research analyst. "
        "When given a topic, you retrieve every source provided, "
        "read all attached documents carefully, and synthesize the findings "
        "into a well-structured markdown report. "
        "Always cite specific sources inline (e.g., [Source Name](url))."
    )


def build_user_message(
    topic: str,
    urls: list[str],
    pdf_names: list[str],
    pdf_file_ids: list[tuple[str, str]],
) -> list[dict]:
    """Build the content list for the user turn."""
    url_lines = "\n".join(f"- {u}" for u in urls) if urls else "_(none provided)_"
    pdf_lines = "\n".join(f"- {n}" for n in pdf_names) if pdf_names else "_(none provided)_"

    instructions = f"""Research the topic below and produce a complete report.

**Topic:** {topic}

**URLs to fetch** (use web_fetch for each):
{url_lines}

**PDF documents attached:**
{pdf_lines}

**Steps:**
1. Call `web_fetch` for every URL listed above.
2. If no URLs are provided, use `web_search` to find 3–5 authoritative sources first.
3. Read every attached PDF document in full.
4. Write the report using this exact markdown structure:

---

# [Descriptive Report Title]

## Executive Summary
_(2–3 paragraphs summarising the key conclusions)_

## Key Findings
_(Bulleted list of the most important insights)_

## Detailed Analysis
_(Multiple ### subsections as needed, e.g., Background, Current State, Challenges, Opportunities)_

## Conclusion
_(Implications and actionable recommendations)_

## Sources
_(Numbered list of all sources with URLs)_

---

Write the complete report now. Be thorough, factual, and cite sources throughout."""

    content: list[dict] = [{"type": "text", "text": instructions}]

    # Attach each PDF as a document block (requires Files API beta header)
    for name, file_id in pdf_file_ids:
        content.append({
            "type": "document",
            "source": {"type": "file", "file_id": file_id},
            "title": name,
        })

    return content


# ---------------------------------------------------------------------------
# Core research loop
# ---------------------------------------------------------------------------

def run_research(
    topic: str,
    urls: list[str] | None = None,
    pdf_paths: list[str] | None = None,
    verbose: bool = True,
) -> str:
    """
    Run the research agent.

    Returns the final report as a markdown string.
    Prints streaming output to stdout while running.
    """
    client = anthropic.Anthropic()
    urls = urls or []
    pdf_paths = pdf_paths or []

    # Upload PDFs (if any) via Files API
    pdf_file_ids: list[tuple[str, str]] = []
    if pdf_paths:
        print("Uploading PDFs to Anthropic Files API...")
        pdf_file_ids = upload_pdfs(client, pdf_paths)

    pdf_names = [name for name, _ in pdf_file_ids]

    # Build message
    user_content = build_user_message(topic, urls, pdf_names, pdf_file_ids)
    messages: list[dict] = [{"role": "user", "content": user_content}]

    # Server-side tools: web_fetch + web_search (both run entirely on Anthropic infra)
    tools: list[dict] = [
        {"type": "web_fetch_20260209", "name": "web_fetch"},
        {"type": "web_search_20260209", "name": "web_search"},
    ]

    # Files API beta header (needed only when PDF documents are attached)
    extra_headers: dict[str, str] = {}
    if pdf_file_ids:
        extra_headers["anthropic-beta"] = "files-api-2025-04-14"

    if verbose:
        print(f"\nResearching: {topic}")
        print("=" * 70)

    report_text = ""

    try:
        for attempt in range(MAX_CONTINUATIONS):
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=build_system_prompt(),
                tools=tools,
                messages=messages,
                extra_headers=extra_headers,
            ) as stream:
                for text_chunk in stream.text_stream:
                    if verbose:
                        print(text_chunk, end="", flush=True)
                response = stream.get_final_message()

            # Collect the last text block as the report
            for block in response.content:
                if block.type == "text":
                    report_text = block.text

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "pause_turn":
                # Server-side tool loop hit its 10-iteration limit; re-send to continue
                messages.append({"role": "assistant", "content": response.content})
                if verbose:
                    print("\n[Continuing research...]\n", flush=True)
            else:
                # Unexpected stop; exit loop
                break

    finally:
        # Always clean up uploaded files
        if pdf_file_ids:
            delete_uploaded_files(client, [fid for _, fid in pdf_file_ids])

    if verbose:
        print("\n" + "=" * 70)

    return report_text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Personal Research Agent — scrapes sites, reads PDFs, writes a report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("topic", help='Research topic, e.g. "AI safety in 2025"')
    parser.add_argument("--urls", nargs="*", default=[], metavar="URL", help="URLs to fetch")
    parser.add_argument("--pdfs", nargs="*", default=[], metavar="FILE", help="PDF files to read")
    parser.add_argument("--output", default="report.md", help="Output markdown file (default: report.md)")
    parser.add_argument(
        "--google-doc",
        action="store_true",
        help="Also publish the report as a Google Doc",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to Google OAuth2 credentials JSON (default: credentials.json)",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    report = run_research(
        topic=args.topic,
        urls=args.urls,
        pdf_paths=args.pdfs,
    )

    if not report.strip():
        print("\nWarning: the agent returned an empty report.", file=sys.stderr)

    # Save markdown
    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {output_path.resolve()}")

    # Optionally create Google Doc
    if args.google_doc:
        try:
            from google_docs import create_google_doc  # type: ignore
        except ImportError:
            print(
                "Error: google_docs.py not found or Google dependencies missing.\n"
                "Run: pip install google-api-python-client google-auth-oauthlib",
                file=sys.stderr,
            )
            sys.exit(1)

        print("Publishing to Google Docs...")
        doc_url = create_google_doc(
            title=f"Research Report: {args.topic}",
            markdown_content=report,
            credentials_file=args.credentials,
        )
        print(f"Google Doc: {doc_url}")


if __name__ == "__main__":
    main()