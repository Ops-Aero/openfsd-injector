"""Mint an openFSD FSD JWT, falling back to a plaintext password token."""

from __future__ import annotations

import logging

import httpx

from .config import AppConfig

log = logging.getLogger(__name__)


async def resolve_token(cfg: AppConfig) -> str:
    if cfg.auth.token:
        return cfg.auth.token
    if cfg.server.api_base:
        url = f"{cfg.server.api_base}/api/v1/fsd-jwt"
        payload = {"cid": str(cfg.auth.cid), "password": cfg.auth.password}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        if not body.get("success"):
            raise RuntimeError(body.get("error_msg") or f"JWT mint failed at {url}")
        token = body.get("token")
        if not token:
            raise RuntimeError("JWT response missing token")
        log.info("minted FSD JWT from %s", url)
        return token
    if not cfg.auth.password:
        raise RuntimeError("set auth.password, auth.token, or server.api_base")
    log.warning("no api_base configured — sending plaintext password as FSD token")
    return cfg.auth.password
