"""Instrument reference model and the v1 catalogue (data-contracts.md §1)."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

INSTRUMENT_ID_PATTERN: Final = r"^[A-Z0-9]+\.[A-Z0-9_]+\.[A-Z0-9_]+$"
INSTRUMENT_ID_RE: Final = re.compile(INSTRUMENT_ID_PATTERN)

#: ``instrument_id`` string; length bound from ``ref.instrument.instrument_id VARCHAR(64)``.
InstrumentId = Annotated[str, StringConstraints(pattern=INSTRUMENT_ID_PATTERN, max_length=64)]


class Source(StrEnum):
    """Data source."""

    EIA = "EIA"
    FRED = "FRED"
    SYN = "SYN"


class AssetClass(StrEnum):
    """Asset class."""

    CRUDE = "crude"
    GAS = "gas"
    POWER = "power"
    FX = "fx"
    REFINED = "refined"


class InstrumentKind(StrEnum):
    """Instrument kind."""

    SPOT = "spot"
    FUTURE = "future"
    CONTINUOUS = "continuous"
    FX = "fx"


class Granularity(StrEnum):
    """Native granularity of an instrument."""

    TICK = "tick"
    S1 = "1s"
    M1 = "1m"
    H1 = "1h"
    D1 = "1d"


class RollMethod(StrEnum):
    """Roll adjustment of a continuous series."""

    RATIO = "ratio"
    DIFFERENCE = "difference"
    NONE = "none"


class Instrument(BaseModel):
    """Instrument reference data (data-contracts §1); also the API ``Instrument`` schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: InstrumentId
    source: Source
    asset_class: AssetClass
    kind: InstrumentKind
    unit: str = Field(min_length=1, max_length=16)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    exchange_tz: str = Field(min_length=1, max_length=64)
    granularity: Granularity
    roll_method: RollMethod | None = None
    first_ts_ns: int | None = None
    last_ts_ns: int | None = None
    description: str = Field(max_length=256)

    @model_validator(mode="after")
    def _roll_method_for_continuous(self) -> Self:
        if self.kind is InstrumentKind.CONTINUOUS and self.roll_method is None:
            msg = "roll_method is required when kind=continuous"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """One row of the v1 catalogue. ``None`` = "per name" in the contract (synthetic rows)."""

    instrument_id: str
    source_series: str | None
    asset_class: AssetClass | None
    kind: InstrumentKind
    unit: str | None
    currency: str | None
    exchange_tz: str | None
    granularity: Granularity


_CRUDE, _GAS, _FX = AssetClass.CRUDE, AssetClass.GAS, AssetClass.FX
_SPOT, _FUT = InstrumentKind.SPOT, InstrumentKind.FUTURE
_D1 = Granularity.D1
_NY, _CHI, _LON = "America/New_York", "America/Chicago", "Europe/London"
_BBL, _MMBTU = "USD/bbl", "USD/MMBtu"
_SYN_NAMES: Final = (
    *("BRENT", "WTI", "HH", "NBP", "TTF", "POWER_DE", "POWER_FR", "GASOIL", "RBOB"),
    "EURUSD",
)

#: Normative seed catalogue (data-contracts §1.1), in contract order.
CATALOGUE_V1: Final[tuple[CatalogueEntry, ...]] = (
    CatalogueEntry("EIA.BRENT.SPOT", "RBRTE", _CRUDE, _SPOT, _BBL, "USD", _LON, _D1),
    CatalogueEntry("EIA.WTI.SPOT", "RWTC", _CRUDE, _SPOT, _BBL, "USD", _CHI, _D1),
    *(
        CatalogueEntry(f"EIA.WTI.FUT{n}", f"RCLC{n}", _CRUDE, _FUT, _BBL, "USD", _NY, _D1)
        for n in range(1, 5)
    ),
    CatalogueEntry("EIA.HH.SPOT", "RNGWHHD", _GAS, _SPOT, _MMBTU, "USD", _CHI, _D1),
    *(
        CatalogueEntry(f"EIA.HH.FUT{n}", f"RNGC{n}", _GAS, _FUT, _MMBTU, "USD", _NY, _D1)
        for n in range(1, 5)
    ),
    CatalogueEntry("FRED.EURUSD.FX", "DEXUSEU", _FX, InstrumentKind.FX, "USD/EUR", "USD", _NY, _D1),
    *(
        CatalogueEntry(f"SYN.{name}.TICK", None, None, _SPOT, None, None, None, Granularity.TICK)
        for name in _SYN_NAMES
    ),
)
