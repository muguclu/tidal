# Runbook: restore and rebuild data (T-602)

Purpose: recover the hot store (Azure SQL), recover deleted cold files (Blob), and rebuild data by re-running tiering or
re-generating synthetic data.

Steps that need a deployed environment are marked **[deployed]**. Names: resource group `<rg-name>` (terraform output
`resource_group_name`), SQL server `sql-tidal-dev-<suffix>`, database `tidal`, storage account `sttidaldev<suffix>`
(output `storage_account_name`). Do not run restores against a live system without telling the others who use it.

## What can be recovered, and how
| Data | Where | Recovery | Window |
|---|---|---|---|
| Hot rows (`md.bar_hot`, `md.tick_hot`, job metadata) | Azure SQL | point-in-time restore (section 1) | Azure default PITR retention; not set in IaC |
| Deleted or overwritten blob | Blob soft delete (section 2) | undelete | `soft_delete_days`, default 7 |
| Deleted container | container soft delete | restore container | same, 7 days |
| Cold Parquet older than the window | `marketdata/v1/...` | rebuild (section 3), there is no copy | n/a |
| Terraform-managed resources | Azure | `terraform apply` from `infra/terraform/envs/dev` | n/a |

What the IaC does and does not turn on:
- **SQL**: serverless `GP_S_Gen5_2`, backup storage `Local` (no zone or geo copy), `geo_backup_enabled = false`
  (`modules/sql/main.tf`). The module sets no short-term retention, so the Azure default applies. Azure documents 7 days for
  new databases. **Unverified for this deployment**: read it before you rely on it (section 1, step 1). A restore into another
  region is not possible, because geo backup is off. This is a cost choice (`docs/specs/SPEC-07-infra-platform.md`).
- **Storage**: LRS, **blob versioning is off on purpose** (`versioning_enabled = false`: Parquet parts are immutable, and
  restatements are new part files). Soft delete is on for blobs and containers. There are no snapshots or immutability
  policies. ADR-0014 is about row versions in the tiering job, not Blob versioning.
- **Key Vault**: soft delete and purge protection on. See `docs/runbooks/key-rotation.md`.

## 1. Azure SQL point-in-time restore **[deployed]**
A restore always creates a new database. It never overwrites the existing one.

