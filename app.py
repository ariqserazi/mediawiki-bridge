import os
import re
import html
from typing import Any, Dict, Optional, List
from fastapi.responses import HTMLResponse

from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.5.1",
)

USER_AGENT = os.getenv("USER_AGENT", "mediawiki_bridge/1.5.1")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

ALLOWED_WIKI_HOST_SUFFIXES = ("fandom.com", "wiki.gg")

TAG_RE = re.compile(r"<[^>]+>")
STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for"}
ROMANS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


# -------------------------
# URL helpers
# -------------------------
def strip_html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    s = raw_html
    s = COMMENT_RE.sub(" ", s)
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = TABLE_RE.sub(" ", s)
    s = html.unescape(s)
    s = TAG_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def first_good_paragraph(parse_html: str) -> str:
    if not parse_html:
        return ""
    s = COMMENT_RE.sub(" ", parse_html)
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = TABLE_RE.sub(" ", s)

    for m in PARA_RE.finditer(s):
        candidate = strip_html_to_text(m.group(1))
        if len(candidate) >= 60:
            return candidate
    return ""

def best_paragraphs(parse_html: str, max_paras: int = 10000, min_len: int = 60, max_chars: int = 1000000) -> str:
    if not parse_html:
        return ""

    s = COMMENT_RE.sub(" ", parse_html)
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = TABLE_RE.sub(" ", s)

    paras: List[str] = []
    total = 0

    for m in PARA_RE.finditer(s):
        text = strip_html_to_text(m.group(1))
        if len(text) < min_len:
            continue

        if text.lower().startswith("this article") or text.lower().startswith("this page"):
            continue

        if total + len(text) > max_chars and paras:
            break

        paras.append(text)
        total += len(text)

        if len(paras) >= max_paras:
            break

    return "\n\n".join(paras).strip()
async def fetch_extract_with_query(base: str, title: str, intro_only: bool) -> str:
    params: Dict[str, Any] = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": title,
        "redirects": "1",
        "format": "json",
    }
    if intro_only:
        params["exintro"] = "1"

    data = await mediawiki_get(base, params)
    pages = data.get("query", {}).get("pages", {})
    page_obj = next(iter(pages.values()), None)
    if not page_obj or "missing" in page_obj:
        return ""

    extract_val = page_obj.get("extract")
    if not extract_val:
        return ""
    s = str(extract_val).strip()
    return s


async def fetch_extract_with_parse(base: str, title: str) -> str:
    data = await mediawiki_get(
        base,
        {
            "action": "parse",
            "page": title,
            "prop": "text",
            "redirects": "1",
            "format": "json",
        },
    )

    text_obj = (data.get("parse") or {}).get("text") or {}
    parse_html = text_obj.get("*") or ""
    if not parse_html:
        return ""

    return best_paragraphs(str(parse_html), max_paras=10000, max_chars=1000000)

def extract_all_visible_text(parse_html: str) -> str:
    if not parse_html:
        return ""

    s = parse_html

    # Remove scripts, styles, comments
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = COMMENT_RE.sub(" ", s)

    # Remove navigation / UI junk commonly found on Fandom
    s = re.sub(r'<nav\b[^>]*>.*?</nav>', ' ', s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'<aside\b[^>]*>.*?</aside>', ' ', s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'<footer\b[^>]*>.*?</footer>', ' ', s, flags=re.DOTALL | re.IGNORECASE)

    # Convert line-breaking tags to newlines so lists remain readable
    s = re.sub(r'</(p|li|dd|dt|h1|h2|h3|h4|h5|h6)>', '\n\n', s, flags=re.IGNORECASE)

    # Strip remaining HTML
    s = html.unescape(s)
    s = TAG_RE.sub(" ", s)

    # Normalize whitespace
    s = re.sub(r'\n\s*\n+', '\n\n', s)
    s = re.sub(r'[ \t]+', ' ', s)

    return s.strip()


def normalize_base(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="invalid wiki url")
    return f"{parsed.scheme}://{host}"


