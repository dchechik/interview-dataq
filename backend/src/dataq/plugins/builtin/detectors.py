"""Built-in semantic type detectors.

Each returns zero or more guesses with a confidence in 0..1. The profiler keeps the
highest-confidence guess above a threshold, so detectors are free to be optimistic
about their own signal and let ranking sort it out.
"""

from __future__ import annotations

import ipaddress
import re
from typing import ClassVar

from ...core.profile import ColumnStats, SemanticGuess
from ..base import register
from ..kinds import Detector

_ISO2_CODES = """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN
    BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL
    GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM
    JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
    MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP
    NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD
    SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
    TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
"""

ISO2 = frozenset(_ISO2_CODES.split())

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
URL_RE = re.compile(r"^(https?|s3|gs|ftp)://\S+$", re.I)

NUMERIC_PHYSICAL = ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "DOUBLE",
                    "FLOAT", "REAL", "DECIMAL")


def _is_numeric(stats: ColumnStats) -> bool:
    return any(stats.physical_type.upper().startswith(p) for p in NUMERIC_PHYSICAL)


def _is_text(stats: ColumnStats) -> bool:
    return stats.physical_type.upper().startswith("VARCHAR")


def _strings(stats: ColumnStats) -> list[str]:
    return [str(v) for v in stats.sample_values if v is not None]


def _hit_rate(values: list[str], predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if predicate(v)) / len(values)


def _name_has(stats: ColumnStats, *needles: str) -> bool:
    n = stats.name.lower()
    return any(x in n for x in needles)


@register
class IpDetector(Detector):
    """IPv4/IPv6 addresses."""

    id: ClassVar[str] = "detect.ip"
    title: ClassVar[str] = "IP address"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_text(stats):
            return []
        vals = _strings(stats)

        def ok(v: str) -> bool:
            try:
                ipaddress.ip_address(v.strip())
                return True
            except ValueError:
                return False

        rate = _hit_rate(vals, ok)
        if rate < 0.9:
            return []
        conf = 0.95 if rate == 1.0 else 0.8
        if _name_has(stats, "ip", "addr"):
            conf = min(0.99, conf + 0.04)
        return [SemanticGuess(semantic_type="net.ip", confidence=conf,
                              rationale=f"{rate:.0%} of sampled values parse as IP addresses")]


@register
class CountryDetector(Detector):
    """ISO-3166 alpha-2 country codes."""

    id: ClassVar[str] = "detect.country_iso2"
    title: ClassVar[str] = "Country code"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_text(stats):
            return []
        vals = _strings(stats)
        rate = _hit_rate(vals, lambda v: v.strip().upper() in ISO2 and len(v.strip()) == 2)
        if rate < 0.9:
            return []
        conf = 0.9 if rate == 1.0 else 0.7
        if _name_has(stats, "country", "cc", "nation"):
            conf = min(0.99, conf + 0.08)
        return [SemanticGuess(semantic_type="geo.country_iso2", confidence=conf,
                              rationale=f"{rate:.0%} of sampled values are ISO-3166 alpha-2 codes")]


@register
class LatLngDetector(Detector):
    """Latitude / longitude, from name plus value range.

    Range alone is weak evidence -- plenty of numbers sit in -90..90 -- so a name
    hint is required for a confident guess.
    """

    id: ClassVar[str] = "detect.latlng"
    title: ClassVar[str] = "Latitude / longitude"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_numeric(stats):
            return []
        try:
            lo, hi = float(stats.min), float(stats.max)
        except (TypeError, ValueError):
            return []
        out: list[SemanticGuess] = []
        lat_name = _name_has(stats, "latitude", "lat")
        lng_name = _name_has(stats, "longitude", "lng", "lon")
        # Check longitude first: "lon" columns often also contain "lat"-free names,
        # and a longitude range implies the latitude range too.
        if lng_name and lo >= -180.0 and hi <= 180.0:
            out.append(SemanticGuess(semantic_type="geo.lng", confidence=0.93,
                                     rationale="name suggests longitude and values fit -180..180"))
        elif lat_name and lo >= -90.0 and hi <= 90.0:
            out.append(SemanticGuess(semantic_type="geo.lat", confidence=0.93,
                                     rationale="name suggests latitude and values fit -90..90"))
        return out


