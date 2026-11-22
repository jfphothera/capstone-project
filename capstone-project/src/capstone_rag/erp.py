"""Deterministic lookups against the synthetic ERP extract.

This is plain dictionary lookup over JSON. No model, no inference, no fuzzy
matching. The agent trace labels every call here as deterministic code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import config

# ERP serials in the synthetic sandbox are either all-digit (e.g. 98148469) or
# digits + a short alphanumeric suffix (e.g. 68177-DV5).
SERIAL_RE = re.compile(r"\b(\d{5,10}(?:-[A-Z]{2}\d)?)\b")


@dataclass
class Device:
    serial_number: str
    model: str
    revision: str
    status: str
    install_date: str
    account_id: str
    customer_name: str
    prescription_id: Optional[str] = None
    last_meter_recal: Optional[str] = None

    @property
    def model_revision(self) -> str:
        return f"{self.model}|{self.revision}"


@dataclass
class Customer:
    account_id: str
    customer_name: str
    customer_kind: str
    devices: list = field(default_factory=list)


def _load(name):
    from . import remote_services
    if remote_services.configured('erp'):
        return remote_services.get('erp','/api/erp/'+name.removesuffix('.json'))['rows']
    with open(os.path.join(config.ERP_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


class ERP:
    """In-memory view of the synthetic ERP extract."""

    def __init__(self):
        self.customers = {}
        self.devices = {}
        self.orders = _load("orders.json")
        try:
            self.shipments = _load("shipments.json")
        except FileNotFoundError:
            self.shipments = []

        for row in _load("customers.json"):
            self.customers[row["account_id"]] = Customer(
                account_id=row["account_id"],
                customer_name=row["customer_name"],
                customer_kind=row["customer_kind"],
            )

        for row in _load("devices.json"):
            dev = Device(
                serial_number=row["serial_number"],
                model=row["model"],
                revision=row["revision"],
                status=row["status"],
                install_date=row["install_date"],
                account_id=row["account_id"],
                customer_name=row["customer_name"],
                prescription_id=row.get("prescription_id"),
                last_meter_recal=row.get("last_meter_recal"),
            )
            self.devices[dev.serial_number] = dev
            if dev.account_id in self.customers:
                self.customers[dev.account_id].devices.append(dev)

    # -- lookups ------------------------------------------------------------

    def extract_serials(self, text: str) -> list:
        """Return serials mentioned in free text that exist in the ERP."""
        seen, out = set(), []
        for cand in SERIAL_RE.findall(text or ""):
            if cand in self.devices and cand not in seen:
                seen.add(cand)
                out.append(cand)
        return out

    def device_by_serial(self, serial: str) -> Optional[Device]:
        return self.devices.get(serial)

    def customer_by_account(self, account_id: str) -> Optional[Customer]:
        return self.customers.get(account_id)

    def devices_for_account(self, account_id: str) -> list:
        cust = self.customers.get(account_id)
        return list(cust.devices) if cust else []

    def orders_for_account(self, account_id: str) -> list:
        return [o for o in self.orders if o["account_id"] == account_id]

    def orders_for_serial(self, serial: str) -> list:
        return [o for o in self.orders if o.get("device_serial_number") == serial]

    def shipments_for_account(self, account_id: str) -> list:
        return [s for s in self.shipments if s["account_id"] == account_id]

    def shipments_for_serial(self, serial: str) -> list:
        return [s for s in self.shipments
                if s.get("device_serial_number") == serial]

    def order_by_id(self, order_id: str):
        for o in self.orders:
            if o["order_id"] == order_id:
                return o
        return None


@dataclass
class IdentityResult:
    """Outcome of the device-identification step."""

    confirmed: bool
    device: Optional[Device] = None
    customer: Optional[Customer] = None
    reason: str = ""
    candidates: list = field(default_factory=list)

    def as_dict(self):
        return {
            "confirmed": self.confirmed,
            "reason": self.reason,
            "serial_number": self.device.serial_number if self.device else None,
            "model": self.device.model if self.device else None,
            "revision": self.device.revision if self.device else None,
            "status": self.device.status if self.device else None,
            "customer_name": self.customer.customer_name if self.customer else None,
            "account_id": self.customer.account_id if self.customer else None,
            "candidate_serials": [d.serial_number for d in self.candidates],
        }


def identify_device(erp: ERP, message: str, account_id: Optional[str] = None
                    ) -> IdentityResult:
    """Confirm exactly one device from the customer's message plus ERP context.

    Device identity is only 'confirmed' when the resolution is unambiguous.
    An account with two devices and no serial in the message is deliberately
    left unconfirmed rather than guessed.
    """
    serials = erp.extract_serials(message)

    if len(serials) == 1:
        dev = erp.device_by_serial(serials[0])
        return IdentityResult(
            confirmed=True,
            device=dev,
            customer=erp.customer_by_account(dev.account_id),
            reason=f"serial {dev.serial_number} found in message and matched in ERP",
        )

    if len(serials) > 1:
        cands = [erp.device_by_serial(s) for s in serials]
        return IdentityResult(
            confirmed=False,
            reason=f"message names {len(serials)} different serials; ambiguous",
            candidates=cands,
        )

    # No serial in the message - fall back to the account's device list.
    if account_id:
        owned = erp.devices_for_account(account_id)
        if len(owned) == 1:
            dev = owned[0]
            return IdentityResult(
                confirmed=True,
                device=dev,
                customer=erp.customer_by_account(account_id),
                reason=("no serial in message; account owns exactly one device, "
                        f"resolved to {dev.serial_number}"),
            )
        if len(owned) > 1:
            return IdentityResult(
                confirmed=False,
                customer=erp.customer_by_account(account_id),
                reason=(f"no serial in message and account owns {len(owned)} "
                        "devices; cannot determine which one without asking"),
                candidates=owned,
            )

    return IdentityResult(
        confirmed=False,
        reason="no serial number in the message and no ERP account context",
    )
