import base64
import hashlib
import hmac
import os
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

# ------------------------------------------------------------------------------
# Environment variables
# ------------------------------------------------------------------------------

BUILDER_API_KEY = os.getenv("BUILDER_API_KEY")
BUILDER_SECRET = os.getenv("BUILDER_SECRET")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASS_PHRASE")

if not all([BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE]):
    raise RuntimeError("Missing one or more BUILDER_* environment variables")

# ------------------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------------------

app = FastAPI(title="Polymarket Builder Signing Service")


# ------------------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------------------
class SignRequest(BaseModel):
    method: str
    path: str
    body: Optional[str] = ""


class SignResponse(BaseModel):
    POLY_BUILDER_SIGNATURE: str
    POLY_BUILDER_TIMESTAMP: str
    POLY_BUILDER_API_KEY: str
    POLY_BUILDER_PASSPHRASE: str


def build_hmac_signature(
    secret: str, timestamp: str, method: str, requestPath: str, body=None
):
    """
    Creates an HMAC signature by signing a payload with the secret
    """
    base64_secret = base64.urlsafe_b64decode(secret)
    message = str(timestamp) + str(method) + str(requestPath)
    if body:
        # NOTE: Necessary to replace single quotes with double quotes
        # to generate the same hmac message as go and typescript
        message += str(body).replace("'", '"')

    h = hmac.new(base64_secret, bytes(message, "utf-8"), hashlib.sha256)

    # ensure base64 encoded
    return (base64.urlsafe_b64encode(h.digest())).decode("utf-8")


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------


@app.post("/sign", response_model=SignResponse)
def sign(req: SignRequest):
    """
    Receives { method, path, body } and returns Polymarket Builder headers
    """
    try:
        timestamp = str(int(time.time()))

        signature = build_hmac_signature(
            BUILDER_SECRET,
            timestamp,
            req.method.upper(),
            req.path,
            req.body or "",
        )

        return {
            "POLY_BUILDER_SIGNATURE": signature,
            "POLY_BUILDER_TIMESTAMP": timestamp,
            "POLY_BUILDER_API_KEY": BUILDER_API_KEY,
            "POLY_BUILDER_PASSPHRASE": BUILDER_PASSPHRASE,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
