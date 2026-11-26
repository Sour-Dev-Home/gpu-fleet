"""Does S3 enforce what the broker signs into a presigned PUT? (ADR 0025, Q76)

The broker signs the upload's length, its SHA-256 and "If-None-Match: *" into the URL. Whether S3
HONOURS each of those on a presigned PUT is not documented clearly, so measure it, against the real
bucket, with your own login (no keys are stored anywhere). From the repo root:

    aws login --profile fleet
    $env:AWS_PROFILE = "fleet"; $env:AWS_REGION = "us-east-1"
    uv run --directory broker python ../scripts/probe-presign.py --bucket <results bucket>

(the bucket name is `terraform output results_bucket` in infra/aws/fleet). The bucket policy lets only the
broker role and the admin role (`admin_principal_arn`, Q94) touch objects, so the login above must be that admin
role: from any other principal the control upload gets 403 and the probe exits 2. It uses the broker's
own signing code, writes only under results/probe/<random>/ and deletes everything it wrote.

Prints one line per question and exits 0 if all three are enforced, 1 if any is not, 2 if the
control upload failed (the probe itself is broken: fix that first, the other answers mean nothing).
What "not enforced" means for the system: nothing breaks, because /complete HEADs the stored
object and compares its size and SHA-256 with what /upload-url declared (a weaker guarantee than
refusing the bytes at upload: a bad object is stored, then refused, and ignored).
"""

import argparse
import hashlib
import os
import sys
import uuid

QUESTIONS = {
    "wrong_length": "a body longer than the signed length is refused",
    "wrong_checksum": "a body whose SHA-256 differs from the signed one is refused",
    "overwrite": "a second PUT to an existing key is refused (If-None-Match: *)",
}


def verdicts(statuses: dict[str, int]) -> tuple[int, list[str]]:
    """(exit code, report lines) from the HTTP status each probe got. A 2xx means S3 accepted the
    bytes; anything else means it refused them."""
    if not 200 <= statuses["control"] < 300:
        return 2, [
            f"CONTROL FAILED: a correct upload got HTTP {statuses['control']}; fix this first"
        ]
    lines, code = [], 0
    for name, text in QUESTIONS.items():
        status = statuses[name]
        enforced = not 200 <= status < 300
        code = code or (0 if enforced else 1)
        lines.append(f"{'ENFORCED    ' if enforced else 'NOT ENFORCED'}  {text} (HTTP {status})")
    return code, lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bucket", required=True)
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = p.parse_args(argv)

    import boto3  # imported here: the report logic above needs nothing
    import httpx
    from fleet_broker import s3

    client = boto3.client("s3", region_name=args.region, config=s3.client_config())
    bucket = s3.ResultBucket(client, args.bucket)
    prefix = f"results/probe/{uuid.uuid4().hex}"
    body = b"probe body, 0123456789"
    keys = {name: f"{prefix}/{name}" for name in ("control", *QUESTIONS)}

    def put(key: str, data: bytes, signed_for: bytes) -> int:
        target = bucket.put_target(
            key, hashlib.sha256(signed_for).hexdigest(), len(signed_for), expires=60
        )
        return httpx.put(target.url, content=data, headers=target.headers, timeout=30).status_code

    statuses: dict[str, int] = {}
    try:
        statuses["control"] = put(keys["control"], body, body)
        statuses["wrong_length"] = put(keys["wrong_length"], body + b"!", body)
        other = b"x" * len(body)  # same length, different bytes: only the checksum can tell
        statuses["wrong_checksum"] = put(keys["wrong_checksum"], other, body)
        statuses["overwrite"] = put(keys["control"], body, body)  # the control key now exists
    finally:
        for key in keys.values():
            client.delete_object(
                Bucket=args.bucket, Key=key
            )  # your login may delete; the role may not
        print(f"deleted everything under {prefix}/")
    code, lines = verdicts(statuses)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
