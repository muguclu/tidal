# tidal Helm chart (T-307)

Backtest fan-out on AKS (SPEC-07 section 5). One KEDA `ScaledJob` runs the `tidal-worker` entrypoint of the
single Tidal image, one Job pod per `backtest-jobs` queue message, on the spot pool, scaling from 0 to
`scaledJob.maxReplicaCount`.

## Contents

| Template | Purpose |
|---|---|
| `namespace.yaml` | Namespace `tidal` with Pod Security Admission `restricted` (enforce/audit/warn) |
| `serviceaccount.yaml` | SA `tidal-worker`: workload identity label + client/tenant annotations (matches the Terraform federated credential subject `system:serviceaccount:tidal:tidal-worker`) |
| `triggerauthentication.yaml` | KEDA `podIdentity: azure-workload`, no connection string anywhere |
| `scaledjob-backtest.yaml` | ScaledJob (azure-servicebus trigger, `minReplicaCount: 0`), spot nodeSelector and both tolerations, hardened pod spec, `/tmp` emptyDir, requests/limits, explicit `TIDAL_ENV` |
| `networkpolicy.yaml` | Default deny (ingress + egress), then egress to CoreDNS and TCP 443 only. IMDS (169.254.169.254) is blocked |
| `resourcequota.yaml` | Namespace quota |

Not included yet: `secretproviderclass` (the worker needs no secrets: Managed Identity only) and
`poddisruptionbudget` (Jobs have no replicas to protect). Add them when an API/query workload moves to AKS.

## Install

Never from a subagent; for humans or the deploy workflow (T-311) after `az aks get-credentials` and
`kubelogin convert-kubeconfig -l azurecli`:

    helm upgrade --install tidal deploy/helm/tidal -n tidal -f deploy/helm/tidal/values-dev.yaml \
      --set-string image.digest=sha256:<digest> \
      --set serviceAccount.clientId=<id-worker client id> --set serviceAccount.tenantId=<tenant>

The chart creates the namespace itself, so do not pass `--create-namespace`. Use `image.digest=sha256:...`
for promotion by digest. `image.tag=latest` fails the render. Required values fail with a clear message.

Prerequisites from Terraform: the AKS module (KEDA add-on, OIDC issuer, workload identity), the
`id-worker` identity with a federated credential for `tidal/tidal-worker`, and (T-403) data-plane roles for
`id-worker`: Service Bus Data Receiver on `backtest-jobs`, Sender on `backtest-results`, Blob Data Contributor.
The KEDA scaler uses a separate identity (`keda.identityClientId`, federated to
`system:serviceaccount:kube-system:keda-operator`) holding **Service Bus Data Owner on `backtest-jobs`**, not the worker.

## Network notes

Egress is 443 only. Consequences for the worker (T-303 must respect them):
- Service Bus must use **AMQP over WebSockets** (port 443). The default AMQP port 5671 is blocked.
- Azure SQL (1433) is unreachable; the worker reads market data through `tidal-query` over HTTPS
  (`TIDAL_QUERY__BASE_URL`, ADR-0016). `TIDAL_SQL__AUTH=entra_mi` and `TIDAL_SQL__SERVER` are set only to satisfy
  Settings validation.
- The workload identity token exchange (login.microsoftonline.com:443) and Blob (443) work as is.
- Enforcement needs the cilium data plane configured by the aks module (kind's default CNI does not enforce NetworkPolicy,
  so the policies are not runtime-tested locally; this is not yet verified on a cluster).

## Park and unpark

Nothing to do in the chart. At rest the ScaledJob has no Jobs and the spot pool is at 0 nodes. `az aks stop`
and `start` (T-313) keep all objects; after `start`, KEDA resumes polling by itself. Namespace, SA, policies and
the quota cost nothing. Do not run `helm uninstall` to park: it deletes the namespace.

## Checks

All run through pinned Docker images (helm 4.3.1, kubeconform v0.8.0, kube-linter v0.8.3), offline of any cluster:

    docker run --rm -v "$PWD/deploy/helm/tidal:/c" alpine/helm:4.3.1 lint /c -f /c/values-dev.yaml
    docker run --rm -v "$PWD/deploy/helm/tidal:/c" alpine/helm:4.3.1 template tidal /c -f /c/values-dev.yaml > out.yaml
    docker run --rm -v "$PWD:/s" ghcr.io/yannh/kubeconform:v0.8.0 -strict -summary \
      -schema-location default \
      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' /s/out.yaml

kubeconform validates the KEDA CRDs against the datree CRD catalog, so nothing needs `-ignore-missing-schemas`.
kube-linter skips unknown CRDs, so lint the worker pod spec by converting the ScaledJob `jobTargetRef` into a
`batch/v1` Job first (a few lines of PyYAML), then run
`stackrox/kube-linter:v0.8.3 lint --config deploy/helm/tidal/.kube-linter.yaml lint.yaml`. Every exclusion in
`.kube-linter.yaml` carries its justification.

## Guard rails

`values.schema.json` constrains env keys (`^TIDAL_[A-Z0-9_]+$`), quantities and the pull policy. The templates
hard-code PSA `restricted`, fail on secret-looking `worker.config` keys (`CONNECTION_STRING|PASSWORD|SECRET|KEY$`),
fail on missing required config, and refuse `namespace.create=false` without `namespace.externallyManagedAck=true`.
The Namespace carries `helm.sh/resource-policy: keep`. `tests/unit/deploy/test_helm_chart.py` renders the chart and
builds `Settings` from the worker env (skipped without Docker).
