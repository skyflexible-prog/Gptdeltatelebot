import time
import json
import hmac
import hashlib
from typing import Dict, Any, Optional, List
import requests

class DeltaClient:
    """
    Minimal Delta Exchange India REST client (HMAC-SHA256 signing).
    Reference: Sign = HMAC(secret, method + timestamp + path + query_string + payload)
    Headers: api-key, timestamp, signature, Content-Type: application/json
    """
    def __init__(self, base_url: str, api_key: str, api_secret: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret

    def _sign(self, method: str, path: str, query_string: str, payload: str, timestamp: str) -> str:
        message = method + timestamp + path + query_string + payload
        return hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None, auth: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        params = params or {}
        payload = json.dumps(body) if body else ""
        query_string = ""
        if params:
            # requests handles params; but signature needs canonical '?k=v&...'
            from urllib.parse import urlencode
            query_string = "?" + urlencode(params)

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            timestamp = str(int(time.time()))
            signature = self._sign(method, path, query_string, payload, timestamp)
            headers.update({"api-key": self.api_key, "timestamp": timestamp, "signature": signature})

        resp = requests.request(method, url, params=params, data=payload if body else None, headers=headers, timeout=(5, 30))
        resp.raise_for_status()
        return resp.json()

    # Market data
    def get_products(self) -> Any:
        return self._request("GET", "/v2/products")

    def get_tickers(self, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", "/v2/tickers", params=params)

    def get_ticker_symbol(self, symbol: str) -> Any:
        return self._request("GET", f"/v2/tickers/{symbol}")

    # Orders
    def place_order(self, body: Dict[str, Any]) -> Any:
        return self._request("POST", "/v2/orders", body=body, auth=True)

    def get_orders(self, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", "/v2/orders", params=params, auth=True)

    def amend_order(self, body: Dict[str, Any]) -> Any:
        return self._request("PUT", "/v2/orders", body=body, auth=True)
