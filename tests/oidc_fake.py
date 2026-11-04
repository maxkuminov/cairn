"""A local fake OIDC provider for the `oidc` tests (oidc-sso-login, task B.7).

Signing keys are generated in the test process and never committed. The provider is an
``httpx.MockTransport`` handler installed through ``src.auth.oidc.transport_factory``, so no network
is touched. Placeholders only: ``https://idp.example.com/realms/example``, client ID ``cairn``.

The authorization code is self-describing — ``<CODE_MARKER>.<nonce>.<subject>`` — so the provider
needs no shared state with the test driving the browser side, and a server running in another
process (the real-server log test) can use the same provider.
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs

import httpx
from joserfc import jwt
from joserfc.jwk import ECKey, OctKey, RSAKey

ISSUER = "https://idp.example.com/realms/example"
CLIENT_ID = "cairn"
CLIENT_SECRET = "zz-client-secret-RECOGNISABLE-0001"
SECRET_KEY = "zz-session-secret-key-RECOGNISABLE-0002-abcdef"
PUBLIC_URL = "https://cairn.example.com"
LINKED_SUB = "zz-linked-subject-0003"
CODE_MARKER = "zzCODEmarker0004"

DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
AUTH_URL = "https://idp.example.com/auth"
TOKEN_URL = "https://idp.example.com/token"
JWKS_URL = "https://keys.example.net/jwks"  # a different host, on purpose (design D3)


def oidc_env(**overrides: str) -> dict[str, str]:
    env = {
        "CAIRN_AUTH_MODE": "oidc",
        "CAIRN_OIDC_ISSUER": ISSUER,
        "CAIRN_OIDC_CLIENT_ID": CLIENT_ID,
        "CAIRN_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "CAIRN_PUBLIC_URL": PUBLIC_URL,
        "CAIRN_SECRET_KEY": SECRET_KEY,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def make_code(nonce: str, sub: str = LINKED_SUB) -> str:
    return f"{CODE_MARKER}.{nonce}.{sub}"


class FakeProvider:
    def __init__(self) -> None:
        self.rsa = RSAKey.generate_key(2048, parameters={"kid": "rsa-1", "use": "sig"})
        self.ec = ECKey.generate_key("P-256", parameters={"kid": "ec-1", "use": "sig"})
        self.alg = "RS256"
        self.discovery_extra: dict = {}
        self.claim_overrides: dict = {}
        self.drop_claims: set[str] = set()
        self.header_overrides: dict = {}
        self.raw_id_token: str | None = None
        self.overrides: dict[str, httpx.Response] = {}  # url -> canned response
        self.fail_all = False
        self.requests: list[httpx.Request] = []
        self.token_forms: list[dict] = []
        self.jwks_keys: list | None = None

    # --- transport ----------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, url: str) -> int:
        return sum(1 for r in self.requests if str(r.url).split("?")[0] == url)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_all:
            raise httpx.ConnectError("provider unreachable", request=request)
        url = str(request.url).split("?")[0]
        if url in self.overrides:
            return self.overrides[url]
        if url == DISCOVERY_URL:
            return httpx.Response(200, json=self.discovery())
        if url == JWKS_URL:
            keys = self.jwks_keys
            if keys is None:
                keys = [self.rsa.as_dict(private=False), self.ec.as_dict(private=False)]
            return httpx.Response(200, json={"keys": keys})
        if url == TOKEN_URL and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append({**form, "_authorization": request.headers.get("authorization")})
            code = form.get("code", "")
            parts = code.split(".")
            if len(parts) != 3 or parts[0] != CODE_MARKER:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": "zz-access-token-RECOGNISABLE-0005",
                "token_type": "Bearer",
                "id_token": self.raw_id_token or self.id_token(nonce=parts[1], sub=parts[2]),
            })
        return httpx.Response(404)

    # --- documents ----------------------------------------------------------------------------

    def discovery(self) -> dict:
        doc = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTH_URL,
            "token_endpoint": TOKEN_URL,
            "jwks_uri": JWKS_URL,
            "id_token_signing_alg_values_supported": ["RS256", "PS256", "ES256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "authorization_response_iss_parameter_supported": True,
        }
        doc.update(self.discovery_extra)
        return {k: v for k, v in doc.items() if v is not None}

    def claims(self, nonce: str, sub: str) -> dict:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": sub,
            "nonce": nonce,
            "iat": now,
            "exp": now + 300,
            "email": "operator@example.com",
            "groups": ["/cairn-admins"],
        }
        claims.update(self.claim_overrides)
        for name in self.drop_claims:
            claims.pop(name, None)
        return claims

    def id_token(self, nonce: str, sub: str = LINKED_SUB) -> str:
        claims = self.claims(nonce, sub)
        alg = self.alg
        header = {"alg": alg, "kid": "ec-1" if alg.startswith("ES") else "rsa-1"}
        header.update(self.header_overrides)
        header = {k: v for k, v in header.items() if v is not None}
        if alg == "none":
            seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
            return f"{seg(header)}.{seg(claims)}."
        if alg.startswith("HS"):
            key = OctKey.import_key(CLIENT_SECRET.encode())
        elif alg.startswith("ES"):
            key = self.ec
        else:
            key = self.rsa
        return jwt.encode(header, claims, key, algorithms=[alg])
