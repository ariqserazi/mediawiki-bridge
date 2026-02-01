import os
import re
import html
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.4.0",
)

USER_AGENT = os.getenv("USER_AGENT", "mediawiki_bridge/1.4.0")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

ALLOWED_WIKI_HOST_SUFFIXES = (
    "fandom.com",
    "wiki.gg",
)

TAG_RE = re.compile(r"<[^>]+>")


def normalize_base(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid wiki url")
    return f"{parsed.scheme}://{parsed.hostname}"


def allowed_host(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return any(host.endswith(sfx) for sfx in ALLOWED_WIKI_HOST_SUFFIXES)


def slugify_topic(topic: str) -> str:
    s = topic.strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="topic is empty")

    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    tokens = s.split(" ")
    cleaned: List[str] = []
    for t in tokens:
        if t in {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for"}:
            continue
        if not t:
            continue
        cleaned.append(t)

    if not cleaned:
        cleaned = tokens

    return "".join(cleaned)

def _tokenize_topic(topic: str) -> List[str]:
    s = topic.strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="topic is empty")

    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return [t for t in s.split(" ") if t]


def _is_roman_numeral(t: str) -> bool:
    return t in {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def _candidate_slugs(topic: str) -> List[str]:
    tokens = _tokenize_topic(topic)

    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for"}
    cleaned = [t for t in tokens if t not in stop]

    def join(ts: List[str]) -> str:
        return "".join(ts)

    candidates: List[str] = []

    if cleaned:
        candidates.append(join(cleaned))
    candidates.append(join(tokens))

    no_roman = [t for t in cleaned if not _is_roman_numeral(t)]
    if no_roman:
        candidates.append(join(no_roman))

    no_digits_tokens = [t for t in cleaned if not t.isdigit()]
    if no_digits_tokens:
        candidates.append(join(no_digits_tokens))

    stripped_digit_suffix = [re.sub(r"\d+$", "", t) for t in cleaned]
    stripped_digit_suffix = [t for t in stripped_digit_suffix if t]
    if stripped_digit_suffix:
        candidates.append(join(stripped_digit_suffix))

    if len(cleaned) >= 2:
        candidates.append(join(cleaned[:-1]))

    if len(tokens) >= 2:
        candidates.append(join(tokens[:-1]))

    if 2 <= len(cleaned) <= 5:
        acronym = "".join(t[0] for t in cleaned if t and t[0].isalnum())
        if acronym:
            candidates.append(acronym)

    uniq: List[str] = []
    seen = set()
    for c in candidates:
        c = c.strip()
        if not c:
            continue
        if len(c) < 3:
            continue
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)

    return uniq


async def resolve_topic_to_bases(topic: str) -> List[str]:
    slugs = _candidate_slugs(topic)

    bases: List[str] = []
    seen = set()

    for slug in slugs:
        for base in (f"https://{slug}.fandom.com", f"https://{slug}.wiki.gg"):
            base = normalize_base(base)
            if base in seen:
                continue
            seen.add(base)
            bases.append(base)

    return bases

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


async def probe_wiki_base(base: str) -> bool:
    """
    Returns True if the base looks like a working MediaWiki site.
    Uses siteinfo which is lightweight and consistent.
    """
    if not allowed_host(base):
        return False

    params = {
        "action": "query",
        "meta": "siteinfo",
        "siprop": "general",
        "format": "json",
    }

    try:
        data = await mediawiki_get(primary_action_api(base), params)
        general = data.get("query", {}).get("general", {})
        return bool(general.get("sitename") or general.get("server"))
    except Exception:
        try:
            data = await mediawiki_get(fallback_action_api(base), params)
            general = data.get("query", {}).get("general", {})
            return bool(general.get("sitename") or general.get("server"))
        except Exception:
            return False


async def resolve_topic_to_bases(topic: str) -> List[str]:
    """
    Generates candidate bases and returns them in priority order.
    Priority is fandom first, then wiki gg.
    """
    slug = slugify_topic(topic)

    candidates = [
        f"https://{slug}.fandom.com",
        f"https://{slug}.wiki.gg",
    ]

    bases: List[str] = []
    seen = set()

    for c in candidates:
        base = normalize_base(c)
        if base in seen:
            continue
        seen.add(base)
        bases.append(base)

    return bases


async def resolve_best_base(topic: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Returns best_base plus a report of what was tried.
    """
    bases = await resolve_topic_to_bases(topic)
    tried: List[Dict[str, Any]] = []

    for base in bases:
        ok = await probe_wiki_base(base)
        tried.append({"wiki": base, "ok": ok})
        if ok:
            return base, tried

    return None, tried


async def try_wiki_chain(
    bases: List[str],
    params: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    for base in bases:
        try:
            data = await mediawiki_get(primary_action_api(base), params)
            return base, data
        except Exception:
            try:
                data = await mediawiki_get(fallback_action_api(base), params)
                return base, data
            except Exception:
                continue

    raise HTTPException(status_code=502, detail="all wiki sources failed")


@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/resolve")
async def resolve(topic: str = Query(..., min_length=1)) -> Dict[str, Any]:
    bases = await resolve_topic_to_bases(topic)

    for base in bases:
        if not allowed_host(base):
            continue
        try:
            await mediawiki_get(
                primary_action_api(base),
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": topic,
                    "srlimit": 1,
                    "format": "json",
                },
            )
            return {
                "topic": topic,
                "wiki": base,
            }
        except Exception:
            continue

    raise HTTPException(
        status_code=404,
        detail="could not resolve topic to a working fandom or wiki gg wiki",
    )


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
        best, tried = await resolve_best_base(q)
        if not best:
            raise HTTPException(status_code=404, detail="could not resolve wiki for search query")
        bases = [best]

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
        if not title:
            continue
        title_str = str(title).strip()
        if not title_str:
            continue

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
        best, tried = await resolve_best_base(title)
        if not best:
            raise HTTPException(status_code=404, detail="could not resolve wiki for page title")
        bases = [best]

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
