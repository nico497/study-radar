#!/usr/bin/env python3
"""
Study Radar
-----------
Watches the companies in your niche for new original research (studies,
surveys, data analyses, benchmarks) and writes a review-ready brief for each:
the headline finding, the key stats, a method check, and what it means for you.

Usage
  python radar.py            # full run: discover -> triage -> brief -> save
  python radar.py --check    # test your sources only (no API key, no writes)

Environment
  ANTHROPIC_API_KEY   required for a full run
  RADAR_CONFIG        optional path to config (default: config.yaml)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RADAR_CONFIG", ROOT / "config.yaml"))
DATA_PATH = ROOT / "docs" / "data.json"      # what the dashboard reads
STATE_PATH = ROOT / "state" / "state.json"   # what the radar has already seen

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULTS = {
    "lookback_days": 45,
    "max_briefs_per_run": 12,
    "max_new_per_source": 20,
    "max_article_chars": 60000,
    "keep_briefs": 400,
    "user_agent": "Mozilla/5.0 (compatible; StudyRadar/1.0; research digest bot)",
    "models": {"triage": "claude-haiku-4-5-20251001", "brief": "claude-sonnet-5-5"},
    "tags": [],
}


# ---------------------------------------------------------------- helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log(msg: str) -> None:
    print(msg, flush=True)


def short_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        hint = {403: "blocked by the site (403)", 404: "URL not found (404)",
                429: "rate limited (429)"}.get(code, f"HTTP {code}")
        return hint
    if isinstance(e, requests.Timeout):
        return "timed out"
    if isinstance(e, requests.ConnectionError):
        return "could not connect"
    return str(e)[:200]


TRACKING = re.compile(r"^(utm_|mc_|hsa_|_hs|fbclid|gclid|ref$|source$)", re.I)


def normalize_url(u: str) -> str:
    p = urlparse(u.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACKING.match(k)])
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), p.path or "/", "", query, ""))


def url_key(u: str) -> str:
    """Identity for de-duplication: ignores scheme, www and trailing slash."""
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def slug_title(url: str) -> str:
    seg = [s for s in urlparse(url).path.split("/") if s]
    words = re.sub(r"[-_]+", " ", seg[-1] if seg else url)
    words = re.sub(r"\.(html?|php|aspx?)$", "", words)
    return words[:1].upper() + words[1:]


def strip_html(s: str, limit: int = 400) -> str:
    text = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


class Fetcher:
    def __init__(self, user_agent: str):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def get(self, url: str) -> requests.Response:
        r = self.s.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------- discovery
# Each method returns items: {url, title, published (datetime|None), snippet}

def discover_feed(src: dict, f: Fetcher) -> list[dict]:
    r = f.get(src["feed"])
    parsed = feedparser.parse(r.content)
    if not parsed.entries:
        raise ValueError("no entries: not a readable RSS/Atom feed")
    items = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        items.append({
            "url": normalize_url(urljoin(r.url, link)),
            "title": strip_html(e.get("title", ""), 300) or slug_title(link),
            "published": pub,
            "snippet": strip_html(e.get("summary", ""), 1500),
            # Full text when the feed carries it (Substack, WordPress): fallback if the page blocks us
            "feed_text": strip_html((e.get("content") or [{}])[0].get("value", ""), 60000),
        })
    return items


def discover_page(src: dict, f: Fetcher) -> list[dict]:
    r = f.get(src["page"])
    soup = BeautifulSoup(r.text, "html.parser")
    pattern = re.compile(src["link_pattern"])
    listing = url_key(r.url)
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = normalize_url(urljoin(r.url, a["href"]))
        if url_key(url) == listing or not pattern.search(url):
            continue
        text = a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", "")
        # Cards often link twice (image + title): keep the most descriptive text.
        if len(text) > len(found.get(url, "")):
            found[url] = text
        else:
            found.setdefault(url, "")
    return [{"url": u, "title": (t[:300] or slug_title(u)), "published": None, "snippet": ""}
            for u, t in found.items()]


def _xml_children(root: ET.Element, name: str):
    return [el for el in root.iter() if el.tag.split("}")[-1] == name]


def _xml_text(el: ET.Element, name: str) -> str | None:
    for child in el:
        if child.tag.split("}")[-1] == name:
            return (child.text or "").strip()
    return None


def discover_sitemap(src: dict, f: Fetcher) -> list[dict]:
    pattern = re.compile(src["link_pattern"]) if src.get("link_pattern") else None
    to_fetch, entries = [src["sitemap"]], []
    child_pat = re.compile(src["sitemap_pattern"]) if src.get("sitemap_pattern") else None
    fetched = 0
    while to_fetch and fetched < 12:
        root = ET.fromstring(f.get(to_fetch.pop(0)).content)
        fetched += 1
        if root.tag.split("}")[-1] == "sitemapindex":
            kids = [_xml_text(s, "loc") for s in _xml_children(root, "sitemap")]
            to_fetch += [k for k in kids if k and (not child_pat or child_pat.search(k))]
            continue
        for u in _xml_children(root, "url"):
            loc = _xml_text(u, "loc")
            if not loc or (pattern and not pattern.search(loc)):
                continue
            entries.append({
                "url": normalize_url(loc),
                "title": slug_title(loc),
                "published": parse_iso(_xml_text(u, "lastmod")),
                "snippet": "",
            })
    entries.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    return entries[:300]


def discover(src: dict, f: Fetcher) -> tuple[str, list[dict]]:
    if src.get("feed"):
        method, items = "feed", discover_feed(src, f)
    elif src.get("page"):
        if not src.get("link_pattern"):
            raise ValueError("'page' sources need a link_pattern")
        method, items = "page", discover_page(src, f)
    elif src.get("sitemap"):
        method, items = "sitemap", discover_sitemap(src, f)
    else:
        raise ValueError("source needs one of: feed, page, sitemap")
    if src.get("link_pattern") and method == "feed":
        pat = re.compile(src["link_pattern"])
        items = [i for i in items if pat.search(i["url"])]
    if src.get("exclude_pattern"):
        ex = re.compile(src["exclude_pattern"])
        items = [i for i in items if not ex.search(i["url"])]
    return method, items


# ---------------------------------------------------------------- reading

GATE_CUE = re.compile(
    r"\b(download|get|access|read|request)\s+(the|our|your|a)?\s*(free\s+)?(full\s+)?"
    r"(report|study|whitepaper|white paper|e-?book|guide|research)\b", re.I)


def read_article(url: str, f: Fetcher, max_chars: int) -> dict:
    r = f.get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    published = parse_iso(meta("article:published_time", "datePublished", "date"))
    has_email_form = bool(soup.select("input[type=email], input[name*=email i]"))
    pdf_links = [urljoin(r.url, a["href"]) for a in soup.find_all("a", href=True)
                 if a["href"].lower().split("?")[0].endswith(".pdf")][:3]

    for t in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer",
                   "header", "aside", "form", "button"]):
        t.decompose()
    candidates = soup.find_all("article") + soup.find_all("main")
    node = max(candidates, key=lambda n: len(n.get_text()), default=None) or soup.body or soup
    text = node.get_text("\n", strip=True)
    if len(text.split()) < 150 and soup.body:
        text = soup.body.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    words = len(text.split())
    gated = words < 900 and (has_email_form or bool(GATE_CUE.search(text)))
    return {"title": title, "published": published, "text": text[:max_chars],
            "words": words, "gated": gated, "pdf_links": pdf_links}


# ---------------------------------------------------------------- Claude

_NO_FORCED_TOOL: set[str] = set()   # models that reject tool_choice "tool"


def _api_error(r: requests.Response) -> str:
    try:
        return f"Anthropic API {r.status_code}: {r.json()['error']['message']}"
    except Exception:  # noqa: BLE001
        return f"Anthropic API {r.status_code}: {r.text[:200]}"


def call_claude(model: str, system: str, user: str, tool: dict, max_tokens: int = 1500) -> dict:
    """One structured call: the model answers by calling `tool`. Returns the tool input."""
    headers = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    messages = [{"role": "user", "content": user}]
    nudged = False
    attempt = 0
    while attempt < 6:
        attempt += 1
        forced = model not in _NO_FORCED_TOOL
        body = {
            "model": model, "max_tokens": max_tokens, "messages": messages, "tools": [tool],
            "system": system if forced else f"{system}\n\nRespond only by calling the {tool['name']} tool.",
            "tool_choice": {"type": "tool", "name": tool["name"]} if forced else {"type": "auto"},
        }
        r = requests.post(API_URL, headers=headers, json=body, timeout=180)
        if r.status_code in (429, 500, 502, 503, 529):
            wait = int(r.headers.get("retry-after", 0) or 0) or 10 * attempt
            log(f"    API busy ({r.status_code}), retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 400 and forced and "tool_choice" in r.text:
            # Some models don't support forcing a tool. Ask for it in the prompt instead.
            _NO_FORCED_TOOL.add(model)
            continue
        if r.status_code >= 400:
            raise RuntimeError(_api_error(r))
        content = r.json().get("content", [])
        for block in content:
            if block.get("type") == "tool_use":
                return block["input"]
        if not nudged:
            nudged = True
            messages = messages + [{"role": "assistant", "content": content or "..."},
                                   {"role": "user", "content": f"Please answer by calling the {tool['name']} tool."}]
            continue
        raise RuntimeError("model returned no structured output")
    raise RuntimeError("Anthropic API kept failing, try again later")


TRIAGE_TOOL = {
    "name": "select_studies",
    "description": "Return the indices of items that are original research.",
    "input_schema": {
        "type": "object",
        "properties": {"studies": {"type": "array", "items": {"type": "integer"}}},
        "required": ["studies"],
    },
}

TRIAGE_SYSTEM = """You screen content for a research digest. From a list of article titles and snippets, pick the ones that are ORIGINAL RESEARCH: the publisher collected or analysed data itself and reports the findings.

