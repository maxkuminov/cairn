"""OIDC relying party for the `oidc` login mode (oidc-sso-login, design D3/D4/D7).

Authorization code flow, confidential client, PKCE ``S256``, ``state`` + ``nonce``, RFC 9207 ``iss``.
Authlib supplies the PKCE challenge; joserfc verifies the ID token's signature. Everything that
decides admission — discovery checks, the algorithm allowlist, the claim profile — is explicit here
so it can be read and tested in one place.

Bounded transport: every provider call disables redirects and environment proxies
(``trust_env=False``), is bounded to 10 s in total, and reads at most 128 KiB of body before
parsing. A response that breaks a bound is refused with a closed reason and nothing is cached.

Failures carry a **closed** reason code (``REASONS``). Nothing in this module logs or raises with a
query string, code, token, state, nonce, verifier, client secret, subject or raw claim.

The transaction store is in-process and relies on ``cairn serve`` running ONE worker (it does; the
image's CMD passes ``--workers 1``). A restart drops in-flight logins; the user clicks again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.jwk import KeySet

from ..config import Settings

# Closed set of reason codes (design D7). Logs carry exactly one of these, never a value.
REASONS = frozenset(
    {
        "state",
        "iss_param",
        "token_exchange",
        "signature",
        "claims",
        "nonce",
        "unlinked",
        "inactive",
        "group",
        "capacity",
        "rate",
        "discovery",
        "config",
    }
)

ALLOWED_ALGS = ("RS256", "PS256", "ES256")
MAX_BODY_BYTES = 128 * 1024
HTTP_TIMEOUT = 10.0
DISCOVERY_TTL = 3600.0
JWKS_REFETCH_INTERVAL = 60.0
LEEWAY = 30
SUBJECT_MAX_LEN = 255
GROUPS_MAX = 256
GROUP_MAX_LEN = 512

TXN_TTL = 300.0
TXN_CAPACITY = 1024
TXN_RETRY_AFTER = 60

# Test hook: a factory returning the httpx transport provider calls use (``None`` = real network).
# Production never sets it.
transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None


class OIDCError(Exception):
    """A refused login step. ``reason`` is one of ``REASONS``; the message never carries a value."""

    def __init__(self, reason: str) -> None:
        assert reason in REASONS, reason
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------------------------
# bounded transport
# --------------------------------------------------------------------------------------------


async def _fetch_json(
    method: str, url: str, reason: str, *, data: dict | None = None, auth: Any = None
) -> dict:
    """One provider call under every bound in the module docstring; a JSON object or ``OIDCError``."""

    async def go() -> dict:
        kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": HTTP_TIMEOUT,
        }
        if transport_factory is not None:
            kwargs["transport"] = transport_factory()
        async with httpx.AsyncClient(**kwargs) as client:
            request_kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
            if data is not None:
                request_kwargs["data"] = data
            if auth is not None:
                request_kwargs["auth"] = auth
            async with client.stream(method, url, **request_kwargs) as resp:
                if resp.status_code < 200 or resp.status_code >= 300:
                    raise OIDCError(reason)
                declared = resp.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                    raise OIDCError(reason)
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BODY_BYTES:
                        raise OIDCError(reason)
        try:
            doc = json.loads(bytes(buf))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OIDCError(reason) from exc
        if not isinstance(doc, dict):
            raise OIDCError(reason)
        return doc

    try:
        return await asyncio.wait_for(go(), timeout=HTTP_TIMEOUT)
    except OIDCError:
        raise
    except (httpx.HTTPError, asyncio.TimeoutError, TimeoutError, OSError, ValueError) as exc:
        raise OIDCError(reason) from exc


def _endpoint_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(host)
        and "@" not in parts.netloc
        and not parts.fragment
        and "#" not in value
    )


# --------------------------------------------------------------------------------------------
# provider metadata
# --------------------------------------------------------------------------------------------


@dataclass
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: tuple[str, ...]
    iss_parameter_required: bool
    keys: KeySet
    fetched_at: float
    jwks_fetched_at: float


def _b64url_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass
class Transaction:
    nonce: str
    verifier: str
    next: str
    expires: float


class TransactionStore:
    """Single-use login transactions keyed by ``state`` (design D4).

    TTL 5 min, at most 1024 live entries, expired entries purged before admission, a full store
    refuses (the caller answers with ``Retry-After``) and never evicts a live entry. Consumption is
    one ``dict.pop`` on the event loop thread: atomic and single-use.
    """

    def __init__(self, capacity: int = TXN_CAPACITY, ttl: float = TXN_TTL) -> None:
        self.capacity = capacity
        self.ttl = ttl
        self._items: dict[str, Transaction] = {}

    def __len__(self) -> int:
        return len(self._items)

    def purge(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for key in [k for k, t in self._items.items() if t.expires <= now]:
            del self._items[key]

    def admit(self, state: str, nonce: str, verifier: str, next_path: str) -> bool:
        now = time.monotonic()
        self.purge(now)
        if len(self._items) >= self.capacity:
            return False
        self._items[state] = Transaction(nonce, verifier, next_path, now + self.ttl)
        return True

    def consume(self, state: str) -> Transaction | None:
        txn = self._items.pop(state, None)
        if txn is None or txn.expires <= time.monotonic():
            return None
        return txn


class RateLimiter:
    """Sliding one-minute windows: a process-global start budget and per-address limits (D4).

    The per-address key is ``request.client.host``, which is only as trustworthy as the forwarded
    header trust allows (design D8): a nuisance limit, not a control. The global budget is the bound.
    """

    WINDOW = 60.0
    MAX_KEYS = 4096

    def __init__(self, global_start: int = 60, per_address: int = 10) -> None:
        self.global_start = global_start
        self.per_address = per_address
        self._global: deque[float] = deque()
        self._addr: dict[tuple[str, str], deque[float]] = {}

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.WINDOW:
            q.popleft()

    def _addr_ok(self, kind: str, addr: str, now: float) -> bool:
        key = (kind, addr)
        q = self._addr.get(key)
        if q is None:
            if len(self._addr) >= self.MAX_KEYS:
                for k in list(self._addr):
                    self._prune(self._addr[k], now)
                    if not self._addr[k]:
                        del self._addr[k]
                if len(self._addr) >= self.MAX_KEYS:
                    return False
            q = self._addr[key] = deque()
        self._prune(q, now)
        if len(q) >= self.per_address:
            return False
        q.append(now)
        return True

    def allow_start(self, addr: str) -> bool:
        now = time.monotonic()
        self._prune(self._global, now)
        if len(self._global) >= self.global_start:
            return False
        if not self._addr_ok("start", addr, now):
            return False
        self._global.append(now)
        return True

    def allow_callback(self, addr: str) -> bool:
        return self._addr_ok("callback", addr, time.monotonic())


# --------------------------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------------------------


class OIDCClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.issuer: str = settings.oidc_issuer or ""
        self.client_id: str = settings.oidc_client_id or ""
        self.redirect_uri = f"{(settings.public_url or '').rstrip('/')}/auth/oidc/callback"
        self.transactions = TransactionStore()
        self.limits = RateLimiter()
        self._provider: Provider | None = None
        self._lock = asyncio.Lock()

    def _client_secret(self) -> str:
        secret = self.settings.oidc_client_secret
        return secret.get_secret_value() if secret is not None else ""

    # --- discovery + JWKS ---------------------------------------------------------------------

    async def provider(self) -> Provider:
        """Discovery + JWKS, lazily, cached for 1 h. Only a fully valid result is cached."""
        cached = self._provider
        if cached is not None and time.monotonic() - cached.fetched_at < DISCOVERY_TTL:
            return cached
        async with self._lock:
            cached = self._provider
            if cached is not None and time.monotonic() - cached.fetched_at < DISCOVERY_TTL:
                return cached
            self._provider = await self._discover()
            return self._provider

    async def _discover(self) -> Provider:
        url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
        doc = await _fetch_json("GET", url, "discovery")
        if doc.get("issuer") != self.issuer:
            raise OIDCError("discovery")
        endpoints = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(name)
            if not _endpoint_ok(value):
                raise OIDCError("discovery")
            endpoints[name] = value

        advertised = doc.get("id_token_signing_alg_values_supported")
        if advertised is None:
            algorithms = ALLOWED_ALGS
        elif isinstance(advertised, list) and all(isinstance(a, str) for a in advertised):
            algorithms = tuple(a for a in ALLOWED_ALGS if a in advertised)
        else:
            raise OIDCError("discovery")
        if not algorithms:
            raise OIDCError("config")

        methods = doc.get("token_endpoint_auth_methods_supported")
        if methods is not None:
            if not isinstance(methods, list):
                raise OIDCError("discovery")
            if self.settings.oidc_token_auth_method not in methods:
                raise OIDCError("config")

        keys = await self._fetch_keys(endpoints["jwks_uri"])
        now = time.monotonic()
        return Provider(
            issuer=self.issuer,
            authorization_endpoint=endpoints["authorization_endpoint"],
            token_endpoint=endpoints["token_endpoint"],
            jwks_uri=endpoints["jwks_uri"],
            algorithms=algorithms,
            iss_parameter_required=doc.get("authorization_response_iss_parameter_supported") is True,
            keys=keys,
            fetched_at=now,
            jwks_fetched_at=now,
        )

    async def _fetch_keys(self, jwks_uri: str) -> KeySet:
        doc = await _fetch_json("GET", jwks_uri, "discovery")
        raw = doc.get("keys")
        if not isinstance(raw, list):
            raise OIDCError("discovery")
        usable = []
        for item in raw:
            if not isinstance(item, dict) or item.get("kty") not in ("RSA", "EC"):
                continue
            if item.get("use") not in (None, "sig"):
                continue
            usable.append(item)
        if not usable:
            raise OIDCError("discovery")
        try:
            return KeySet.import_key_set({"keys": usable})
        except Exception as exc:  # noqa: BLE001 - any malformed key refuses the whole set
            raise OIDCError("discovery") from exc

    async def _refetch_keys(self, provider: Provider) -> bool:
        """Refetch the JWKS for an unknown ``kid`` — at most once per 60 s."""
        async with self._lock:
            if time.monotonic() - provider.jwks_fetched_at < JWKS_REFETCH_INTERVAL:
                return False
            provider.jwks_fetched_at = time.monotonic()
            try:
                provider.keys = await self._fetch_keys(provider.jwks_uri)
            except OIDCError:
                return False
            return True

    # --- the flow -----------------------------------------------------------------------------

    @staticmethod
    def new_secrets() -> tuple[str, str, str]:
        """``(state, nonce, verifier)``: 32 random bytes each for state/nonce, a 64-byte verifier."""
        return secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)

    def authorization_url(self, provider: Provider, state: str, nonce: str, verifier: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.settings.oidc_scopes()),
            "state": state,
            "nonce": nonce,
            "code_challenge": create_s256_code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        endpoint = provider.authorization_endpoint
        sep = "&" if urlsplit(endpoint).query else "?"
        return endpoint + sep + urlencode(params)

    def check_iss_parameter(self, provider: Provider, iss: str | None) -> None:
        """RFC 9207: required when advertised; whenever present it must equal the issuer."""
        if iss is None:
            if provider.iss_parameter_required:
                raise OIDCError("iss_param")
            return
        if not secrets.compare_digest(iss.encode("utf-8"), self.issuer.encode("utf-8")):
            raise OIDCError("iss_param")

    async def exchange(self, provider: Provider, code: str, verifier: str) -> str:
        """Exchange the code (with the PKCE verifier) for an ID token; returns the raw token."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        auth = None
        if self.settings.oidc_token_auth_method == "client_secret_post":
            data["client_id"] = self.client_id
            data["client_secret"] = self._client_secret()
        else:
            auth = httpx.BasicAuth(self.client_id, self._client_secret())
        doc = await _fetch_json("POST", provider.token_endpoint, "token_exchange", data=data, auth=auth)
        id_token = doc.get("id_token")
        if not isinstance(id_token, str) or not id_token or len(id_token) > MAX_BODY_BYTES:
            raise OIDCError("token_exchange")
        return id_token

    async def verify_id_token(self, provider: Provider, id_token: str, nonce: str) -> dict:
        """Signature, algorithm and the full claim profile of design D3. Returns the claims."""
        parts = id_token.split(".")
        if len(parts) != 3:
            raise OIDCError("signature")
        try:
            header = _b64url_json(parts[0])
        except Exception as exc:  # noqa: BLE001
            raise OIDCError("signature") from exc
        alg = header.get("alg")
        # `none` and every HS* are refused here whatever the provider advertises: they can never be
        # in `provider.algorithms`, which is a subset of ALLOWED_ALGS.
        if not isinstance(alg, str) or alg not in provider.algorithms:
            raise OIDCError("signature")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise OIDCError("signature")

        key = self._pick_key(provider.keys, kid, alg)
        if key is None and kid is not None and await self._refetch_keys(provider):
            key = self._pick_key(provider.keys, kid, alg)
        if key is None:
            raise OIDCError("signature")
        try:
            token = jwt.decode(id_token, key, algorithms=[alg])
        except Exception as exc:  # noqa: BLE001 - any verification failure is a signature refusal
            raise OIDCError("signature") from exc
        claims = dict(token.claims)
        self._check_claims(claims, nonce)
        return claims

    @staticmethod
    def _pick_key(keys: KeySet, kid: str | None, alg: str):
        want = "EC" if alg.startswith("ES") else "RSA"
        candidates = [k for k in keys if k.key_type == want]
        if kid is not None:
            candidates = [k for k in candidates if k.kid == kid]
        elif len(candidates) != 1:
            return None
        if len(candidates) != 1:
            return None
        key = candidates[0]
        key_alg = key.get("alg") if hasattr(key, "get") else None
        if key_alg is not None and key_alg != alg:
            return None
        return key

    def _check_claims(self, claims: dict, nonce: str) -> None:
        cid = self.client_id
        if claims.get("iss") != self.issuer:
            raise OIDCError("claims")
        aud = claims.get("aud")
        azp = claims.get("azp")
        if isinstance(aud, str):
            if aud != cid:
                raise OIDCError("claims")
        elif isinstance(aud, list):
            if not aud or not all(isinstance(a, str) for a in aud) or cid not in aud:
                raise OIDCError("claims")
            # OIDC Core 3.1.3.7: several audiences are acceptable only when the authorized party
            # names this client.
            if len(aud) > 1 and azp != cid:
                raise OIDCError("claims")
        else:
            raise OIDCError("claims")
        if azp is not None and azp != cid:
            raise OIDCError("claims")
        exp, iat = claims.get("exp"), claims.get("iat")
        if not (_is_int(exp) and _is_int(iat)):
            raise OIDCError("claims")
        now = int(time.time())
        if exp <= now - LEEWAY or iat > now + LEEWAY:
            raise OIDCError("claims")
        got_nonce = claims.get("nonce")
        if not isinstance(got_nonce, str) or not secrets.compare_digest(
            got_nonce.encode("utf-8"), nonce.encode("utf-8")
        ):
            raise OIDCError("nonce")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > SUBJECT_MAX_LEN:
            raise OIDCError("claims")

    def group_ok(self, claims: dict) -> bool:
        """Design D10: unset = no check; else an array (≤ 256 strings ≤ 512 chars) with an exact match."""
        required = self.settings.oidc_required_group
        if not required:
            return True
        groups = claims.get(self.settings.oidc_groups_claim)
        if not isinstance(groups, list) or len(groups) > GROUPS_MAX:
            return False
        if not all(isinstance(g, str) and len(g) <= GROUP_MAX_LEN for g in groups):
            return False
        return required in groups
