"""Mint an openFSD FSD JWT, falling back to a plaintext password token.

OpsAero / openFSD always accept the CID password as the ``#AA`` token.
JWT minting is optional and only used when ``server.api_base`` is set.

The FSD JWT from ``POST /api/v1/fsd-jwt`` expires in five minutes and is
checked only at logon, so it must be minted immediately before ``#AA``.
"""

from __future__ import annotations

import logging

import httpx

from .config import AppConfig

log = logging.getLogger(__name__)


def jwt_url(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/api/v1/fsd-jwt"


async def mint_fsd_jwt(api_base: str, cid: int, password: str) -> str:
    url = jwt_url(api_base)
    payload = {"cid": str(cid), "password": password}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.is_error:
            hint = ""
            if resp.status_code == 404:
                hint = (
                    " (OpsAero: point api_base at the openfsd admin API on :8010, "
                    "not the Laravel website on :8000)"
                )
            detail = body.get("error_msg") or body.get("error") or resp.text[:200]
            raise RuntimeError(f"JWT mint failed at {url}: HTTP {resp.status_code} {detail}{hint}")
    if isinstance(body, dict) and body.get("success") is False:
        raise RuntimeError(body.get("error_msg") or f"JWT mint failed at {url}")
    token = body.get("token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError(
            f"JWT response missing token from {url}. "
            "On OpsAero this endpoint lives on fsdweb (:8010), not the website (:8000)."
        )
    log.info("minted FSD JWT from %s", url)
    return token


async def resolve_token(cfg: AppConfig) -> str:
    if cfg.auth.token:
        return cfg.auth.token
    if cfg.server.api_base:
        return await mint_fsd_jwt(cfg.server.api_base, cfg.auth.cid, cfg.auth.password)
    if not cfg.auth.password:
        raise RuntimeError("set auth.password, auth.token, or server.api_base")
    log.info("no api_base configured — sending CID password as FSD token (supported by openFSD/OpsAero)")
    return cfg.auth.password