Prerequisites: `az login`, `SQL DB Contributor` on the SQL server, time-bound (see [Access](README.md#access)). If SQL is
unreachable from your network, do not open public access or add firewall rules by hand; ask the owner. The new database is billed like any serverless
database. The free offer (`use_free_offer`) is set on the Terraform-managed database only, so the copy is billed normally
until you delete it. Check the cost impact against `docs/finops/cost-model.md` first.

1. Check retention and the earliest restore point:
   ```bash
   az sql db show -g <rg-name> -s sql-tidal-dev-<suffix> -n tidal --query "earliestRestoreDate"
   az sql db str-policy show -g <rg-name> -s sql-tidal-dev-<suffix> -n tidal
   ```
2. Restore to a point in time (UTC, ISO 8601), into a new name:
   ```bash
   az sql db restore -g <rg-name> -s sql-tidal-dev-<suffix> -n tidal \
     --dest-name tidal_restore --time "2026-10-24T09:30:00Z"
   ```
   A paused serverless database restores normally; the restore resumes nothing and takes minutes for a database this small.
3. Check the copy before you use it. Entra users and roles are stored inside the database, so they come with the copy.
   Connect with an Entra token (there is no SQL login), for example with `sqlcmd -G`, and compare row counts and the newest
   timestamps with what you expect:
   ```sql
   SELECT COUNT(*), MAX(ts_ns) FROM md.bar_hot;
   SELECT COUNT(*), MAX(ts_ns) FROM md.tick_hot;
   ```
4. Switch over. Pick one:
   - **Swap names** (no config change): rename the broken database, then rename `tidal_restore` to `tidal`
     (`az sql db rename -g <rg-name> -s sql-tidal-dev-<suffix> -n tidal --new-name tidal_broken`, then the same for
     `tidal_restore` to `tidal`).
     Do not run `terraform state rm` or `terraform import`. Terraform tracks the database by resource ID, which
     contains the name, so after the swap the state already refers to the restored database. **Human only:** run
     `terraform plan` in `infra/terraform/envs/dev`. It must show no replace (`-/+`) of
     `module.sql.azurerm_mssql_database.tidal`. In-place updates (tags, `azapi_update_resource.free_offer`) are expected.
     If it shows a replace or destroy, stop and do not apply. Delete `tidal_broken` once the restored database is
     verified, because it is billed and holds the free-offer flag.
   - **Copy back only what is missing** with SQL from the restored copy into `tidal`. Prefer this for a small loss.
5. Free-offer property: after a swap, re-apply so `azapi_update_resource.free_offer` sets it on the new database.

After a restore, the hot store may contain rows that tiering had already moved to Parquet and deleted from SQL, or may
lack rows that were ingested after the restore point. Both are handled by section 3.

Verification: the API `GET` for a recent range returns the expected bars; `tidal db migrate` reports nothing to apply
if the schema is current.

Rollback: nothing was changed until step 4. Drop the copy with `az sql db delete -g <rg-name> -s sql-tidal-dev-<suffix> -n tidal_restore --yes`.
After a swap, rename back the same way.

## 2. Blob soft delete **[deployed]**
Prerequisites: a time-bound `Storage Blob Data Contributor` on the container (see [Access](README.md#access)). Shared key
is off, so all commands use `--auth-mode login`. Writing blob index tags needs more than Contributor (`id-tiering` has
Owner for that reason), so re-tag through the tiering job, not from a laptop.

A delete of a blob or container by an unexpected caller raises `alert-tidal-blob-audit-<env>`
(`modules/monitoring`). Act within 7 days.

List soft-deleted blobs and undelete one:
```bash
az storage blob list --account-name sttidaldev<suffix> -c marketdata --prefix v1/<dataset>/ \
  --include d --auth-mode login --query "[?deleted].name" -o tsv
az storage blob undelete --account-name sttidaldev<suffix> -c marketdata -n <blob-name> --auth-mode login
```
For many blobs, undelete one prefix at a time and re-list. Containers: `az storage container list --include-deleted`,
then `az storage container restore --name <container> --deleted-version <version> --account-name sttidaldev<suffix> --auth-mode login`.

An overwritten blob keeps its previous state as a soft-deleted snapshot. `az storage blob undelete` restores the
snapshots but does not change the current blob. Then copy the snapshot over the base blob:
1. Find the snapshot time with
   `az storage blob list --account-name sttidaldev<suffix> -c marketdata --include sd --prefix <blob-name> --auth-mode login`.
2. Copy it over with
   `az storage blob copy start --account-name sttidaldev<suffix> -c marketdata -b <blob-name> --source-uri "https://sttidaldev<suffix>.blob.core.windows.net/marketdata/<blob-name>?snapshot=<snapshot>" --auth-mode login`.

Only 7 days exist: past that, use section 3.

After the restore:
- The blob index tags (`age_band`, `compacted`) come back with the blob. Check one with `az storage blob tag list`.
- The lifecycle policy moves tiers about once a day; a restored blob may be re-tiered within 24 to 48 hours.
- Run `uv run tidal tiering retag` if tags look wrong. It recomputes `age_band` for all blobs.

Verification: a query for the restored range returns the same rows as before (compare with a saved response or the
benchmark data), and `az storage blob list` shows the blob without `deleted`.

Rollback: delete the restored blob again (`az storage blob delete`). It will be soft deleted for another 7 days.

## 3. Re-run tiering, or rebuild from source
Tiering moves rows older than the hot window from SQL to Parquet, then deletes them from SQL, so once a month is cold
**the SQL copy no longer exists**. Rebuild cold data from the source, not from SQL.

### Re-run tiering (safe, idempotent, resumable)
Use after a SQL restore, after a failed job, or to refill a restored range. The rules for equal rows, restatements and
conflicts are in `docs/adr/0014-tiering-versioning.md`.
**Default path: send a `tiering.job/v1` message** to the `tiering-jobs` queue (payload in
`docs/contracts/messaging-contract.md` section 3, for example `{"table": "md.bar_hot", "hot_window_days": 90,
"dry_run": true, "batch_rows": 500000}`; run with `dry_run` true first). The worker runs it as `id-tiering`, which
already has the roles it needs. Failures land in the DLQ (`docs/runbooks/dlq.md`).

Manual path, same code, from a shell. It needs the Access roles above on storage, SQL and the queue, and the environment
from `terraform output -json tidal_config`: export every key (it contains no secrets), including `TIDAL_ENV=dev`.
```bash
uv run tidal tiering run --dry-run                       # plan only, changes nothing
uv run tidal tiering run --table bar_hot --table tick_hot
uv run tidal tiering retag                               # yearly; also after a blob restore
uv run tidal tiering compact --dataset bars_1d --year 2020
```

Exit code 0 means done, or another run holds the lock (then wait and re-run). Exit code 1 means a data error in at least one
table; the output line shows `errors=<n>` and the log has the detail (a `TieringConflict` is a same-key, same-version row
with different values: stop and read ADR-0014, do not force it).

Verification: the dry run reports `moved=0` after a real run, and a query across the hot/cold boundary returns each bar once.

### Re-ingest synthetic data
Synthetic data is deterministic: the same seed, the same date range and the same clock give byte-identical output. Groups already
in the store are skipped, so a re-run resumes rather than duplicates.
```bash
export TIDAL_CLOCK__NOW=2026-10-01T00:00:00Z     # fix the clock, else ingested_at differs per run
uv run tidal synth daily-history --start 2006-01-01 --seed <seed>
uv run tidal synth --scale S --seed <seed> --require-empty
```
Use the seed and `TIDAL_SYNTH__START` / `TIDAL_SYNTH__END` that built the original data (see the benchmark and load-test
reports in `docs/finops/` for the values used there). With `--require-empty`, a group that already exists fails the run instead of
being skipped. The first command writes bars only; the second writes ticks and bars at scale S, M or L.

After a re-run, run `uv run tidal tiering run` to move anything older than the hot window back to Parquet.

Cost note: scale L writes a lot of data; check `docs/finops/` for the size and egress numbers before running it in Azure.

### Real market data (EIA, FRED)
There is no ingest command for the real sources in the CLI yet (`tidal --help` lists `synth`, `db`, `tiering`, `dlq`
only). The verbatim source responses live in the `raw` container (lifecycle: Cool at 30 days, Archive at 180 days, so older
ones need rehydration, which takes hours). Treat a loss of real data as a manual task and raise it with the orchestrator.

## Related
- [Access](README.md#access)
- `docs/runbooks/dlq.md`, `docs/runbooks/park.md`
- `docs/adr/0014-tiering-versioning.md`, `docs/specs/SPEC-07-infra-platform.md`
