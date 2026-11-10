"""HTMLParser 兜底正文抽取。"""
from __future__ import annotations

from html.parser import HTMLParser

class _TextExtract(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "iframe", "nav",
             "footer", "header", "form", "aside", "button", "select"}
    _BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)
