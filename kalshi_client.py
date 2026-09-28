import os
import time
import base64
from pathlib import Path

import requests
from requests import exceptions as requests_exceptions
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


# --------------------------------------------------
# Paths
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PRIVATE_KEY_PATH = DATA_DIR / "kalshi-key.key"

# Always load .env from the project directory
load_dotenv(BASE_DIR / ".env")


class KalshiClient:
    def __init__(self):
        # --------------------------------------------------
        # Environment variables
        # --------------------------------------------------

        self.key_id = os.environ["KALSHI_API_KEY_ID"]
        self.base_url = os.environ["KALSHI_BASE_URL"].rstrip("/")

        self.connect_timeout = float(
            os.getenv(
                "KALSHI_CONNECT_TIMEOUT",
                "1.5",
            )
        )

        self.read_timeout = float(
            os.getenv(
                "KALSHI_READ_TIMEOUT",
                "2.5",
            )
        )

        self.get_retries = max(
            0,
            int(
                os.getenv(
                    "KALSHI_GET_RETRIES",
                    "1",
                )
            ),
        )

        # --------------------------------------------------
        # Private key
        #
        # Expected location:
        # C:\Users\shk\Python\Kalshi Bot\data\kalshi-key.key
        # --------------------------------------------------

        if not PRIVATE_KEY_PATH.exists():
            raise FileNotFoundError(
                "Kalshi private key not found.\n"
                f"Expected location: {PRIVATE_KEY_PATH}"
            )

        if not PRIVATE_KEY_PATH.is_file():
            raise FileNotFoundError(
                "Kalshi private key path exists, "
                "but is not a file.\n"
                f"Path: {PRIVATE_KEY_PATH}"
            )

        with PRIVATE_KEY_PATH.open("rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
            )

        if not isinstance(
            private_key,
            rsa.RSAPrivateKey,
        ):
            raise TypeError(
                "kalshi-key.key must contain "
                "an RSA private key."
            )

        self.private_key: rsa.RSAPrivateKey = private_key

    # --------------------------------------------------
    # Authentication
    # --------------------------------------------------

    def _sign(
        self,
        timestamp: str,
        method: str,
        path: str,
    ) -> str:
        message = (
            f"{timestamp}{method}{path}"
        ).encode()

        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(
                    hashes.SHA256()
                ),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return base64.b64encode(
            signature
        ).decode()

    # --------------------------------------------------
    # API request
    # --------------------------------------------------

    def request(
        self,
        method: str,
        endpoint: str,
        params=None,
        json=None,
    ):
        method = method.upper()

        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        full_path = (
            "/trade-api/v2"
            + endpoint
        )

        # GET requests may be retried.
        #
        # POST requests are deliberately NOT retried.
        # A POST timeout could occur after Kalshi already
        # accepted an order, so blindly retrying could
        # create duplicate orders.
        if method == "GET":
            attempts = 1 + self.get_retries
        else:
            attempts = 1

        for attempt in range(attempts):
            timestamp = str(
                int(time.time() * 1000)
            )

            headers = {
                "KALSHI-ACCESS-KEY":
                    self.key_id,

                "KALSHI-ACCESS-TIMESTAMP":
                    timestamp,

                "KALSHI-ACCESS-SIGNATURE":
                    self._sign(
                        timestamp,
                        method,
                        full_path,
                    ),

                "Content-Type":
                    "application/json",
            }

            try:
                response = requests.request(
                    method=method,
                    url=(
                        self.base_url
                        + endpoint
                    ),
                    headers=headers,
                    params=params,
                    json=json,
                    timeout=(
                        self.connect_timeout,
                        self.read_timeout,
                    ),
                )

                if not response.ok:
                    raise RuntimeError(
                        f"HTTP "
                        f"{response.status_code}: "
                        f"{response.text}"
                    )

                # Some endpoints may theoretically return
                # an empty response body.
                if not response.content:
                    return {}

                return response.json()

            except (
                requests_exceptions.ConnectTimeout,
                requests_exceptions.ReadTimeout,
                requests_exceptions.ConnectionError,
            ):
                # Last attempt -> preserve and raise the
                # original requests exception.
                if attempt + 1 >= attempts:
                    raise

                time.sleep(0.05)

        # Defensive fallback.
        # Normal execution should never reach this point.
        raise RuntimeError(
            "Kalshi request failed unexpectedly: "
            f"{method} {endpoint}"
        )