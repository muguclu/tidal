"""The dev environment's planned app settings must start and authenticate the apps (T-608).

T-604 review: after a dev apply neither app started. This test reads the setting names that
``infra/terraform/envs/dev/{apps,entra}.tf`` assign to the tidal-api web app and the tidal-query
container app, fills them with placeholder values (placeholder GUIDs, no network, no Azure) and
builds ``Settings`` plus the startup paths that raised ``ConfigError`` (cursor key, query
validator, API validator). The values Terraform computes at apply time are replaced by fixed
placeholders; the keys are what the test pins.
"""

import base64
import re
from pathlib import Path

import pytest

from tidal.api.auth import (
    EntraApiValidator,
    EntraTokenValidator,
    api_validator_from_settings,
    query_validator_from_settings,
    resolve_cursor_key,
)
from tidal.common.config import Settings
from tidal.common.errors import ConfigError

DEV = Path(__file__).resolve().parents[3] / "infra" / "terraform" / "envs" / "dev"
MODULES = DEV.parent.parent / "modules"

TENANT = "11111111-1111-1111-1111-111111111111"
API_APP = "22222222-2222-2222-2222-222222222222"
CLI_APP = "33333333-3333-3333-3333-333333333333"
QUERY_APP = "44444444-4444-4444-4444-444444444444"
ID_API = "55555555-5555-5555-5555-555555555555"
ID_WORKER = "66666666-6666-6666-6666-666666666666"
OID_API = "77777777-7777-7777-7777-777777777777"
OID_WORKER = "88888888-8888-8888-8888-888888888888"
VAULT = "https://kv-tidal-dev.vault.azure.net"

# Placeholder for every value Terraform computes at apply time, by config-contract key.
ENTRA_PLACEHOLDERS = {
    "TIDAL_AUTH__AUDIENCE": "api://tidal-api-dev",
    "TIDAL_AUTH__API_APP_CLIENT_ID": API_APP,
    "TIDAL_AUTH__ALLOWED_CLIENT_IDS": CLI_APP,
    "TIDAL_QUERY__AUDIENCE": "api://tidal-query-dev",
    "TIDAL_QUERY__APP_CLIENT_ID": QUERY_APP,
    "TIDAL_AUTH__QUERY_CALLER_CLIENT_IDS": f"{ID_API},{ID_WORKER}",
    "TIDAL_AUTH__QUERY_CALLER_OBJECT_IDS": f"{OID_API},{OID_WORKER}",
}
OTHER_PLACEHOLDERS = {
    "TIDAL_STORAGE__ACCOUNT_URL": "https://sttidaldev.blob.core.windows.net",
    "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
    "TIDAL_SQL__SERVER": "sql-tidal-dev.database.windows.net",
    "TIDAL_SQL__DATABASE": "tidal",
    "TIDAL_SQL__AUTH": "entra_mi",
    "TIDAL_SERVICEBUS__FQDN": "sb-tidal-dev.servicebus.windows.net",
    "TIDAL_AUTH__TENANT_ID": TENANT,
    "TIDAL_QUERY__BASE_URL": "https://ca-tidal-query.example.azurecontainerapps.io",
    "TIDAL_CURSOR__KEYVAULT_URL": VAULT,
    # App Service / Container Apps deliver the resolved Key Vault secret under this name.
    "TIDAL_OTEL__CONNECTION_STRING": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
}
KEY_RE = re.compile(r"^\s*(TIDAL_[A-Z0-9_]+)\s*=", re.MULTILINE)


def _block(text: str, start_marker: str) -> str:
    """Text inside the first bracket pair after ``start_marker`` (brackets balanced)."""
    begin = text.index(start_marker) + len(start_marker)
    opener = next(i for i in range(begin, len(text)) if text[i] in "{(")
    closer = {"{": "}", "(": ")"}[text[opener]]
    depth = 0
    for i in range(opener, len(text)):
        if text[i] == text[opener]:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[opener + 1 : i]
    raise AssertionError(f"unbalanced block after {start_marker!r}")


def _keys(block: str) -> set[str]:
    return set(KEY_RE.findall(block))


def _list_keys(text: str, local_name: str) -> set[str]:
    """Key names in ``for k in ["A", "B"] : k => local.entra_config[k]`` of a local."""
    block = _block(text, f"{local_name} = ")
    match = re.search(r"in \[([^\]]*)\]", block)
    assert match, local_name
    return set(re.findall(r'"(TIDAL_[A-Z0-9_]+)"', match.group(1)))


