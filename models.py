from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class LineItem:
    charge: str
    amount: Optional[float] = None
    currency: str = "USD"


@dataclass
class BreakdownSection:
    name: str
    items: list[LineItem] = field(default_factory=list)


@dataclass
class PriceBreakdown:
    container_type: Optional[str] = None
    currency: str = "USD"
    sections: list[BreakdownSection] = field(default_factory=list)
    total_per_container: Optional[float] = None


@dataclass
class Sailing:
    vessel: str = ""
    voyage: str = ""
    pol: str = ""
    pod: str = ""
    route_type: str = "Direct"
    etd: Optional[str] = None
    eta: Optional[str] = None
    duration_days: Optional[int] = None
    vgm_cutoff: Optional[str] = None
    last_gate_in: Optional[str] = None
    doc_cutoff: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    allocation_available: bool = True
    containers: Optional[str] = None
    basic_price: Optional[float] = None
    premium_price: Optional[float] = None
    basic_breakdown: Optional[PriceBreakdown] = None
    premium_breakdown: Optional[PriceBreakdown] = None

    def to_dict(self) -> dict:
        return asdict(self)