Include: surveys, analyses of a dataset (e.g. "we analysed 1M keywords / 10k sites / 13B searches"), controlled experiments or tests, benchmarks, indexes, annual "state of" reports with new numbers, studies with a specific measured finding in the title (e.g. "X cuts CTR by 23%").

Exclude: how-to guides, tutorials, tool walkthroughs, product or feature announcements, funding/awards/company news, opinion or strategy essays without new data, roundups of other people's statistics, webinars, job posts.

When a title states a specific measured result, include it. Otherwise, if unsure, exclude."""


OFFICIAL_TRIAGE_SYSTEM = """You screen posts from an official platform source (e.g. Google Search) for a digest read by practitioners. Pick the ones that announce or document a REAL CHANGE practitioners should know about.

Include: ranking/core/spam updates, new or changed features and reports (e.g. in Search Console), documentation or guideline changes, policy changes, deprecations, new structured data or crawling behaviour, official data or explanations of how systems work.

Exclude: events and conferences, community spotlights, recaps of talks, hiring, consumer tips and seasonal features, general marketing.

If an item says [only keep: ...], keep it only if it fits that focus.

If unsure, include it."""


def triage(cands: list[dict], cfg: dict, official: bool = False) -> set[int]:
    lines = []
    for i, c in enumerate(cands):
        snip = f" | {c['snippet'][:280]}" if c.get("snippet") else ""
        focus = f" [only keep: {c['focus']}]" if c.get("focus") else ""
        lines.append(f"[{i}] ({c['source']}){focus} {c['title']}{snip}\n    {c['url']}")
    user = (f"Niche: {cfg['niche']}\n\nItems:\n" + "\n".join(lines) +
            "\n\nReturn the indices of the original-research items.")
    system = OFFICIAL_TRIAGE_SYSTEM if official else TRIAGE_SYSTEM
    out = call_claude(cfg["models"]["triage"], system, user, TRIAGE_TOOL, 600)
    return {i for i in out.get("studies", []) if isinstance(i, int) and 0 <= i < len(cands)}


def brief_tool(tags: list[str]) -> dict:
    tag_schema = {"type": "string"}
    if tags:
        tag_schema["enum"] = tags
    s = {"type": "string"}
    return {
        "name": "write_brief",
        "description": "Write the review brief for one study.",
        "input_schema": {
            "type": "object",
            "properties": {
                "is_study": {"type": "boolean", "description": "False if, on reading, this is not original research."},
                "headline": {**s, "description": "The finding. One plain sentence, max 18 words, with the key number. Start with who or what, not 'Study finds'."},
                "study_type": {"type": "string", "enum": ["survey", "data analysis", "experiment", "benchmark", "industry report", "case study", "official update", "other"]},
                "key_stats": {"type": "array", "items": s, "maxItems": 3,
                              "description": "Up to 3 numbers worth knowing. Each max 12 words, plain English, exact figures."},
                "sample": {**s, "description": "How big the study was. Max 8 words, e.g. '1,042 marketers surveyed' or '300k keywords'. 'Not disclosed' if absent."},
                "data_source": {**s, "description": "Where the data came from. Max 8 words, e.g. 'their own tool', 'online survey'. 'Not disclosed' if absent."},
                "timeframe": {**s, "description": "When the data was collected. Max 6 words. 'Not disclosed' if absent."},
                "conflict": {**s, "description": "Does the publisher sell something this finding helps? One plain sentence, max 15 words."},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "caveat": {**s, "description": "The main reason to be careful. One plain sentence, max 15 words."},
                "why_it_matters": {**s, "description": "Why it matters to the reader. One plain sentence, max 18 words. Say what to do or watch. If nothing changes, say so."},
                "importance": {"type": "integer", "enum": [1, 2, 3],
                               "description": "3 = must know: changes how the audience should work or overturns a common belief, with credible evidence. 2 = useful data point. 1 = minor, incremental or weak."},
                "tags": {"type": "array", "items": tag_schema, "maxItems": 3},
            },
            "required": ["is_study", "headline", "study_type", "key_stats", "sample", "data_source", "timeframe",
                         "conflict", "confidence", "caveat", "why_it_matters", "importance", "tags"],
        },
    }


STYLE_RULES = """Writing style: Smart Brevity, plain English.
- Easy English. Short sentences. Everyday words. Write so a smart 15-year-old gets it on first read.
- No jargon. If a technical term can't be avoided, explain it in a few words in brackets,
  e.g. "fan-out queries (the extra searches AI tools run behind the scenes)".
