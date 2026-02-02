import os
import re
import html
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.5.0",
)

USER_AGENT = os.getenv("USER_AGENT", "mediawiki_bridge/1.5.0")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

ALLOWED_WIKI_HOST_SUFFIXES = ("fandom.com", "wiki.gg")

TAG_RE = re.compile(r"<[^>]+>")
STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for"}
ROMANS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


# -------------------------
# Utilities
# -------------------------

def normalize_base(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid wiki url")
    return f"{parsed.scheme}://{parsed.hostname.lower()}"


def allowed_host(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return any(host.endswith(sfx) for sfx in ALLOWED_WIKI_HOST_SUFFIXES)


def clean_snippet(value: Any) -> str:
    if not value:
        return ""
    s = html.unescape(str(value))
    s = TAG_RE.sub("", s)
    return s.strip()


def page_url(base: str, title: str) -> str:
    return f"{base}/wiki/{quote(title.replace(' ', '_'))}"


# -------------------------
# Topic Resolution
# -------------------------

def tokenize_topic(topic: str) -> List[str]:
    s = topic.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    return [t for t in s.split() if t and t not in STOPWORDS]


def candidate_slugs(topic: str) -> List[str]:
    tokens = tokenize_topic(topic)
    variants = [
        "".join(tokens),
        "".join(t for t in tokens if t not in ROMANS),
        "".join(tokens[:-1]) if len(tokens) > 1 else "",
    ]
    return [v for v in dict.fromkeys(variants) if len(v) >= 3]


async def resolve_topic(topic: str) -> str:
    slugs = candidate_slugs(topic)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        for slug in slugs:
            for base in (f"https://{slug}.fandom.com", f"https://{slug}.wiki.gg"):
                try:
                    api = f"{base}/api.php" if base.endswith("fandom.com") else f"{base}/w/api.php"
                    r = await client.get(api, params={
                        "action": "query",
                        "list": "search",
                        "srsearch": topic,
                        "srlimit": 1,
                        "format": "json",
                    })
                    if r.status_code == 200 and "query" in r.json():
                        return normalize_base(base)
                except Exception:
                    continue

    raise HTTPException(status_code=404, detail="could not resolve topic")


# -------------------------
# MediaWiki fetch
# -------------------------

async def mediawiki_get(api: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(api, params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail="upstream mediawiki error")
    return r.json()


def action_api(base: str) -> str:
    return f"{base}/api.php" if base.endswith("fandom.com") else f"{base}/w/api.php"


# -------------------------
# Routes
# -------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/resolve")
async def resolve(topic: str = Query(..., min_length=1)):
    wiki = await resolve_topic(topic)
    return {"topic": topic, "wiki": wiki}


@app.get("/search")
async def search(
    topic: str = Query(..., min_length=1),
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
):
    base = await resolve_topic(topic)
    data = await mediawiki_get(action_api(base), {
        "action": "query",
        "list": "search",
        "srsearch": q,
        "srlimit": limit,
        "format": "json",
    })

    results = []
    for item in data.get("query", {}).get("search", []):
        results.append({
            "title": item["title"],
            "pageid": item.get("pageid"),
            "url": page_url(base, item["title"]),
            "snippet": clean_snippet(item.get("snippet")),
            "timestamp": item.get("timestamp"),
        })

    return {
        "topic": topic,
        "wiki": base,
        "query": q,
        "results": results,
    }


@app.get("/page")
async def page(
    topic: str = Query(..., min_length=1),
    title: str = Query(..., min_length=1),
):
    base = await resolve_topic(topic)
    data = await mediawiki_get(action_api(base), {
        "action": "query",
        "prop": "extracts|info",
        "exintro": "1",
        "explaintext": "1",
        "inprop": "url",
        "titles": title,
        "format": "json",
    })

    pages = data.get("query", {}).get("pages", {})
    page_obj = next(iter(pages.values()), None)

    if not page_obj or "missing" in page_obj:
        raise HTTPException(status_code=404, detail="page not found")

    return {
        "topic": topic,
        "wiki": base,
        "title": page_obj["title"],
        "pageid": page_obj.get("pageid"),
        "url": page_obj.get("fullurl") or page_url(base, page_obj["title"]),
        "extract": page_obj.get("extract"),
    }
