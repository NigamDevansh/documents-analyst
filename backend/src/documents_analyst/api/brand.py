"""The product name and mark, for pages the API serves itself (the status page shown
before the dashboard is built). The dashboard has its own copy of the mark in
`frontend/src/assets/icon.svg`; `tests/test_brand.py` keeps the two identical."""

from urllib.parse import quote

NAME = "Document Analyst"

ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><g transform="translate(0 0) scale(1)"><rect width="64" height="64" rx="15" fill="#c4622d"/><g transform="translate(10.5 6.8) scale(1.8333)" fill="none" stroke="#ffffff" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12.5v-5A1.5 1.5 0 0 1 4.5 6H8l1.5 2h10A1.5 1.5 0 0 1 21 9.5v9a1.5 1.5 0 0 1-1.5 1.5h-8"/><path d="M4.9 19.1 2.5 21.5"/><circle cx="7" cy="17" r="3"/></g></g></svg>'

ICON_DATA_URI = "data:image/svg+xml," + quote(ICON_SVG)
