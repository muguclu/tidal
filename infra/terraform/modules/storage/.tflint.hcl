plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "azurerm" {
  enabled = true
  version = "0.32.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}

# SPEC-07 section 6: terraform destroy removes everything except bootstrap at the end of the cloud window,
# so prevent_destroy is deliberately not used. Key Vault purge protection and soft delete cover accidents.
rule "azurerm_resources_missing_prevent_destroy" {
  enabled = false
}
