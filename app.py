import os
import re
import html
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="MediaWiki Bridge API", version="1.4.0")

USER_AGENT = os.getenv("USER_AGENT", "mediawiki-bridge/1.4.0")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

ALLOWED_WIKI_HOST_SUFFIXES = ("fandom.com", "wiki.gg")

TAG_RE = re.compile(r"<[^>]+>")


def normalize_base(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="invalid wiki url")
    return f"{parsed.scheme}://{host}"


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
    s = html.unescape(str(value))
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


def slugify_topic(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[\(\)\[\]\{\}]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace(" ", "")


async def mediawiki_get(api_url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        r = await client.get(api_url, params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"upstream mediawiki error {r.status_code}")
    return r.json()


async def mediawiki_get_with_fallback(base: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return await mediawiki_get(primary_action_api(base), params)
    except HTTPException:
        return await mediawiki_get(fallback_action_api(base), params)


async def validate_wiki_base(base: str) -> bool:
    try:
        data = await mediawiki_get_with_fallback(
            base,
            {
                "action": "query",
                "meta": "siteinfo",
                "siprop": "general",
                "format": "json",
            },
        )
        general = data.get("query", {}).get("general", {})
        return bool(general.get("sitename") or general.get("server"))
    except Exception:
        return False


def candidate_bases_from_text(text: str) -> List[str]:
    slug = slugify_topic(text)
    if not slug:
        return []
    return [
        f"https://{slug}.fandom.com",
        f"https://{slug}.wiki.gg",
    ]


async def resolve_base(topic: str, q: Optional[str] = None) -> str:
    candidates: List[str] = []
    candidates.extend(candidate_bases_from_text(topic))
    if q:
        candidates.extend(candidate_bases_from_text(q))

    seen = set()
    ordered: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)

    for base in ordered:
        if not allowed_host(base):
            continue
        ok = await validate_wiki_base(base)
        if ok:
            return base

    raise HTTPException(
        status_code=404,
        detail="could not resolve a valid fandom.com or wiki.gg base for this topic",
    )


@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/resolve")
async def resolve(topic: str = Query(..., min_length=1), q: Optional[str] = Query(None)) -> Dict[str, Any]:
    base = await resolve_base(topic=topic, q=q)
    return {"topic": topic, "wiki": base}


@app.get("/search") 
async def search(
    q: str = Query(..., min_length=1),
    topic: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    wiki: Optional[str] = Query(None, min_length=1),
) -> Dict[str, Any]:
    if wiki:
        base = normalize_base(wiki)
        if not allowed_host(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
    else:
        base = await resolve_base(topic=topic, q=q)

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
                "url": page_url(base, title_str),
                "snippet": clean_snippet(item.get("snippet")),
                "timestamp": item.get("timestamp"),
            }
        )

    return {"topic": topic, "wiki": base, "query": q, "limit": limit, "results": results}


@app.get("/page")
async def page(
    title: str = Query(..., min_length=1),
    topic: str = Query(..., min_length=1),
    wiki: Optional[str] = Query(None, min_length=1),
) -> Dict[str, Any]:
    if wiki:
        base = normalize_base(wiki)
        if not allowed_host(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
    else:
        base = await resolve_base(topic=topic, q=title)

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
        raise HTTPException(status_code=404, detail="page not found")

    page_obj = next(iter(pages.values()))
    if "missing" in page_obj:
        raise HTTPException(status_code=404, detail="page not found")

    resolved_title = page_obj.get("title") or title
    url = page_obj.get("fullurl") or page_url(base, str(resolved_title))

    return {
        "topic": topic,
        "wiki": base,
        "title": resolved_title,
        "pageid": page_obj.get("pageid"),
        "url": url,
        "extract": page_obj.get("extract"),
    }
