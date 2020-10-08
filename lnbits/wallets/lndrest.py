import httpx
import json
import base64
from os import getenv
from typing import Optional, Dict, AsyncGenerator

from .base import InvoiceResponse, PaymentResponse, PaymentStatus, Wallet


class LndRestWallet(Wallet):
    """https://api.lightning.community/rest/index.html#lnd-rest-api-reference"""

    def __init__(self):
        endpoint = getenv("LND_REST_ENDPOINT")
        endpoint = endpoint[:-1] if endpoint.endswith("/") else endpoint
        endpoint = "https://" + endpoint if not endpoint.startswith("http") else endpoint
        self.endpoint = endpoint

        macaroon = (
            getenv("LND_MACAROON")
            or getenv("LND_ADMIN_MACAROON")
            or getenv("LND_REST_ADMIN_MACAROON")
            or getenv("LND_INVOICE_MACAROON")
            or getenv("LND_REST_INVOICE_MACAROON")
        )
        self.auth = {"Grpc-Metadata-macaroon": macaroon}
        self.cert = getenv("LND_REST_CERT")

    def create_invoice(
        self, amount: int, memo: Optional[str] = None, description_hash: Optional[bytes] = None
    ) -> InvoiceResponse:
        data: Dict = {
            "value": amount,
            "private": True,
        }
        if description_hash:
            data["description_hash"] = base64.b64encode(description_hash).decode("ascii")
        else:
            data["memo"] = memo or ""

        r = httpx.post(
            url=f"{self.endpoint}/v1/invoices",
            headers=self.auth,
            verify=self.cert,
            json=data,
        )

        if r.is_error:
            error_message = r.text
            try:
                error_message = r.json()["error"]
            except Exception:
                pass
            return InvoiceResponse(False, None, None, error_message)

        data = r.json()
        payment_request = data["payment_request"]
        payment_hash = base64.b64decode(data["r_hash"]).hex()
        checking_id = payment_hash

        return InvoiceResponse(True, checking_id, payment_request, None)

    def pay_invoice(self, bolt11: str) -> PaymentResponse:
        r = httpx.post(
            url=f"{self.endpoint}/v1/channels/transactions",
            headers=self.auth,
            verify=self.cert,
            json={"payment_request": bolt11},
        )

        if r.is_error:
            error_message = r.text
            try:
                error_message = r.json()["error"]
            except:
                pass
            return PaymentResponse(False, None, 0, error_message)

        payment_hash = r.json()["payment_hash"]
        checking_id = payment_hash

        return PaymentResponse(True, checking_id, 0, None)

    def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        checking_id = checking_id.replace("_", "/")
        r = httpx.get(
            url=f"{self.endpoint}/v1/invoice/{checking_id}",
            headers=self.auth,
            verify=self.cert,
        )

        if r.is_error or not r.json().get("settled"):
            # this must also work when checking_id is not a hex recognizable by lnd
            # it will return an error and no "settled" attribute on the object
            return PaymentStatus(None)

        return PaymentStatus(True)

    def get_payment_status(self, checking_id: str) -> PaymentStatus:
        r = httpx.get(
            url=f"{self.endpoint}/v1/payments",
            headers=self.auth,
            verify=self.cert,
            params={"include_incomplete": "True", "max_payments": "20"},
        )

        if r.is_error:
            return PaymentStatus(None)

        payments = [p for p in r.json()["payments"] if p["payment_hash"] == checking_id]
        payment = payments[0] if payments else None

        # check payment.status:
        # https://api.lightning.community/rest/index.html?python#peersynctype
        statuses = {"UNKNOWN": None, "IN_FLIGHT": None, "SUCCEEDED": True, "FAILED": False}

        return PaymentStatus(statuses[payment["status"]])

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        url = self.endpoint + "/v1/invoices/subscribe"

        async with httpx.AsyncClient(timeout=None, headers=self.auth, verify=self.cert) as client:
            async with client.stream("GET", url) as r:
                async for line in r.aiter_lines():
                    try:
                        inv = json.loads(line)["result"]
                        if not inv["settled"]:
                            continue
                    except:
                        continue

                    payment_hash = base64.b64decode(inv["r_hash"]).hex()
                    yield payment_hash