def host_is_allowed(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return any(host.endswith(sfx) for sfx in ALLOWED_WIKI_HOST_SUFFIXES)


def is_fandom(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return host.endswith("fandom.com")


def candidate_action_apis(base: str) -> List[str]:
    base = normalize_base(base)
    if is_fandom(base):
        # Fandom usually works on /api.php, sometimes /w/api.php exists too
        return [f"{base}/api.php", f"{base}/w/api.php"]
    # wiki gg usually uses /w/api.php, some installs also answer /api.php
    return [f"{base}/w/api.php", f"{base}/api.php"]


def page_url(base: str, title: str) -> str:
    return f"{base}/wiki/{quote(title.replace(' ', '_'))}"


def clean_snippet(value: Any) -> str:
    if not value:
        return ""
    s = html.unescape(str(value))
    s = TAG_RE.sub("", s)
    return s.strip()

def _is_roman_numeral(t: Any) -> bool:
    try:
        return str(t).strip().lower() in ROMANS
    except Exception:
        return False

# -------------------------
# Topic resolution
# -------------------------

def _is_roman_numeral(t: str) -> bool:
    return (t or "").lower() in ROMANS


def tokenize_topic(topic: str) -> List[str]:
    s = (topic or "").strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="topic is empty")

    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [t for t in s.split(" ") if t]
    return tokens


def candidate_slugs(topic: str) -> List[str]:
    tokens = tokenize_topic(topic)
    cleaned = [t for t in tokens if t not in STOPWORDS]

    def join_compact(ts: List[str]) -> str:
        return "".join(ts)

    def join_hyphen(ts: List[str]) -> str:
        return "-".join(ts)

    candidates: List[str] = []

    if cleaned:
        candidates.append(join_compact(cleaned))
        candidates.append(join_hyphen(cleaned))

    candidates.append(join_compact(tokens))
    candidates.append(join_hyphen(tokens))

    if len(cleaned) >= 2:
        candidates.append(join_compact(cleaned[:2]))
        candidates.append(join_hyphen(cleaned[:2]))
    if len(tokens) >= 2:
        candidates.append(join_compact(tokens[:2]))
        candidates.append(join_hyphen(tokens[:2]))

    no_roman = [t for t in cleaned if not _is_roman_numeral(t)]
    if no_roman:
        candidates.append(join_compact(no_roman))
        candidates.append(join_hyphen(no_roman))

    no_digits = [t for t in cleaned if not t.isdigit()]
    if no_digits:
        candidates.append(join_compact(no_digits))
        candidates.append(join_hyphen(no_digits))

    stripped_digit_suffix = [re.sub(r"\d+$", "", t) for t in cleaned]
    stripped_digit_suffix = [t for t in stripped_digit_suffix if t]
    if stripped_digit_suffix:
        candidates.append(join_compact(stripped_digit_suffix))
        candidates.append(join_hyphen(stripped_digit_suffix))

    for n in range(len(cleaned) - 1, 0, -1):
        candidates.append(join_compact(cleaned[:n]))
        candidates.append(join_hyphen(cleaned[:n]))

    if 2 <= len(cleaned) <= 6:
        acronym = "".join(t[0] for t in cleaned if t and t[0].isalnum())
        if acronym:
            candidates.append(acronym)

    uniq: List[str] = []
    seen = set()
    for c in candidates:
        c = (c or "").strip().lower()
        if not c:
            continue
        if len(c) < 3:
            continue
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)

    return uniq


async def _probe_api(client: httpx.AsyncClient, api_url: str, hint: str) -> bool:
    # Some wikis block siteinfo, search tends to work more often
    params = {
        "action": "query",
        "list": "search",
        "srsearch": hint,
        "srlimit": 1,
        "format": "json",
    }
    try:
        r = await client.get(api_url, params=params)
        if r.status_code != 200:
            return False
        data = r.json()
        q = data.get("query") or {}
        s = q.get("search")
        return isinstance(s, list)
    except Exception:
        return False


async def resolve_topic(topic: str) -> str:
    if topic.startswith("http://") or topic.startswith("https://"):
        base = normalize_base(topic)
        if not host_is_allowed(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
        return base
    slugs = candidate_slugs(topic)

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        for slug in slugs:
            for raw_base in (f"https://{slug}.fandom.com", f"https://{slug}.wiki.gg"):
                base = normalize_base(raw_base)

                if not host_is_allowed(base):
                    continue

                for api in candidate_action_apis(base):
                    ok = await _probe_api(client, api, hint=topic)
                    if ok:
                        return base

    raise HTTPException(status_code=404, detail="could not resolve topic to fandom.com or wiki.gg")

async def resolve_title(base: str, title: str) -> str:
    """
    Resolve MediaWiki redirects and return the canonical page title.
    Works for Episode_1 → The Storm Dragon, Veldora.
    """
    data = await mediawiki_get(
        base,
        {
            "action": "query",
            "titles": title,
            "redirects": "1",
            "format": "json",
        },
    )

    pages = (data.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), None)

    if not page or "missing" in page:
        raise HTTPException(status_code=404, detail="page not found")

    return page.get("title") or title

async def resolve_via_http_redirect(base: str, title: str) -> Optional[str]:
    """
    Fallback for Fandom frontend redirects (e.g. Episode_1 on Tensura).
    Performs a HEAD request to /wiki/{title} and captures the final URL.
    """
    url = page_url(base, title)

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        try:
            r = await client.head(url)
            final = str(r.url)
        except Exception:
            return None

    if "/wiki/" not in final:
        return None

    return final.split("/wiki/", 1)[1].replace("_", " ")


def normalize_episode_title(raw: str) -> Optional[str]:
    s = raw.strip().lower()

    m = re.fullmatch(r"(episode\s*)?0*(\d+)", s)
    if m:
        return f"Episode_{int(m.group(2))}"

    if s.startswith("episode_"):
        return s.title().replace(" ", "_")

    return None

# -------------------------
# MediaWiki fetch with fallback
# -------------------------

