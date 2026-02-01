import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="MediaWiki Bridge API", version="1.1.0")

DEFAULT_WIKI = os.getenv("DEFAULT_WIKI", "https://en.wikipedia.org")
USER_AGENT = os.getenv("USER_AGENT", "mediawiki-bridge/1.1")

ALLOWED_WIKI_HOST_SUFFIXES = tuple(
    s.strip().lower()
    for s in os.getenv(
        "ALLOWED_WIKI_HOST_SUFFIXES",
        "wikipedia.org,fandom.com,wiki.gg",
    ).split(",")
    if s.strip()
)


@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


def normalize_wiki_base(wiki: str) -> str:
    wiki = wiki.strip()
    if not wiki:
        raise HTTPException(status_code=400, detail="wiki is empty")

    if not (wiki.startswith("https://") or wiki.startswith("http://")):
        raise HTTPException(status_code=400, detail="wiki must start with http or https")

    parsed = urlparse(wiki)
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="wiki host is invalid")

    if not any(host.endswith(sfx) for sfx in ALLOWED_WIKI_HOST_SUFFIXES):
        raise HTTPException(status_code=403, detail="wiki host is not allowed")

    return f"{parsed.scheme}://{host}"


def derive_action_api(base: str) -> str:
    host = urlparse(base).hostname or ""
    if host.endswith("fandom.com"):
        return f"{base}/api.php"
    return f"{base}/w/api.php"


async def mw_get(action_api: str, params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        r = await client.get(action_api, params=params)

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Upstream MediaWiki error {r.status_code}")

    return r.json()


async def mw_get_with_fallback(base: str, params: Dict[str, Any]) -> Dict[str, Any]:
    primary = derive_action_api(base)

    try:
        return await mw_get(primary, params)
    except HTTPException as e:
        if e.status_code != 502:
            raise

    host = urlparse(base).hostname or ""
    if host.endswith("fandom.com"):
        fallback = f"{base}/w/api.php"
    else:
        fallback = f"{base}/api.php"

    return await mw_get(fallback, params)


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    wiki: Optional[str] = Query(None, min_length=1),
) -> Dict[str, Any]:
    base = normalize_wiki_base(wiki or DEFAULT_WIKI)

    data = await mw_get_with_fallback(
        base,
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": limit,
            "format": "json",
        },
    )

    results = []
    for item in data.get("query", {}).get("search", []):
        results.append(
            {
                "title": item.get("title"),
                "pageid": item.get("pageid"),
                "snippet": item.get("snippet"),
                "timestamp": item.get("timestamp"),
            }
        )

    return {"wiki": base, "query": q, "limit": limit, "results": results}


@app.get("/page")
async def page(
    title: str = Query(..., min_length=1),
    wiki: Optional[str] = Query(None, min_length=1),
) -> Dict[str, Any]:
    base = normalize_wiki_base(wiki or DEFAULT_WIKI)

    data = await mw_get_with_fallback(
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
    if not pages:
        raise HTTPException(status_code=404, detail="Page not found")

    page_obj = next(iter(pages.values()))
    if "missing" in page_obj:
        raise HTTPException(status_code=404, detail="Page not found")

    return {
        "wiki": base,
        "title": page_obj.get("title"),
        "pageid": page_obj.get("pageid"),
        "url": page_obj.get("fullurl"),
        "extract": page_obj.get("extract"),
    }