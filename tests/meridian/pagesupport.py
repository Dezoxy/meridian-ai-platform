"""What the tests of the adjuster's pages share beyond ``test_adjuster_pages.py``
(S070): the claim ID they use and two readers of the rendered HTML, one for the
claim page's ``<dt>``/``<dd>`` pairs and one for the queue's table."""

from html.parser import HTMLParser

CLAIM = "CLM-9301"


class Definitions(HTMLParser):
    """The page's ``<dt>``/``<dd>`` pairs, in order, as (label, text)."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.pairs: list[tuple[str, str]] = []
        self._open: str | None = None
        self._label = ""
        self._text = ""
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("dt", "dd"):
            self._open, self._text = tag, ""

    def handle_data(self, data: str) -> None:
        if self._open:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "dt" and self._open == "dt":
            self._label = self._text.strip()
        elif tag == "dd" and self._open == "dd":
            self.pairs.append((self._label, self._text.strip()))
        if tag in ("dt", "dd"):
            self._open = None

    def labels(self) -> list[str]:
        return [label for label, _ in self.pairs]

    def value(self, label: str) -> str:
        (found,) = [text for name, text in self.pairs if name == label]
        return found


class Table(HTMLParser):
    """The queue's table: each body row as {header: cell text}."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.headers: list[str] = []
        self.rows: list[dict[str, str]] = []
        self._cells: list[str] | None = None
        self._in = ""
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells = []
        elif tag in ("th", "td"):
            self._in = tag
            if self._cells is not None:
                self._cells.append("")

    def handle_data(self, data: str) -> None:
        if self._in and self._cells:
            self._cells[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td"):
            self._in = ""
        elif tag == "tr" and self._cells is not None:
            cells = [c.strip() for c in self._cells]
            if self.headers and len(cells) == len(self.headers):
                self.rows.append(dict(zip(self.headers, cells, strict=True)))
            elif not self.headers:
                self.headers = cells
            self._cells = None

    def row(self, claim_id: str) -> dict[str, str]:
        (found,) = [r for r in self.rows if r["Claim"] == claim_id]
        return found
