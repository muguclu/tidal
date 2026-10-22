# Blob lifecycle management (SPEC-03 section 4, storage-layout sections 1 and 4, ADR-0004).
#
# marketdata/ is tiered by DATA AGE through the blob index tags that tidal tiering retag / compact
# write (src/tidal/storage/retag.py: dataset, instrument, data_year, age_band, compacted), not by
# last-modified: a backfill writes 20-year-old data today. The policy runs about once a day, so a
# freshly tagged blob moves within roughly 24-48 h.
#
# Tag filters are AND-ed and only support "==". A blob without tags never matches a tag filter, so
# the untagged, derived _manifest.json (retag skips it; storage-layout section 2) stays in the
# account default tier (Hot) and can never be tiered, archived or rehydrated by these rules.
#
# The two marketdata rules also require compacted=true (SPEC-03 section 4 "Compaction guard";
# numbers in docs/finops/deltas/T-207.md). Compaction (yearly, after retag) writes the merged
# part and deletes the old ones; a part tiered to Cool/Cold before that would be billed the
# early-deletion fee (Cool 30 d, Cold 90 d) for bytes about to be deleted, plus a pointless tier-change
# write. compact_partition tags every kept part compacted=true, including partitions that already
# had a single part, so every old partition is eligible after one compaction run. Parts that are
# restated later are compacted=false and stay Hot until the next yearly compaction.
#
# No Archive for marketdata/: rehydration takes hours and breaks backtests (ADR-0004). Archive is only
# used for raw/ (verbatim source responses, re-readable on demand with hours of latency and a
# retrieval fee; raw is only needed for replay/audit).

locals {
  # "v1/" is a literal that must equal layout.MD_VERSION in src/tidal/storage/layout.py
  # (storage-layout section 2: marketdata/v1/...). Container names come from the container resources.
  md_prefix = "${azurerm_storage_container.this["marketdata"].name}/v1/"
}

resource "azurerm_storage_management_policy" "this" {
  storage_account_id = azurerm_storage_account.this.id

  # md-cool: data 3-10 years old, compacted -> Cool.
  rule {
    name    = "md-cool"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = [local.md_prefix]

      match_blob_index_tag {
        name      = "age_band"
        operation = "=="
        value     = "cool"
      }

      match_blob_index_tag {
        name      = "compacted"
        operation = "=="
        value     = "true"
      }
    }

    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 0
      }
    }
  }

  # md-cold: data older than 10 years, compacted -> Cold (online tier; never Archive).
  rule {
    name    = "md-cold"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = [local.md_prefix]

      match_blob_index_tag {
        name      = "age_band"
        operation = "=="
        value     = "cold"
      }

      match_blob_index_tag {
        name      = "compacted"
        operation = "=="
        value     = "true"
      }
    }

    actions {
      base_blob {
        tier_to_cold_after_days_since_modification_greater_than = 0
      }
    }
  }

  rule {
    name    = "raw"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${azurerm_storage_container.this["raw"].name}/"]
    }

    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 30
        tier_to_archive_after_days_since_modification_greater_than = 180
      }
    }
  }

  rule {
    name    = "backtests"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${azurerm_storage_container.this["backtests"].name}/"]
    }

    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 30
        delete_after_days_since_modification_greater_than       = 365
      }
    }
  }

  rule {
    name    = "exports"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${azurerm_storage_container.this["exports"].name}/"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 14
      }
    }
  }

  rule {
    name    = "bench"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${azurerm_storage_container.this["bench"].name}/"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 30
      }
    }
  }
}