- Lead with the point. No warm-up, no "this study shows", no hedging words, no hype.
- Keep numbers exactly as published, but at most two numbers per sentence.
- Respect every word limit."""

BRIEF_SYSTEM = """You turn industry studies into short briefs for a busy reader who may not be an expert. They should get each one in 10 seconds: what was found, why it matters, and whether to trust it.

""" + STYLE_RULES + """

Accuracy rules:
- Use only what the text says. Never invent or round numbers.
- If a method detail is missing, write "Not disclosed". A hidden method is worth knowing.
- Confidence: high = big sample, method explained, limits admitted. medium = decent sample but vendor data or thin method. low = small, self-picked or hidden sample, or mainly selling the publisher's product.
- Importance: be stingy. Most studies are a 2. Give 3 only if the reader should change what they do. Low-confidence studies are rarely a 3.
- Gated landing page: brief only what's visible, and say so in the caveat."""


def write_brief(item: dict, art: dict, cfg: dict) -> dict:
    gated_note = ("\nNOTE: This looks like a gated landing page. The full report is behind a form, "
                  "so only the visible summary is available.") if art["gated"] else ""
    if item.get("official"):
        gated_note += ("\nNOTE: This is an OFFICIAL announcement from the platform itself, not a study. "
                       "Set is_study true unless it is an event, community or consumer post. Use study_type "
                       "'official update'. The headline says what changed, in plain words. For method: sample and "
                       "data_source 'n/a (official announcement)', timeframe = when it applies or rolls out, "
                       "conflict = what the platform doesn't say (max 15 words). "
                       "Confidence = how concrete it is: high = specific, dated change; medium = general guidance; "
                       "low = vague hints. Importance 3 for ranking updates or changes practitioners must act on.")
    user = f"""Audience: {cfg['audience']}
Niche: {cfg['niche']}

Publisher: {item['source']}
Title: {art.get('title') or item['title']}
URL: {item['url']}
Published: {iso(item.get('published')) or 'unknown'}{gated_note}

--- ARTICLE TEXT ---
{art['text']}
--- END ---"""
    raw = call_claude(cfg["models"]["brief"], BRIEF_SYSTEM, user, brief_tool(cfg.get("tags") or []), 1800)
    return normalize_brief(raw)


