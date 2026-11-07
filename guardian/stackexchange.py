"""Stack Overflow questions through the Stack Exchange API instead of the browser.

stackoverflow.com rate-limits browsers per IP: after about three question pages in a
minute it answers 429 with a Cloudflare page ("Just a moment..."), with 20 or 45
seconds between page loads as well, and stays blocked for minutes while requests
keep coming. All users of this container share one IP. The API
(api.stackexchange.com) serves the same question and answers without that limit,
within a daily quota per IP (300 requests without an app key, 10,000 with one,
STACKEXCHANGE_KEY). One question costs two requests (question, answers), a link to
an answer three.

fetch() returns a page in browser_read's form ({url, title, text}) or None when the
URL is not a question or answer link or the API gives no answer; browser_read then
falls back to the browser.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

import anyio
import httpx

logger = logging.getLogger(__name__)

API = "https://api.stackexchange.com/2.3"
API_HOST = "api.stackexchange.com"
KEY = os.environ.get("STACKEXCHANGE_KEY", "").strip()
TIMEOUT = 30.0
# The API asks clients to wait this long after a response with "backoff"; longer
# waits fall back to the browser instead of holding the user.
_MAX_BACKOFF_WAIT = 10.0
_CACHE_SECONDS = 600.0
_CACHE_SIZE = 64

_QUESTION = re.compile(r"^https?://(?:www\.)?stackoverflow\.com/(?:questions|q)/(\d+)(?:[/?#]|$)", re.I)
_ANSWER = re.compile(r"^https?://(?:www\.)?stackoverflow\.com/a/(\d+)(?:[/?#]|$)", re.I)

_not_before = 0.0
_cache: OrderedDict[int, tuple[float, dict]] = OrderedDict()


def match(url: Any) -> bool:
    return isinstance(url, str) and bool(_QUESTION.match(url.strip()) or _ANSWER.match(url.strip()))


class _Text(HTMLParser):
    """Rendered text of an answer body: code blocks fenced, links to other sites
    as <url> (as browser_read does), images by their alt text only."""

    _BLOCK = {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "tr", "dl"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.pre = 0
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in self._BLOCK:
            self.out.append("\n\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n---\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "a":
            self.links.append(a.get("href") or "")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt']}]")

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in self._BLOCK:
            self.out.append("\n\n")
        elif tag == "a" and self.links:
            href = self.links.pop()
            if re.match(r"^https?://", href) and not re.match(r"^https?://(?:www\.)?stackoverflow\.com/", href):
                self.out.append(f" <{href}>")

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html or "")
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _date(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "?"


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> list[dict] | None:
    global _not_before
    wait = _not_before - time.monotonic()
    if wait > _MAX_BACKOFF_WAIT:
        logger.warning("Stack Exchange API backoff %.0f s, using the browser", wait)
        return None
    if wait > 0:
        await anyio.sleep(wait)
    params = {"site": "stackoverflow", **params, **({"key": KEY} if KEY else {})}
    try:
        response = await client.get(f"{API}{path}", params=params)
        data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Stack Exchange API %s: %s", path, e)
        return None
    if data.get("backoff"):
        _not_before = time.monotonic() + float(data["backoff"])
    if "error_id" in data:
        logger.warning("Stack Exchange API %s: %s %s", path, data.get("error_name"), data.get("error_message"))
        if data.get("error_name") == "throttle_violation":
            _not_before = time.monotonic() + 600.0
        return None
    if data.get("quota_remaining", 1) <= 10:
        logger.warning("Stack Exchange API quota: %s of %s left", data.get("quota_remaining"), data.get("quota_max"))
    return data.get("items")


async def fetch(url: str) -> dict | None:
    url = url.strip()
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": "playwright-mcp-guarded"}) as client:
        m = _QUESTION.match(url)
        if m:
            qid = int(m.group(1))
        else:
            m = _ANSWER.match(url)
            if not m:
                return None
            items = await _get(client, f"/answers/{m.group(1)}")
            if not items:
                return None
            qid = int(items[0]["question_id"])
        hit = _cache.get(qid)
        if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]
        questions = await _get(client, f"/questions/{qid}", filter="withbody")
        if not questions:
            return None
        answers = await _get(client, f"/questions/{qid}/answers", filter="withbody",
                             sort="votes", order="desc", pagesize=100)
        if answers is None:
            return None
    q = questions[0]
    title = html_to_text(q.get("title", ""))
    parts = [
        title,
        f"Question · score {q.get('score', 0)} · {q.get('answer_count', 0)} answers · asked {_date(q.get('creation_date'))}"
        + (f" · tags: {', '.join(q['tags'])}" if q.get("tags") else ""),
        html_to_text(q.get("body", "")),
    ]
    for i, a in enumerate(answers, 1):
        parts.append(
            f"Answer {i} · score {a.get('score', 0)}" + (" · accepted" if a.get("is_accepted") else "")
            + f" · {_date(a.get('creation_date'))}"
        )
        parts.append(html_to_text(a.get("body", "")))
    page = {
        "url": q.get("link") or f"https://stackoverflow.com/questions/{qid}",
        "title": f"{title} - Stack Overflow",
        "text": "\n\n".join(parts),
    }
    _cache[qid] = (time.monotonic(), page)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return page
