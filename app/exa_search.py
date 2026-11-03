"""Optional Exa web search.

The converter never depends on this. It powers the small "look this up" panel:
when a document type or a column name is unfamiliar, the user can ask Exa for a
definition without leaving the workspace. With no ``EXA_API_KEY`` the routes
report ``enabled: false`` and the UI hides the panel.
"""
from __future__ import annotations

import json
from urllib.request import Request, urlopen

from app import settings

EXA_ENDPOINT = "https://api.exa.ai/search"


def enabled() -> bool:
    return bool(settings.exa_api_key)


def search(query: str, num_results: int = 4, timeout: float = 12.0) -> dict:
    """Search the web through Exa and return plain ``{title, url, summary}`` rows."""
    query = (query or "").strip()
    if not query:
        return {"enabled": enabled(), "query": "", "results": [], "error": "empty query"}
    if not enabled():
        return {"enabled": False, "query": query, "results": [], "error": "EXA_API_KEY is not set"}

    payload = json.dumps(
        {
            "query": query,
            "numResults": max(1, min(int(num_results), 10)),
            "type": "auto",
            "contents": {"text": {"maxCharacters": 700}},
        }
    ).encode("utf-8")
    request = Request(
        EXA_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": settings.exa_api_key,
            "User-Agent": "firm-pdf-workspace",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:  # network, auth or JSON problems share one answer
        return {"enabled": True, "query": query, "results": [], "error": str(exc)}

    results = []
    for item in body.get("results") or []:
        text = (item.get("text") or "").strip().replace("\n", " ")
        results.append(
            {
                "title": item.get("title") or item.get("url") or "",
                "url": item.get("url") or "",
                "summary": text[:420],
            }
        )
    return {"enabled": True, "query": query, "results": results, "error": ""}
