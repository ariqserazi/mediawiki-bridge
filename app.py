import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query

# -----------------------------------------------------------------------------
# App configuration
# -----------------------------------------------------------------------------

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.1.0",
)

DEFAULT_WIKI = os.getenv("DEFAULT_WIKI", "https://en.wikipedia.org")
USER_AGENT = os.getenv("USER_AGENT", "mediawiki-bridge/1.1")

ALLOWED_WIKI_HOST_SUFFIXES = tuple(
    suffix.strip().lower()
    for suffix in os.getenv(
        "ALLOWED_WIKI_HOST_SUFFIXES",
        "wikipedia.org,fandom.com,wiki.gg",
    ).split(",")
    if suffix.strip()
)

HTTP_TIMEOUT = 30.0


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def validate_and_normalize_wiki_base(wiki: str) -> str:
    """
    Validate the wiki base URL and normalize it to scheme + hostname only.
    """
    if not wiki:
        raise HTTPException(status_code=400, detail="wiki is empty")

    if not wiki.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="wiki must start with http or https")

    parsed = urlparse(wiki)
    host = (parsed.hostname or "").lower()

    if not host:
        raise HTTPException(status_code=400, detail="wiki host is invalid")

    if not any(host.endswith(sfx) for sfx in ALLOWED_WIKI_HOST_SUFFIXES):
        raise HTTPException(status_code=403, detail="wiki host is not allowed")

    return f"{parsed.scheme}://{host}"


def primary_action_api(base: str) -> str:
    """
    Determine the primary MediaWiki action API endpoint.
    """
    host = urlparse(base).hostname or ""
    if host.endswith("fandom.com"):
        return f"{base}/api.php"
    return f"{base}/w/api.php"


def fallback_action_api(base: str) -> str:
    """
    Determine the fallback MediaWiki action API endpoint.
    """
    host = urlparse(base).hostname or ""
    if host.endswith("fandom.com"):
        return f"{base}/w/api.php"
    return f"{base}/api.php"


async def mediawiki_get(
    api_url: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Perform a GET request against a MediaWiki action API.
    """
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers=headers,
    ) as client:
        response = await client.get(api_url, params=params)

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream MediaWiki error {response.status_code}",
        )

    return response.json()


async def mediawiki_get_with_fallback(
    base: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Try the primary API endpoint, then fall back if it fails.
    """
    try:
        return await mediawiki_get(primary_action_api(base), params)
    except HTTPException as exc:
        if exc.status_code != 502:
            raise

    return await mediawiki_get(fallback_action_api(base), params)


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
    wiki: Optional[str] = Query(None, min_length=1),
) -> Dict[str, Any]:
    base = validate_and_normalize_wiki_base(wiki or DEFAULT_WIKI)

    data = await mediawiki_get_with_fallback(
        base,
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
            "timestamp": item.get("timestamp"),
        }
        for item in data.get("query", {}).get("search", [])
    ]

    return {
        "wiki": base,
        "query": q,
        "limit": limit,
        "results": results,
    }


@app.get("/page")
async def page(
    title: str = Query(..., min_length=1),
    wiki: Optional[str] = Query(None, min_length=1),
) -> Dict[str, Any]:
    base = validate_and_normalize_wiki_base(wiki or DEFAULT_WIKI)

    data = await mediawiki_get_with_fallback(
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
