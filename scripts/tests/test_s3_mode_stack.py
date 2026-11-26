"""Text checks on the Cloud Run broker's S3 mode wiring (ADR 0025): the four settings travel together, the bucket
and role come from the AWS stack's remote state, and the audience agrees with the AWS stack. No cloud call."""

import re

from test_gcp_stack import ROOT, STACK, all_code, block, code

S3_NAMES = ("FLEET_RESULTS_BUCKET", "FLEET_AWS_ROLE_ARN", "FLEET_AWS_AUDIENCE", "AWS_REGION")


def s3_env_local() -> str:
    text = code("cloudrun.tf")
    start = text.index("s3_env = ")
    return text[start : text.index("} : {}", start) + len("} : {}")]


def test_the_four_settings_are_set_together_or_none():
    local = s3_env_local()
    assert local.startswith("s3_env = var.s3_mode ? {")
    assert local.rstrip().endswith("} : {}")
    for name in S3_NAMES:
        assert len(re.findall(rf"\b{name}\b", local)) == 1, name
    # and nowhere else in the stack as an env name: no way to set one alone
    for name in S3_NAMES:
        assert len(re.findall(rf"\b{name}\b", all_code())) == 1, name
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    assert re.search(r'dynamic "env" \{\s*for_each = local\.s3_env', run)


def test_bucket_and_role_come_from_the_aws_remote_state():
    local = s3_env_local()
    assert "data.terraform_remote_state.aws[0].outputs.results_bucket" in local
    assert "data.terraform_remote_state.aws[0].outputs.broker_role_arn" in local
    d = block("cloudrun.tf", 'data "terraform_remote_state" "aws"')
    assert 'backend = "s3"' in d and "count   = var.s3_mode ? 1 : 0" in d
    assert 'key    = "aws/fleet/terraform.tfstate"' in d
    assert 'region = "us-east-1"' in d
    # the same state bucket as this stack's own backend
    versions = code("versions.tf")
    bucket = re.search(r'bucket\s*=\s*"([^"]+)"', versions).group(1)
    assert f'bucket = "{bucket}"' in d
    out = (ROOT / "infra/aws/fleet/outputs.tf").read_text(encoding="utf-8")
    assert 'output "results_bucket"' in out and 'output "broker_role_arn"' in out


def test_s3_mode_is_a_bool_defaulting_to_true():
    v = block("variables.tf", 'variable "s3_mode"')
    assert re.search(r"type\s*=\s*bool", v) and re.search(r"default\s*=\s*true", v)


def test_nothing_aws_is_hardcoded():
    text = all_code()
    assert "arn:aws" not in text
    assert not re.search(r"\d{12}", text)  # an account number
    assert not re.search(r"[\w-]+\.s3[.\w-]*\.amazonaws\.com", text)
    assert not re.search(r'FLEET_RESULTS_BUCKET"\s*=\s*"', text)
    assert "FLEET_AWS_AUDIENCE   = var.aws_federation_audience" in s3_env_local()


def _default(path: str, variable: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index(f'variable "{variable}"')
    rest = text[start:]
    end = re.search(r"(?m)^variable\b", rest[10:])
    body = rest[: end.start() + 10] if end else rest
    return re.search(r'(?m)^\s*default\s*=\s*"([^"]+)"', body).group(1)


def test_the_audience_matches_the_aws_stacks_federation_audience():
    gcp = _default("infra/gcp/fleet/variables.tf", "aws_federation_audience")
    aws = _default("infra/aws/fleet/variables.tf", "federation_audience")
    assert gcp == aws == "fleet-aws-federation"


def test_readme_documents_s3_mode_apply_order_and_rollback():
    text = (STACK / "README.md").read_text(encoding="utf-8")
    sec = text[text.index("## S3 mode") :]
    for needle in (
        "TF_VAR_broker_image",
        "AssumeRoleWithWebIdentity",
        "AccessDenied",
        "input_url",
        "-var s3_mode=false",
        "FLEET_RESULTS_HOST",
        "output -raw results_bucket",
        "s3.amazonaws.com",
    ):
        assert needle in sec, needle


def test_adr_0025_records_the_cloud_run_wiring():
    text = (ROOT / "docs/adr/0025-presigned-urls-and-aws-federation.md").read_text(encoding="utf-8")
    assert "As deployed" in text and "terraform_remote_state" in text and "s3_mode" in text
