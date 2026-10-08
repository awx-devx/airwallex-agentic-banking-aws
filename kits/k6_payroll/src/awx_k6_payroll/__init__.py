"""Kit 6 Multi-Employer Payroll Executor: domain core.

Exports SPECS (workflow), TOOLS (tools) and READS (reads), the surface every kit package
exposes to the spine and the agent plane.
"""

from .reads import READS
from .tools import TOOLS
from .workflow import EMPLOYER_SPEC, PAYROLL_SPEC, SPECS

__all__ = ["EMPLOYER_SPEC", "PAYROLL_SPEC", "READS", "SPECS", "TOOLS"]
