output "id" {
  description = "Resource ID of the web app."
  value       = azurerm_linux_web_app.this.id
}

output "name" {
  description = "Web app name; target of the deploy workflow (T-311) and of az webapp stop in the park script (T-313)."
  value       = azurerm_linux_web_app.this.name
}

output "default_hostname" {
  description = "Default host name (<name>.azurewebsites.net); managed TLS applies."
  value       = azurerm_linux_web_app.this.default_hostname
}

output "plan_id" {
  description = "Resource ID of the App Service plan (the B1 plan is the billed resource)."
  value       = azurerm_service_plan.this.id
}