@pytest.fixture(scope="module")
def planned() -> dict[str, dict[str, str]]:
    """Planned environment per app, keys read from the .tf files, values placeholders."""
    apps = (DEV / "apps.tf").read_text(encoding="utf-8")
    entra = (DEV / "entra.tf").read_text(encoding="utf-8")
    entra_config = _keys(_block(entra, "entra_config = "))
    assert entra_config == set(ENTRA_PLACEHOLDERS), "entra_config keys changed; update the test"

    common = _keys(_block(apps, "apps_common_env = "))
    api_entra = _list_keys(apps, "apps_api_entra_env")
    query_entra = _list_keys(apps, "apps_query_entra_env")
    assert api_entra <= entra_config
    assert query_entra <= entra_config

    api_block = _block(apps[apps.index('module "app_service"') :], "app_settings = merge")
    assert "local.apps_api_entra_env" in api_block
    api_keys = common | api_entra | _keys(api_block)

    query_module = apps[apps.index('module "container_apps"') : apps.index('module "app_service"')]
    query_block = _block(query_module, "query = ")
    assert "local.apps_query_entra_env" in _block(query_block, "extra_env = ")
    query_keys = common | query_entra | _keys(query_block)

    values = {**OTHER_PLACEHOLDERS, **ENTRA_PLACEHOLDERS}
    out = {}
    for name, keys in (("api", api_keys), ("query", query_keys)):
        env = {k: values[k] for k in sorted(keys)}
        env.update(TIDAL_ENV="dev", TIDAL_SERVICE_NAME=name, AZURE_CLIENT_ID=ID_API)
        out[name] = env
    return out


class _FakeVault:
    """In-memory stand-in for Key Vault: only the cursor secret exists."""

    def get(self, name: str) -> str:
        if name == "cursor-hmac-key":
            return base64.b64encode(bytes(range(32))).decode()
        raise KeyError(name)


def test_api_settings_build_and_cursor_key_resolves(planned: dict[str, dict[str, str]]) -> None:
    settings = Settings.from_env(planned["api"])
    assert settings.cursor.keyvault_url is not None
    assert resolve_cursor_key(settings, _FakeVault()) is not None


def test_api_validator_accepts_the_cli_client_and_both_audiences(
    planned: dict[str, dict[str, str]],
) -> None:
    settings = Settings.from_env(planned["api"])
    assert settings.auth.allowed_clients() == {CLI_APP}
    assert settings.auth.api_app_client_id == API_APP
    assert settings.query.app_client_id == QUERY_APP
    assert isinstance(api_validator_from_settings(settings), EntraApiValidator)


def test_query_validator_starts_with_caller_allowlists(planned: dict[str, dict[str, str]]) -> None:
    settings = Settings.from_env(planned["query"])
    validator = query_validator_from_settings(settings)
    assert isinstance(validator, EntraTokenValidator)
    assert settings.auth.caller_client_ids() == {ID_API, ID_WORKER}
    assert settings.auth.caller_object_ids() == {OID_API, OID_WORKER}
    assert settings.query.app_client_id == QUERY_APP


def test_otel_connection_string_is_a_key_vault_reference_not_a_value() -> None:
    apps = (DEV / "apps.tf").read_text(encoding="utf-8")
    assert "@Microsoft.KeyVault(SecretUri=${local.apps_otel_secret_uri})" in apps
    assert "app_insights_connection_string" not in apps
    assert "TIDAL_OTEL__CONNECTION_STRING = local.apps_otel_secret_uri" in apps


def test_missing_wiring_would_still_fail(planned: dict[str, dict[str, str]]) -> None:
    """Guards the test itself: dropping the keys this task added reproduces the T-604 failures."""
    no_cursor = {k: v for k, v in planned["api"].items() if k != "TIDAL_CURSOR__KEYVAULT_URL"}
    with pytest.raises(ConfigError):
        resolve_cursor_key(Settings.from_env(no_cursor), _FakeVault())
    no_callers = {
        k: v for k, v in planned["query"].items() if not k.startswith("TIDAL_AUTH__QUERY_CALLER")
    }
    with pytest.raises(ConfigError):
        Settings.from_env(no_callers)


@pytest.mark.parametrize("module", ["app_service", "container_apps"])
def test_guard_exempts_only_the_cursor_vault_url(module: str) -> None:
    text = (MODULES / module / "variables.tf").read_text(encoding="utf-8")
    assert text.count('k == "TIDAL_CURSOR__KEYVAULT_URL"') == 1
    assert "PASSWORD|CONNECTION_STRING|SECRET|KEY|TOKEN|SAS|CREDENTIAL" in text
