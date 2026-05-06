"""
OAuth2 token management for the Domain API.
"""
import time
from typing import Optional
import httpx
from dotenv import load_dotenv
import os

load_dotenv()

TOKEN_URL = "https://auth.domain.com.au/v1/connect/token"


class DomainAuth:
    def __init__(self):
        self.client_id = os.getenv("DOMAIN_CLIENT_ID")
        self.client_secret = os.getenv("DOMAIN_CLIENT_SECRET")
        self._token: Optional[str] = None
        self._token_expiry: float = 0

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Missing DOMAIN_CLIENT_ID or DOMAIN_CLIENT_SECRET in .env file.\n"
                "Register at https://developer.domain.com.au to get credentials."
            )

    def get_token(self) -> str:
        """Return a valid access token, refreshing if expired."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": "api_listings_read api_agencies_read api_suburbPerformance_read",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        response.raise_for_status()
        data = response.json()

        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        return self._token

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}
