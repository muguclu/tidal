plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "azurerm" {
  enabled = true
  version = "0.32.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}

# prevent_destroy would block `terraform destroy` of a dev environment (the cloud window ends with a destroy,
# docs/LOCAL-DEV.md section 2). Durable data lives in Blob; the hot store is re-creatable from the migrations.
rule "azurerm_resources_missing_prevent_destroy" {
  enabled = false
}
