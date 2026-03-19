import os
import re
import html
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Literal
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(
    title="MediaWiki Bridge API",
    version="1.6.0",
)

BLOCKED = set(
    ip.strip()
    for ip in os.getenv("BLOCKED_IPS", "").split(",")
    if ip.strip()
)

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))

DEFAULT_HEADERS = {
    "User-Agent": os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

ALLOWED_WIKI_HOST_SUFFIXES = (
    "fandom.com",
    "wiki.gg",
    "wikipedia.org",
)

TAG_RE = re.compile(r"<[^>]+>")
STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for"}
ROMANS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

MAX_EXTRACT_CHARS = int(os.getenv("MAX_EXTRACT_CHARS", "200000"))

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "1800"))
CACHE_MAX_ITEMS = int(os.getenv("CACHE_MAX_ITEMS", "500"))


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


CACHE: Dict[str, CacheEntry] = {}


def _cache_cleanup() -> None:
    now = time.time()

    expired_keys = [k for k, v in CACHE.items() if v.expires_at <= now]
    for k in expired_keys:
        CACHE.pop(k, None)

    if len(CACHE) > CACHE_MAX_ITEMS:
        sorted_items = sorted(CACHE.items(), key=lambda item: item[1].expires_at)
        overflow = len(CACHE) - CACHE_MAX_ITEMS
        for k, _ in sorted_items[:overflow]:
            CACHE.pop(k, None)


def cache_get(key: str) -> Optional[Any]:
    entry = CACHE.get(key)
    if not entry:
        return None

    if entry.expires_at <= time.time():
        CACHE.pop(key, None)
        return None

    return entry.value


def cache_set(key: str, value: Any, ttl: int = CACHE_TTL_SECONDS) -> None:
    CACHE[key] = CacheEntry(value=value, expires_at=time.time() + ttl)
    _cache_cleanup()


@app.middleware("http")
async def request_diagnostics(request: Request, call_next):
    start_time = time.time()

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_addr = forwarded_for.split(",")[0].strip()
    else:
        client_addr = request.client.host if request.client else "unknown"

    service_addr = request.client.host if request.client else "unknown"
    client_agent = request.headers.get("user-agent", "unknown")

    print("\n=== Request Diagnostics ===")
    print(f"Client Address: {client_addr}")
    print(f"Service Address: {service_addr}")
    print(f"Client Agent: {client_agent}")
    print(f"Endpoint: {request.url.path}")
    print(f"Parameters: {request.url.query}")

    if client_addr in BLOCKED:
        print(f"Blocked request from {client_addr}")
        print("================================\n")
        return JSONResponse(
            status_code=403,
            content={"detail": "Access denied"},
        )

    response = await call_next(request)

    duration = (time.time() - start_time) * 1000
    print(f"Response Status: {response.status_code}")
    print(f"Duration: {duration:.2f} ms")
    print("================================\n")

    return response


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


