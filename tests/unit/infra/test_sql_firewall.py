"""SQL firewall wiring (T-610): single-IP rules only, no Allow Azure services, list built from outbound IPs."""

import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[3] / "infra" / "terraform"
SQL_MAIN = (INFRA / "modules" / "sql" / "main.tf").read_text(encoding="utf-8")
SQL_VARS = (INFRA / "modules" / "sql" / "variables.tf").read_text(encoding="utf-8")
DEV_APPS = (INFRA / "envs" / "dev" / "apps.tf").read_text(encoding="utf-8")
DEV_MAIN = (INFRA / "envs" / "dev" / "main.tf").read_text(encoding="utf-8")


def _block(text: str, header: str) -> str:
    start = text.index(header)
    return text[start : text.index("\n}\n", start)]


def test_rule_is_single_address_and_only_with_public_access() -> None:
    rule = _block(SQL_MAIN, 'resource "azurerm_mssql_firewall_rule" "allowed_ip"')
    assert "local.public_network_access_enabled ? toset(var.allowed_ip_addresses)" in rule
    assert "start_ip_address = each.value" in rule
    assert "end_ip_address   = each.value" in rule


def test_no_open_range_rules() -> None:
    assert "255.255.255.255" not in SQL_MAIN + DEV_APPS + DEV_MAIN
    starts = re.findall(r'start_ip_address\s*=\s*"([^"]+)"', SQL_MAIN)
    assert starts == ["0.0.0.0"]  # only the optional allow_azure_services rule
    assert "allow_azure_services" not in DEV_MAIN


def test_module_validation_rejects_zero_and_ranges() -> None:
    var = _block(SQL_VARS, 'variable "allowed_ip_addresses"')
    assert 'ip != "0.0.0.0"' in var
    assert "/" not in re.search(r"regex\(\"([^\"]+)\"", var).group(1)  # type: ignore[union-attr]
    assert "default     = []" in var


def test_dev_list_is_built_from_outbound_ips() -> None:
    assert "allowed_ip_addresses       = local.sql_allowed_ip_addresses" in DEV_MAIN
    local = DEV_APPS[DEV_APPS.index("sql_allowed_ip_addresses = ") :].split("apps_image")[0]
    assert "var.enable_private_networking ? []" in local
    assert "module.app_service[*].possible_outbound_ip_addresses" in local
    assert "data.azurerm_container_app.query_egress[*].outbound_ip_addresses" in local
    assert "var.sql_admin_ip_addresses" in local
