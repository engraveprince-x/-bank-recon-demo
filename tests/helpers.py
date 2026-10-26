from datetime import date
from decimal import Decimal

from recon.models import Transaction


def b(id, d, desc, amt, ref=""):
    return Transaction(id, "bank", date.fromisoformat(d), desc, Decimal(str(amt)).quantize(Decimal("0.01")), ref)


def l(id, d, desc, amt, ref=""):
    return Transaction(id, "ledger", date.fromisoformat(d), desc, Decimal(str(amt)).quantize(Decimal("0.01")), ref)
