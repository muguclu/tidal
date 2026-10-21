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
    """One row of the v1 catalogue; ``source_series`` is ``generator`` for ``SYN.*``."""

    instrument_id: str
    source_series: str
    asset_class: AssetClass
    kind: InstrumentKind
    unit: str
    currency: str
    exchange_tz: str
    granularity: Granularity

    @property
    def source(self) -> Source:
        """Source, from the ``instrument_id`` prefix."""
        return Source(self.instrument_id.split(".")[0])

    @property
    def description(self) -> str:
        """``<source> <series or name>, <kind>, <unit>`` (data-contracts §1.1)."""
        name = self.instrument_id.split(".")[1] if self.source is Source.SYN else self.source_series
        return f"{self.source} {name}, {self.kind}, {self.unit}"

    def to_instrument(self) -> Instrument:
        """Reference-data row for this entry (``roll_method``/ts bounds unset)."""
        return Instrument(
            instrument_id=self.instrument_id,
            source=self.source,
            asset_class=self.asset_class,
            kind=self.kind,
            unit=self.unit,
            currency=self.currency,
            exchange_tz=self.exchange_tz,
            granularity=self.granularity,
            description=self.description,
        )


_CRUDE, _GAS, _POWER, _REF = AssetClass.CRUDE, AssetClass.GAS, AssetClass.POWER, AssetClass.REFINED
_SPOT, _FUT, _FX = InstrumentKind.SPOT, InstrumentKind.FUTURE, InstrumentKind.FX
_D1, _TICK = Granularity.D1, Granularity.TICK
_NY, _CHI, _LON = "America/New_York", "America/Chicago", "Europe/London"
_BBL, _MMBTU, _EMWH = "USD/bbl", "USD/MMBtu", "EUR/MWh"
_E = CatalogueEntry


def _syn(name: str, asset: AssetClass, unit: str, ccy: str, tz: str) -> CatalogueEntry:
    kind = _FX if asset is AssetClass.FX else _SPOT
    return _E(f"SYN.{name}.TICK", "generator", asset, kind, unit, ccy, tz, _TICK)


#: Normative seed catalogue (data-contracts §1.1), in contract order.
CATALOGUE_V1: Final[tuple[CatalogueEntry, ...]] = (
    _E("EIA.BRENT.SPOT", "RBRTE", _CRUDE, _SPOT, _BBL, "USD", _LON, _D1),
    _E("EIA.WTI.SPOT", "RWTC", _CRUDE, _SPOT, _BBL, "USD", _CHI, _D1),
    *(_E(f"EIA.WTI.FUT{n}", f"RCLC{n}", _CRUDE, _FUT, _BBL, "USD", _NY, _D1) for n in range(1, 5)),
    _E("EIA.HH.SPOT", "RNGWHHD", _GAS, _SPOT, _MMBTU, "USD", _CHI, _D1),
    *(_E(f"EIA.HH.FUT{n}", f"RNGC{n}", _GAS, _FUT, _MMBTU, "USD", _NY, _D1) for n in range(1, 5)),
    _E("FRED.EURUSD.FX", "DEXUSEU", AssetClass.FX, _FX, "USD/EUR", "USD", _NY, _D1),
    _syn("BRENT", _CRUDE, _BBL, "USD", _LON),
    _syn("WTI", _CRUDE, _BBL, "USD", _CHI),
    _syn("HH", _GAS, _MMBTU, "USD", _CHI),
    _syn("NBP", _GAS, "GBp/therm", "GBP", _LON),
    _syn("TTF", _GAS, _EMWH, "EUR", "Europe/Amsterdam"),
    _syn("POWER_DE", _POWER, _EMWH, "EUR", "Europe/Berlin"),
    _syn("POWER_FR", _POWER, _EMWH, "EUR", "Europe/Paris"),
    _syn("GASOIL", _REF, "USD/t", "USD", _LON),
    _syn("RBOB", _REF, "USD/gal", "USD", _NY),
    _syn("EURUSD", AssetClass.FX, "USD/EUR", "USD", _NY),
)
