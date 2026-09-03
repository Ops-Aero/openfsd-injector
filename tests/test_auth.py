import httpx
import pytest

from openfsd_injector.auth import jwt_url, mint_fsd_jwt, resolve_token
from openfsd_injector.config import AppConfig, AuthConfig, ServerConfig


def test_jwt_url_ops_aero_port():
    assert jwt_url("http://127.0.0.1:8010") == "http://127.0.0.1:8010/api/v1/fsd-jwt"
    assert jwt_url("http://fsdweb:8010/") == "http://fsdweb:8010/api/v1/fsd-jwt"


@pytest.mark.asyncio
async def test_resolve_token_plaintext_when_no_api_base():
    cfg = AppConfig(
        server=ServerConfig(api_base=""),
        auth=AuthConfig(cid=1, password="opsaeroadmin"),
    )
    assert await resolve_token(cfg) == "opsaeroadmin"


@pytest.mark.asyncio
async def test_mint_fsd_jwt_reads_openfsd_body(monkeypatch):
    class FakeResp:
        status_code = 200
        is_error = False
        text = ""

        def json(self):
            return {"success": True, "token": "hdr.payload.sig"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            assert url.endswith("/api/v1/fsd-jwt")
            assert json == {"cid": "1", "password": "opsaeroadmin"}
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    token = await mint_fsd_jwt("http://127.0.0.1:8010", 1, "opsaeroadmin")
    assert token == "hdr.payload.sig"


@pytest.mark.asyncio
async def test_mint_fsd_jwt_hints_wrong_laravel_port(monkeypatch):
    class FakeResp:
        status_code = 404
        is_error = True
        text = "Not Found"

        def json(self):
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(RuntimeError, match="8010"):
        await mint_fsd_jwt("http://127.0.0.1:8000", 1, "opsaeroadmin")