def is_wikipedia(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return host.endswith("wikipedia.org")


def candidate_action_apis(base: str) -> List[str]:
    base = normalize_base(base)

    if is_wikipedia(base):
        return [f"{base}/w/api.php"]

    if is_fandom(base):
        return [f"{base}/api.php", f"{base}/w/api.php"]

    return [f"{base}/w/api.php", f"{base}/api.php"]


def page_url(base: str, title: str) -> str:
    return f"{base}/wiki/{quote(title.replace(' ', '_'))}"


def clean_snippet(value: Any) -> str:
    if not value:
        return ""
    s = html.unescape(str(value))
    s = TAG_RE.sub("", s)
    return s.strip()


def strip_html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    s = raw_html
    s = COMMENT_RE.sub(" ", s)
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = TABLE_RE.sub(" ", s)
    s = html.unescape(s)
    s = TAG_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def first_good_paragraph(parse_html: str) -> str:
    if not parse_html:
        return ""
    s = COMMENT_RE.sub(" ", parse_html)
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = TABLE_RE.sub(" ", s)

    for m in PARA_RE.finditer(s):
        candidate = strip_html_to_text(m.group(1))
        if len(candidate) >= 60:
            return candidate
    return ""


def best_paragraphs(
    parse_html: str,
    max_paras: int = 10000,
    min_len: int = 60,
    max_chars: int = 1000000,
) -> str:
    if not parse_html:
        return ""

    s = COMMENT_RE.sub(" ", parse_html)
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = TABLE_RE.sub(" ", s)

    paras: List[str] = []
    total = 0

    for m in PARA_RE.finditer(s):
        text = strip_html_to_text(m.group(1))
        if len(text) < min_len:
            continue

        lowered = text.lower()
        if lowered.startswith("this article") or lowered.startswith("this page"):
            continue

        if total + len(text) > max_chars and paras:
            break

        paras.append(text)
        total += len(text)

        if len(paras) >= max_paras:
            break

    return "\n\n".join(paras).strip()


def extract_all_visible_text(parse_html: str) -> str:
    if not parse_html:
        return ""

    s = parse_html
    s = SCRIPT_STYLE_RE.sub(" ", s)
    s = COMMENT_RE.sub(" ", s)

    s = re.sub(r"<nav\b[^>]*>.*?</nav>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<aside\b[^>]*>.*?</aside>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<footer\b[^>]*>.*?</footer>", " ", s, flags=re.DOTALL | re.IGNORECASE)

    s = re.sub(r"</(p|li|dd|dt|h1|h2|h3|h4|h5|h6)>", "\n\n", s, flags=re.IGNORECASE)

    s = html.unescape(s)
    s = TAG_RE.sub(" ", s)

    s = re.sub(r"\n\s*\n+", "\n\n", s)
    s = re.sub(r"[ \t]+", " ", s)

    return s.strip()


async def mediawiki_get(base: str, params: Dict[str, Any]) -> Dict[str, Any]:
    base = normalize_base(base)
    if not host_is_allowed(base):
        raise HTTPException(status_code=403, detail="wiki host not allowed")

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
    ) as client:
        last_status: Optional[int] = None
        last_error: Optional[str] = None
        last_body: Optional[str] = None

        for api in candidate_action_apis(base):
            try:
                r = await client.get(api, params=params)
                last_status = r.status_code
                last_body = r.text[:500]

                print(f"[mediawiki_get] API tried: {api}")
                print(f"[mediawiki_get] Params: {params}")
                print(f"[mediawiki_get] Upstream status: {r.status_code}")
                print(f"[mediawiki_get] Upstream body preview: {last_body}")

                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception as e:
                        last_error = f"json_decode_error: {repr(e)}"
                        print(f"[mediawiki_get] {last_error}")
                        print(f"[mediawiki_get] Non JSON body preview: {last_body}")
                        continue

            except Exception as e:
                last_error = repr(e)
                print(f"[mediawiki_get] Exception for {api}: {last_error}")
                continue

    raise HTTPException(
        status_code=502,
        detail={
            "error": "upstream_mediawiki_error",
            "status": last_status or 0,
            "exception": last_error,
            "body_preview": last_body,
        },
    )


