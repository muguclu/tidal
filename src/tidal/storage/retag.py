"""Blob index tags of market-data parts and the yearly ``age_band`` re-tagging (SPEC-03 §3).

Tag names and values are the contract of storage-layout §4 and of the Terraform lifecycle filters
(T-207), so they are spelled once here:

==============  ==============================================================
``dataset``     ``ticks`` | ``bars_1s`` | ``bars_1m`` | ``bars_1h`` | ``bars_1d``
``instrument``  ``instrument_id`` (e.g. ``EIA.WTI.SPOT``)
``data_year``   ``YYYY`` of the partition
``age_band``    ``hot`` (< 3 y) | ``cool`` (3-10 y) | ``cold`` (> 10 y)
``compacted``   ``true`` | ``false``
==============  ==============================================================

``age_band`` depends on *data age* (``clock year - data_year``), not on upload time, because a
backfill writes 20-year-old data today. Taggers are injected: :class:`AzureTagger` wraps a
``BlobServiceClient`` (Azurite or Azure), :class:`LocalTagger` keeps JSON sidecars outside the
listed ``marketdata/`` tree for local dev and tests.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from tidal.common.errors import NotFound
from tidal.common.logging import get_logger
from tidal.common.timeutil import Clock
from tidal.storage import layout
from tidal.storage.parquet_writer import ObjectStore

if TYPE_CHECKING:
    from azure.storage.blob import BlobServiceClient

log = get_logger(__name__)

TAG_DATASET: Final = "dataset"
TAG_INSTRUMENT: Final = "instrument"
TAG_DATA_YEAR: Final = "data_year"
TAG_AGE_BAND: Final = "age_band"
TAG_COMPACTED: Final = "compacted"
HOT_YEARS: Final = 3  # age < 3 years -> hot
COOL_YEARS: Final = 10  # age <= 10 years -> cool, older -> cold


class BlobTagger(Protocol):
    """Blob index tags of container-qualified paths."""

    def get_tags(self, path: str) -> dict[str, str]:
        """Tags of the blob (empty if it has none); raise ``NotFound`` if the blob is absent."""
        ...

    def set_tags(self, path: str, tags: Mapping[str, str]) -> None:
        """Replace all tags of the blob."""
        ...


def age_band(data_year: int, now_year: int) -> str:
    """``hot`` for data younger than 3 years, ``cool`` up to 10 years, else ``cold``."""
    age = now_year - data_year
    if age < HOT_YEARS:
        return "hot"
    return "cool" if age <= COOL_YEARS else "cold"


def desired_tags(path: str, now_year: int, *, compacted: bool = False) -> dict[str, str]:
    """The full tag set of a part blob at ``path`` as of calendar year ``now_year``."""
    part = layout.parse_md_path(path)
    return {
        TAG_DATASET: part.dataset,
        TAG_INSTRUMENT: part.instrument_id,
        TAG_DATA_YEAR: f"{part.year:04d}",
        TAG_AGE_BAND: age_band(part.year, now_year),
        TAG_COMPACTED: "true" if compacted else "false",
    }


def ensure_tags(
    tagger: BlobTagger, path: str, now_year: int, *, compacted: bool | None = None
) -> bool:
    """Set the desired tags if they differ; returns whether a write happened.

    ``compacted=None`` keeps the blob's current flag (default ``false``), so re-tagging by age
    never un-marks a compacted file.
    """
    current = tagger.get_tags(path)
    keep = current.get(TAG_COMPACTED) == "true" if compacted is None else compacted
    want = desired_tags(path, now_year, compacted=keep)
    if current == want:
        return False
    tagger.set_tags(path, want)
    return True


class AzureTagger:
    """Blob index tags through an injected ``BlobServiceClient``."""

    def __init__(self, service: "BlobServiceClient") -> None:
        """Wrap ``service``; credentials are the caller's concern."""
        self.service = service

    def get_tags(self, path: str) -> dict[str, str]:
        """Current tags; ``NotFound`` if the blob does not exist."""
        from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

        container, name = layout.split_container(path)
        try:
            return dict(self.service.get_blob_client(container, name).get_blob_tags())
        except ResourceNotFoundError:
            raise NotFound(f"{path} not found") from None

    def set_tags(self, path: str, tags: Mapping[str, str]) -> None:
        """Replace the blob's tags."""
        from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

        container, name = layout.split_container(path)
        try:
            self.service.get_blob_client(container, name).set_blob_tags(dict(tags))
        except ResourceNotFoundError:
            raise NotFound(f"{path} not found") from None


class LocalTagger:
    """JSON sidecars under ``<root>/.tags/<path>.json`` (never listed by ``LocalStore``)."""

    def __init__(self, root: Path) -> None:
        """Keep sidecars for the store rooted at ``root``."""
        self.root = root.resolve()

    def _sidecar(self, path: str) -> Path:
        layout.split_container(path)  # validates "container/name"
        full = (self.root / ".tags" / (path + ".json")).resolve()
        if not full.is_relative_to(self.root / ".tags"):
            raise layout.InvalidPath(f"path escapes the tag root: {path!r}")
        return full

    def get_tags(self, path: str) -> dict[str, str]:
        """Tags of ``path`` (empty if never tagged); ``NotFound`` if the blob is absent."""
        if not (self.root / path).is_file():
            raise NotFound(f"{path} not found")
        try:
            data = json.loads(self._sidecar(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def set_tags(self, path: str, tags: Mapping[str, str]) -> None:
        """Atomically replace the sidecar."""
        if not (self.root / path).is_file():
            raise NotFound(f"{path} not found")
        dest = self._sidecar(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(json.dumps(dict(tags), sort_keys=True), encoding="utf-8")
        os.replace(tmp, dest)


@dataclass(frozen=True, slots=True)
class RetagResult:
    """Counts of one re-tagging run."""

    blobs: int
    changed: int
    missing: int  # listed but deleted before they could be tagged


def retag(
    store: ObjectStore,
    tagger: BlobTagger,
    clock: Clock,
    *,
    datasets: tuple[str, ...] = layout.DATASETS,
) -> RetagResult:
    """Recompute ``age_band`` (and complete missing tags) of every part of ``datasets``.

    Idempotent: a blob whose tags already match is not written. Run once a year (Jan 2) so the
    lifecycle rules (T-207) move data that crossed a band boundary. ``compacted`` is preserved.
    """
    now_year = clock.now().year
    blobs = changed = missing = 0
    for dataset in datasets:
        prefix = f"{layout.CONTAINER_MARKETDATA}/{layout.MD_VERSION}/{dataset}/"
        for path in store.list(prefix):
            try:
                part = layout.parse_md_path(path)
            except layout.InvalidPath:
                continue  # temp files, foreign blobs
            if part.seq is None:
                continue  # manifest
            blobs += 1
            try:
                changed += ensure_tags(tagger, path, now_year)
            except NotFound:
                missing += 1
                continue
    log.info("retag_done", blobs=blobs, changed=changed, missing=missing, year=now_year)
    return RetagResult(blobs, changed, missing)