METHOD_KEYS = ("sample", "data_source", "timeframe", "conflict", "confidence", "caveat")


def _clean(v) -> str:
    """Strip stray tool-call markup a model sometimes leaves inside a string."""
    v = re.sub(r"</?parameter[^>]*>", " ", str(v or ""))
    return re.sub(r"\s+", " ", v).strip()


def normalize_brief(b: dict) -> dict:
    """Coerce model output into the shape the dashboard expects, whatever the model sent."""
    method = b.get("method") if isinstance(b.get("method"), dict) else {}
    if isinstance(b.get("method"), str):          # garbled nested output: '<parameter name="sample">...'
        m = re.search(r'name="sample">(.*?)(?:</parameter>|$)', b["method"], re.S)
        if m:
            method.setdefault("sample", m.group(1))
    for k in METHOD_KEYS:
        if k in b and not isinstance(b[k], (dict, list)):
            method[k] = b[k]
    method = {k: _clean(method.get(k)) or ("Not disclosed" if k in ("sample", "data_source", "timeframe") else "")
              for k in METHOD_KEYS}
    if method["confidence"] not in ("high", "medium", "low"):
        method["confidence"] = "medium"
    stats = b.get("key_stats") or []
    if isinstance(stats, str):
        stats = [x for x in re.split(r"\n+|(?<=[.;])\s+(?=[A-Z0-9])", stats) if x.strip()]
    tags = b.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        importance = int(b.get("importance", 2))
    except (TypeError, ValueError):
        importance = 2
    return {
        "is_study": b.get("is_study", True) not in (False, "false", "False"),
        "headline": _clean(b.get("headline")),
        "study_type": _clean(b.get("study_type")) or "other",
        "key_stats": [_clean(x) for x in stats][:3],
        "method": method,
        "why_it_matters": _clean(b.get("why_it_matters")),
        "importance": min(3, max(1, importance)),
        "tags": [_clean(t) for t in tags][:3],
    }