async def fandom_hub_lookup(topic: str) -> Optional[str]:
    cache_key = f"fandom_hub:{topic.strip().lower()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    search_url = "https://www.fandom.com/api/v1/Search/List"
    params = {
        "query": topic,
        "limit": 10,
        "ns": 0,
    }

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
    ) as client:
        try:
            r = await client.get(search_url, params=params)
            print(f"[fandom_hub_lookup] status={r.status_code}")
            print(f"[fandom_hub_lookup] body_preview={r.text[:500]}")

            if r.status_code != 200:
                cache_set(cache_key, None, ttl=300)
                return None

            try:
                items = r.json().get("items", [])
            except Exception as e:
                print(f"[fandom_hub_lookup] json decode failed: {repr(e)}")
                cache_set(cache_key, None, ttl=300)
                return None

            for item in items:
                url = item.get("url")
                if not url:
                    continue

                parsed = urlparse(url)
                host = parsed.hostname or ""
                if not host.endswith("fandom.com"):
                    continue

                base = f"{parsed.scheme}://{host}"

                for api in candidate_action_apis(base):
                    try:
                        probe = await client.get(
                            api,
                            params={
                                "action": "query",
                                "list": "search",
                                "srsearch": topic,
                                "srlimit": 1,
                                "format": "json",
                            },
                        )
                        print(f"[fandom_hub_lookup] probe api={api} status={probe.status_code}")

                        if probe.status_code == 200:
                            try:
                                probe.json()
                                cache_set(cache_key, base, ttl=3600)
                                return base
                            except Exception:
                                continue
                    except Exception as e:
                        print(f"[fandom_hub_lookup] probe exception for {api}: {repr(e)}")
                        continue

        except Exception as e:
            print(f"[fandom_hub_lookup] exception: {repr(e)}")
            cache_set(cache_key, None, ttl=300)
            return None

    cache_set(cache_key, None, ttl=300)
    return None


def _is_roman_numeral(t: str) -> bool:
    return (t or "").lower() in ROMANS


def tokenize_topic(topic: str) -> List[str]:
    s = (topic or "").strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="topic is empty")

    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return [t for t in s.split(" ") if t]


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
        if not c or len(c) < 3 or c in seen:
            continue
        seen.add(c)
        uniq.append(c)

    return uniq


async def _probe_api(client: httpx.AsyncClient, api_url: str, hint: str) -> bool:
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

        try:
            data = r.json()
        except Exception:
            return False

        q = data.get("query") or {}
        s = q.get("search")
        return isinstance(s, list)
    except Exception:
        return False


async def resolve_topic(topic: str) -> tuple[str, str]:
    if topic.startswith("http://") or topic.startswith("https://"):
        base = normalize_base(topic)
        if not host_is_allowed(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
        return base, "explicit"

    cache_key = f"resolve_topic:{topic.strip().lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached["base"], cached["method"]

    slugs = candidate_slugs(topic)

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
    ) as client:
        for slug in slugs:
            for raw_base in (f"https://{slug}.fandom.com", f"https://{slug}.wiki.gg"):
                base = normalize_base(raw_base)

                if not host_is_allowed(base):
                    continue

                for api in candidate_action_apis(base):
                    ok = await _probe_api(client, api, hint=topic)
                    if ok:
                        cache_set(
                            cache_key,
                            {"base": base, "method": "slug"},
                            ttl=3600,
                        )
                        return base, "slug"

    fandom_base = await fandom_hub_lookup(topic)
    if fandom_base and host_is_allowed(fandom_base):
        cache_set(
            cache_key,
            {"base": fandom_base, "method": "fandom_hub"},
            ttl=3600,
        )
        return fandom_base, "fandom_hub"

    raise HTTPException(
        status_code=404,
        detail="could not resolve topic to fandom.com or wiki.gg",
    )


async def resolve_with_optional_base(topic: Optional[str], wiki: Optional[str]) -> tuple[str, str]:
    if wiki:
        base = normalize_base(wiki)
        if not host_is_allowed(base):
            raise HTTPException(status_code=403, detail="wiki host not allowed")
        return base, "explicit"

    if not topic:
        raise HTTPException(status_code=400, detail="Either wiki or topic must be provided")

    cache_key = f"resolve_with_optional_base:{topic.strip().lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached["base"], cached["method"]

    base, method = await resolve_topic(topic)
    cache_set(cache_key, {"base": base, "method": method}, ttl=3600)
    return base, method


