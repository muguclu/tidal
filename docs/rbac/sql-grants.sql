-- SQL database roles for the Tidal workload identities (SPEC-06 section 2, T-403).
-- Azure RBAC cannot grant these: they are database objects. Run once after the first apply, connected to the
-- tidal database as the SQL Entra admin (Entra-only authentication, no SQL logins). Idempotent.
--   sqlcmd -S <server>.database.windows.net -d <database> -G -i docs/rbac/sql-grants.sql
-- id-worker has NO SQL access (ADR-0016): it reads market data through tidal-query. id-legacy-vm has none either.
-- Requires the schemas ref, md and ops (sql/migrations/0001_init.sql).

-- id-api: read everywhere, execute the ops procedures, write ops.backtest_job only.
IF USER_ID('id-api') IS NULL CREATE USER [id-api] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-api];
GRANT EXECUTE ON SCHEMA::ops TO [id-api];
-- Backtest job store and in-process result aggregator (T-607): the API inserts and updates job rows.
-- No DELETE, and no db_datawriter (which would open md and ref to writes).  Idempotent.
GRANT SELECT, INSERT, UPDATE ON ops.backtest_job TO [id-api];

-- id-query: read only.
IF USER_ID('id-query') IS NULL CREATE USER [id-query] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-query];

-- id-ingest: read and write md, ref and ops (checkpoints, instrument first/last timestamps).
IF USER_ID('id-ingest') IS NULL CREATE USER [id-ingest] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-ingest];
ALTER ROLE db_datawriter ADD MEMBER [id-ingest];

-- id-tiering: read, write and delete (db_datawriter includes DELETE) hot rows after they are tiered to Parquet.
IF USER_ID('id-tiering') IS NULL CREATE USER [id-tiering] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-tiering];
ALTER ROLE db_datawriter ADD MEMBER [id-tiering];
