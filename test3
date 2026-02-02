import os
import re
import html
from typing import Any, Dict, Optional, List
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query

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


# -------------------------
# URL helpers
# -------------------------

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
    title: str = Query(..., min_length=1),
) -> Dict[str, Any]:
    base = await resolve_topic(topic)

    data = await mediawiki_get(
        base,
        {
            "action": "query",
            "prop": "extracts|info",
            "exintro": "1",
            "explaintext": "1",
            "inprop": "url",
            "titles": title,
            "format": "json",
        },
    )

    pages = data.get("query", {}).get("pages", {})
    page_obj = next(iter(pages.values()), None)

    if not page_obj or "missing" in page_obj:
        raise HTTPException(status_code=404, detail="page not found")

    resolved_title = page_obj.get("title") or title

    return {
        "topic": topic,
        "wiki": base,
        "title": resolved_title,
        "pageid": page_obj.get("pageid"),
        "url": page_obj.get("fullurl") or page_url(base, str(resolved_title)),
        "extract": page_obj.get("extract"),
    }