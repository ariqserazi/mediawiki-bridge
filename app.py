import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query

MCP_URL = os.getenv("MCP_URL", "http://localhost:8000")

app = FastAPI(title="MediaWiki bridge API", version="1.0.0")


async def call_mcp(payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(MCP_URL, json=payload)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Upstream error {r.status_code}: {r.text}")
        return r.json()


@app.get("/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(5, ge=1, le=20)):
    """
    Returns a simplified search result list.
    """
    payload = {
        "tool": "search",
        "input": {"query": q, "limit": limit},
    }
    return await call_mcp(payload)


@app.get("/page")
async def page(title: str = Query(..., min_length=1)):
    """
    Returns page content and metadata.
    """
    payload = {
        "tool": "get_page",
        "input": {"title": title},
    }
    return await call_mcp(payload)

@app.get("/health")
def health():
    return {"ok": True}
