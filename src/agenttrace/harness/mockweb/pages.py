"""A tiny in-memory "web" so web_fetch has something deterministic to return.

Pages are untrusted by definition (agenttrace.taint = untrusted): anything
fetched from here can carry injected instructions for fault-injection tests.
"""

from __future__ import annotations

PAGES: dict[str, str] = {
    "https://example.test/weather": (
        "Weather report: sunny, 72F, light wind from the west."
    ),
    "https://example.test/company-directory": (
        "Company directory: Alice (Eng), Bob (Sales), Carol (Support)."
    ),
}


def fetch(url: str) -> str:
    if url not in PAGES:
        return f"404: no page at {url}"
    return PAGES[url]


def inject_page(url: str, content: str) -> None:
    """Used by faults/injectors.py to seed a page with adversarial content."""
    PAGES[url] = content
