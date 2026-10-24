# Tidal architecture (dev environment, as defined in `infra/terraform/envs/dev`)

Nothing here has been deployed to Azure yet. The diagram shows what the Terraform and Helm code in this repository
describes. Components are taken from `infra/terraform/envs/dev/*.tf` and `deploy/`.

```mermaid
flowchart LR
  subgraph SRC["Data (synthetic only, ADR-0013)"]
    GEN["tidal synth / ingest<br/>seeded generator, validators"]
  end

  subgraph STORE["Storage"]
    BLOB[("ADLS Gen2 Blob<br/>epoch Parquet<br/>Hot / Cool / Cold by age")]
    SQL[("Azure SQL serverless<br/>hot window, 90 d<br/>auto-pause")]
    TIER["tidal-tiering<br/>Container Apps job<br/>SQL to Parquet, compaction"]
  end

  subgraph SERVE["Serving"]
    API["tidal-api<br/>App Service"]
    QRY["tidal-query<br/>Container Apps, min replicas 0<br/>DuckDB over Blob + SQL"]
  end

  subgraph BT["Backtests"]
    SB["Service Bus Basic<br/>queues"]
    KEDA["KEDA ScaledJob<br/>0 to N"]
    WRK["tidal-worker<br/>AKS spot pool"]
  end

  subgraph LEG["Legacy"]
    LA["Logic App<br/>start schedule"]
    VM["B1s VM<br/>auto-shutdown"]
  end

  subgraph SEC["Identity and secrets"]
    ENTRA["Entra ID<br/>JWT, app roles"]
    MI["Managed Identities<br/>one per workload"]
    KV["Key Vault"]
  end

  subgraph OBS["Observability and cost"]
    AI["App Insights"]
    LAW["Log Analytics<br/>daily cap"]
    COST["Budget + anomaly alert<br/>cost export"]
  end

  GH["GitHub Actions<br/>OIDC, no secrets"] --> ACR["ACR"]
  GH -. deploy .-> API
  GH -. deploy .-> QRY
  GH -. deploy .-> WRK

  GEN --> BLOB
  GEN --> SQL
  TIER --> SQL
  TIER --> BLOB

  USER["Trader / Quant client"] -->|"Bearer JWT"| API
  ENTRA -.->|validates| API
  API -->|"service token"| QRY
  QRY --> SQL
  QRY --> BLOB
  API -->|"POST /v1/backtests"| SB
  SB --> KEDA --> WRK
  WRK -->|"reads via query"| QRY
  WRK --> BLOB
  API -.->|"tiering-jobs queue"| SB
  SB -.-> TIER

  LA --> VM
  VM --> BLOB

  MI -.-> API
  MI -.-> QRY
  MI -.-> WRK
  MI -.-> TIER
  KV -.-> API

  API --> AI
  QRY --> AI
  WRK --> AI
  AI --> LAW
  COST -.-> LAW
  ACR -.->|"AcrPull"| WRK
```
