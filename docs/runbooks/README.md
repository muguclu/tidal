# Runbooks

Each runbook gives purpose, prerequisites, exact commands, verification and rollback. Steps that need a deployed Azure
environment are marked **[deployed]**. Environment-specific values are placeholders such as `<rg-name>`; the runbook says
which Terraform output holds them.

| Runbook | Use it to |
|---|---|
| [bootstrap.md](bootstrap.md) | Create the Terraform state storage and the CI identities, once per subscription. |
| [images.md](images.md) | Build, scan and push the container image from CI. |
| [deploy.md](deploy.md) | Roll one image digest to App Service, Container Apps, jobs and the AKS worker. |
| [park.md](park.md) | Stop dev compute to save cost (quick park), and bring it back. |
| [dlq.md](dlq.md) | Inspect dead-lettered Service Bus messages, then replay or discard them. |
| [key-rotation.md](key-rotation.md) | Rotate the one signing key, revoke CI/deploy OIDC access, and handle a suspected leak. |
| [restore.md](restore.md) | Restore Azure SQL or deleted blobs, re-run tiering, re-generate synthetic data. |
| [integration-tests.md](integration-tests.md) | Run the integration tests against local emulators. |
