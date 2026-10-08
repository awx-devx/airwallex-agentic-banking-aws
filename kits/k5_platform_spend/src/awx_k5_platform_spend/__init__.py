"""Kit 5 Platform Spend Controller domain core.

Exports SPECS (workflow), TOOLS (tools), READS (reads) and OPERATOR_COMMANDS (operator), the
names every kit package exports for the spine.
"""

from .allocator import Allocation, BridgeCandidate, allocate, eligibility
from .book import Obligation, PlatformBook, PlatformSetup, seed_platform_setup
from .config import CardProgram, PlatformConfig
from .ledger import CapitalLedger, LedgerError, ProviderMovement, ReserveBreach
from .operator import OPERATOR_COMMANDS
from .reads import READS
from .tenants import TenantMap, TenantRecord
from .tools import TOOLS
from .workflow import APPROVAL_MATRIX, SPECS, book_for, build_specs, configure_platform

__all__ = [
    "APPROVAL_MATRIX",
    "OPERATOR_COMMANDS",
    "READS",
    "SPECS",
    "TOOLS",
    "Allocation",
    "BridgeCandidate",
    "CapitalLedger",
    "CardProgram",
    "LedgerError",
    "Obligation",
    "PlatformBook",
    "PlatformConfig",
    "PlatformSetup",
    "ProviderMovement",
    "ReserveBreach",
    "TenantMap",
    "TenantRecord",
    "allocate",
    "book_for",
    "build_specs",
    "configure_platform",
    "eligibility",
    "seed_platform_setup",
]
