"""
services/mpesa_service.py
──────────────────────────
Handles all communication with the Safaricom Daraja API.

PERFORMANCE:
  • Shared httpx.Client with connection pooling (reuses TCP connections)
  • Re-usable credentials encoded once at module load
  • Token cached with 60s buffer, validated in O(1)
  • Password generation uses pre-encoded passkey where possible

FLOW:
  1. get_access_token()     → OAuth2 token (valid 1 hour, cached)
  2. stk_push()             → Sends payment prompt to customer's phone
  3. query_stk_status()     → Polls transaction status (fallback if callback missed)
  4. parse_callback()       → Validates and extracts data from Safaricom callback
"""

import base64
import logging
from datetime import datetime, timezone
from time import time as _time

import httpx

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Shared HTTP client (connection pooling) ────────────────────────────────
# httpx will keep connections alive and reuse them, avoiding TCP handshake
# overhead on every call. Timeout=15 is generous; connection pooling handles
# the rest.
_client = httpx.Client(timeout=15, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))

# ── Pre-encoded credentials ─────────────────────────────────────────────────
_KEY = settings.MPESA_CONSUMER_KEY.get_secret_value()
_SECRET = settings.MPESA_CONSUMER_SECRET.get_secret_value()
_BASIC_AUTH = base64.b64encode(f"{_KEY}:{_SECRET}".encode()).decode()

# ── Token cache ─────────────────────────────────────────────────────────────
# Safaricom tokens are valid for 3600 seconds. We store the token and its
# expiry time so we only re-fetch when it has expired.
_token: str | None = None
_token_expires_at: float = 0
_TOKEN_BUFFER = 60  # seconds before expiry to refresh


def get_access_token() -> str:
    """
    Fetch (or return cached) Daraja OAuth2 access token.
    Safaricom requires Basic Auth with Consumer Key + Secret.

    Returns cached token if still valid (>60s buffer before expiry).
    """
    global _token, _token_expires_at

    now = _time()
    if _token is not None and now < _token_expires_at - _TOKEN_BUFFER:
        return _token

    url = f"{settings.MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials"

    response = _client.get(
        url,
        headers={"Authorization": f"Basic {_BASIC_AUTH}"},
    )
    response.raise_for_status()
    data = response.json()

    _token = data["access_token"]
    _token_expires_at = now + int(data.get("expires_in", 3600))

    logger.info("Daraja: fetched new access token (expires in %ds)", int(data.get("expires_in", 3600)))
    return _token


def _generate_password() -> tuple[str, str]:
    """
    Daraja STK Push password = base64(shortcode + passkey + timestamp).
    Returns (password, timestamp) — both needed in the request body.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shortcode = settings.MPESA_SHORTCODE
    passkey = settings.MPESA_PASSKEY.get_secret_value()
    raw = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def stk_push(
    phone_number: str,
    amount: int,
    account_reference: str,
    description: str,
) -> dict:
    """
    Initiate an STK Push — sends a payment prompt to the customer's phone.
    Uses the shared httpx client for connection reuse.

    Args:
        phone_number:      Customer phone in 254XXXXXXXXX format (no +, no 0)
        amount:            Amount in KES, must be a whole number (int)
        account_reference: Short label shown to customer (e.g. "INV-001")
        description:       Brief description (e.g. "Pharmacy payment")

    Returns:
        Safaricom response dict containing CheckoutRequestID and MerchantRequestID.
        Store CheckoutRequestID — you need it to match the callback.

    Raises:
        httpx.HTTPStatusError: on Safaricom API errors
        ValueError: if phone number format is invalid
    """
    phone_number = _normalise_phone(phone_number)
    password, timestamp = _generate_password()
    token = get_access_token()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerBuyGoodsOnline",
        "Amount": int(amount),
        "PartyA": phone_number,
        "PartyB": settings.MPESA_SHORTCODE_TYPE,
        "PhoneNumber": phone_number,
        "CallBackURL": settings.MPESA_CALLBACK_URL,
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    }

    url = f"{settings.MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest"

    response = _client.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    response.raise_for_status()
    data = response.json()

    logger.info(
        "STK Push initiated: CheckoutRequestID=%s phone=%s amount=%d",
        data.get("CheckoutRequestID"), phone_number, amount,
    )
    return data


def query_stk_status(checkout_request_id: str) -> dict:
    """
    Query the status of an STK Push transaction.
    Use this as a fallback if the callback was not received within ~60 seconds.
    Uses the shared httpx client for connection reuse.
    """
    password, timestamp = _generate_password()
    token = get_access_token()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }

    url = f"{settings.MPESA_BASE_URL}/mpesa/stkpushquery/v1/query"

    response = _client.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    response.raise_for_status()
    return response.json()


def parse_callback(body: dict) -> dict:
    """
    Extract meaningful fields from a Safaricom STK callback body.

    Returns a normalised dict with:
        success         bool
        checkout_request_id   str
        mpesa_receipt   str | None   (M-Pesa transaction code, e.g. "QJ12345KLM")
        amount          int | None
        phone           str | None
        transaction_date str | None
        result_desc     str          (human-readable status message)
    """
    stk_callback = body.get("Body", {}).get("stkCallback", {})
    result_code = stk_callback.get("ResultCode")
    result_desc = stk_callback.get("ResultDesc", "Unknown")
    checkout_request_id = stk_callback.get("CheckoutRequestID")

    success = result_code == 0

    mpesa_receipt = None
    amount = None
    phone = None
    transaction_date = None

    if success:
        items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
        meta = {item["Name"]: item.get("Value") for item in items}
        mpesa_receipt = meta.get("MpesaReceiptNumber")
        amount = meta.get("Amount")
        phone = str(meta.get("PhoneNumber", ""))
        transaction_date = str(meta.get("TransactionDate", ""))

    return {
        "success": success,
        "checkout_request_id": checkout_request_id,
        "result_code": result_code,
        "result_desc": result_desc,
        "mpesa_receipt": mpesa_receipt,
        "amount": amount,
        "phone": phone,
        "transaction_date": transaction_date,
    }


def _normalise_phone(phone: str) -> str:
    """
    Convert any Kenyan phone format to 254XXXXXXXXX.
      07XXXXXXXX  → 2547XXXXXXXX
      +2547XXXXXXX → 2547XXXXXXX
      2547XXXXXXX → 2547XXXXXXX (unchanged)
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not phone.startswith("254") or len(phone) != 12:
        raise ValueError(
            f"Invalid Kenyan phone number: '{phone}'. "
            "Expected format: 07XXXXXXXX or 2547XXXXXXXX"
        )
    return phone
