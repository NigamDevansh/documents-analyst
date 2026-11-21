#!/usr/bin/env python3
"""Turn the styled architecture page into a plain, shareable standalone HTML file.

The artifact version relies on the host to inject <html>/<head>/<body> and to
render mermaid natively. A file someone opens from disk or email gets neither,
so this adds the document skeleton, pulls mermaid from a CDN, and swaps the
elaborate stylesheet for something plain.

Diagram colours are kept: they encode meaning (our code / gate / database),
not decoration.

Usage:  python3 scripts/build_architecture_html.py <source.html> [dest.html]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SIMPLE_CSS = """
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 15px; line-height: 1.6; color: #111; background: #fff;
    max-width: 1000px; margin: 0 auto; padding: 32px 20px 80px;
  }
  .wrap, .wide { max-width: 100%; margin: 0; }
  h1 { font-size: 26px; margin: 0 0 8px; }
  h2 { font-size: 20px; margin: 44px 0 6px; padding-top: 16px; border-top: 2px solid #111; }
  h3 { font-size: 16px; margin: 26px 0 6px; }
  h2 .n, .eyebrow {
    display: block; font-size: 11px; font-weight: normal; letter-spacing: .1em;
    text-transform: uppercase; color: #666; margin-bottom: 4px;
  }
  p { margin: 0 0 12px; }
  .sub, .cap, .note { color: #555; font-size: 14px; }
  .cap { margin: 6px 0 18px; }
  code {
    font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px;
    background: #f2f2f2; padding: 1px 4px; border-radius: 3px;
  }
  pre.code {
    font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px;
    line-height: 1.55; background: #f7f7f7; border: 1px solid #ddd;
    padding: 12px 14px; overflow-x: auto;
  }
  table { border-collapse: collapse; width: 100%; margin: 10px 0 18px; font-size: 13.5px; }
  th, td { border: 1px solid #ddd; padding: 7px 9px; text-align: left; vertical-align: top; }
  th { background: #f2f2f2; font-weight: 600; }
  .diagram {
    border: 1px solid #ddd; padding: 16px; margin: 12px 0 6px;
    overflow-x: auto; background: #fcfcfc;
  }
  pre.mermaid { margin: 0; text-align: center; background: none; }
  pre.mermaid svg { max-width: 100%; height: auto; }
  .key {
    border-left: 3px solid #888; background: #f7f7f7;
    padding: 10px 14px; margin: 14px 0;
  }
  .key.warn { border-left-color: #b3261e; }
  .key p:last-child { margin-bottom: 0; }
  .schema { border: 1px solid #ddd; margin: 12px 0 18px; }
  .schema .hd {
    background: #eee; padding: 6px 10px; font-family: ui-monospace, Menlo, monospace;
    font-size: 13px; font-weight: 600; border-bottom: 1px solid #ddd;
  }
  .schema table { margin: 0; }
  .schema th, .schema td { border: none; border-bottom: 1px solid #eee; }
  ol.steps, ul.rules { padding-left: 20px; }
  ol.steps li, ul.rules li { margin-bottom: 8px; }
  .legend { list-style: none; padding: 0; margin: 14px 0; font-size: 13px; color: #555; }
  .legend li { display: inline-block; margin-right: 18px; }
  .legend .sw {
    display: inline-block; width: 10px; height: 10px;
    border: 1px solid #333; margin-right: 6px;
  }
  .sw.sd { background: #e4ecfd; } .sw.gate { background: #fff3d6; }
  .sw.db { background: #f2f3f5; } .sw.ours { background: #fff; }
  .pill { font-size: 11px; border: 1px solid #999; padding: 1px 5px; margin-right: 4px; }
  .toc { border: 1px solid #ddd; background: #fafafa; padding: 12px 18px; margin: 18px 0; }
  .toc ol { margin: 0; padding-left: 20px; font-size: 14px; }
  .toc a { color: #111; }
  footer { margin-top: 50px; padding-top: 12px; border-top: 1px solid #ddd; font-size: 12px; color: #666; }
  @media print {
    body { max-width: none; }
    h2 { page-break-after: avoid; }
    .diagram, .schema, table { page-break-inside: avoid; }
  }
"""

MERMAID = """<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({ startOnLoad: true, securityLevel: 'loose' });
</script>"""


def build(src: Path, dest: Path) -> None:
    html = src.read_text(encoding="utf-8")

    m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = m.group(1).strip() if m else "Architecture"

    # Drop the artifact's <title> and <style>; we supply our own head.
    body = re.sub(r"<title>.*?</title>", "", html, flags=re.S)
    body = re.sub(r"<style>.*?</style>", "", body, flags=re.S)
    body = body.strip()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{SIMPLE_CSS}</style>
</head>
<body>
{body}
{MERMAID}
</body>
</html>
""",
        encoding="utf-8",
    )

    diagrams = body.count('<pre class="mermaid">')
    print(f"wrote {dest}")
    print(f"  {dest.stat().st_size:,} bytes, {diagrams} diagrams")
    print("  self-contained except the mermaid CDN (needs internet to draw diagrams)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: build_architecture_html.py <source.html> [dest.html]")
    source = Path(sys.argv[1])
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("docs/architecture.html")
    build(source, target)