async def resolve_title(base: str, title: str) -> str:
    cache_key = f"title:{base}:{title.strip().lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    data = await mediawiki_get(
        base,
        {
            "action": "query",
            "titles": title,
            "redirects": "1",
            "format": "json",
        },
    )

    pages = (data.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), None)

    if not page or "missing" in page:
        raise HTTPException(status_code=404, detail="page not found")

    resolved = page.get("title") or title
    cache_set(cache_key, resolved, ttl=3600)
    return resolved


async def resolve_via_http_redirect(base: str, title: str) -> Optional[str]:
    cache_key = f"http_redirect:{base}:{title.strip().lower()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    url = page_url(base, title)

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
    ) as client:
        try:
            r = await client.head(url)
            final = str(r.url)
        except Exception as e:
            print(f"[resolve_via_http_redirect] exception: {repr(e)}")
            cache_set(cache_key, None, ttl=300)
            return None

    if "/wiki/" not in final:
        cache_set(cache_key, None, ttl=300)
        return None

    resolved = final.split("/wiki/", 1)[1].replace("_", " ")
    cache_set(cache_key, resolved, ttl=3600)
    return resolved


def normalize_episode_title(raw: str) -> Optional[str]:
    s = raw.strip().lower()

    m = re.fullmatch(r"(episode\s*)?0*(\d+)", s)
    if m:
        return f"Episode_{int(m.group(2))}"

    if s.startswith("episode_"):
        return s.title().replace(" ", "_")

    return None


async def fetch_extract_with_query(base: str, title: str, intro_only: bool) -> str:
    cache_key = f"extract_query:{base}:{title.strip().lower()}:{int(intro_only)}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    params: Dict[str, Any] = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": title,
        "redirects": "1",
        "format": "json",
    }

    if intro_only:
        params["exintro"] = "1"

    data = await mediawiki_get(base, params)
    pages = data.get("query", {}).get("pages", {})
    page_obj = next(iter(pages.values()), None)

    if not page_obj or "missing" in page_obj:
        cache_set(cache_key, "", ttl=300)
        return ""

    extract_val = page_obj.get("extract")
    if not extract_val:
        cache_set(cache_key, "", ttl=300)
        return ""

    result = str(extract_val).strip()
    cache_set(cache_key, result, ttl=1800)
    return result


async def fetch_extract_with_parse(base: str, title: str) -> str:
    cache_key = f"extract_parse:{base}:{title.strip().lower()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    data = await mediawiki_get(
        base,
        {
            "action": "parse",
            "page": title,
            "prop": "text",
            "redirects": "1",
            "format": "json",
        },
    )

    text_obj = (data.get("parse") or {}).get("text") or {}
    parse_html = text_obj.get("*") or ""
    if not parse_html:
        cache_set(cache_key, "", ttl=300)
        return ""

    result = best_paragraphs(str(parse_html), max_paras=10000, max_chars=1000000)
    cache_set(cache_key, result, ttl=1800)
    return result


async def get_parsed_page_payload(
    base: str,
    page_title: Optional[str] = None,
    pageid: Optional[int] = None,
) -> Dict[str, Any]:
    if pageid is not None:
        cache_key = f"page_payload:{base}:pageid:{pageid}"
    else:
        normalized_title = (page_title or "").strip().lower()
        cache_key = f"page_payload:{base}:title:{normalized_title}"

    cached = cache_get(cache_key)
    if cached:
        return cached

    parse_params = {
        "action": "parse",
        "prop": "text",
        "format": "json",
    }

    if pageid is not None:
        parse_params["pageid"] = pageid
    else:
        parse_params["page"] = page_title

    data = await mediawiki_get(base, parse_params)

    parse = data.get("parse")
    if not parse:
        raise HTTPException(status_code=404, detail="page not found")

    canonical_title = parse.get("title")
    parsed_pageid = parse.get("pageid")

    parse_html = (parse.get("text") or {}).get("*") or ""
    extract_text = extract_all_visible_text(parse_html)

    if len(extract_text) > MAX_EXTRACT_CHARS:
        extract_text = extract_text[:MAX_EXTRACT_CHARS]

    if not extract_text:
        raise HTTPException(status_code=404, detail="no extractable content")

    payload = {
        "canonical_title": canonical_title,
        "pageid": parsed_pageid,
        "extract_text": extract_text,
    }

    cache_set(cache_key, payload, ttl=1800)

    if canonical_title:
        canonical_cache_key = f"page_payload:{base}:title:{canonical_title.strip().lower()}"
        cache_set(canonical_cache_key, payload, ttl=1800)

    if parsed_pageid is not None:
        pageid_cache_key = f"page_payload:{base}:pageid:{parsed_pageid}"
        cache_set(pageid_cache_key, payload, ttl=1800)

    return payload