async def mediawiki_get(base: str, params: Dict[str, Any]) -> Dict[str, Any]:
    base = normalize_base(base)
    if not host_is_allowed(base):
        raise HTTPException(status_code=403, detail="wiki host not allowed")

    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        last_status: Optional[int] = None

        for api in candidate_action_apis(base):
            try:
                r = await client.get(api, params=params)
                last_status = r.status_code
                if r.status_code == 200:
                    return r.json()
            except Exception:
                continue

    raise HTTPException(status_code=502, detail=f"upstream mediawiki error {last_status or 0}")


# -------------------------
# Routes
# -------------------------

@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/resolve")
async def resolve(topic: str = Query(..., min_length=1)) -> Dict[str, str]:
    wiki = await resolve_topic(topic)
    return {"topic": topic, "wiki": wiki}


@app.get("/render", response_class=HTMLResponse)
async def render(
    topic: str = Query(..., min_length=1),
    title: Optional[str] = Query(None),
    pageid: Optional[int] = Query(None),
):
    base = await resolve_topic(topic)

    if not title and pageid is None:
        raise HTTPException(
            status_code=400,
            detail="Either title or pageid must be provided",
        )

    resolved_title = None

    # Resolve title if provided
    if title:
        episode_title = normalize_episode_title(title)
        lookup_title = episode_title or title

        try:
            resolved_title = await resolve_title(base, lookup_title)
        except HTTPException:
            fallback = await resolve_via_http_redirect(base, lookup_title)
            resolved_title = fallback or lookup_title

    parse_params = {
        "action": "parse",
        "prop": "text",
        "format": "json",
        "formatversion": 2,
    }

    if pageid is not None:
        parse_params["pageid"] = pageid
    else:
        parse_params["page"] = resolved_title

    data = await mediawiki_get(base, parse_params)

    parse = data.get("parse")
    if not parse:
        raise HTTPException(status_code=404, detail="page not found")

    html_content = parse.get("text")
    if not html_content:
        raise HTTPException(status_code=404, detail="no renderable content")

    # Minimal wrapper so browser displays cleanly
    return HTMLResponse(
        content=f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{parse.get("title", "MediaWiki Render")}</title>
<style>
    body {{
        font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        line-height: 1.6;
        max-width: 900px;
        margin: 2rem auto;
        padding: 0 1rem;
        background: #fff;
        color: #111;
    }}
    img {{ max-width: 100%; }}
    table {{ border-collapse: collapse; }}
    th, td {{ border: 1px solid #ccc; padding: 0.4rem; }}
</style>
</head>
<body>
{html_content}
</body>
</html>
"""
    )


@app.get("/search")
async def search(
    topic: str = Query(..., min_length=1),
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
) -> Dict[str, Any]:
    base = await resolve_topic(topic)

    data = await mediawiki_get(
        base,
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": limit,
            "format": "json",
        },
    )

    results: List[Dict[str, Any]] = []
    for item in data.get("query", {}).get("search", []):
        title_val = item.get("title")
        if not title_val:
            continue
        title_str = str(title_val).strip()
        if not title_str:
            continue

        results.append(
            {
                "title": title_str,
                "pageid": item.get("pageid"),
                "url": page_url(base, title_str),
                "snippet": clean_snippet(item.get("snippet")),
                "timestamp": item.get("timestamp"),
            }
        )

    return {
        "topic": topic,
        "wiki": base,
        "query": q,
        "limit": limit,
        "results": results,
    }


@app.get("/page")
async def page(
    topic: str = Query(..., min_length=1),
    title: Optional[str] = Query(None),
    pageid: Optional[int] = Query(None),
) -> Dict[str, Any]:
    base = await resolve_topic(topic)

    if not title and pageid is None:
        raise HTTPException(
            status_code=400,
            detail="Either title or pageid must be provided",
        )

    requested_title = title
    resolved_title = None

    # Resolve title only if provided
    if title:
        episode_title = normalize_episode_title(title)
        lookup_title = episode_title or title

        try:
            resolved_title = await resolve_title(base, lookup_title)
        except HTTPException:
            fallback = await resolve_via_http_redirect(base, lookup_title)
            resolved_title = fallback or lookup_title

    # Build parse params
    parse_params = {
        "action": "parse",
        "prop": "text",
        "format": "json",
    }

    if pageid is not None:
        parse_params["pageid"] = pageid
    else:
        parse_params["page"] = resolved_title

    data = await mediawiki_get(base, parse_params)

    parse = data.get("parse")
    if not parse:
        raise HTTPException(status_code=404, detail="page not found")

    canonical_title = parse.get("title")
    parsed_pageid = parse.get("pageid")

    parse_html = (parse.get("text") or {}).get("*") or ""
    extract_text = extract_all_visible_text(parse_html)
    if not extract_text:
        raise HTTPException(status_code=404, detail="no extractable content")

    return {
        "topic": topic,
        "wiki": base,

        # 🔒 Stable, non-confusing fields
        "requested_title": requested_title,
        "resolved_title": resolved_title,
        "canonical_title": canonical_title,

        "pageid": parsed_pageid,
        "url": page_url(base, canonical_title),

        "extract": extract_text,
        "extract_source": "parse_full",
    }
