# vm_legacy

The "VM workload" of the portfolio: a **Standard_B1s** Ubuntu 24.04 VM that runs a plain Python script from systemd (no container), and a **Logic App (Consumption)** that starts it daily (SPEC-07 section 2).

- **Flow:** Logic App starts the VM at 21:55 W. Europe time with its own managed identity. The VM boots, `tidal-exporter.service` (oneshot, `legacy/exporter/`) reads recent `bars_1d` Parquet from `marketdata`, writes `risk-feed/daily_bars_<date>.csv` to `exports`, then deallocates the VM through the Resource Manager API (IMDS token). A daily auto-shutdown at 20:00 is the safety net.
- **No public IP, no inbound:** the NIC sits in a private subnet behind an NSG that denies all inbound and allows only outbound 80/443 to the internet. Outbound uses the platform default outbound access of the subnet (a NAT gateway or public IP costs more than the VM, see `docs/finops/deltas/T-310.md`). Pass `subnet_id` once T-404 provides `snet-vm`.
- **Identities (SPEC-06):** the VM runs as the user-assigned `id-legacy-vm` (Blob roles are assigned in `envs/dev/vm.tf`, scoped to containers). Two custom roles, each with exactly one action and assignable only on this VM: `tidal-vm-self-deallocate-<env>` (`deallocate/action`, held by `id-legacy-vm`) and `tidal-vm-start-<env>` (`start/action`, held by the Logic App system-assigned identity). Neither identity can read, change or delete the VM.
- **Config:** cloud-init (`custom_data`, rendered by the env composition) writes `/etc/tidal/exporter.env` with `TIDAL_ENV` set explicitly (ADR-0012), the storage account URL and the identity client ID. No secret is rendered.
- **Outbound access is load-bearing:** `default_outbound_access_enabled = true` on the subnet is what lets the VM reach Blob, the IMDS-token audience `management.azure.com` and PyPI. Removing it (or moving to a subnet without outbound) breaks both the export and the self-deallocate. T-404 follow-up: Blob service endpoint plus NAT or an explicit IP.
- **First boot:** the exporter retries token and deallocate with backoff for about 5 minutes to cover role-assignment propagation; the unit has `TimeoutStartSec=15min`. Dependencies install with `--require-hashes --only-binary=:all:` from the hash-locked `legacy/exporter/requirements.txt`.
- **Cost levers:** the VM is deallocated except for the run (compute meter off), `start_enabled = false` pauses the daily start, `enable_legacy_vm = false` in the env removes everything. Logs stay in journald on the VM: no Log Analytics agent, so no ingestion cost.
- **Security defaults:** SSH key only (password login off), VM extensions disabled, no data disk, managed boot diagnostics.

## Checkov

No skips: `checkov -d examples/basic` passes all checks.

