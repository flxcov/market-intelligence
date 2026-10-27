"""Plain-text extraction from HTML (stdlib only). Used on untrusted documents."""

import html
import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "head", "title", "noscript"}
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "td"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Visible text with whitespace collapsed. Never executes or follows anything."""
    if "<" not in markup:
        return re.sub(r"\s+", " ", html.unescape(markup)).strip()
    parser = _Text()
    parser.feed(markup)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()
