"""Static checks on the PC's Grafana Alloy setup (ADR 0027): the config, scripts/setup-alloy.ps1 and
scripts/grafana-env.ps1. No Windows, no Alloy, no vault: the files are read as text. What can only be seen on
the PC (Alloy parsing the config, the ACL taking effect, the service restart) is listed as unverified in the
hand-over, not faked here.

The scripts handle a token. Rather than hunting for bad lines, every line that mentions a secret-carrying
variable must match an ALLOWED form (read from the vault, written to the ACL-protected file, put into one
process's environment, cleared); anything else, such as Write-Host, argv or a log call, fails."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "observability/alloy/config.alloy"
SETUP = ROOT / "scripts/setup-alloy.ps1"
ENVSCRIPT = ROOT / "scripts/grafana-env.ps1"

COLLECTORS = ["cpu", "memory", "logical_disk", "net", "gpu"]


def strip_alloy(text: str) -> str:
    return re.sub(r"(?m)(^|\s)(//|#).*$", "", text)


def strip_ps(text: str) -> str:
    text = re.sub(r"(?s)<#.*?#>", "", text)
    return re.sub(r"(?m)(^|\s)#.*$", "", text)


def test_alloy_collects_exactly_the_five_windows_collectors():
    cfg = strip_alloy(CONFIG.read_text(encoding="utf-8"))
    m = re.search(
        r'prometheus\.exporter\.windows\s+"\w+"\s*\{[^}]*enabled_collectors\s*=\s*\[(.*?)\]',
        cfg,
        re.DOTALL,
    )
    assert m
    assert re.findall(r'"([a-z_]+)"', m.group(1)) == COLLECTORS


# Every component the config may use. Each one was confirmed to have a reference page under
# grafana.com/docs/alloy/latest/reference/components/ on 2026-11-26. `otelcol.processor.resource` is NOT here:
# its page is a 404 (it does not exist in Alloy; use otelcol.processor.transform).
KNOWN_COMPONENTS = {
    "prometheus.exporter.windows",
    "prometheus.scrape",
    "otelcol.receiver.prometheus",
    "otelcol.processor.transform",
    "otelcol.auth.basic",
    "otelcol.exporter.otlphttp",
}


def test_only_documented_components_are_used():
    cfg = strip_alloy(CONFIG.read_text(encoding="utf-8"))
    declared = set(re.findall(r'(?m)^([a-z]+(?:\.[a-z0-9_]+)+)\s+"', cfg))
    # references such as otelcol.processor.transform.pc.input or prometheus.scrape.pc.receiver
    referenced = set(
        re.findall(
            r"\b((?:otelcol|prometheus|local|discovery|loki)\.[a-z]+\.[a-z0-9_]+)\.\w+\.", cfg
        )
    )
    assert declared, "no components found: the regex is broken"
    assert declared <= KNOWN_COMPONENTS, f"unknown components: {declared - KNOWN_COMPONENTS}"
    assert referenced <= KNOWN_COMPONENTS, f"unknown references: {referenced - KNOWN_COMPONENTS}"
    assert "otelcol.processor.resource" not in cfg


def test_pipeline_is_exporter_scrape_receiver_transform_otlphttp():
    cfg = strip_alloy(CONFIG.read_text(encoding="utf-8"))
    for component in (
        "prometheus.exporter.windows",
        "prometheus.scrape",
        "otelcol.receiver.prometheus",
        "otelcol.processor.transform",
        "otelcol.exporter.otlphttp",
        "otelcol.auth.basic",
    ):
        assert re.search(rf'(?m)^{re.escape(component)}\s+"', cfg), component
    assert re.search(r"targets\s*=\s*prometheus\.exporter\.windows\.\w+\.targets", cfg)
    assert re.search(r"forward_to\s*=\s*\[otelcol\.receiver\.prometheus\.\w+\.receiver\]", cfg)
    assert re.search(r"auth\s*=\s*otelcol\.auth\.basic\.\w+\.handler", cfg)


def test_the_token_comes_from_a_secret_file_and_nothing_secret_is_literal():
    cfg = strip_alloy(CONFIG.read_text(encoding="utf-8"))
    m = re.search(r'otelcol\.auth\.basic\s+"\w+"\s*\{(.*?)\n\}', cfg, re.DOTALL)
    assert m
    block = m.group(1)
    # the top-level username/password are deprecated: both live in a client_auth block, the token as a file
    assert re.search(r"client_auth\s*\{", block)
    top = re.sub(r"client_auth\s*\{.*?\}", "", block, flags=re.DOTALL)
    assert not re.search(r"\b(username|password)\s*=", top), "deprecated top-level credentials"
    assert re.search(r'password_file\s*=\s*sys\.env\("FLEET_GRAFANA_TOKEN_FILE"\)', block)
    assert not re.search(r"\bpassword\s*=", block)
    assert "local.file" not in cfg
    # no password, token or key written in the file
    assert not re.search(r'(?i)(password|token|secret|api_key)\s*=\s*"', cfg)
    assert not re.search(r"glc_|glsa_|Bearer |Basic ", cfg)


def test_account_identifiers_come_from_environment_variables():
    cfg = strip_alloy(CONFIG.read_text(encoding="utf-8"))
    assert re.search(r'username\s*=\s*sys\.env\("FLEET_GRAFANA_INSTANCE_ID"\)', cfg)
    assert re.search(r'endpoint\s*=\s*sys\.env\("FLEET_GRAFANA_OTLP_URL"\)', cfg)
    assert not re.search(r"https?://", cfg)  # no endpoint written in the file
    assert not re.search(r"grafana\.net", cfg)


def test_every_series_carries_the_install_id_as_service_instance_id():
    cfg = strip_alloy(CONFIG.read_text(encoding="utf-8"))
    m = re.search(r'otelcol\.processor\.transform\s+"\w+"\s*\{(.*?)\n\}', cfg, re.DOTALL)
    assert m
    body = m.group(1)
    assert re.search(r'metric_statements\s*\{[^}]*context\s*=\s*"resource"', body, re.DOTALL)
    # Alloy string concatenation injects the environment value into the OTTL statement
    assert re.search(
        r'`set\(attributes\["service\.instance\.id"\], "`\s*\+\s*sys\.env\("FLEET_INSTALL_ID"\)\s*\+\s*`"\)`',
        body,
    )
    assert re.search(r'`set\(attributes\["service\.name"\], "fleet-pc"\)`', body)
    # the scrape forwards through the receiver, whose output goes to the transform processor, then out
    assert re.search(r"metrics\s*=\s*\[otelcol\.processor\.transform\.\w+\.input\]", cfg)
    assert re.search(r"metrics\s*=\s*\[otelcol\.exporter\.otlphttp\.\w+\.input\]", cfg)


# --- the PowerShell scripts --------------------------------------------------------------------------------

SECRET_VARS = re.compile(r"\$(?:token|b64|pair|header|secret)\b", re.IGNORECASE)

ALLOWED = [
    r"^\$token\s*=\s*Get-Secret\s+-Name\s+\S+\s+-Vault\s+fleet\s+-AsPlainText$",  # from the vault
    r"^\[IO\.File\]::WriteAllText\(\$\w+,\s*\$token,",  # into the protected file
    r"^\$token\s*=\s*\$null$",  # cleared
    r"^\$pair\s*=",  # building the header
    r"^\$b64\s*=",
    r"^\$header\s*=",
    r"^Set-Item\s+\"?env:OTEL_EXPORTER_OTLP_HEADERS\"?\s+\$header$",  # one process's environment
    r"^Remove-Variable\b.*$",
]


def lines_with_secrets(path: Path):
    for raw in strip_ps(path.read_text(encoding="utf-8")).splitlines():
        line = raw.strip()
        if line and SECRET_VARS.search(line):
            yield line


def check_secret_lines(path: Path):
    bad = [
        line for line in lines_with_secrets(path) if not any(re.search(p, line) for p in ALLOWED)
    ]
    assert not bad, f"{path.name}: unexpected use of a secret variable: {bad}"


def test_setup_script_only_reads_writes_and_clears_the_token():
    check_secret_lines(SETUP)
    assert any("Get-Secret" in line for line in lines_with_secrets(SETUP))
    assert any("WriteAllText" in line for line in lines_with_secrets(SETUP))


def test_env_script_only_reads_and_sets_the_header():
    check_secret_lines(ENVSCRIPT)
    assert any("Get-Secret" in line for line in lines_with_secrets(ENVSCRIPT))


def test_no_script_prints_or_logs_anything_secret_looking():
    for path in (SETUP, ENVSCRIPT):
        code = strip_ps(path.read_text(encoding="utf-8"))
        for line in code.splitlines():
            if re.search(
                r"(?i)Write-(Host|Output|Verbose|Debug|Information|Warning|Error)|Out-Host|\becho\b|Start-Transcript",
                line,
            ):
                assert not SECRET_VARS.search(line), line
                assert not re.search(
                    r"(?i)Get-Secret|FLEET_GRAFANA_TOKEN|Environment\b.*\$", line
                ), line
        # the token is never an argument of another program
        assert not re.search(r"(?i)-ArgumentList[^\n]*\$(token|b64|header)", code)
        assert not re.search(r"(?m)^\s*&\s+\S+[^\n]*\$(token|b64|header|pair)\b", code)


def test_the_header_is_built_in_the_percent_20_form():
    code = strip_ps(ENVSCRIPT.read_text(encoding="utf-8"))
    assert '"Authorization=Basic%20$b64"' in code
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in code and "FLEET_GRAFANA_OTLP_URL" in code
    assert "FLEET_GRAFANA_TOKEN_PC_WORKER" in code and "FLEET_GRAFANA_INSTANCE_ID" in code


def test_env_script_scopes_the_variables_to_one_process_and_removes_them():
    code = strip_ps(ENVSCRIPT.read_text(encoding="utf-8"))
    assert "finally" in code
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"):
        assert re.search(rf"Remove-Item\s+\"?env:{name}", code), name
    assert "[Environment]::SetEnvironmentVariable" not in code  # nothing persistent
    assert "setx" not in code.lower()


def test_setup_script_protects_the_token_file_and_sets_the_service_environment():
    code = strip_ps(SETUP.read_text(encoding="utf-8"))
    assert "#Requires -RunAsAdministrator" in SETUP.read_text(encoding="utf-8")
    assert "$env:ProgramData" in code and "fleet" in code and "alloy" in code
    # inheritance off, SYSTEM and Administrators only (by SID, so a localized Windows works)
    assert "SetAccessRuleProtection($true, $false)" in code
    assert "S-1-5-18" in code and "S-1-5-32-544" in code
    assert not re.search(r"S-1-5-32-545|S-1-1-0|BUILTIN\\Users|Everyone|Authenticated Users", code)
    # the ACL is applied BEFORE the token is written
    assert code.index("SetAccessRuleProtection") < code.index("WriteAllText")
    assert r"HKLM:\SOFTWARE\GrafanaLabs\Alloy" in code
    for name in (
        "FLEET_GRAFANA_OTLP_URL",
        "FLEET_GRAFANA_INSTANCE_ID",
        "FLEET_GRAFANA_TOKEN_PC_ALLOY",
        "FLEET_GRAFANA_TOKEN_FILE",
        "FLEET_INSTALL_ID",
    ):
        assert name in code, name
    assert "MultiString" in code
    assert "Restart-Service" in code
    assert "config.alloy" in code
    # the token is read from the vault, never from a parameter or the environment
    assert not re.search(r"(?i)param\([^)]*token", code)


def test_setup_script_is_idempotent_by_construction():
    code = strip_ps(SETUP.read_text(encoding="utf-8"))
    # every step is "set to": no appending to the registry values or the file
    assert (
        "Add-Content" not in code
        and "-Append" not in code
        and "+= " not in code.replace("$changes += ", "")
    )
    assert "New-Item -ItemType Directory -Force" in code
    assert (
        "Get-ItemProperty" in code
    )  # keeps the existing storage flags instead of overwriting them


def test_docs_mention_the_installer_and_the_vault_names():
    readme = (ROOT / "observability/alloy/README.md").read_text(encoding="utf-8")
    assert "winget install GrafanaLabs.Alloy" in readme
    for name in (
        "FLEET_GRAFANA_OTLP_URL",
        "FLEET_GRAFANA_INSTANCE_ID",
        "FLEET_GRAFANA_TOKEN_PC_ALLOY",
        "FLEET_GRAFANA_TOKEN_PC_WORKER",
    ):
        assert name in readme, name
    assert "UNVERIFIED" in readme


def test_service_runs_a_protected_copy_of_the_config_not_the_repo_file():
    code = strip_ps(SETUP.read_text(encoding="utf-8"))
    assert re.search(r"Copy-Item[^\n]*\$config\b[^\n]*\$configCopy", code) or "Copy-Item" in code
    assert re.search(r"\$configCopy\s*=\s*Join-Path\s+\$dir\s+'config\.alloy'", code)
    # the `run` argument is the copy in the protected directory, never the repo path
    assert re.search(r"\$arguments\s*=\s*@\('run',\s*\$configCopy\)", code)
    assert not re.search(r"@\('run',\s*\$config\)", code)
    # the copy happens after the directory ACL is set, so it is never briefly user-writable
    assert code.index("Set-Acl") < code.index("Copy-Item")


def test_setup_takes_ownership_and_refuses_reparse_points():
    code = strip_ps(SETUP.read_text(encoding="utf-8"))
    assert "ReparsePoint" in code
    assert re.search(r"(?i)throw[^\n]*(reparse|symbolic|junction|link)", code)
    assert re.search(r"\$acl\.SetOwner\(\$admins\)", code)
    assert code.index("SetOwner") < code.index("Set-Acl")
    # the reparse check comes before any ACL is changed or the token written
    assert code.index("ReparsePoint") < code.index("SetOwner")
    assert code.index("ReparsePoint") < code.index("WriteAllText")


def test_readme_validates_the_copied_config_before_restart():
    readme = (ROOT / "observability/alloy/README.md").read_text(encoding="utf-8")
    assert re.search(r"alloy validate\s+\S*config\.alloy", readme)
    assert "ProgramData" in readme
    assert "from the repo" not in readme.lower() or "copy" in readme.lower()
    assert "observability/alloy/config.alloy" in readme  # the source is still named


def test_env_script_restores_preexisting_otlp_variables():
    code = strip_ps(ENVSCRIPT.read_text(encoding="utf-8"))
    final = code[code.index("finally") :]
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"):
        # saved before being overwritten, restored (not just removed) in finally
        assert re.search(
            rf"\$saved\w*\s*=\s*\[Environment\]::GetEnvironmentVariable\(\"{name}\"|\$saved\w*\s*=\s*\$env:{name}",
            code,
        ), name
        assert re.search(
            rf"(?s)\$saved\w*.*Set-Item\s+\"?env:{name}|Set-Item\s+\"?env:{name}[^\n]*\$saved",
            final,
        ), name
        assert code.index("saved") < code.index(f'Set-Item "env:{name}"')
    assert "$null -ne $saved" in final or "$null -eq $saved" in final


def test_env_script_requires_a_numeric_instance_id():
    code = strip_ps(ENVSCRIPT.read_text(encoding="utf-8"))
    assert re.search(r"\$instanceId\s+-notmatch\s+'\^\[0-9\]\+\$'", code)
    assert code.index("-notmatch '^[0-9]+$'") < code.index("$pair")
