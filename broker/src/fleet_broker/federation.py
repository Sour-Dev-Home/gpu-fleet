"""AWS credentials for the broker without any stored key (ADR 0025).

On Cloud Run the broker's service account can ask the metadata server for a Google-signed ID token
naming an audience. AWS trusts Google as an identity provider (`accounts.google.com` is built in),
so STS swaps that token for one hour of credentials, if the role's trust policy accepts the
service account (infra/aws/fleet/broker_role.tf). Nothing secret is stored anywhere: the token
is minted on demand and the credentials expire on their own.

botocore's RefreshableCredentials calls `refresh` again shortly before expiry, so a long-lived
broker keeps working; presigning itself is local and needs no network.
"""

import datetime as dt
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.credentials import RefreshableCredentials

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
METADATA_TIMEOUT = 5.0  # seconds: the metadata server is on the same host
SESSION_NAME = "fleet-broker"  # shows in CloudTrail as the assumed-role session
# Presigned URL lifetimes (s3.py GET_SECONDS, PUT_SECONDS) must stay <= botocore's 10-15 minute refresh margin.
SESSION_SECONDS = 3600  # the role's max_session_duration (Terraform) is the same


def fetch_id_token(audience: str, opener: Callable[..., Any] = urllib.request.urlopen) -> str:
    """A Google-signed ID token for `audience`, from the metadata server (reachable only from
    inside Google Cloud). `opener` is injectable for tests."""
    query = urllib.parse.urlencode({"audience": audience})
    request = urllib.request.Request(
        f"{METADATA_IDENTITY_URL}?{query}", headers={"Metadata-Flavor": "Google"}
    )
    with opener(request, timeout=METADATA_TIMEOUT) as response:
        return response.read().decode().strip()


def sts_client(region: str):
    """STS, unsigned: AssumeRoleWithWebIdentity is authorised by the token in the request, and
    a signing client would go looking for credentials the broker does not have yet."""
    return boto3.client("sts", region_name=region, config=Config(signature_version=UNSIGNED))


def make_credentials(
    *,
    role_arn: str,
    audience: str,
    region: str,
    sts: Any = None,
    fetch: Callable[[str], str] = fetch_id_token,
) -> RefreshableCredentials:
    """Credentials that renew themselves. The first assumption happens now, so a wrong role or
    audience stops the broker at startup instead of failing the first lease."""
    client = sts if sts is not None else sts_client(region)

    def refresh() -> dict[str, str]:
        granted = client.assume_role_with_web_identity(
            RoleArn=role_arn,
            RoleSessionName=SESSION_NAME,
            WebIdentityToken=fetch(audience),
            DurationSeconds=SESSION_SECONDS,
        )["Credentials"]
        expiry = granted["Expiration"]
        if isinstance(expiry, dt.datetime):
            expiry = expiry.isoformat()
        return {
            "access_key": granted["AccessKeyId"],
            "secret_key": granted["SecretAccessKey"],
            "token": granted["SessionToken"],
            "expiry_time": expiry,
        }

    return RefreshableCredentials.create_from_metadata(
        metadata=refresh(), refresh_using=refresh, method="fleet-web-identity"
    )