@register
class TimestampDetector(Detector):
    """Temporal columns, physical or string-encoded."""

    id: ClassVar[str] = "detect.timestamp"
    title: ClassVar[str] = "Timestamp"

    ISO_RE: ClassVar[re.Pattern] = re.compile(
        r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?"
    )

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        pt = stats.physical_type.upper()
        if pt.startswith("TIMESTAMP"):
            return [SemanticGuess(semantic_type="time.timestamp", confidence=0.99,
                                  rationale="column is physically a TIMESTAMP")]
        if pt.startswith("DATE"):
            return [SemanticGuess(semantic_type="time.date", confidence=0.99,
                                  rationale="column is physically a DATE")]
        if not _is_text(stats):
            return []
        vals = _strings(stats)
        rate = _hit_rate(vals, lambda v: bool(self.ISO_RE.match(v.strip())))
        if rate < 0.9:
            return []
        has_time = any(":" in v for v in vals)
        return [SemanticGuess(
            semantic_type="time.timestamp" if has_time else "time.date",
            confidence=0.75,
            rationale=f"{rate:.0%} of sampled values look like ISO date/times (stored as text)",
        )]


@register
class EmailDetector(Detector):
    """Email addresses."""

    id: ClassVar[str] = "detect.email"
    title: ClassVar[str] = "Email"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_text(stats):
            return []
        rate = _hit_rate(_strings(stats), lambda v: bool(EMAIL_RE.match(v.strip())))
        if rate < 0.9:
            return []
        return [SemanticGuess(semantic_type="identity.email", confidence=0.95,
                              rationale=f"{rate:.0%} of sampled values match an email pattern")]


@register
class UrlDetector(Detector):
    """URLs."""

    id: ClassVar[str] = "detect.url"
    title: ClassVar[str] = "URL"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_text(stats):
            return []
        rate = _hit_rate(_strings(stats), lambda v: bool(URL_RE.match(v.strip())))
        if rate < 0.9:
            return []
        return [SemanticGuess(semantic_type="identity.url", confidence=0.9,
                              rationale=f"{rate:.0%} of sampled values are URLs")]


@register
class MoneyDetector(Detector):
    """Monetary amounts, inferred from column naming."""

    id: ClassVar[str] = "detect.money"
    title: ClassVar[str] = "Monetary amount"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_numeric(stats):
            return []
        if not _name_has(stats, "amount", "fare", "price", "cost", "total", "revenue",
                         "tip", "fee", "charge", "usd", "payment"):
            return []
        return [SemanticGuess(semantic_type="money.amount", confidence=0.7,
                              rationale=f"column name {stats.name!r} suggests a monetary amount")]


@register
class CardinalityDetector(Detector):
    """Low-cardinality enums and high-cardinality identifiers.

    Deliberately low confidence: this is the fallback when no pattern detector
    fires, and any specific detector should outrank it.
    """

    id: ClassVar[str] = "detect.cardinality"
    title: ClassVar[str] = "Enum / identifier"

    def detect(self, stats: ColumnStats) -> list[SemanticGuess]:
        if not _is_text(stats) or stats.row_count < 10:
            return []
        ndv, frac = stats.distinct_count, stats.distinct_frac
        if 0 < ndv <= 200 and frac < 0.2:
            return [SemanticGuess(semantic_type="categorical", confidence=0.5,
                                  rationale=f"only {ndv} distinct values ({frac:.1%} of rows)")]
        if frac > 0.9:
            return [SemanticGuess(semantic_type="identity.key", confidence=0.55,
                                  rationale=f"{frac:.0%} of values are distinct; looks like a key")]
        return []
