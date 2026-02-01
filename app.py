import os
from typing import Any, Dict, Optional, List
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query

# -----------------------------------------------------------------------------
# App configuration
# -----------------------------------------------------------------------------

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.3.0",
)

USER_AGENT = os.getenv("USER_AGENT", "mediawiki-bridge/1.3")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

ALLOWED_WIKI_HOST_SUFFIXES = (
    "fandom.com",
    "wiki.gg",
    "wikipedia.org",
)


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def normalize_base(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("invalid url")
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


async def mediawiki_get(api_url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        r = await client.get(api_url, params=params)

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Upstream MediaWiki error {r.status_code}")

    return r.json()


async def try_wiki_chain(
    bases: List[str],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    last_error = None

    for base in bases:
        try:
            return await mediawiki_get(primary_action_api(base), params)
        except Exception as e:
            last_error = e
            try:
                return await mediawiki_get(fallback_action_api(base), params)
            except Exception:
                continue

    raise HTTPException(status_code=502, detail="All wiki sources failed")


def default_wiki_chain(query: str) -> List[str]:
    q = query.lower().replace(" ", "")
    return [
        f"https://{q}.fandom.com",
        f"https://{q}.wiki.gg",
        "https://en.wikipedia.org",
    ]


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

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

    data = await try_wiki_chain(
        bases,
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": limit,
            "format": "json",
        },
    )

    results = [
        {
            "title": item.get("title"),
            "pageid": item.get("pageid"),
            "snippet": item.get("snippet"),
        }
        for item in data.get("query", {}).get("search", [])
    ]

    return {
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

    data = await try_wiki_chain(
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
    page_obj = next(iter(pages.values()))

    if "missing" in page_obj:
        raise HTTPException(status_code=404, detail="Page not found")

    return {
        "title": page_obj.get("title"),
        "pageid": page_obj.get("pageid"),
        "url": page_obj.get("fullurl"),
        "extract": page_obj.get("extract"),
    }