PULSE_TOOL = {
    "name": "write_pulse",
    "description": "Summarise what mattered in this week's studies.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Two sentences max: the overall picture this week. Say plainly if little of note was published."},
            "points": {
                "type": "array", "maxItems": 3, "items": {"type": "string"},
                "description": "Up to 3 takeaways, one plain sentence each, max 22 words. End each with the numbers of the studies it draws on in square brackets, e.g. 'Zero-click keeps rising. [0, 3]'",
            },
        },
        "required": ["summary", "points"],
    },
}

PULSE_SYSTEM = """You write the top-of-page summary for a weekly research digest. The reader has 60 seconds and may not be an expert.

""" + STYLE_RULES + """

Rules:
- Use only the briefs provided. Don't add outside facts or numbers.
- Summary: max 25 words, two short sentences: the big picture this week.
- Up to 3 points, ranked by what matters most. Fewer is fine. Skip minor studies.
- Each point: ONE sentence, max 22 words. The takeaway first, then what to do.
- Weigh confidence. Don't state a weak finding as fact; say "early signal" or "one vendor's data" instead."""


def write_pulse(new_briefs: list[dict], cfg: dict) -> dict:
    lines = []
    for i, r in enumerate(new_briefs):
        b = r["brief"]
        lines.append(f"[{i}] {r['source']} | importance {b.get('importance')} | confidence {b['method'].get('confidence')}\n"
                     f"    {b['headline']}\n    Why: {b.get('why_it_matters')}\n    Caveat: {b['method'].get('caveat')}")
    user = f"Audience: {cfg['audience']}\nNiche: {cfg['niche']}\n\nThis week's studies:\n" + "\n".join(lines)
    out = call_claude(cfg["models"]["brief"], PULSE_SYSTEM, user, PULSE_TOOL, 900)
    raw_points = out.get("points") or []
    if isinstance(raw_points, str):
        try:
            raw_points = json.loads(raw_points)
        except json.JSONDecodeError:
            raw_points = [x for x in raw_points.split("\n") if x.strip()]
    points = []
    for pt in raw_points[:3]:
        if isinstance(pt, dict):
            text, nums = str(pt.get("text", "")), [n for n in pt.get("ids", []) if isinstance(n, int)]
        else:
            text = str(pt)
            m = re.search(r"\[([\d,\s]+)\]\s*\.?\s*$", text)
            nums = [int(n) for n in re.findall(r"\d+", m.group(1))] if m else []
            text = text[: m.start()].rstrip() if m else text
        text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", _clean(text))
        ids = [new_briefs[i]["id"] for i in nums if 0 <= i < len(new_briefs)]
        if text:
            points.append({"text": text, "ids": ids})
    return {"summary": _clean(out.get("summary")), "points": points}


# ---------------------------------------------------------------- storage

