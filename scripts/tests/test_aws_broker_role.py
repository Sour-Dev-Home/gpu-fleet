"""Text checks on the AWS broker role (ADR 0025): who may assume it and what it may do. No cloud
call, so a regression fails in CI before anyone runs a plan. Comments are stripped first, so a
comment that mentions a setting cannot satisfy a check."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "infra" / "aws" / "fleet"


def code(name: str) -> str:
    text = (STACK / name).read_text(encoding="utf-8")
    return re.sub(r"(?m)(^|\s)(#|//).*$", "", text)


def block(name: str, header: str) -> str:
    """The text of a block, up to the next top-level block."""
    text = code(name)
    start = text.index(header)
    rest = text[start + len(header) :]
    end = re.search(r"(?m)^(resource|data|variable|output)\b", rest)
    return rest[: end.start()] if end else rest


def trust() -> str:
    return block("broker_role.tf", 'data "aws_iam_policy_document" "broker_trust"')


def permissions() -> str:
    return block("broker_role.tf", 'data "aws_iam_policy_document" "broker_permissions"')


def statements(doc: str) -> list[str]:
    return re.findall(r"statement\s*\{.*?\n  \}", doc, flags=re.DOTALL)


def test_trust_is_google_web_identity_federation_and_nothing_else():
    t = trust()
    assert re.search(r'type\s*=\s*"Federated"', t)
    assert re.search(r'identifiers\s*=\s*\["accounts\.google\.com"\]', t)
    assert re.search(r'actions\s*=\s*\["sts:AssumeRoleWithWebIdentity"\]', t)
    assert len(statements(t)) == 1


def test_trust_pins_the_service_accounts_unique_ids_with_string_equals():
    t = trust()
    # :sub is the service account's unique ID; any other Google identity is refused
    sub = re.search(
        r'condition\s*\{[^}]*test\s*=\s*"StringEquals"[^}]*variable\s*=\s*"accounts\.google\.com:sub"'
        r"[^}]*values\s*=\s*var\.broker_google_sa_unique_id",
        t,
        flags=re.DOTALL,
    )
    assert sub, "no StringEquals condition on accounts.google.com:sub"


def test_trust_pins_the_audience_with_string_equals():
    t = trust()
    # :oaud is the audience the broker asked for: a token minted for any other service is refused
    aud = re.search(
        r'condition\s*\{[^}]*test\s*=\s*"StringEquals"[^}]*variable\s*=\s*"accounts\.google\.com:oaud"'
        r"[^}]*values\s*=\s*\[var\.federation_audience\]",
        t,
        flags=re.DOTALL,
    )
    assert aud, "no StringEquals condition on accounts.google.com:oaud"


def test_trust_pins_azp_to_the_same_ids_as_defense_in_depth():
    aud = re.search(
        r'condition\s*\{[^}]*test\s*=\s*"StringEquals"[^}]*variable\s*=\s*"accounts\.google\.com:aud"'
        r"[^}]*values\s*=\s*var\.broker_google_sa_unique_id",
        trust(),
        flags=re.DOTALL,
    )
    assert aud, "no StringEquals condition on accounts.google.com:aud (azp)"


def test_trust_has_exactly_the_three_conditions():
    assert len(re.findall(r"\bcondition\s*\{", trust())) == 3


def test_the_unique_ids_are_not_marked_sensitive():
    # an identifier, not a credential; sensitive would hide the whole trust policy in the plan
    ids = block("variables.tf", 'variable "broker_google_sa_unique_id"')
    assert "sensitive" not in ids


def test_no_wildcard_or_stringlike_in_the_trust():
    t = trust()
    assert "StringLike" not in t and '"*"' not in t


def test_the_role_is_named_fleet_broker_and_lasts_an_hour():
    role = block("broker_role.tf", 'resource "aws_iam_role" "broker"')
    assert re.search(r'name\s*=\s*"fleet-broker"', role)
    assert re.search(r"max_session_duration\s*=\s*3600\b", role)
    assert "data.aws_iam_policy_document.broker_trust.json" in role


def test_the_permissions_are_exactly_get_on_inputs_and_results_and_put_on_results():
    p = permissions()
    stmts = statements(p)
    assert len(stmts) == 3
    pairs = set()
    for s in stmts:
        if "s3:ListBucket" in s:
            continue  # the bucket-level statement has its own test below
        actions = re.search(r"actions\s*=\s*\[([^\]]*)\]", s).group(1)
        resources = re.search(r"resources\s*=\s*\[([^\]]*)\]", s).group(1)
        assert re.search(r'effect\s*=\s*"Allow"', s) or "effect" not in s  # Allow is the default
        pairs.add(
            (tuple(re.findall(r'"([^"]+)"', actions)), tuple(re.findall(r'"([^"]+)"', resources)))
        )
    assert pairs == {
        (
            ("s3:GetObject",),
            ("${module.results.arn}/inputs/open/*", "${module.results.arn}/results/open/*"),
        ),
        (("s3:PutObject",), ("${module.results.arn}/results/open/*",)),
    }


def test_the_role_cannot_touch_footage_or_derived_objects():
    # Q82: the data class is in the key, and this (cloud) role is cleared for `open` only. The
    # own-backend role of phase 6 is where the other classes get read access.
    p = permissions()
    for other in ("footage", "derived"):
        assert other not in p
    for resource in re.findall(r'"\$\{module\.results\.arn\}/([^"]*)"', p):
        assert resource in ("inputs/open/*", "results/open/*"), resource


def test_list_bucket_is_on_the_bucket_itself_with_no_condition():
    # An s3:prefix condition never matches HeadObject or GetObject (they carry no prefix), so a
    # missing key would stay 403 and every first upload would get 503. The bucket holds only
    # inputs/ and results/, and the role can already read those keys: listing adds only hash names.
    (stmt,) = [s for s in statements(permissions()) if "s3:ListBucket" in s]
    assert re.search(r'actions\s*=\s*\["s3:ListBucket"\]', stmt)
    assert re.search(r"resources\s*=\s*\[module\.results\.arn\]", stmt)
    assert "condition" not in stmt
    assert "s3:prefix" not in stmt


def test_there_is_no_delete_or_wildcard_action():
    p = permissions()
    for forbidden in ("DeleteObject", "s3:*", '"*"', "PutObjectAcl", "iam:"):
        assert forbidden not in p
    assert p.count("ListBucket") == 1


def test_the_policy_is_attached_to_the_role():
    pol = block("broker_role.tf", 'resource "aws_iam_role_policy" "broker"')
    assert "aws_iam_role.broker.id" in pol
    assert "data.aws_iam_policy_document.broker_permissions.json" in pol


def test_the_unique_ids_are_a_numeric_list_with_no_default_and_the_audience_has_one():
    ids = block("variables.tf", 'variable "broker_google_sa_unique_id"')
    assert re.search(r"type\s*=\s*list\(string\)", ids)
    assert "default" not in ids  # from TF_VAR_broker_google_sa_unique_id, never from a file
    assert "^[0-9]{21}$" in ids  # Google service account unique IDs are 21 digits
    assert re.search(r"length\(var\.broker_google_sa_unique_id\)\s*>\s*0", ids)
    aud = block("variables.tf", 'variable "federation_audience"')
    assert re.search(r'default\s*=\s*"fleet-aws-federation"', aud)


def test_role_arn_is_an_output():
    out = block("outputs.tf", 'output "broker_role_arn"')
    assert "aws_iam_role.broker.arn" in out


def test_no_google_identifier_is_committed():
    # unique IDs are 21 digits: none may appear in the stack's files (tfvars are git-ignored)
    for path in STACK.glob("*.tf"):
        assert not re.search(r"\b[0-9]{18,}\b", path.read_text(encoding="utf-8")), path.name