Usage: see `examples/basic`.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
| <a name="requirement_azurerm"></a> [azurerm](#requirement\_azurerm) | ~> 4.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_azurerm"></a> [azurerm](#provider\_azurerm) | ~> 4.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [azurerm_dev_test_global_vm_shutdown_schedule.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/dev_test_global_vm_shutdown_schedule) | resource |
| [azurerm_linux_virtual_machine.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/linux_virtual_machine) | resource |
| [azurerm_logic_app_action_custom.start_vm](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/logic_app_action_custom) | resource |
| [azurerm_logic_app_trigger_recurrence.daily](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/logic_app_trigger_recurrence) | resource |
| [azurerm_logic_app_workflow.start](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/logic_app_workflow) | resource |
| [azurerm_network_interface.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/network_interface) | resource |
| [azurerm_network_interface_security_group_association.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/network_interface_security_group_association) | resource |
| [azurerm_network_security_group.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/network_security_group) | resource |
| [azurerm_role_assignment.self_deallocate](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.start](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_definition.self_deallocate](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_definition) | resource |
| [azurerm_role_definition.start](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_definition) | resource |
| [azurerm_subnet.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/subnet) | resource |
| [azurerm_subnet_network_security_group_association.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/subnet_network_security_group_association) | resource |
| [azurerm_virtual_network.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/virtual_network) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_admin_ssh_public_key"></a> [admin\_ssh\_public\_key](#input\_admin\_ssh\_public\_key) | SSH public key for the VM admin user. Password login is disabled and the VM has no public IP and no inbound rule, so the key is only a provisioning requirement of Azure; nobody can reach port 22. | `string` | n/a | yes |
| <a name="input_admin_username"></a> [admin\_username](#input\_admin\_username) | Admin user name of the VM. | `string` | `"tidaladmin"` | no |
| <a name="input_auto_shutdown_time"></a> [auto\_shutdown\_time](#input\_auto\_shutdown\_time) | Daily auto-shutdown time as HHMM in start\_time\_zone (SPEC-07: 2000). Safety net if the exporter fails to deallocate. | `string` | `"2000"` | no |
| <a name="input_component"></a> [component](#input\_component) | Value of the component tag for the resources in this module (for example legacy-vm); merged over tags["component"] inside the module. | `string` | n/a | yes |
| <a name="input_custom_data"></a> [custom\_data](#input\_custom\_data) | Rendered cloud-init document (envs/dev/vm.tf renders legacy/exporter/cloud-init.yaml.tftpl). Must not contain secrets: it is readable from the VM metadata. | `string` | n/a | yes |
| <a name="input_encryption_at_host_enabled"></a> [encryption\_at\_host\_enabled](#input\_encryption\_at\_host\_enabled) | Encrypt temp disk and caches at the host. Needs the Microsoft.Compute/EncryptionAtHost feature registered on the subscription, so it is off by default. | `bool` | `false` | no |
| <a name="input_env"></a> [env](#input\_env) | Environment name used in resource names (dev or prod). | `string` | n/a | yes |
| <a name="input_identity_id"></a> [identity\_id](#input\_identity\_id) | Resource ID of the user-assigned managed identity id-legacy-vm attached to the VM. | `string` | n/a | yes |
| <a name="input_identity_principal_id"></a> [identity\_principal\_id](#input\_identity\_principal\_id) | Object (principal) ID of id-legacy-vm; receives the custom role vm-self-deallocate on the VM only (SPEC-06 section 2). | `string` | n/a | yes |
| <a name="input_image_version"></a> [image\_version](#input\_image\_version) | Version of the Canonical ubuntu-24\_04-lts server image, pinned so a re-created VM is reproducible (not latest). Check current versions with az vm image list --publisher Canonical --offer ubuntu-24\_04-lts --sku server --all and bump deliberately. UNVERIFIED default: confirm it exists before the first apply. | `string` | `"24.04.202510160"` | no |
| <a name="input_location"></a> [location](#input\_location) | Azure region. | `string` | `"westeurope"` | no |
| <a name="input_os_disk_type"></a> [os\_disk\_type](#input\_os\_disk\_type) | OS disk SKU. StandardSSD\_LRS is the cheapest managed SSD; the 30 GiB Ubuntu image bills as an E4 disk. | `string` | `"StandardSSD_LRS"` | no |
| <a name="input_resource_group_name"></a> [resource\_group\_name](#input\_resource\_group\_name) | Resource group that holds the VM, network and Logic App. | `string` | n/a | yes |
| <a name="input_start_enabled"></a> [start\_enabled](#input\_start\_enabled) | Whether the Logic App recurrence is enabled. false is the park lever for the daily VM start (scripts/park.sh, T-313). | `bool` | `true` | no |
| <a name="input_start_hour"></a> [start\_hour](#input\_start\_hour) | Hour (0-23, in start\_time\_zone) at which the Logic App starts the VM. SPEC-07: 21:55. | `number` | `21` | no |
| <a name="input_start_minute"></a> [start\_minute](#input\_start\_minute) | Minute (0-59) at which the Logic App starts the VM. | `number` | `55` | no |
| <a name="input_start_time_zone"></a> [start\_time\_zone](#input\_start\_time\_zone) | Windows time zone name used by the Logic App recurrence and the auto-shutdown schedule (SPEC-07 section 2 says CET). | `string` | `"W. Europe Standard Time"` | no |
| <a name="input_subnet_id"></a> [subnet\_id](#input\_subnet\_id) | Existing subnet for the VM NIC (snet-vm from the network module, T-404). null creates a small private VNet and subnet in this module. | `string` | `null` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tag map from module.resource\_group.tags. Must contain project, env, owner, cost\_center, component, park (non-empty), park must be true or false, project must be tidal. The component tag is overridden per resource from var.component. | `map(string)` | n/a | yes |
| <a name="input_vm_size"></a> [vm\_size](#input\_vm\_size) | VM size. Standard\_B1s is the SPEC-07 section 2 choice (burstable, 1 vCPU / 1 GiB, enough for a CSV export that runs minutes per day). | `string` | `"Standard_B1s"` | no |
| <a name="input_vnet_address_space"></a> [vnet\_address\_space](#input\_vnet\_address\_space) | Address space of the VNet created when subnet\_id is null. Must not overlap the T-404 network. | `string` | `"10.250.0.0/28"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_logic_app_id"></a> [logic\_app\_id](#output\_logic\_app\_id) | Resource ID of the Logic App (Consumption) that starts the VM daily. |
| <a name="output_logic_app_name"></a> [logic\_app\_name](#output\_logic\_app\_name) | Name of the Logic App; the park script disables it with az logic workflow update --state Disabled (or the start\_enabled variable). |
| <a name="output_logic_app_principal_id"></a> [logic\_app\_principal\_id](#output\_logic\_app\_principal\_id) | Principal ID of the Logic App system-assigned identity (holds only the start role on the VM). |
| <a name="output_private_ip_address"></a> [private\_ip\_address](#output\_private\_ip\_address) | Private IP of the VM NIC. There is no public IP. |
| <a name="output_subnet_id"></a> [subnet\_id](#output\_subnet\_id) | Subnet of the VM NIC (created here or the one passed in). |
| <a name="output_vm_id"></a> [vm\_id](#output\_vm\_id) | Resource ID of the legacy VM; scope of the park script (az vm deallocate) and of its two custom roles. |
| <a name="output_vm_name"></a> [vm\_name](#output\_vm\_name) | Name of the legacy VM (vm-tidal-legacy-<env>). |
<!-- END_TF_DOCS -->
