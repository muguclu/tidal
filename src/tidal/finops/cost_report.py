# ruff: noqa: E501  # long prose strings in the generated Markdown
"""Weekly cost report from an Azure Cost Management export (SPEC-09 section 2.1, T-502).

Input schema (decision, T-502): the **Actual cost (usage and charges)** CSV that
``azurerm_resource_group_cost_management_export`` writes (``type = "ActualCost"``, daily, month-to-date),
in the modern (Microsoft Customer Agreement / Enterprise) column naming:

    Date, ResourceGroupName, ResourceId, MeterCategory, MeterSubCategory, MeterName, MeterId, ProductName,
    Quantity, UnitOfMeasure, CostInBillingCurrency, BillingCurrencyCode, Tags

Header matching is case-insensitive and also accepts the classic pay-as-you-go names (``UsageDateTime``,
``ResourceGroup``, ``MeterSubcategory``, ``UsageQuantity``, ``PreTaxCost``, ``Currency``) through
:data:`COLUMN_ALIASES`. FOCUS is not used: the azurerm export resource does not emit it. ``Tags`` is parsed from
both the JSON-object form and the brace-less ``"k": "v", ...`` form that classic exports use.

Labels (CLAUDE.md 2.2, agent rules): every figure that comes from the export is **MEASURED**; anything derived
from ``docs/finops/prices.yaml`` or extrapolated (monthly run-rate = daily mean x 730 / 24) is **MODELLED**.

Determinism: the same CSV files give byte-identical Markdown. All sums are :class:`~decimal.Decimal`, every
ordering has an explicit tie-break, and the report contains no wall-clock time. When several export snapshots
contain the same day (month-to-date exports are re-written daily) the **last snapshot wins for that day**;
the partitions (``part_*.csv``) of one run are one snapshot. Callers pass snapshots oldest first.

What a cost export cannot show is stated in the report (:data:`LIMITS`), not hidden.

CLI: ``uv run python -m tidal.finops.cost_report --export <csv-or-dir> ... [--week 2026-W44]``
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from tidal.common.errors import TidalError
from tidal.finops.fetch_prices import Price, PriceBook, load_prices

DEFAULT_PRICES = Path("docs/finops/prices.yaml")
DEFAULT_COST_MODEL = Path("docs/finops/cost-model.md")
DEFAULT_OUT_DIR = Path("docs/finops/results")
HOURS_PER_MONTH = Decimal(730)
HOURS_PER_DAY = Decimal(24)
UNTAGGED = "(untagged)"
MODEL_CURRENCY = "EUR"

# canonical name -> accepted header spellings (lower-case)
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "usagedatetime", "usagedate"),
    "resource_group": ("resourcegroupname", "resourcegroup"),
    "resource_id": ("resourceid", "instanceid"),
    "category": ("metercategory",),
    "subcategory": ("metersubcategory",),
    "meter": ("metername",),
    "meter_id": ("meterid",),
    "product": ("productname", "product"),
    "quantity": ("quantity", "usagequantity"),
    "uom": ("unitofmeasure",),
    "cost": ("costinbillingcurrency", "pretaxcost", "cost"),
    "currency": ("billingcurrencycode", "currency", "billingcurrency"),
    "tags": ("tags",),
}
REQUIRED = ("date", "category", "meter", "quantity", "cost")

LIMITS: tuple[str, ...] = (
    "Daily granularity only: there is no hour-of-day in a cost export, so C10 (parking) is evidenced by cost per "
    "day, not per hour. Hourly shape needs Azure Monitor metrics (VM running hours, node-hours).",
    "Resource-group scope: the AKS node resource group `MC_*` is a separate group and is NOT in an RG-scoped "
    "export. Spot node-hours (C4), the cluster load balancer and its public IP are therefore invisible here "
    "(NEEDS-CLOUD: subscription-scope export or the Azure Monitor node-hours metric).",
    "Cost Management data lags 8 to 24 hours; the last day of an export can be incomplete, and a preliminary "
    "figure can change after the invoice closes.",
    "Latency trade-offs (cold start, SQL resume, Archive rehydration) are not in a bill; they need the load "
    "test (T-504).",
    "Reserved Cool/Cold early-deletion and retrieval charges appear only if they were incurred in the window; "
    "an empty row means 'not incurred', not 'not billable'.",
    "Meters whose unit price is rounded by the Retail API (`precision_limited`) are never modelled; their "
    "cost is MEASURED from the export only.",
)

# SPEC-09 C-register rows that a bill can evidence: id, title, MeterCategory values (lower-case).
EVIDENCE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "C3",
        "Blob tiering: stored GB, transactions, retrieval, early delete",
        ("storage", "blob storage"),
    ),
    (
        "C5",
        "Container Apps: active/idle vCPU-seconds, GiB-seconds, requests",
        ("azure container apps", "container apps"),
    ),
    (
        "C6",
        "SQL serverless: vCore-seconds and storage (pause shows as zero days)",
        ("sql database",),
    ),
    (
        "C7",
        "Scheduled VM: compute hours and disk",
        ("virtual machines", "virtual machines licenses"),
    ),
    (
        "C8",
        "Log ingestion: GB per day and retention",
        ("log analytics", "azure monitor", "application insights"),
    ),
    ("C9", "ACR hygiene: registry-days and stored GB", ("container registry",)),
)

MODEL_MIN_CELLS = 4
_TWO = Decimal("0.01")
_FOUR = Decimal("0.0001")


class CostReportError(TidalError):
    """The export or a supporting file cannot be turned into a report."""

    status = 422
    slug = "cost-report-invalid"
    title = "Cost report input invalid"


@dataclass(frozen=True)
class CostRow:
    """One export line, normalised."""

    day: date
    resource_group: str
    resource_id: str
    category: str
    subcategory: str
    meter: str
    meter_id: str
    product: str
    quantity: Decimal
    uom: str
    cost: Decimal
    currency: str
    tags: dict[str, str]

    @property
    def component(self) -> str:
        """The Tidal ``component`` tag, or ``(untagged)``."""
        return self.tags.get("component", UNTAGGED)

    @property
    def park(self) -> str:
        """The ``park`` tag (``true``/``false``), or ``(untagged)``."""
        return self.tags.get("park", UNTAGGED)


def _money(value: Decimal) -> str:
    return f"{value.quantize(_TWO, rounding=ROUND_HALF_UP):.2f}"


def _qty(value: Decimal) -> str:
    return f"{value.quantize(_FOUR, rounding=ROUND_HALF_UP):.4f}"


def _pct(part: Decimal, whole: Decimal) -> str:
    if whole == 0:
        return "-"
    return f"{(part * 100 / whole).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):.1f}%"


def _decimal(text: str, field: str, line: int) -> Decimal:
    stripped = text.strip()
    if not stripped:
        return Decimal(0)
    try:
        return Decimal(stripped)
    except ArithmeticError as exc:
        msg = f"line {line}: {field} is not a number: {text!r}"
        raise CostReportError(msg) from exc


def _parse_date(text: str, line: int) -> date:
    value = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).date()  # noqa: DTZ007 - a calendar date, no instant involved
        except ValueError:
            continue
    msg = f"line {line}: unsupported date {text!r}"
    raise CostReportError(msg)


def parse_tags(text: str) -> dict[str, str]:
    """Parse the export ``Tags`` cell (JSON object, or classic brace-less ``"k": "v", ...``)."""
    raw = text.strip()
    if not raw:
        return {}
    if not raw.startswith("{"):
        raw = "{" + raw + "}"
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k).lower(): str(v).lower() for k, v in loaded.items()}


def _column_map(header: Sequence[str], source: str) -> dict[str, str]:
    lowered = {h.strip().lower(): h for h in header}
    mapping: dict[str, str] = {}
    for canon, names in COLUMN_ALIASES.items():
        for name in names:
            if name in lowered:
                mapping[canon] = lowered[name]
                break
    missing = [c for c in REQUIRED if c not in mapping]
    if missing:
        msg = f"{source}: missing required columns {missing}; header is {list(header)}"
        raise CostReportError(msg)
    return mapping


def parse_export(path: Path) -> list[CostRow]:
    """Read one Actual-cost export CSV (see module docstring for the schema)."""
    if not path.is_file():
        msg = f"export file not found: {path.name}"
        raise CostReportError(msg)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            msg = f"{path}: empty file"
            raise CostReportError(msg)
        cols = _column_map(reader.fieldnames, str(path))
        rows: list[CostRow] = []
        for record in reader:
            line = reader.line_num

            def cell(name: str, rec: dict[str, str] = record) -> str:
                col = cols.get(name)
                return (rec.get(col) or "") if col else ""

            rows.append(
                CostRow(
                    day=_parse_date(cell("date"), line),
                    resource_group=cell("resource_group").strip().lower(),
                    resource_id=cell("resource_id").strip().lower(),
                    category=cell("category").strip(),
                    subcategory=cell("subcategory").strip(),
                    meter=cell("meter").strip(),
                    meter_id=cell("meter_id").strip().lower(),
                    product=cell("product").strip(),
                    quantity=_decimal(cell("quantity"), "quantity", line),
                    uom=cell("uom").strip(),
                    cost=_decimal(cell("cost"), "cost", line),
                    currency=cell("currency").strip().upper(),
                    tags=parse_tags(cell("tags")),
                )
            )
    return rows


def expand_snapshots(inputs: Iterable[Path]) -> list[list[Path]]:
    """Group inputs into export snapshots, oldest first.

    A file argument is one snapshot. A directory is walked: every directory that directly holds ``*.csv``
    files is one snapshot (one export run; its ``part_*.csv`` partitions all belong together), ordered by path.
    Cost Management lays runs out as ``<export>/<YYYYMMDD-YYYYMMDD>/<run id>/part_*.csv``, so path order is
    month order.
    """
    out: list[list[Path]] = []
    for item in inputs:
        if item.is_dir():
            leaves: dict[Path, list[Path]] = defaultdict(list)
            for csv_path in item.rglob("*.csv"):
                if csv_path.is_file():
                    leaves[csv_path.parent].append(csv_path)
            out.extend(sorted(leaves[d]) for d in sorted(leaves))
        else:
            out.append([item])
    return out


def merge_snapshots(snapshots: Sequence[Sequence[Path]]) -> list[CostRow]:
    """Load snapshots in order; for each day only the last snapshot that contains it is kept."""
    last_for_day: dict[date, int] = {}
    parsed: list[list[CostRow]] = []
    for index, files in enumerate(snapshots):
        rows = [row for file in files for row in parse_export(file)]
        parsed.append(rows)
        for day in {r.day for r in rows}:
            last_for_day[day] = index
    merged: list[CostRow] = []
    for index, rows in enumerate(parsed):
        merged.extend(r for r in rows if last_for_day[r.day] == index)
    return merged


def merge_exports(files: Sequence[Path]) -> list[CostRow]:
    """Like :func:`merge_snapshots` with every file its own snapshot (oldest first)."""
    return merge_snapshots([[f] for f in files])


def iso_week_label(day: date) -> str:
    """``YYYY-Www`` ISO week label (used in the file name)."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def parse_week(label: str) -> tuple[date, date]:
    """Monday and Sunday of an ISO week label such as ``2026-W44``."""
    match = re.fullmatch(r"(\d{4})-W(\d{2})", label)
    if not match:
        msg = f"week must look like 2026-W44, got {label!r}"
        raise CostReportError(msg)
    monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return monday, monday + timedelta(days=6)


