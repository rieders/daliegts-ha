"""Datenmodell von Lagerwerk.

Regel: Änderungen nur additiv und ausschließlich über Alembic-Migrationen (siehe docs/ARCHITEKTUR.md).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now, server_default=func.current_timestamp())


# ---------------------------------------------------------------- Benutzer & Rechte
class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list["GroupPermission"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class GroupPermission(Base):
    __tablename__ = "group_permissions"
    __table_args__ = (UniqueConstraint("group_id", "area"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    area: Mapped[str] = mapped_column(String(40))
    level: Mapped[int] = mapped_column(Integer, default=0)


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    theme: Mapped[str] = mapped_column(String(10), default="auto")
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    group: Mapped[Group | None] = relationship(lazy="joined")
    overrides: Mapped[list["UserPermission"]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    @property
    def label(self) -> str:
        return self.display_name or self.username


class UserPermission(Base):
    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "area"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    area: Mapped[str] = mapped_column(String(40))
    level: Mapped[int] = mapped_column(Integer, default=0)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user: Mapped[User] = relationship(lazy="joined")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(20))  # create, update, delete, stock, login, import, ...
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    user: Mapped[User | None] = relationship(lazy="joined")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)


class CodeSequence(Base):
    """Fortlaufende Codes (L-, A-, S-) – Nummern werden nie wiederverwendet."""
    __tablename__ = "code_sequences"
    prefix: Mapped[str] = mapped_column(String(4), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, default=1)


class ExternalId(Base):
    """Zuordnung von Datensätzen aus Altsystemen (Part-DB, InvenTree) für idempotente Importe."""
    __tablename__ = "external_ids"
    __table_args__ = (UniqueConstraint("source", "ext_type", "ext_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    ext_type: Mapped[str] = mapped_column(String(40))
    ext_id: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- Stammdaten
class Location(Base, TimestampMixin):
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(30), default="sonstiges")
    description: Mapped[str] = mapped_column(Text, default="")
    photo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    icon: Mapped[str] = mapped_column(String(40), default="")
    code: Mapped[str] = mapped_column(String(20), unique=True)
    structural: Mapped[bool] = mapped_column(Boolean, default=False)
    is_full: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Position im Raster des Elternmöbels
    grid_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grid_col: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grid_rowspan: Mapped[int] = mapped_column(Integer, default=1)
    grid_colspan: Mapped[int] = mapped_column(Integer, default=1)
    # Für Lagermöbel: Rastergröße und Anzeige-Einstellungen
    grid_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grid_cols: Mapped[int | None] = mapped_column(Integer, nullable=True)
    light_mode: Mapped[str] = mapped_column(String(10), default="both")  # blink | segment | both
    # Für Abteile: Position innerhalb des Fachs
    compartment_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compartment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compartment_layout: Mapped[str | None] = mapped_column(String(10), nullable=True)  # row | column | grid
    zone_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    zone_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    label_printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # Stichproben-Inventur (v0.6)
    # Wandansicht (v0.2): Platz des Moduls auf der Fläche des Elternorts, in cm (y von oben)
    wall_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Für Wände/Räume: LED-Ketten über mehrere Module; für Möbel: Verkabelung des eigenen Rasters
    layout: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    led_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    parent: Mapped["Location | None"] = relationship(remote_side="Location.id", back_populates="children")
    children: Mapped[list["Location"]] = relationship(back_populates="parent", order_by="Location.sort_order, Location.id")
    lights: Mapped[list["LocationLight"]] = relationship(back_populates="location", cascade="all, delete-orphan")
    aliases: Mapped[list["LocationAlias"]] = relationship(cascade="all, delete-orphan")


class LocationAlias(Base):
    """Externe Codes eines Lagerorts (z. B. alte InvenTree-Etiketten)."""
    __tablename__ = "location_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(255), unique=True)
    source: Mapped[str] = mapped_column(String(40), default="manuell")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(40), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    default_params: Mapped[list | None] = mapped_column(JSON, nullable=True)
    parent: Mapped["Category | None"] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent", order_by="Category.sort_order, Category.name")


class Footprint(Base):
    __tablename__ = "footprints"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("footprints.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    parent: Mapped["Footprint | None"] = relationship(remote_side="Footprint.id", back_populates="children")
    children: Mapped[list["Footprint"]] = relationship(back_populates="parent", order_by="Footprint.name")


class Manufacturer(Base):
    __tablename__ = "manufacturers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    website: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    website: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    # v0.2: Shop-Suche ({query}), Versandkosten, Kundennummer
    search_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    shipping_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    free_shipping_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_order_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    customer_no: Mapped[str] = mapped_column(String(80), default="", server_default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class ParameterTemplate(Base):
    __tablename__ = "parameter_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    unit: Mapped[str] = mapped_column(String(20), default="")
    kind: Mapped[str] = mapped_column(String(10), default="number")  # number | text | choice
    choices: Mapped[list | None] = mapped_column(JSON, nullable=True)


# ---------------------------------------------------------------- Artikel & Bestand
ITEM_KINDS = {
    "bauteil": "Bauteil",
    "werkzeug": "Werkzeug",
    "geraet": "Gerät",
    "verbrauch": "Verbrauchsmaterial",
    "filament": "Filament",
    "sonstiges": "Sonstiges",
}
STOCK_MODES = {"exact": "exakt", "coarse": "grob (Ampel)", "unknown": "unbekannt"}
COARSE_LEVELS = {"empty": "leer", "low": "wenig", "ok": "genug"}
LOT_STATUS = {
    "ok": "OK",
    "attention": "Achtung",
    "damaged": "beschädigt",
    "defect": "defekt",
    "lost": "verloren",
    "used": "verbraucht",
}


class Item(Base, TimestampMixin):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="bauteil")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    manufacturer_id: Mapped[int | None] = mapped_column(ForeignKey("manufacturers.id"), nullable=True)
    mpn: Mapped[str] = mapped_column(String(120), default="")
    footprint_id: Mapped[int | None] = mapped_column(ForeignKey("footprints.id"), nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(10), default="Stk")
    stock_mode: Mapped[str] = mapped_column(String(10), default="exact")
    min_stock: Mapped[float] = mapped_column(Float, default=0)
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    origin: Mapped[str] = mapped_column(String(20), default="")  # DDR, UdSSR, West, ...
    historic_manufacturer: Mapped[str] = mapped_column(String(120), default="")
    date_code: Mapped[str] = mapped_column(String(40), default="")
    condition: Mapped[str] = mapped_column(String(40), default="")
    collector_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    variant_of_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    catalog_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Werkzeug/Gerät
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    purchase_vendor: Mapped[str] = mapped_column(String(120), default="")
    warranty_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    reorder_qty: Mapped[float] = mapped_column(Float, default=0, server_default="0")  # v0.2: übliche Nachbestellmenge
    online_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # v0.3: letzter Vergleich bei anderen Shops
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    category: Mapped[Category | None] = relationship(lazy="joined")
    manufacturer: Mapped[Manufacturer | None] = relationship(lazy="joined")
    footprint: Mapped[Footprint | None] = relationship(lazy="joined")
    lots: Mapped[list["StockLot"]] = relationship(back_populates="item", cascade="all, delete-orphan", order_by="StockLot.id")
    parameters: Mapped[list["ItemParameter"]] = relationship(cascade="all, delete-orphan", order_by="ItemParameter.id")
    codes: Mapped[list["ItemCode"]] = relationship(cascade="all, delete-orphan")
    variant_of: Mapped["Item | None"] = relationship(remote_side="Item.id", back_populates="variants")
    variants: Mapped[list["Item"]] = relationship(back_populates="variant_of")

    @property
    def total_quantity(self) -> float:
        return sum(lot.quantity or 0 for lot in self.lots if lot.status not in ("lost", "used"))

    @property
    def coarse_level(self) -> str | None:
        levels = [lot.coarse for lot in self.lots if lot.coarse]
        if not levels:
            return None
        order = {"empty": 0, "low": 1, "ok": 2}
        return max(levels, key=lambda v: order.get(v, 0))

    @property
    def below_min(self) -> bool:
        if self.stock_mode == "exact":
            return self.min_stock > 0 and self.total_quantity < self.min_stock
        if self.stock_mode == "coarse":
            return self.coarse_level in (None, "empty", "low") and bool(self.lots)
        return False


class ItemParameter(Base):
    __tablename__ = "item_parameters"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    value_text: Mapped[str] = mapped_column(String(255), default="")
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    value_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="")


class ItemCode(Base):
    """Barcodes/Fremdcodes eines Artikels (EAN, Distributor, alte Systeme)."""
    __tablename__ = "item_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="barcode")
    source: Mapped[str] = mapped_column(String(40), default="manuell")


class StockLot(Base, TimestampMixin):
    __tablename__ = "stock_lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True, index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0)
    coarse: Mapped[str | None] = mapped_column(String(10), nullable=True)
    batch: Mapped[str] = mapped_column(String(80), default="")
    serial: Mapped[str] = mapped_column(String(80), default="")
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="ok")
    notes: Mapped[str] = mapped_column(Text, default="")
    needs_refill: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    item: Mapped[Item] = relationship(back_populates="lots", lazy="joined")
    location: Mapped[Location | None] = relationship(lazy="joined")
    codes: Mapped[list["LotCode"]] = relationship(cascade="all, delete-orphan")


class LotCode(Base):
    """Fremdcodes eines einzelnen Bestands (z. B. Herstellercode einer Filamentspule)."""
    __tablename__ = "lot_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("stock_lots.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(255), unique=True)
    source: Mapped[str] = mapped_column(String(40), default="manuell")


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("stock_lots.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    delta: Mapped[float] = mapped_column(Float, default=0)
    quantity_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    from_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    to_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    ref: Mapped[str] = mapped_column(String(80), default="")
    item: Mapped[Item] = relationship(lazy="joined")
    user: Mapped[User | None] = relationship(lazy="joined")
    from_location: Mapped[Location | None] = relationship(foreign_keys=[from_location_id], lazy="joined")
    to_location: Mapped[Location | None] = relationship(foreign_keys=[to_location_id], lazy="joined")


MOVEMENT_KINDS = {
    "created": "angelegt",
    "added": "eingelagert",
    "removed": "entnommen",
    "moved": "umgelagert",
    "counted": "gezählt (Inventur)",
    "edited": "geändert",
    "merged": "zusammengeführt",
    "split": "aufgeteilt",
    "imported": "importiert",
    "loan": "verliehen",
    "returned": "zurückgegeben",
}


class SupplierPart(Base):
    __tablename__ = "supplier_parts"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    sku: Mapped[str] = mapped_column(String(120), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    pack_qty: Mapped[float] = mapped_column(Float, default=1)
    obsolete: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    # v0.3: Online-Abgleich (Info-Provider)
    stock_available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    check_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    origin: Mapped[str] = mapped_column(String(20), default="manuell", server_default="manuell")  # manuell | automatisch
    supplier: Mapped[Supplier] = relationship(lazy="joined")
    prices: Mapped[list["PriceInfo"]] = relationship(order_by="PriceInfo.min_qty", viewonly=True)


class PriceInfo(Base):
    __tablename__ = "price_infos"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    supplier_part_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_parts.id", ondelete="CASCADE"), nullable=True)
    min_qty: Mapped[float] = mapped_column(Float, default=1)
    unit_price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manuell")  # manuell, provider, historisch


SHOPPING_STATUS = {"open": "offen", "ordered": "bestellt", "received": "erhalten", "cancelled": "storniert"}


class ShoppingItem(Base):
    """Einkaufsliste / Bestellungen (v0.2)."""
    __tablename__ = "shopping_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=True, index=True)
    text: Mapped[str] = mapped_column(String(255), default="", server_default="")  # für Einträge ohne Artikel
    quantity: Mapped[float] = mapped_column(Float, default=1, server_default="1")
    supplier_part_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_parts.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="open", server_default="open", index=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # beim Bestellen festgehalten
    order_ref: Mapped[str] = mapped_column(String(80), default="", server_default="")
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)  # Fehlteil für Auftrag (v0.9)
    item: Mapped[Item | None] = relationship(lazy="joined")
    supplier_part: Mapped[SupplierPart | None] = relationship(lazy="joined")


CANDIDATE_REASONS = {"mehrdeutig": "mehrere passende Treffer", "unsicher": "Hersteller-Nr. passt nicht genau", "zu_wenig": "Angaben reichen nicht für die Suche"}


class ProviderCandidate(Base):
    """Online-Treffer, bei denen Lagerwerk nachfragt statt selbst einzutragen (v0.3)."""
    __tablename__ = "provider_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(20), default="mehrdeutig", server_default="mehrdeutig")
    data: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)  # Liste der Treffer
    status: Mapped[str] = mapped_column(String(12), default="open", server_default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    item: Mapped[Item] = relationship(lazy="joined")


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(20))  # item, location, lot
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="other")  # image, datasheet, invoice, manual, other
    title: Mapped[str] = mapped_column(String(255), default="")
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Equivalent(Base):
    """Vergleichstypen (z. B. DDR-Bauelemente)."""
    __tablename__ = "equivalents"
    id: Mapped[int] = mapped_column(primary_key=True)
    type_a: Mapped[str] = mapped_column(String(80), index=True)
    type_b: Mapped[str] = mapped_column(String(120), index=True)
    relation: Mapped[str] = mapped_column(String(20), default="aehnlich")  # pinkompatibel, funktionsgleich, aehnlich
    note: Mapped[str] = mapped_column(Text, default="")


class Loan(Base):
    __tablename__ = "loans"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("stock_lots.id", ondelete="SET NULL"), nullable=True)
    borrower_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    borrower_name: Mapped[str] = mapped_column(String(120), default="")
    since: Mapped[date] = mapped_column(Date, default=date.today)
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    returned_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    item: Mapped[Item] = relationship(lazy="joined")


# ---------------------------------------------------------------- WLED & Vorlagen
class WledController(Base):
    __tablename__ = "wled_controllers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    host: Mapped[str] = mapped_column(String(120))
    port: Mapped[int] = mapped_column(Integer, default=80)
    led_count: Mapped[int] = mapped_column(Integer, default=60)
    segment_id: Mapped[int] = mapped_column(Integer, default=0)
    brightness: Mapped[int] = mapped_column(Integer, default=160)
    color: Mapped[str] = mapped_column(String(6), default="00FF40")
    restore_mode: Mapped[str] = mapped_column(String(12), default="off")  # off | preset | previous
    restore_preset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_s: Mapped[int] = mapped_column(Integer, default=30)
    idle_mode: Mapped[str] = mapped_column(String(12), default="off")  # off | preset | lowstock
    idle_preset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idle_after_min: Mapped[int] = mapped_column(Integer, default=5)
    idle_brightness: Mapped[int] = mapped_column(Integer, default=40)
    idle_from: Mapped[str] = mapped_column(String(5), default="07:00")
    idle_to: Mapped[str] = mapped_column(String(5), default="22:00")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class LocationLight(Base):
    __tablename__ = "location_lights"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"), index=True)
    controller_id: Mapped[int] = mapped_column(ForeignKey("wled_controllers.id", ondelete="CASCADE"))
    led_start: Mapped[int] = mapped_column(Integer)
    led_count: Mapped[int] = mapped_column(Integer, default=1)
    color: Mapped[str | None] = mapped_column(String(6), nullable=True)
    location: Mapped[Location] = relationship(back_populates="lights")
    controller: Mapped[WledController] = relationship(lazy="joined")


class StorageTemplate(Base):
    __tablename__ = "storage_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(30), default="magazin")
    definition: Mapped[dict] = mapped_column(JSON)
    notes: Mapped[str] = mapped_column(Text, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


# ------------------------------------------------------------------ Aufträge (v0.9)
ORDER_STATUS = {"offen": "offen", "in_arbeit": "in Arbeit", "erledigt": "erledigt", "storniert": "storniert"}
ORDER_ACTIVE = ("offen", "in_arbeit")


class Order(Base):
    """Auftrag/Projekt: Materialliste, reserviert Bestand, Teile werden nach und nach entnommen."""
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    requester: Mapped[str] = mapped_column(String(120), default="", server_default="")  # für wen / Kunde / Anlage
    status: Mapped[str] = mapped_column(String(12), default="offen", server_default="offen", index=True)
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # höher = bekommt Bestand zuerst
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)  # v0.10
    build_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project: Mapped["Project | None"] = relationship(lazy="joined")
    lines: Mapped[list["OrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.sort, OrderLine.id")

    @property
    def active(self) -> bool:
        return self.status in ORDER_ACTIVE


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    qty_required: Mapped[float] = mapped_column(Float, default=1)
    qty_issued: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    note: Mapped[str] = mapped_column(String(255), default="", server_default="")  # z. B. Bestückungsreferenz R1, R5
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    order: Mapped[Order] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship(lazy="joined")

    @property
    def remaining(self) -> float:
        return max(0.0, (self.qty_required or 0) - (self.qty_issued or 0))


# ------------------------------------------------------------------ Projekte / Baugruppen (v0.10)
class Project(Base):
    """Gespeicherte Stückliste einer Baugruppe, z. B. Platine „Lichtschranke“ (auch aus KiCad)."""
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[str] = mapped_column(String(40), default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    source: Mapped[str] = mapped_column(String(255), default="", server_default="")  # Dateiname des KiCad-Imports
    pcb_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True)  # die Leiterplatte selbst
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    lines: Mapped[list["ProjectLine"]] = relationship(back_populates="project", cascade="all, delete-orphan",
                                                      order_by="ProjectLine.sort, ProjectLine.id")


class ProjectLine(Base):
    __tablename__ = "project_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True, index=True)
    refs: Mapped[str] = mapped_column(Text, default="", server_default="")  # R1, R2, R5
    value: Mapped[str] = mapped_column(String(200), default="", server_default="")
    footprint: Mapped[str] = mapped_column(String(200), default="", server_default="")
    mpn: Mapped[str] = mapped_column(String(120), default="", server_default="")
    qty: Mapped[float] = mapped_column(Float, default=1)  # je Platine
    dnp: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # nicht bestücken
    match: Mapped[str] = mapped_column(String(12), default="", server_default="")  # wie zugeordnet (code/mpn/lcsc/gemerkt/wert/hand/neu)
    note: Mapped[str] = mapped_column(String(255), default="", server_default="")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    project: Mapped[Project] = relationship(back_populates="lines")
    item: Mapped[Item | None] = relationship(lazy="joined")


# ------------------------------------------------------------------ Etiketten-Vorlagen (v0.12)
class LabelTemplate(Base):
    """Eigene Etikettenvorlage: Format (Größe/Bogen), Code-Art und frei gewählte Textzeilen – je Etikettenart."""
    __tablename__ = "label_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(12), default="location", index=True)  # location | item | lot
    media: Mapped[str] = mapped_column(String(30), default="eigen")  # Schlüssel eines Formats (Bogen/Band) oder „eigen“
    width_mm: Mapped[float] = mapped_column(Float, default=55)
    height_mm: Mapped[float] = mapped_column(Float, default=16)
    symbology: Mapped[str] = mapped_column(String(12), default="qr")  # qr | code128 | qr+code128 | none
    code_pos: Mapped[str] = mapped_column(String(8), default="left")  # left | right
    lines: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"f": "name", "size": "l", "bold": true, "text": ""}]
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ------------------------------------------------------------------ Etikettendrucker (v0.13)
class LabelPrinter(Base):
    """Netzwerk-Etikettendrucker (Brother QL) für Direktdruck ohne Druckdialog."""
    __tablename__ = "label_printers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(20), default="QL-820NWB")
    host: Mapped[str] = mapped_column(String(200))
    port: Mapped[int] = mapped_column(Integer, default=9100)
    media: Mapped[str] = mapped_column(String(12), default="29")  # brother_ql-Etikettenkennung, z. B. 29, 62, 62x29
    cut: Mapped[bool] = mapped_column(Boolean, default=True)  # nach jedem Etikett schneiden
    hq: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold: Mapped[int] = mapped_column(Integer, default=70)
    rotate: Mapped[int] = mapped_column(Integer, default=90)  # Drehung des Etiketts auf dem Band: 90 oder 270
    offset_mm: Mapped[float] = mapped_column(Float, default=0)  # Feinjustierung quer zum Band
    # v0.19: weitere Druckersprachen
    driver: Mapped[str] = mapped_column(String(20), default="brother_ql", server_default="brother_ql")  # brother_ql | zpl | tspl | cups
    dpi: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    media_w_mm: Mapped[float | None] = mapped_column(Float, nullable=True)  # Etikettengröße (Zebra/TSPL/CUPS)
    media_h_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_mm: Mapped[float | None] = mapped_column(Float, nullable=True)  # TSPL: Lücke zwischen Etiketten
    invert: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # TSPL: Farben umkehren
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_ok: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ------------------------------------------------------------------ 3D-Drucker / Filamentverbrauch (v0.16)
class Printer3D(Base):
    """3D-Drucker im Heimnetz (Creality K1 mit Original-Firmware: WebSocket Port 9999). Bucht Filament nach jedem Druck ab."""
    __tablename__ = "printers_3d"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="creality")  # creality | bambu | prusalink | moonraker | octoprint
    host: Mapped[str] = mapped_column(String(200))
    port: Mapped[int] = mapped_column(Integer, default=9999)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    diameter_mm: Mapped[float] = mapped_column(Float, default=1.75)
    spool_lot_id: Mapped[int | None] = mapped_column(ForeignKey("stock_lots.id", ondelete="SET NULL"), nullable=True)
    job: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # laufender Druck: {"file", "used_mm", "expected_g", "expected_mm", "seen"}
    pending: Mapped[list | None] = mapped_column(JSON, nullable=True)  # Drucke ohne eingesetzte Spule: [{"file", "grams", "meters", "ts"}]
    last_state: Mapped[str] = mapped_column(String(20), default="")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    warning: Mapped[str | None] = mapped_column(String(300), nullable=True)  # z. B. „Spule reicht nicht“
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v0.17: Modell, CFS (4 Plätze), Mitschnitt der Telemetrie
    model: Mapped[str] = mapped_column(String(40), default="K1C", server_default="K1C")
    has_cfs: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    cfs_slots: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [lot_id|None] × 4
    recording_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # v0.23: weitere Druckerfamilien – Zugangscode/API-Schlüssel/Passwort und Seriennummer bzw. Benutzername
    access_code: Mapped[str] = mapped_column(String(200), default="", server_default="")
    serial: Mapped[str] = mapped_column(String(64), default="", server_default="")
    spool_lot: Mapped["StockLot | None"] = relationship(lazy="joined")


class PrintModel(Base):
    """Druckmodell (G-Code-Datei auf dem Drucker) mit geplantem und tatsächlichem Filamentverbrauch (v0.17)."""
    __tablename__ = "print_models"
    __table_args__ = (UniqueConstraint("printer_id", "path", name="uq_print_models_printer_path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers_3d.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(255))
    expected_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_g: Mapped[float | None] = mapped_column(Float, nullable=True)  # zuletzt tatsächlich verbraucht
    prints: Mapped[int] = mapped_column(Integer, default=0)
    last_printed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    on_printer: Mapped[bool] = mapped_column(Boolean, default=True)  # in der letzten Dateiliste des Druckers enthalten
    seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # v0.23.1: zuletzt auf dem Drucker gesehen (sonst Archiv)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def need_g(self) -> float | None:
        """Bedarf für den nächsten Druck: zuletzt tatsächlich verbraucht, sonst Slicer-Angabe."""
        return self.last_g if self.last_g else self.expected_g
