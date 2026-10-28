"""Optional LLM judge that confirms or rejects contradiction candidates.

The model must quote both conflicting claims verbatim; any quote not found in the
source text is rejected, so hallucinated findings never reach a report.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

from app import config

log = logging.getLogger(__name__)

SYSTEM = (
    "You audit documentation for contradictions. Two passages come from different pages of the same "
    "website. Decide whether they make claims that cannot both be true for the same product, plan, "
    "region and version (e.g. different refund windows, prices, limits, steps, or requirements). "
    "Differences explained by different plans, versions, regions or contexts are NOT contradictions. "
    "Missing information is NOT a contradiction. Respond with JSON only."
)

USER_TMPL = """Passage A ({url_a}, section "{head_a}"):
<<<
{text_a}
>>>

Passage B ({url_b}, section "{head_b}"):
<<<
{text_b}
>>>

Return exactly this JSON object:
{{"contradiction": true|false, "confidence": 0.0-1.0,
  "claim_a": "exact sentence copied from A", "claim_b": "exact sentence copied from B",
  "explanation": "one short sentence for a docs owner"}}"""


@dataclass
class Verdict:
    contradiction: bool
    confidence: float
    claim_a: str
    claim_b: str
    explanation: str


def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def quote_in(quote: str, text: str) -> bool:
    q = _squash(quote)
    return len(q) >= 12 and q in _squash(text)


def _parse(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _call(client: httpx.AsyncClient, prompt: str, *, system: str | None = None, max_tokens: int = 400) -> str:
    s = config.settings
    if s.llm_provider == "anthropic":
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": s.llm_model, "max_tokens": max_tokens, "system": system or SYSTEM,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        return "".join(part.get("text", "") for part in r.json().get("content", []))
    r = await client.post(
        f"{s.openai_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {s.openai_api_key}"},
        json={"model": s.llm_model, "max_tokens": max_tokens, "temperature": 0,
              "messages": [{"role": "system", "content": system or SYSTEM}, {"role": "user", "content": prompt}]},
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def judge(client: httpx.AsyncClient, a: dict, b: dict) -> Verdict | None:
    """a/b: {"url","heading","text"}. Returns a validated verdict or None."""
    prompt = USER_TMPL.format(url_a=a["url"], head_a=a["heading"][:120], text_a=a["text"][:2500],
                              url_b=b["url"], head_b=b["heading"][:120], text_b=b["text"][:2500])
    try:
        raw = await _call(client, prompt)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("LLM call failed: %s", e)
        return None
    data = _parse(raw)
    if not data:
        return None
    try:
        v = Verdict(bool(data.get("contradiction")), float(data.get("confidence", 0)),
                    str(data.get("claim_a", "")), str(data.get("claim_b", "")), str(data.get("explanation", "")))
    except (TypeError, ValueError):
        return None
    if v.contradiction and not (quote_in(v.claim_a, a["text"]) and quote_in(v.claim_b, b["text"])):
        log.info("rejected verdict with unverifiable quotes")
        return None
    return v


async def judge_many(pairs: list[tuple[dict, dict]], *, concurrency: int = 4,
                     client: httpx.AsyncClient | None = None) -> list[Verdict | None]:
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    sem = asyncio.Semaphore(concurrency)

    async def one(pair):
        async with sem:
            return await judge(client, *pair)

    try:
        return await asyncio.gather(*(one(p) for p in pairs))
    finally:
        if own:
            await client.aclose()
