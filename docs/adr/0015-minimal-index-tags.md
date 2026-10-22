# ADR-0015: Minimal blob index tags (two tags, tick parts only)

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator, data-engineer)
- Related: SPEC-03 §3/§3.1/§4, storage-layout §4, ADR-0004, ADR-0014, T-205, T-207, T-211, T-503

## Context
T-205 writes five blob index tags (`dataset`, `instrument`, `data_year`, `age_band`, `compacted`) on
every part of all five datasets. Azure bills index tags per tag per month, in every tier, hot
included. The T-207 model (`docs/finops/deltas/T-207_model.py`, output in `docs/finops/deltas/T-207.md`;
modelled, not measured, prices of 2026-10-22) shows that this costs more than the tiering saves.
Only `age_band` and `compacted` are read by the lifecycle rules (`modules/storage/lifecycle.tf`); the
other three are recoverable from the path (`layout.parse_md_path`).

## Options (net EUR/month versus keeping everything Hot, scales S / M / L, all from T-207)
a. **Keep 5 tags on all five datasets.** 3,675 / 24,500 / 49,000 tags; tag cost 0.0126 / 0.0840 /
   0.1681; net **-0.0104 / -0.0663 / -0.0445**. Loses at every scale; at L the tag charge alone is 112 %
   of the at-rest saving (0.1494). Rejected.
b. **(chosen) 2 tags (`age_band`, `compacted`), ticks only.** 180 / 1,200 / 2,400 tagged parts; tag
   cost 0.0012 / 0.0082 / 0.0165; net **+0.0010 / +0.0100 / +0.1108**.
c. **Ticks only, 5 tags.** Tag cost 0.0031 / 0.0206 / 0.0412; net -0.0008 / -0.0023 / +0.0861. Still
   negative at S and M, because the 3 path-derivable tags are pure cost. Rejected.
d. **No tiering (all Hot).** Net 0 by definition. Gives up the at-rest saving 0.0025 / 0.0202 /
   0.1494, which option (b) keeps after its tag cost. Rejected, but it is the fallback if T-503
   measures a negative result for (b).

## Decision
1. Write only `age_band` and `compacted`. `dataset`, `instrument` and `data_year` are not written;
   consumers derive them from the path.
2. Tags are written only for datasets in `tidal.storage.retag.TAGGED_DATASETS`, a module constant
   defaulting to `("ticks",)` (not a config key: no config-contract change). Parts of `bars_*`
   stay untagged; no lifecycle rule matches them, so they stay Hot. The migration, compaction and
   retag make no tag call for them.
3. **Decision rule.** A dataset joins the list when its measured average part size exceeds the
   2-tag break-even (S / M / L: 1.50 / 1.13 / 0.85 MB; 5 tags: 3.74 / 2.81 / 2.05 MB). Average tick
   parts are 2.78 / 2.50 / 6.25 MB; bar parts are KB-sized. `bars_1s` is not modelled (T-121 has
   tick bytes only). Re-evaluate with T-503 measurements.
4. `retag` migrates existing blobs: it removes the three retired tags from tagged parts, removes
   all tags from parts of untagged datasets, keeps `compacted`, and derives the year from the path.
   It stays idempotent and under the table lock.
5. Compaction sets `compacted=true` only where tags are written; the guard of SPEC-03 §4 (a
   partition leaves Hot after its first compaction) holds for ticks. Compaction still runs for
   untagged datasets (small-file control). Follow-up from T-207: a partition that is already a
   single part is skipped without reading data (`compacted=true` tag for ticks; manifest listing
   exactly that part for untagged datasets), which removes most of the modelled compaction read cost.

## Consequences
* `find_blobs_by_tags` queries by dataset, instrument or year are no longer possible; list by
  prefix instead (the path has all three). Verified by grep: no code outside one integration test
  uses blob-tag queries, and that test queries `age_band` only.
* Bars never leave Hot; the saving comes from ticks, which hold the bytes (T-121).
* Lifecycle rules (`age_band` + `compacted`) need no change; the Terraform comment listing five tags is stale.
* Storage-layout §4 changes (contract change, `contract-change` label).
* The margin at S is small (+0.0010 EUR/month); the numbers are modelled until T-503.

## Rollout
Run `tidal tiering retag` (T-211) **before** the lifecycle policy (T-207) is first applied: it clears
the tags of all `bars_*` parts, so no bars part ever matches a rule and none leaves Hot. If the policy
was already applied while bars were tagged, bars parts that moved to Cool/Cold must be set back to Hot
by hand after retag; that costs tier-change operations and retrieval, and Cool/Cold early-deletion
fees can apply if they are rewritten within the minimum retention.