@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/cache")
def cache_stats() -> Dict[str, Any]:
    _cache_cleanup()
    now = time.time()

    return {
        "enabled": True,
        "items": len(CACHE),
        "max_items": CACHE_MAX_ITEMS,
        "default_ttl_seconds": CACHE_TTL_SECONDS,
        "entries": [
            {
                "key": key,
                "expires_in_seconds": max(0, int(entry.expires_at - now)),
            }
            for key, entry in sorted(CACHE.items())
        ],
    }


@app.get("/resolve")
async def resolve(
    topic: str = Query(..., min_length=1),
    wiki: Optional[str] = Query(None),
) -> Dict[str, str]:
    base, method = await resolve_with_optional_base(topic, wiki)
    return {
        "topic": topic,
        "wiki": base,
        "resolution_method": method,
    }


@app.get("/render", response_class=HTMLResponse)
async def render(
    topic: str = Query(..., min_length=1),
    title: Optional[str] = Query(None),
    pageid: Optional[int] = Query(None),
    wiki: Optional[str] = Query(None),
):
    base, resolution_method = await resolve_with_optional_base(topic, wiki)

    if not title and pageid is None:
        raise HTTPException(
            status_code=400,
            detail="Either title or pageid must be provided",
        )

    resolved_title = None

    if title:
        episode_title = normalize_episode_title(title)
        lookup_title = episode_title or title

        try:
            resolved_title = await resolve_title(base, lookup_title)
        except HTTPException:
            fallback = await resolve_via_http_redirect(base, lookup_title)
            resolved_title = fallback or lookup_title

    parse_params = {
        "action": "parse",
        "prop": "text",
        "format": "json",
        "formatversion": 2,
    }

    if pageid is not None:
        parse_params["pageid"] = pageid
    else:
        parse_params["page"] = resolved_title

    try:
        data = await mediawiki_get(base, parse_params)
    except HTTPException:
        bridge_page_url = (
            "https://mediawiki-bridge.onrender.com/page"
            f"?wiki={quote(base)}"
            f"&topic={quote(title or topic)}"
            f"&title={quote((title or topic).replace(' ', '_'))}"
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "parse_failed",
                "message": "Unable to render page via API.",
                "view_full_page": bridge_page_url,
            },
        )

    parse = data.get("parse")
    if not parse:
        raise HTTPException(status_code=404, detail="page not found")

    html_content = parse.get("text")
    if not html_content:
        raise HTTPException(status_code=404, detail="no renderable content")

    return HTMLResponse(
        content=f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{parse.get("title", "MediaWiki Render")}</title>
<style>
    body {{
        font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        line-height: 1.6;
        max-width: 900px;
        margin: 2rem auto;
        padding: 0 1rem;
        background: #fff;
        color: #111;
    }}
    img {{ max-width: 100%; }}
    table {{ border-collapse: collapse; }}
    th, td {{ border: 1px solid #ccc; padding: 0.4rem; }}
</style>
</head>
<body>
{html_content}
</body>
</html>
"""
    )


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    topic: Optional[str] = Query(None, min_length=1),
    limit: int = Query(5, ge=1, le=20),
    wiki: Optional[str] = Query(None),
) -> Dict[str, Any]:
    if not wiki and not topic:
        raise HTTPException(
            status_code=400,
            detail="Either wiki or topic must be provided",
        )

    base, resolution_method = await resolve_with_optional_base(topic, wiki)

    data = await mediawiki_get(
        base,
        {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": limit,
            "srprop": "snippet|timestamp",
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

        snippet = clean_snippet(item.get("snippet"))
        if not snippet:
            snippet = "(No text preview available)"

        results.append(
            {
                "title": title_str,
                "pageid": item.get("pageid"),
                "url": page_url(base, title_str),
                "snippet": snippet,
                "timestamp": item.get("timestamp"),
            }
        )

    return {
        "topic": topic,
        "wiki": base,
        "resolution_method": resolution_method,
        "query": q,
        "limit": limit,
        "results": results,
    }


@app.get("/page")
async def page(
    title: Optional[str] = Query(None, min_length=1),
    pageid: Optional[int] = Query(None, ge=1),
    topic: Optional[str] = Query(None, min_length=1),
    wiki: Optional[str] = Query(None),
    mode: Literal["full", "chunk"] = Query("chunk"),
    chunk: int = Query(0, ge=0),
    chunk_size: int = Query(8000, ge=4000, le=100000),
) -> Dict[str, Any]:
    if not title and pageid is None:
        raise HTTPException(
            status_code=400,
            detail="Either title or pageid must be provided",
        )

    if not wiki and not topic:
        raise HTTPException(
            status_code=400,
            detail="Either wiki or topic must be provided",
        )

    if wiki and not wiki.startswith(("http://", "https://")):
        wiki = "https://" + wiki

    base, resolution_method = await resolve_with_optional_base(topic, wiki)

    requested_title = title
    resolved_title = None

    if title:
        episode_title = normalize_episode_title(title)
        lookup_title = episode_title or title

        try:
            resolved_title = await resolve_title(base, lookup_title)
        except HTTPException:
            fallback = await resolve_via_http_redirect(base, lookup_title)
            resolved_title = fallback or lookup_title

    parsed = await get_parsed_page_payload(
        base=base,
        page_title=resolved_title,
        pageid=pageid,
    )

    canonical_title = parsed["canonical_title"]
    parsed_pageid = parsed["pageid"]
    extract_text = parsed["extract_text"]

    source = (
        "wikipedia"
        if is_wikipedia(base)
        else "fandom"
        if is_fandom(base)
        else "wiki.gg"
    )

    if mode == "full" and len(extract_text) > chunk_size:
        raise HTTPException(
            status_code=413,
            detail="full mode disabled for large pages; use chunk mode",
        )

    if mode == "full":
        return {
            "topic": topic,
            "wiki": base,
            "resolution_method": resolution_method,
            "source": source,
            "requested_title": requested_title,
            "resolved_title": resolved_title,
            "canonical_title": canonical_title,
            "pageid": parsed_pageid,
            "url": page_url(base, canonical_title),
            "mode": "full",
            "extract": extract_text,
            "extract_source": "parse_full_cached",
        }

    total_len = len(extract_text)
    total_chunks = (total_len + chunk_size - 1) // chunk_size

    start = chunk * chunk_size
    end = start + chunk_size

    if start >= total_len:
        raise HTTPException(
            status_code=416,
            detail="chunk out of range",
        )

    chunk_text = extract_text[start:end]

    return {
        "topic": topic,
        "wiki": base,
        "resolution_method": resolution_method,
        "source": source,
        "requested_title": requested_title,
        "resolved_title": resolved_title,
        "canonical_title": canonical_title,
        "pageid": parsed_pageid,
        "url": page_url(base, canonical_title),
        "mode": "chunk",
        "chunk": chunk,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "is_last_chunk": chunk == total_chunks - 1,
        "extract": chunk_text,
        "extract_source": "parse_full_cached",
    }