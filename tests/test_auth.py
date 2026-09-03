import httpx
import pytest

from openfsd_injector.auth import jwt_url, mint_fsd_jwt, resolve_token
from openfsd_injector.config import (
    AppConfig,
    AuthConfig,
    MissingCredentialError,
    ServerConfig,
)

# Obviously fake placeholders. Never put a real credential in a fixture.
FAKE_CID = 999999
FAKE_PASSWORD = "fake-password-not-a-real-credential"


def test_jwt_url_ops_aero_port():
    assert jwt_url("http://127.0.0.1:8010") == "http://127.0.0.1:8010/api/v1/fsd-jwt"
    assert jwt_url("http://fsdweb:8010/") == "http://fsdweb:8010/api/v1/fsd-jwt"


@pytest.mark.asyncio
async def test_resolve_token_plaintext_when_no_api_base():
    cfg = AppConfig(
        server=ServerConfig(api_base=""),
        auth=AuthConfig(cid=FAKE_CID, password=FAKE_PASSWORD),
    )
    assert await resolve_token(cfg) == FAKE_PASSWORD


@pytest.mark.asyncio
async def test_resolve_token_prefers_configured_token():
    cfg = AppConfig(
        server=ServerConfig(api_base="http://127.0.0.1:8010"),
        auth=AuthConfig(cid=FAKE_CID, token="fake.jwt.value"),
    )
    assert await resolve_token(cfg) == "fake.jwt.value"


@pytest.mark.asyncio
async def test_resolve_token_fails_fast_without_credential():
    cfg = AppConfig(server=ServerConfig(api_base=""), auth=AuthConfig(cid=FAKE_CID))
    with pytest.raises(MissingCredentialError, match="no injector credential"):
        await resolve_token(cfg)


@pytest.mark.asyncio
async def test_resolve_token_without_credential_makes_no_request(monkeypatch):
    """With api_base set but no password, fail before touching the network."""

    class Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("no HTTP call may be made without a credential")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    cfg = AppConfig(
        server=ServerConfig(api_base="http://127.0.0.1:8010"),
        auth=AuthConfig(cid=FAKE_CID),
    )
    with pytest.raises(MissingCredentialError):
        await resolve_token(cfg)


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
            assert json == {"cid": str(FAKE_CID), "password": FAKE_PASSWORD}
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    token = await mint_fsd_jwt("http://127.0.0.1:8010", FAKE_CID, FAKE_PASSWORD)
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
        await mint_fsd_jwt("http://127.0.0.1:8000", FAKE_CID, FAKE_PASSWORD)
