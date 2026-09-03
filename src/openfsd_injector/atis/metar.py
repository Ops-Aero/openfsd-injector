from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def fetch_metar(url_template: str, icao: str) -> str:
    url = url_template.format(icao=icao)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "openfsd-injector/0.1"})
        resp.raise_for_status()
    text = resp.text.strip().replace("\n", " ")
    if not text:
        raise RuntimeError(f"empty METAR for {icao}")
    log.info("METAR %s: %s", icao, text)
    return text
