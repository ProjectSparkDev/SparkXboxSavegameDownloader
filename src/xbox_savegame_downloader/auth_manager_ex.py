"""
Authentication Manager

Authenticate with Windows Live Server and Xbox Live.
"""
import logging
import uuid
from typing import List, Optional

import httpx

from xbox.webapi.authentication.models import (
    OAuth2TokenResponse,
    XAUResponse,
    XSTSResponse,
    XADResponse
)
from xbox.webapi.common.exceptions import AuthenticationException
from xbox.webapi.common.signed_session import SignedSession

log = logging.getLogger("authentication")

DEFAULT_SCOPES = ["Xboxlive.signin", "Xboxlive.offline_access"]


class AuthenticationManagerEx:
    def __init__(
        self,
        client_session: SignedSession,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: Optional[List[str]] = None,
        device_id: str = None
    ):
        if not isinstance(client_session, (SignedSession, httpx.AsyncClient)):
            raise DeprecationWarning(
                """Xbox WebAPI changed to use SignedSession (wrapped httpx.AsyncClient).
                Please check the documentation"""
            )

        self.session: SignedSession = client_session
        self._client_id: str = client_id
        self._client_secret: str = client_secret
        self._redirect_uri: str = redirect_uri
        self._scopes: List[str] = scopes or DEFAULT_SCOPES
        self.device_id: str = device_id or str(uuid.uuid4())

        self.oauth: OAuth2TokenResponse = None
        self.user_token: XAUResponse = None
        self.xsts_token: XSTSResponse = None
        self.device_token: XADResponse = None

    def generate_authorization_url(self, state: Optional[str] = None) -> str:
        """Generate Windows Live Authorization URL."""
        query_params = {
            "client_id": self._client_id,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": " ".join(self._scopes),
            "redirect_uri": self._redirect_uri,
        }

        if state:
            query_params["state"] = state

        return str(
            httpx.URL(
                "https://login.live.com/oauth20_authorize.srf", params=query_params
            )
        )

    async def request_tokens(self, authorization_code: str) -> None:
        """Request all tokens."""
        self.oauth = await self.request_oauth_token(authorization_code)
        self.device_token = await self.request_device_token()
        self.user_token = await self.request_user_token()
        self.xsts_token = await self.request_xsts_token()

    async def refresh_tokens(self) -> None:
        """Refresh all tokens."""
        if not (self.oauth and self.oauth.is_valid()):
            self.oauth = await self.refresh_oauth_token()
        if not (self.device_token and self.device_token.is_valid()):
            self.device_token = await self.request_device_token()
        if not (self.user_token and self.user_token.is_valid()):
            self.user_token = await self.request_user_token()
        if not (self.xsts_token and self.xsts_token.is_valid()):
            self.xsts_token = await self.request_xsts_token()

    async def request_oauth_token(self, authorization_code: str) -> OAuth2TokenResponse:
        """Request OAuth2 token."""
        return await self._oauth2_token_request(
            {
                "grant_type": "authorization_code",
                "code": authorization_code,
                "scope": " ".join(self._scopes),
                "redirect_uri": self._redirect_uri,
            }
        )

    async def refresh_oauth_token(self) -> OAuth2TokenResponse:
        """Refresh OAuth2 token."""
        return await self._oauth2_token_request(
            {
                "grant_type": "refresh_token",
                "scope": " ".join(self._scopes),
                "refresh_token": self.oauth.refresh_token,
            }
        )

    async def _oauth2_token_request(self, data: dict) -> OAuth2TokenResponse:
        """Execute token requests."""
        data["client_id"] = self._client_id
        if self._client_secret:
            data["client_secret"] = self._client_secret
        resp = await self.session.post(
            "https://login.live.com/oauth20_token.srf", data=data
        )
        resp.raise_for_status()
        return OAuth2TokenResponse(**resp.json())

    async def request_device_token(
        self,
        relying_party: str = "http://auth.xboxlive.com",
    ) -> XADResponse:
        """Authenticate via access token and receive user token."""
        url = "https://device.auth.xboxlive.com/device/authenticate"
        headers = {"x-xbl-contract-version": "1"}
        data = {
            "RelyingParty": relying_party,
            "TokenType": "JWT",
            "Properties": {
                "AuthMethod": "ProofOfPossession",
                "Id": self.device_id,
                "DeviceType": "Win32",
                "Version": "10.0.22621.0",
                "ProofKey": self.session.request_signer.proof_field
            },
        }

        resp = await self.session.send_signed("POST", url, json=data, headers=headers)
        resp.raise_for_status()
        return XADResponse(**resp.json())

    async def request_user_token(
        self,
        relying_party: str = "http://auth.xboxlive.com",
        use_compact_ticket: bool = False,
    ) -> XAUResponse:
        """Authenticate via access token and receive user token."""
        url = "https://user.auth.xboxlive.com/user/authenticate"
        headers = {"x-xbl-contract-version": "1"}
        data = {
            "RelyingParty": relying_party,
            "TokenType": "JWT",
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": self.oauth.access_token
                if use_compact_ticket
                else f"d={self.oauth.access_token}",
            },
        }

        resp = await self.session.send_signed("POST", url, json=data, headers=headers)
        resp.raise_for_status()
        return XAUResponse(**resp.json())

    async def request_xsts_token(
        self, relying_party: str = "http://xboxlive.com"
    ) -> XSTSResponse:
        """Authorize via user token and receive final X token."""
        url = "https://xsts.auth.xboxlive.com/xsts/authorize"
        headers = {"x-xbl-contract-version": "1"}
        data = {
            "RelyingParty": relying_party,
            "TokenType": "JWT",
            "Properties": {
                "UserTokens": [self.user_token.token],
                "DeviceToken": self.device_token.token,
                "SandboxId": "RETAIL",
            },
        }

        resp = await self.session.send_signed("POST", url, json=data, headers=headers)
        if resp.status_code == 401:  # if unauthorized
            print(
                "Failed to authorize you! Your password or username may be wrong or you are trying to use child account (< 18 years old)"
            )
            raise AuthenticationException()
        resp.raise_for_status()
        return XSTSResponse(**resp.json())
