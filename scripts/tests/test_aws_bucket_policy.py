"""Text checks on the results bucket's data-plane lock (Q94): only the broker role and the maintainer's admin
role may read, write or delete objects under inputs/ and results/. No cloud call. Comments are stripped."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AWS = ROOT / "infra" / "aws"


def code(path: str) -> str:
    text = (AWS / path).read_text(encoding="utf-8")
    return re.sub(r"(?m)(^|\s)(#|//).*$", "", text)


def statement(sid: str) -> str:
    text = code("modules/private-bucket/main.tf")
    start = re.search(rf'sid\s*=\s*"{sid}"', text).start()
    begin = max(text.rindex("statement {", 0, start), text.rindex("content {", 0, start))
    return text[begin : text.index("\n  }\n", start)]


def test_the_data_lock_denies_exactly_get_put_delete_object():
    s = statement("DenyDataAccessExceptAllowedRoles")
    assert re.search(r'effect\s*=\s*"Deny"', s)
    actions = re.search(r"actions\s*=\s*\[(.*?)\]", s, re.DOTALL).group(1)
    assert re.findall(r'"([^"]+)"', actions) == ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]


def test_the_data_lock_covers_inputs_and_results_for_every_principal():
    s = statement("DenyDataAccessExceptAllowedRoles")
    assert (
        '"${aws_s3_bucket.this.arn}/inputs/*"' in s and '"${aws_s3_bucket.this.arn}/results/*"' in s
    )
    assert re.search(r'principals\s*\{\s*type\s*=\s*"\*"\s*identifiers\s*=\s*\["\*"\]', s)
    # object actions only: bucket-level calls (policy, config, ListBucket) are never denied here, so
    # Terraform, which manages the bucket and never touches objects, cannot lock itself out
    assert "s3:*" not in s and "ListBucket" not in s and "Policy" not in s


def test_the_exemption_is_by_principal_arn_with_arnnotlike():
    s = statement("DenyDataAccessExceptAllowedRoles")
    assert re.search(r'test\s*=\s*"ArnNotLike"\s*variable\s*=\s*"aws:PrincipalArn"', s)
    assert "values   = var.data_allowed_principal_arns" in s
    # a role ARN, not a session ARN: aws:PrincipalArn of an assumed-role session is the role's ARN
    assert "StringNotEquals" not in s and "NotPrincipal" not in s and "aws:userid" not in s


def test_the_module_adds_the_statement_only_when_principals_are_given():
    text = code("modules/private-bucket/main.tf")
    assert re.search(
        r'dynamic\s+"statement"\s*\{\s*for_each\s*=\s*length\(var\.data_allowed_principal_arns\)\s*>\s*0',
        text,
    )
    v = code("modules/private-bucket/variables.tf")
    assert re.search(
        r'variable\s+"data_allowed_principal_arns"\s*\{[^}]*default\s*=\s*\[\]', v, re.DOTALL
    )


def test_the_tls_only_denies_are_kept():
    text = code("modules/private-bucket/main.tf")
    assert 'sid       = "DenyInsecureTransport"' in text and "aws:SecureTransport" in text
    assert 'sid       = "DenyOldTls"' in text and "s3:TlsVersion" in text


def test_the_results_bucket_allows_the_broker_role_and_the_admin_role():
    main = code("fleet/main.tf")
    m = main[main.index('module "results"') :]
    assert re.search(
        r"data_allowed_principal_arns\s*=\s*\[\s*aws_iam_role\.broker\.arn,\s*var\.admin_principal_arn\s*\]",
        m,
    )


def _admin_variable_block():
    v = code("fleet/variables.tf")
    b = v[v.index('variable "admin_principal_arn"') :]
    return b[: b.index("\n}\n")]


def test_admin_principal_arn_is_a_sensitive_validated_arn_with_no_default():
    b = _admin_variable_block()
    assert "sensitive   = true" in b
    assert "default" not in b
    assert "arn:aws:iam::" in b and "validation" in b


def _validation_regex():
    m = re.search(
        r'regex\("((?:[^"\\]|\\.)*)",\s*var\.admin_principal_arn', _admin_variable_block()
    )
    assert m, "validation regex not found"
    # HCL "..." string -> Python pattern (only \\ escapes are used here)
    return re.compile(m.group(1).replace("\\\\", "\\"))


def test_admin_principal_arn_accepts_a_user_arn_and_a_role_arn():
    rx = _validation_regex()
    assert rx.search("arn:aws:iam::123456789012:user/maintainer")
    assert rx.search("arn:aws:iam::123456789012:user/team/path/maintainer")
    assert rx.search("arn:aws:iam::123456789012:role/admin")
    assert rx.search(
        "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/us-east-1/AWSReservedSSO_Admin_abc"
    )


def test_admin_principal_arn_rejects_session_arns_and_garbage():
    rx = _validation_regex()
    assert not rx.search("arn:aws:sts::123456789012:assumed-role/admin/session")
    assert not rx.search("arn:aws:iam::123456789012:root")
    assert not rx.search("arn:aws:iam::12345:user/maintainer")
    assert not rx.search("maintainer")
    assert not rx.search("")


def test_no_account_id_or_role_arn_literal_is_committed():
    for f in AWS.rglob("*.tf"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"arn:aws:iam::\d{12}", text), f.name


def test_the_docs_say_which_principal_the_probe_and_terraform_run_as():
    probe = (ROOT / "scripts" / "probe-presign.py").read_text(encoding="utf-8")
    assert "admin_principal_arn" in probe
    readme = (AWS / "README.md").read_text(encoding="utf-8")
    assert "TF_VAR_admin_principal_arn" in readme and "session" in readme
    assert "get-caller-identity" in readme and "Set-Secret" in readme
    assert "admin_role_arn" not in readme
