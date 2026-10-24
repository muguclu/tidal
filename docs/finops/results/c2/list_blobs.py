# ruff: noqa: T201, E501  # standalone evidence helper run inside the dev image
"""Evidence helper (T-503 C2): sum Parquet blob bytes per dataset created at/after a UTC instant, from Azurite.

Run inside the dev image: python /tmp/list_blobs.py 2026-10-24T22:17 > blobs_created.json
"""

import collections
import json
import os
import sys

from azure.storage.blob import BlobServiceClient

since = sys.argv[1]
svc = BlobServiceClient.from_connection_string(os.environ["TIDAL_STORAGE__CONNECTION_STRING"])
acc: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
for c in svc.list_containers():
    for b in svc.get_container_client(c.name).list_blobs():
        if b.name.endswith(".parquet") and b.creation_time.isoformat() >= since:
            ds = b.name.split("/")[1]
            acc[ds][0] += 1
            acc[ds][1] += b.size
print(
    json.dumps(
        {
            "since_utc": since,
            "datasets": {k: {"blobs": v[0], "bytes": v[1]} for k, v in sorted(acc.items())},
        },
        indent=1,
    )
)
