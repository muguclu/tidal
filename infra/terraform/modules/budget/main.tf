locals {
  tags            = merge(var.tags, { component = var.component })
  subscription_id = split("/", var.resource_group_id)[2]
  budget_name     = "budget-tidal-${var.env}"

  # 50/80/100 % of actual spend plus 100 % of forecasted spend (SPEC-07 section 2, PLANNING section 6).
  # The 80 % actual notification doubles as the SPEC-10 section 3 "budget 80 %" alert.
  notifications = {
    actual_50    = { threshold = 50, threshold_type = "Actual" }
    actual_80    = { threshold = 80, threshold_type = "Actual" }
    actual_100   = { threshold = 100, threshold_type = "Actual" }
    forecast_100 = { threshold = 100, threshold_type = "Forecasted" }
  }
}

# Global resource; also reused by the T-506 platform alerts (output action_group_id).
resource "azurerm_monitor_action_group" "this" {
  name                = "ag-tidal-${var.env}"
  resource_group_name = var.resource_group_name
  short_name          = var.action_group_short_name

  dynamic "email_receiver" {
    for_each = { for i, e in var.alert_emails : tostring(i) => e }
    content {
      name                    = "email-${email_receiver.key}"
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }

  tags = local.tags
}

# Budgets and anomaly alerts do not support tags in the azurerm schema (4.81.0); the action group above is tagged.
resource "azurerm_consumption_budget_resource_group" "this" {
  count = var.budget_scope == "resource_group" ? 1 : 0

  name              = local.budget_name
  resource_group_id = var.resource_group_id
  amount            = var.amount
  time_grain        = "Monthly"

  time_period {
    start_date = var.start_date
    end_date   = var.end_date
  }

  dynamic "notification" {
    for_each = local.notifications
    content {
      enabled        = true
      threshold      = notification.value.threshold
      threshold_type = notification.value.threshold_type
      operator       = "GreaterThan"
      contact_emails = var.alert_emails
      contact_groups = [azurerm_monitor_action_group.this.id]
    }
  }
}

resource "azurerm_consumption_budget_subscription" "this" {
  count = var.budget_scope == "subscription" ? 1 : 0

  name            = local.budget_name
  subscription_id = "/subscriptions/${local.subscription_id}"
  amount          = var.amount
  time_grain      = "Monthly"

  time_period {
    start_date = var.start_date
    end_date   = var.end_date
  }

  dynamic "notification" {
    for_each = local.notifications
    content {
      enabled        = true
      threshold      = notification.value.threshold
      threshold_type = notification.value.threshold_type
      operator       = "GreaterThan"
      contact_emails = var.alert_emails
      contact_groups = [azurerm_monitor_action_group.this.id]
    }
  }
}

# Cloud-window credit alerts (LOCAL-DEV section 2). Always subscription scope, 100 % Actual only.
resource "azurerm_consumption_budget_subscription" "credit" {
  for_each = { for a in var.credit_alert_amounts : tostring(a) => a }

  name            = "${local.budget_name}-credit-${each.key}"
  subscription_id = "/subscriptions/${local.subscription_id}"
  amount          = each.value
  time_grain      = "Monthly"

  time_period {
    start_date = var.start_date
    end_date   = var.end_date
  }

  notification {
    enabled        = true
    threshold      = 100
    threshold_type = "Actual"
    operator       = "GreaterThan"
    contact_emails = var.alert_emails
    contact_groups = [azurerm_monitor_action_group.this.id]
  }
}

# Subscription-scoped. Needs subscription-level rights; apply is human-only in this project.
resource "azurerm_cost_anomaly_alert" "this" {
  count = var.enable_anomaly_alert ? 1 : 0

  name            = "anomaly-tidal-${var.env}"
  display_name    = "Tidal ${var.env} cost anomaly"
  subscription_id = "/subscriptions/${local.subscription_id}"
  email_subject   = "Tidal ${var.env}: cost anomaly detected"
  email_addresses = var.alert_emails
}
