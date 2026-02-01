import os
import re
import html
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.3.1",
)

USER_AGENT = os.getenv("USER_AGENT", "mediawiki_bridge/1.3.1")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

ALLOWED_WIKI_HOST_SUFFIXES = (
    "fandom.com",
    "wiki.gg",
    "wikipedia.org",
)

TAG_RE = re.compile(r"<[^>]+>")


def normalize_base(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid wiki url")
    return f"{parsed.scheme}://{parsed.hostname}"


def allowed_host(base: str) -> bool:
    host = urlparse(base).hostname or ""
    return any(host.endswith(sfx) for sfx in ALLOWED_WIKI_HOST_SUFFIXES)


def primary_action_api(base: str) -> str:
    host = urlparse(base).hostname or ""
    if host.endswith("fandom.com"):
        return f"{base}/api.php"
    return f"{base}/w/api.php"


def fallback_action_api(base: str) -> str:
    host = urlparse(base).hostname or ""
    if host.endswith("fandom.com"):
        return f"{base}/w/api.php"
    return f"{base}/api.php"


def clean_snippet(value: Any) -> str:
    if not value:
        return ""
    s = str(value)
    s = html.unescape(s)
    s = TAG_RE.sub("", s)
    return s.strip()


def is_reasonable_title(title: Any) -> bool:
    if not title:
        return False
    t = str(title).strip()
    if not t:
        return False
    if t == "{":
        return False
    if len(t) > 200:
        return False
    return True


def page_url(base: str, title: str) -> str:
    safe = quote(title.replace(" ", "_"))
    return f"{base}/wiki/{safe}"


async def mediawiki_get(api_url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        r = await client.get(api_url, params=params)

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"upstream mediawiki error {r.status_code}")

    return r.json()


async def try_wiki_chain(
    bases: List[str],
    params: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    last_error: Optional[Exception] = None

    for base in bases:
        try:
            data = await mediawiki_get(primary_action_api(base), params)
            return base, data
        except Exception as e:
            last_error = e
            try:
                data = await mediawiki_get(fallback_action_api(base), params)
                return base, data
            except Exception as e2:
                last_error = e2
                continue

    raise HTTPException(status_code=502, detail="all wiki sources failed")


def default_wiki_chain(query: str) -> List[str]:
    q = query.lower().replace(" ", "")
    return [
        f"https://{q}.fandom.com",
        f"https://{q}.wiki.gg",
        "https://en.wikipedia.org",
    ]


@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    wiki: Optional[str] = Query(None),
) -> Dict[str, Any]:
    if wiki:
        base = normalize_base(wiki)
        if not allowed_host(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
        bases = [base]
    else:
        bases = default_wiki_chain(q)

    used_base, data = await try_wiki_chain(
        bases,
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": limit,
            "format": "json",
        },
    )

    items = data.get("query", {}).get("search", [])
    results: List[Dict[str, Any]] = []

    for item in items:
        title = item.get("title")
        if not is_reasonable_title(title):
            continue

        title_str = str(title).strip()
        results.append(
            {
                "title": title_str,
                "pageid": item.get("pageid"),
                "url": page_url(used_base, title_str),
                "snippet": clean_snippet(item.get("snippet")),
                "timestamp": item.get("timestamp"),
            }
        )

    return {
        "wiki": used_base,
        "query": q,
        "limit": limit,
        "results": results,
    }


@app.get("/page")
async def page(
    title: str = Query(..., min_length=1),
    wiki: Optional[str] = Query(None),
) -> Dict[str, Any]:
    if wiki:
        base = normalize_base(wiki)
        if not allowed_host(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
        bases = [base]
    else:
        bases = default_wiki_chain(title)

    used_base, data = await try_wiki_chain(
        bases,
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
    if not pages:
        raise HTTPException(status_code=404, detail="page not found")

    page_obj = next(iter(pages.values()))
    if "missing" in page_obj:
        raise HTTPException(status_code=404, detail="page not found")

    resolved_title = page_obj.get("title") or title

    return {
        "wiki": used_base,
        "title": resolved_title,
        "pageid": page_obj.get("pageid"),
        "url": page_obj.get("fullurl") or page_url(used_base, str(resolved_title)),
        "extract": page_obj.get("extract"),
    }