def load_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    merged = {**DEFAULTS, **cfg}
    merged["models"] = {**DEFAULTS["models"], **(cfg.get("models") or {})}
    for k in ("niche", "audience", "sources"):
        if not merged.get(k):
            sys.exit(f"config.yaml is missing '{k}'")
    names = [s.get("name") for s in merged["sources"]]
    if len(set(names)) != len(names) or None in names:
        sys.exit("Every source needs a unique 'name'")
    return merged


# ---------------------------------------------------------------- run

RESTYLE_SYSTEM = """You rewrite an existing study brief so anyone can read it in 10 seconds.

""" + STYLE_RULES + """

Rules:
- Keep every fact and number. Don't add facts. Don't round numbers.
- Keep study_type, confidence, importance and tags the same.
- Respect the word limit on every field."""


def restyle(cfg: dict) -> None:
    """Rewrite existing briefs in the current style, from the brief itself (no re-fetching)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first.")
    data = load_json(DATA_PATH, {"briefs": []})
    now = utcnow()
    done = failed = 0
    for r in data.get("briefs", []):
        if not r.get("brief"):
            continue
        log(f"  restyle: {r['source']}: {r['title'][:60]}")
        user = (f"Audience: {cfg['audience']}\nNiche: {cfg['niche']}\nPublisher: {r['source']}\n"
                f"Title: {r['title']}\n\nCurrent brief (JSON):\n{json.dumps(r['brief'], ensure_ascii=False)}")
        try:
            new = normalize_brief(call_claude(cfg["models"]["brief"], RESTYLE_SYSTEM, user,
                                              brief_tool(cfg.get("tags") or []), 1500))
        except Exception as e:  # noqa: BLE001
            log(f"    failed, kept the old version: {e}")
            failed += 1
            continue
        old = r["brief"]
        for k in ("headline", "why_it_matters", "key_stats", "tags"):   # never lose content
            if not new.get(k):
                new[k] = old.get(k, new.get(k))
        for k, v in (old.get("method") or {}).items():
            if not new["method"].get(k):
                new["method"][k] = v
        new.update(is_study=True, study_type=old.get("study_type", new["study_type"]),
                   importance=old.get("importance", new["importance"]))
        new["method"]["confidence"] = old.get("method", {}).get("confidence", new["method"]["confidence"])
        r["brief"] = new
        done += 1
    week_start = now - timedelta(days=6, hours=12)
    week = [r for r in data["briefs"] if r.get("brief") and (parse_iso(r.get("found")) or now) >= week_start]
    if week:
        try:
            pulse = {"date": iso(now), **write_pulse(week, cfg)}
            data["pulses"] = [pulse] + data.get("pulses", [])[1:]
        except Exception as e:  # noqa: BLE001
            log(f"  summary failed, kept the old one: {e}")
    save_json(DATA_PATH, data)
    log(f"\nRestyled {done} briefs" + (f", {failed} failed (kept as they were)" if failed else ""))


def check_sources(cfg: dict) -> None:
    f = Fetcher(cfg["user_agent"])
    for src in cfg["sources"]:
        try:
            method, items = discover(src, f)
            log(f"OK   {src['name']:<22} {method:<8} {len(items):>3} items")
            for it in items[:3]:
                d = it["published"].date().isoformat() if it["published"] else "no date   "
                log(f"       {d}  {it['title'][:80]}")
        except Exception as e:  # noqa: BLE001
            log(f"FAIL {src['name']:<22} {short_error(e)}")


def run(cfg: dict) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY (GitHub: Settings > Secrets and variables > Actions).")

    f = Fetcher(cfg["user_agent"])
    now = utcnow()
    cutoff = now - timedelta(days=int(cfg["lookback_days"]))
    state = load_json(STATE_PATH, {"seen": {}, "sources": {}})
    data = load_json(DATA_PATH, {"briefs": []})
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and state.get("repo") and state["repo"] != repo:
        # This is a fresh copy made from someone else's template: start clean.
        log(f"New copy of {state['repo']}: clearing the original's briefs and history")
        state, data = {"seen": {}, "sources": {}}, {"briefs": []}
    if repo:
        state["repo"] = repo
    seen, src_state = state["seen"], state["sources"]
    health, cands = [], []
    errors: list[str] = []   # surfaced in the dashboard so failures are never silent
    batch_keys: set[str] = set()   # same study syndicated by two sources: brief once

    def mark(url, source, status):
        seen[url_key(url)] = {"source": source, "status": status, "at": iso(now)}

    # 1. Discover
    log("== Discover")
    for src in cfg["sources"]:
        name = src["name"]
        h = {"name": name, "checked": iso(now), "ok": True, "found": 0, "new": 0, "error": None,
             "url": src.get("feed") or src.get("page") or src.get("sitemap")}
        try:
            method, items = discover(src, f)
        except Exception as e:  # noqa: BLE001
            h.update(ok=False, error=short_error(e), method=None)
            health.append(h)
            log(f"  FAIL {name}: {h['error']}")
            continue
        h.update(method=method, found=len(items))
        first_run = not src_state.get(name, {}).get("initialized")
        undated_seen = 0
        new = []
        for it in items:
            if url_key(it["url"]) in seen or url_key(it["url"]) in batch_keys:
                continue
            batch_keys.add(url_key(it["url"]))
            if it["published"] and it["published"] < cutoff:
                continue
            if it["published"] is None and first_run:
                # Undated listing on its first check: treat the top few as new,
                # the rest as history so we don't brief a company's whole archive.
                undated_seen += 1
                if undated_seen > int(src.get("first_run_keep", 3)):
                    mark(it["url"], name, "baseline")
                    continue
            new.append({**it, "source": name, "all_studies": bool(src.get("all_studies")),
                        "official": src.get("kind") == "official", "focus": src.get("focus", "")})
        new = new[: int(cfg["max_new_per_source"])]
        h["new"] = len(new)
        cands += new
        src_state[name] = {"initialized": True, "last_ok": iso(now)}
        health.append(h)
        log(f"  OK   {name}: {len(items)} found, {len(new)} new")

    # 2. Triage (cheap model) — which new items are actual studies?
    log(f"== Triage {len(cands)} new items")
    studies = [c for c in cands if c["all_studies"]]
    to_triage = [c for c in cands if not c["all_studies"]]
    batches = []
    for official in (False, True):
        group = [c for c in to_triage if c["official"] == official]
        batches += [(group[i:i + 30], official) for i in range(0, len(group), 30)]
    for batch, official in batches:
        try:
            picked = triage(batch, cfg, official)
        except Exception as e:  # noqa: BLE001
            log(f"  triage failed, will retry next run: {e}")
            errors.append(f"triage: {str(e)[:300]}")
            continue
        for i, c in enumerate(batch):
            if i in picked:
                studies.append(c)
            else:
                mark(c["url"], c["source"], "not_study")
    log(f"  {len(studies)} look like studies")

    # 3. Brief (better model), newest first, capped per run
    studies.sort(key=lambda c: c["published"] or now, reverse=True)
    queue = studies[: int(cfg["max_briefs_per_run"])]
    deferred = len(studies) - len(queue)
    log(f"== Brief {len(queue)} studies" + (f" ({deferred} deferred to next run)" if deferred else ""))
    briefs, counts = data.get("briefs", []), {"briefed": 0, "unreadable": 0, "rejected": 0}
    for c in queue:
        log(f"  {c['source']}: {c['title'][:70]}")
        record = {"id": url_key(c["url"]), "url": c["url"], "source": c["source"], "title": c["title"],
                  "published": iso(c["published"]), "found": iso(now),
                  "gated": False, "unreadable": None, "pdf_links": [], "brief": None}
        try:
            art = read_article(c["url"], f, int(cfg["max_article_chars"]))
        except Exception as e:  # noqa: BLE001
            art = None
            err = e
        if c.get("feed_text") and len(c["feed_text"].split()) > 150 and (art is None or art["words"] < 150):
            text = c["feed_text"][: int(cfg["max_article_chars"])]
            art = {"title": (art or {}).get("title"), "published": None, "text": text,
                   "words": len(text.split()), "gated": False, "pdf_links": (art or {}).get("pdf_links", [])}
        if art is None:
            e = err
            record["unreadable"] = short_error(e)
            briefs.insert(0, record)
            mark(c["url"], c["source"], "unreadable")
            counts["unreadable"] += 1
            log(f"    unreadable: {record['unreadable']}")
            continue
        if art["words"] < 120 and c.get("snippet"):
            art["text"] = f"Feed summary: {c['snippet']}\n\nPage text:\n{art['text']}"
            art["words"] += len(c["snippet"].split())
        if art["words"] < (8 if c.get("official") else 25 if art["gated"] else 120):
            record["unreadable"] = ("page has almost no readable text (paywalled, blocked, or built with JavaScript)")
            record["title"] = art["title"] or c["title"]
            briefs.insert(0, record)
            mark(c["url"], c["source"], "unreadable")
            counts["unreadable"] += 1
            log("    unreadable: no text")
            continue
        try:
            b = write_brief(c, art, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"    brief failed, will retry next run: {e}")
            errors.append(f"brief: {str(e)[:300]}")
            continue
        if not b.get("is_study", True) and not c.get("official"):
            mark(c["url"], c["source"], "not_study")
            counts["rejected"] += 1
            log("    not a study on closer reading, skipped")
            continue
        record.update(title=art["title"] or c["title"],
                      published=iso(c["published"] or art["published"]),
                      gated=art["gated"], pdf_links=art["pdf_links"], words=art["words"], brief=b)
        briefs.insert(0, record)
        mark(c["url"], c["source"], "briefed")
        counts["briefed"] += 1

    # 4. Weekly pulse: the 60-second summary at the top of the dashboard
    # "This week" = briefs found in roughly the last 7 days, so manual re-runs add to the week.
    week_start = now - timedelta(days=6, hours=12)
    this_run = [r for r in briefs if r.get("brief") and (parse_iso(r.get("found")) or now) >= week_start]
    pulses = data.get("pulses", [])
    if this_run:
        try:
            pulse = write_pulse(this_run, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"  weekly summary failed: {e}")
            errors.append(f"summary: {str(e)[:300]}")
            pulse = {"summary": f"{len(this_run)} new studies this week. The summary couldn't be written this run, so see the cards below.", "points": []}
    else:
        failed = sum(1 for h in health if not h["ok"])
        if studies and errors:
            pulse = {"summary": f"Found {len(studies)} new studies but couldn't brief them this run "
                                f"({errors[0] if errors else 'unknown error'}). They'll be retried next run.",
                     "points": []}
        elif failed:
            pulse = {"summary": f"No new studies found, but {failed} of {len(health)} sources couldn't be checked, "
                                "so something may have been missed. See Sources at the bottom.", "points": []}
        else:
            pulse = {"summary": "No new studies from your sources this week. Nothing you missed.", "points": []}
    pulses.insert(0, {"date": iso(now), **pulse})

    # 5. Save
    briefs.sort(key=lambda r: (r.get("found") or "", r.get("published") or ""), reverse=True)
    data = {
        "meta": {
            "niche": cfg["niche"], "audience": cfg["audience"], "updated": iso(now),
            "title": cfg.get("title", "Study Radar"),
            "repo": repo,
            "run": {"sources": len(cfg["sources"]), "new_items": len(cands),
                    "studies": len(studies), "deferred": deferred, **counts,
                    "errors": list(dict.fromkeys(errors))[:5]},
        },
        "health": health,
        "pulses": pulses[:26],
        "briefs": briefs[: int(cfg["keep_briefs"])],
    }
    per_src = {}
    for r in this_run:
        per_src[r["source"]] = per_src.get(r["source"], 0) + 1
    for h in health:
        h["posts_new"], h["new"] = h.get("new", 0), per_src.get(h["name"], 0)
    save_json(DATA_PATH, data)
    save_json(STATE_PATH, state)
    summary(data)
    return data


def summary(data: dict) -> None:
    run = data["meta"]["run"]
    failed = [h for h in data["health"] if not h["ok"]]
    lines = [
        "## Study Radar run",
        f"- New items checked: {run['new_items']}",
        f"- Briefs written: {run['briefed']}",
        f"- Unreadable pages: {run['unreadable']}",
        f"- Deferred to next run: {run['deferred']}",
    ]
    if run.get("errors"):
        lines.append("\n**Errors**")
        lines += [f"- {e}" for e in run["errors"]]
    if failed:
        lines.append("\n**Sources that failed**")
        lines += [f"- {h['name']}: {h['error']}" for h in failed]
    text = "\n".join(lines)
    log("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Study Radar")
    ap.add_argument("--check", action="store_true", help="test sources only, no API calls or writes")
    ap.add_argument("--restyle", action="store_true", help="rewrite existing briefs in the current writing style")
    args = ap.parse_args()
    cfg = load_config()
    if args.check:
        check_sources(cfg)
    elif args.restyle:
        restyle(cfg)
    else:
        run(cfg)


if __name__ == "__main__":
    main()