def _sum(rows: Iterable[CostRow]) -> Decimal:
    return sum((r.cost for r in rows), Decimal(0))


def _group[K](rows: Iterable[CostRow], key: Callable[[CostRow], K]) -> dict[K, list[CostRow]]:
    groups: dict[K, list[CostRow]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def _ranked(groups: dict[str, list[CostRow]]) -> list[tuple[str, Decimal]]:
    return sorted(((k, _sum(v)) for k, v in groups.items()), key=lambda kv: (-kv[1], kv[0]))


def _table(header: Sequence[str], body: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(r) + " |" for r in body)
    return lines


# --- cost-model.md comparison -----------------------------------------------------------------------

# cost-model.md row label prefix -> value of the Tidal `component` tag its resources carry.
MODEL_ROW_COMPONENT: tuple[tuple[str, str], ...] = (
    ("AKS", "compute"),
    ("Container Apps", "compute"),
    ("App Service", "compute"),
    ("Azure SQL", "data"),
    ("Storage", "storage"),
    ("Service Bus", "messaging"),
    ("ACR", "registry"),
    ("Key Vault", "secrets"),
    ("Log Analytics", "observability"),
    ("VM legacy", "legacy-vm"),
    ("Logic App", "legacy-vm"),
    ("Budget", "finops"),
)
_NUM = re.compile(r"^[\s≈≤<>~`*]*(\d+(?:\.\d+)?)(?:\s*[–-]\s*(\d+(?:\.\d+)?))?")  # noqa: RUF001 - cost-model.md uses an en dash for ranges


@dataclass(frozen=True)
class ModelEstimate:
    """Per-component estimate (EUR/month) summed from cost-model.md rows."""

    active_low: Decimal
    active_high: Decimal
    parked: Decimal
    rows: tuple[str, ...]


def _leading_range(cell: str) -> tuple[Decimal, Decimal] | None:
    match = _NUM.match(cell)
    if not match:
        return None
    low = Decimal(match.group(1))
    return low, Decimal(match.group(2)) if match.group(2) else low


def parse_cost_model(text: str) -> dict[str, ModelEstimate]:
    """Sum the first table of cost-model.md per component tag. Cells without a leading number count as 0 (listed)."""
    acc: dict[str, list[Decimal]] = {}
    names: dict[str, list[str]] = defaultdict(list)
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < MODEL_MIN_CELLS:
            continue
        label = cells[0].replace("*", "")
        component = next((c for prefix, c in MODEL_ROW_COMPONENT if label.startswith(prefix)), None)
        if component is None:
            continue
        active = _leading_range(cells[2]) or (Decimal(0), Decimal(0))
        parked = _leading_range(cells[3]) or (Decimal(0), Decimal(0))
        totals = acc.setdefault(component, [Decimal(0)] * 3)
        totals[0] += active[0]
        totals[1] += active[1]
        totals[2] += parked[0]
        names[component].append(label)
    return {k: ModelEstimate(v[0], v[1], v[2], tuple(names[k])) for k, v in sorted(acc.items())}


# --- price reconciliation (MODELLED) ----------------------------------------------------------------


def _single_tier_prices(book: PriceBook) -> dict[str, Price]:
    """Meter id -> price, only for meters with exactly one entry, tier 0 and a trustworthy unit price."""
    seen: dict[str, list[Price]] = defaultdict(list)
    for price in book.prices:
        seen[price.meterId.lower()].append(price)
    return {
        mid: ps[0]
        for mid, ps in seen.items()
        if len(ps) == 1 and ps[0].tierMinimumUnits == 0 and not ps[0].precision_limited
    }


# --- report -----------------------------------------------------------------------------------------


def build_report(
    rows: Sequence[CostRow],
    *,
    week: tuple[date, date],
    book: PriceBook,
    cost_model_text: str,
    source_names: Sequence[str],
) -> str:
    """Render the weekly Markdown report (pure function; no I/O, no clock)."""
    if not rows:
        msg = "export contains no rows"
        raise CostReportError(msg)
    monday, sunday = week
    wk = [r for r in rows if monday <= r.day <= sunday]
    if not wk:
        msg = f"no export rows fall in {monday}..{sunday}"
        raise CostReportError(msg)
    currencies = sorted({r.currency for r in rows if r.currency})
    currency = currencies[0] if len(currencies) == 1 else "MIXED"
    label = iso_week_label(monday)
    week_days = sorted({r.day for r in wk})
    month_start = (
        sunday.replace(day=1)
        if week_days[-1].month == sunday.month
        else week_days[-1].replace(day=1)
    )
    last_day = week_days[-1]
    mtd = [r for r in rows if month_start <= r.day <= last_day]
    mtd_days = sorted({r.day for r in mtd})

    out: list[str] = [f"# Cost report {label}", ""]
    out.append(
        f"Window {monday} to {sunday} ({len(week_days)} of 7 days with data, last day {last_day}). "
        f"Source: {', '.join(sorted(source_names))}. Schema: Cost Management Actual cost, daily CSV (T-502). "
        f"Currency: {currency}. Prices: `docs/finops/prices.yaml` captured {book.captured_at}."
    )
    out.append("")
    out.append(
        "Labels: MEASURED = from the export; MODELLED = unit price x quantity or extrapolation."
    )
    out.append("")
    if currency != MODEL_CURRENCY:
        out.append(
            f"> WARNING: export currency is {currency}, the cost model and price snapshot are {MODEL_CURRENCY}. "
            "The comparison and reconciliation sections are skipped."
        )
        out.append("")

    week_total = _sum(wk)
    mtd_total = _sum(mtd)
    out += [
        "## 1. Totals",
        "",
        *_table(
            ["Figure", f"{currency}", "Label"],
            [
                [f"Week {label} ({len(week_days)} days)", _money(week_total), "MEASURED"],
                [
                    f"Month to date {month_start:%Y-%m} ({len(mtd_days)} days)",
                    _money(mtd_total),
                    "MEASURED",
                ],
                [
                    "Mean per day with data (week)",
                    _money(week_total / len(week_days)),
                    "MEASURED",
                ],
                [
                    "Run-rate per month (mean per day x 730 / 24)",
                    _money(week_total / len(week_days) * HOURS_PER_MONTH / HOURS_PER_DAY),
                    "MODELLED",
                ],
            ],
        ),
        "",
    ]

    # 2 by day
    by_day = _group(wk, lambda r: r.day)
    floor = min(_sum(v) for v in by_day.values())
    out += [
        "## 2. Cost by day (week)",
        "",
        *_table(
            ["Day", "Weekday", currency, "x cheapest day", "Label"],
            [
                [
                    str(d),
                    f"{d:%a}",
                    _money(_sum(by_day[d])),
                    f"{(_sum(by_day[d]) / floor):.2f}" if floor > 0 else "-",
                    "MEASURED",
                ]
                for d in week_days
            ],
        ),
        "",
    ]

    # 3 by category
    out += ["## 3. Cost by service / meter category (week)", ""]
    out += _table(
        ["MeterCategory", currency, "Share", "Label"],
        [
            [k or "(none)", _money(v), _pct(v, week_total), "MEASURED"]
            for k, v in _ranked(_group(wk, lambda r: r.category))
        ],
    )
    out.append("")

    # 4 by component
    comp_groups = _group(wk, lambda r: r.component)
    out += ["## 4. Cost by `component` tag (week)", ""]
    out += _table(
        ["component", currency, "Share", "Label"],
        [[k, _money(v), _pct(v, week_total), "MEASURED"] for k, v in _ranked(comp_groups)],
    )
    untagged = _sum(comp_groups.get(UNTAGGED, []))
    if untagged > 0:
        out += [
            "",
            f"Untagged spend is {_money(untagged)} {currency} ({_pct(untagged, week_total)}). Some meters (for "
            "example data transfer, or resources whose tags are not inherited) carry no tags; attribute by "
            "`MeterCategory` instead.",
        ]
    out.append("")

    # 5 comparison with cost model
    out += _comparison_section(comp_groups, week_total, len(week_days), cost_model_text, currency)

    # 6 evidence
    out += _evidence_section(wk, currency)

    # 7 C10
    out += _parking_section(rows, mtd_days, currency)

    # 8 reconciliation
    out += _reconciliation_section(wk, book, currency)

    out += ["## 9. What this export cannot show", ""]
    out += [f"- {line}" for line in LIMITS]
    out.append("")
    return "\n".join(out)


def _comparison_section(
    comp_groups: dict[str, list[CostRow]],
    week_total: Decimal,
    n_days: int,
    cost_model_text: str,
    currency: str,
) -> list[str]:
    out = ["## 5. Comparison with `docs/finops/cost-model.md`", ""]
    if currency != MODEL_CURRENCY:
        return [*out, "Skipped: currency mismatch.", ""]
    estimates = parse_cost_model(cost_model_text)
    per_month = HOURS_PER_MONTH / HOURS_PER_DAY / n_days
    body: list[list[str]] = []
    components = sorted(set(estimates) | set(comp_groups))
    for comp in components:
        measured = _sum(comp_groups.get(comp, []))
        runrate = measured * per_month
        est = estimates.get(comp)
        if est is None:
            body.append([comp, _money(measured), _money(runrate), "-", "-", "not in cost model"])
            continue
        low = min(est.parked, est.active_low)
        verdict = (
            "within"
            if low <= runrate <= est.active_high
            else ("below" if runrate < low else "ABOVE")
        )
        body.append(
            [
                comp,
                _money(measured),
                _money(runrate),
                f"{_money(est.parked)} / {_money(est.active_low)}-{_money(est.active_high)}",
                verdict,
                "; ".join(est.rows),
            ]
        )
    out += _table(
        [
            "component",
            f"Week {currency} (MEASURED)",
            "Run-rate/month (MODELLED)",
            "Model: parked / active (MODELLED)",
            "Verdict",
            "cost-model rows",
        ],
        body,
    )
    out += [
        "",
        f"Total run-rate {_money(week_total * per_month)} {currency}/month (MODELLED) against the cost-model total "
        "(see its Total row). The estimate is read from the first table of `cost-model.md` (leading number or "
        "range per cell); a verdict of `within` means the run-rate lies between the parked estimate and the top "
        "of the active estimate. A week is not a month: one-off meters and the 8-24 h billing lag skew short "
        "windows, so treat `ABOVE` as a prompt to look, not as a defect.",
        "",
    ]
    return out


def _evidence_section(wk: Sequence[CostRow], currency: str) -> list[str]:
    out = ["## 6. Evidence columns for C3 to C9 (week)", ""]
    nodes = [r for r in wk if r.resource_group.startswith("mc_")]
    out.append(
        f"C4 (AKS autoscaling and spot): {len(nodes)} export rows come from an `MC_*` node resource group. "
        + (
            "An RG-scoped export normally has none, so node-hours are NEEDS-CLOUD (see section 9)."
            if not nodes
            else "Node-hour evidence is the `Virtual Machines` rows of those groups."
        )
    )
    out.append("")
    for cid, title, cats in EVIDENCE:
        sel = [
            r for r in wk if r.category.lower() in cats and not r.resource_group.startswith("mc_")
        ]
        out += [f"### {cid} {title}", ""]
        if not sel:
            out += ["No rows in this window (MEASURED: zero).", ""]
            continue
        groups: dict[tuple[str, str, str], list[CostRow]] = defaultdict(list)
        for r in sel:
            groups[(r.category, r.meter, r.uom)].append(r)
        ordered = sorted(groups.items(), key=lambda kv: (-_sum(kv[1]), kv[0]))
        out += _table(
            ["Category", "Meter", "Unit", "Quantity", "Days billed", currency, "Label"],
            [
                [
                    cat,
                    meter,
                    uom or "-",
                    _qty(sum((x.quantity for x in v), Decimal(0))),
                    str(len({x.day for x in v})),
                    _money(_sum(v)),
                    "MEASURED",
                ]
                for (cat, meter, uom), v in ordered
            ],
        )
        out.append("")
    return out


def _parking_section(rows: Sequence[CostRow], mtd_days: Sequence[date], currency: str) -> list[str]:
    out = ["## 7. C10 parking: cost by day, split by `park` tag (month to date)", ""]
    sel = [r for r in rows if r.day in set(mtd_days)]
    by_day = _group(sel, lambda r: r.day)
    body: list[list[str]] = []
    for d in sorted(by_day):
        day_rows = by_day[d]
        parkable = _sum(r for r in day_rows if r.park == "true")
        always = _sum(r for r in day_rows if r.park == "false")
        other = _sum(r for r in day_rows if r.park not in ("true", "false"))
        body.append(
            [
                str(d),
                f"{d:%a}",
                _money(parkable),
                _money(always),
                _money(other),
                _money(parkable + always + other),
            ]
        )
    out += _table(
        ["Day", "Weekday", "park=true", "park=false", "untagged", f"Total {currency}"],
        body,
    )
    out += [
        "",
        "All columns MEASURED. A parked day shows `park=true` spend near zero while `park=false` stays flat; the "
        "per-month saving of C10 is (active-day `park=true` mean minus parked-day mean) x parked days, computed "
        "by T-504 from this series. Hour of day is not available (section 9).",
        "",
    ]
    return out


def _reconciliation_section(wk: Sequence[CostRow], book: PriceBook, currency: str) -> list[str]:
    out = ["## 8. Billed vs list price x quantity (week, by `MeterId`)", ""]
    if currency != MODEL_CURRENCY:
        return [*out, "Skipped: currency mismatch.", ""]
    prices = _single_tier_prices(book)
    groups: dict[str, list[CostRow]] = defaultdict(list)
    for r in wk:
        if r.meter_id in prices:
            groups[r.meter_id].append(r)
    if not groups:
        return [*out, "No billed meter matches a single-tier meter of the price snapshot.", ""]
    body: list[list[str]] = []
    for mid, v in sorted(groups.items(), key=lambda kv: (-_sum(kv[1]), kv[0])):
        price = prices[mid]
        qty = sum((x.quantity for x in v), Decimal(0))
        billed = _sum(v)
        modelled = qty * Decimal(str(price.unitPrice))
        body.append(
            [
                price.key,
                _qty(qty),
                price.unit,
                _money(billed),
                _money(modelled),
                _money(billed - modelled),
            ]
        )
    out += _table(
        [
            "Price key",
            "Quantity",
            "Unit",
            f"Billed {currency} (MEASURED)",
            "List x qty (MODELLED)",
            "Billed - list",
        ],
        body,
    )
    out += [
        "",
        "A negative difference is a free grant, rounding or a discount; a positive one is a meter the snapshot "
        "does not price correctly (tiered, regional or changed since capture). Tiered and precision-limited "
        "meters are excluded. The export quantity is assumed to use the retail meter unit.",
        "",
    ]
    return out


# --- Redaction --------------------------------------------------------------------------------------

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SUBSCRIPTION_RE = re.compile(rf"/subscriptions/{_GUID}", re.IGNORECASE)
_GUID_RE = re.compile(rf"{_GUID}|(?<![0-9a-fA-F])[0-9a-fA-F]{{32}}(?![0-9a-fA-F])")


def redact(text: str) -> str:
    """Remove tenant-identifying GUIDs from report text (T-609, ADR-0008 R-29).

    The repository is public, so CI job summaries and artifacts are world-readable.
    ``/subscriptions/<guid>`` becomes ``/subscriptions/<sub>`` and every other GUID (resource ids,
    principal or client ids) becomes ``<id>``. Resource group and resource names are **kept**: they
    are deterministic from the public Terraform naming and the report needs them to be useful.
    Digits, money amounts and dates never match a GUID, so no number changes.
    """
    return _GUID_RE.sub("<id>", _SUBSCRIPTION_RE.sub("/subscriptions/<sub>", text))


# --- CLI --------------------------------------------------------------------------------------------


def generate(
    exports: Sequence[Path],
    *,
    out_dir: Path,
    prices: Path,
    cost_model: Path,
    week: str | None,
    redact_ids: bool = True,
) -> Path:
    """Read exports (oldest first), write ``cost-weekly-<YYYY-Www>.md`` into ``out_dir`` and return its path.

    ``redact_ids`` (default on) strips subscription and other GUIDs via :func:`redact`.
    """
    snapshots = [snap for snap in expand_snapshots(exports) if snap]
    if not snapshots:
        msg = "no export CSV files found"
        raise CostReportError(msg)
    files = [f for snap in snapshots for f in snap]
    rows = merge_snapshots(snapshots)
    if not rows:
        msg = "export contains no rows"
        raise CostReportError(msg)
    window = parse_week(week) if week else parse_week(iso_week_label(max(r.day for r in rows)))
    text = build_report(
        rows,
        week=window,
        book=load_prices(prices),
        cost_model_text=cost_model.read_text(encoding="utf-8"),
        source_names=[f.name for f in files],
    )
    if redact_ids:
        text = redact(text)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"cost-weekly-{iso_week_label(window[0])}.md"
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Weekly cost report from a Cost Management export")
    parser.add_argument(
        "--export", type=Path, action="append", required=True, help="CSV or directory; oldest first"
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    parser.add_argument("--cost-model", type=Path, default=DEFAULT_COST_MODEL)
    parser.add_argument(
        "--week", default=None, help="ISO week, e.g. 2026-W44 (default: week of the latest day)"
    )
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="keep subscription/resource GUIDs in the report (local use only; CI must redact)",
    )
    args = parser.parse_args(argv)
    try:
        target = generate(
            args.export,
            out_dir=args.out_dir,
            prices=args.prices,
            cost_model=args.cost_model,
            week=args.week,
            redact_ids=not args.no_redact,
        )
    except CostReportError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    print(target)  # noqa: T201 - CLI
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
