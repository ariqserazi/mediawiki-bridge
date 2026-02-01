import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="MediaWiki Bridge API", version="1.0.0")

WIKI_API = os.getenv("WIKI_API", "https://en.wikipedia.org/w/api.php")
USER_AGENT = os.getenv("USER_AGENT", "mediawiki-bridge/1.0")


@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


async def mw_get(params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        r = await client.get(WIKI_API, params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Upstream MediaWiki error {r.status_code}")
    return r.json()


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
) -> Dict[str, Any]:
    data = await mw_get(
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": limit,
            "format": "json",
        }
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

    return {"query": q, "limit": limit, "results": results}


@app.get("/page")
async def page(title: str = Query(..., min_length=1)) -> Dict[str, Any]:
    data = await mw_get(
        {
            "action": "query",
            "prop": "extracts|info",
            "exintro": "1",
            "explaintext": "1",
            "inprop": "url",
            "titles": title,
            "format": "json",
        }
    )

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        raise HTTPException(status_code=404, detail="Page not found")

    page_obj = next(iter(pages.values()))
    if "missing" in page_obj:
        raise HTTPException(status_code=404, detail="Page not found")

    return {
        "title": page_obj.get("title"),
        "pageid": page_obj.get("pageid"),
        "url": page_obj.get("fullurl"),
        "extract": page_obj.get("extract"),
    }
