"""Setting-name guard regexes of the app_service and container_apps modules (T-608 review).

The patterns are parsed out of the ``validation`` blocks in ``variables.tf`` (never copied), so
the test follows the Terraform code. Terraform uses RE2 with ``can(regex(...))``; the patterns
are anchored with ``^...$`` and RE2 ``$`` matches only at the very end of the text, so
``re.fullmatch`` (which cannot match before a trailing newline) is at least as strict.
"""

import re
from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parents[3] / "infra" / "terraform" / "modules"
VAULT = "https://kv-tidal-dev.vault.azure.net"
REF = "@Microsoft.KeyVault(SecretUri=" + VAULT + "/secrets/appinsights-connection-string)"
EXEMPT = "TIDAL_CURSOR__KEYVAULT_URL"
CONN = "TIDAL_OTEL__CONNECTION_STRING"

# (name, value) pairs; first element of the value tuple is the settings name.
VARIABLE_BY_MODULE = {"app_service": "app_settings", "container_apps": "common_env"}


def _hcl_regexes(module: str) -> dict[str, str]:
    """The regex literals of the module's secret guard, HCL-unescaped, keyed by role."""
    text = (MODULES / module / "variables.tf").read_text(encoding="utf-8")
    variable = VARIABLE_BY_MODULE[module]
    line = next(
        row for row in text.splitlines() if f"keys(var.{variable})" in row and "condition" in row
    )
    literals = [m.replace("\\\\", "\\") for m in re.findall(r'regex\("((?:[^"\\]|\\.)*)"', line)]
    by_role: dict[str, str] = {}
    for lit in literals:
        if lit.startswith("^@Microsoft"):
            by_role["kvref"] = lit
        elif lit.startswith("^https://"):
            by_role["vault_url"] = lit
        elif "PASSWORD" in lit:
            by_role["name"] = lit
        elif "sig=" in lit:
            by_role["value"] = lit
    assert set(by_role) == {"kvref", "vault_url", "name", "value"}, by_role
    return by_role


def _accepts(module: str, name: str, value: str) -> bool:
    """Port of the Terraform per-key condition (reserved names are checked elsewhere)."""
    r = _hcl_regexes(module)
    ok = (
        re.fullmatch(r["kvref"], value) is not None
        or (name == EXEMPT and re.fullmatch(r["vault_url"], value) is not None)
        or re.search(r["name"], name) is None
    )
    return ok and re.search(r["value"], value) is None


@pytest.mark.parametrize("module", sorted(VARIABLE_BY_MODULE))
@pytest.mark.parametrize(
    ("name", "value", "accepted"),
    [
        (CONN, REF, True),
        (CONN, REF.replace(")", "/" + "a" * 32 + ")"), True),
        (EXEMPT, VAULT, True),
        (EXEMPT, VAULT + "/", True),
        ("TIDAL_AUTH__ALLOWED_CLIENT_IDS", "a,b", True),
        (CONN, REF + " trailing", False),
        (CONN, REF + "\nInstrumentationKey=abc", False),
        (CONN, "InstrumentationKey=abc\n" + REF, False),
        (CONN, REF.replace("@Microsoft.KeyVault", "@microsoft.keyvault"), False),
        (CONN, " " + REF, False),
        (CONN, REF.replace("kv-tidal-dev.vault.azure.net", "x.vault.azure.net.evil.com"), False),
        (EXEMPT, VAULT + "\n", False),
        (EXEMPT, VAULT + "/secrets/a", False),
        (EXEMPT, "https://example.com", False),
        (CONN, REF.replace("appinsights-connection-string", "a?sig=b"), False),
        (CONN, "InstrumentationKey=abc", False),
        ("TIDAL_CURSOR__KEY", "abc", False),
        ("TIDAL_FOO__KEYVAULT_URL", VAULT, False),
        ("TIDAL_X", "https://a.blob.core.windows.net/c?sig=abc", False),
    ],
)
def test_guard(module: str, name: str, value: str, accepted: bool) -> None:
    assert _accepts(module, name, value) is accepted


@pytest.mark.parametrize("module", sorted(VARIABLE_BY_MODULE))
def test_patterns_are_anchored(module: str) -> None:
    for role in ("kvref", "vault_url"):
        pattern = _hcl_regexes(module)[role]
        assert pattern.startswith("^")
        assert pattern.endswith("$")


def test_both_modules_use_identical_patterns() -> None:
    assert _hcl_regexes("app_service") == _hcl_regexes("container_apps")
