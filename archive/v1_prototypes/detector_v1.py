"""V1 Prototype: Revenue Risk Detector (Archived).

Preserved for reference. Production detection is powered by src/agent/upi_detector.py.
"""

from src.models.risk_models import RiskType, RiskSeverity, RevenueRisk


class RevenueRiskDetector:
    """Detects revenue at risk from payment events and signals."""

    def __init__(self):
        self._handlers: dict[RiskType, list] = {}

    def register_handler(self, risk_type: RiskType, handler):
        """Register a handler for a specific risk type."""
        if risk_type not in self._handlers:
            self._handlers[risk_type] = []
        self._handlers[risk_type].append(handler)

    async def detect(self, event: dict) -> RevenueRisk | None:
        """V1 stub."""
        raise NotImplementedError("Archived v1 prototype. See src/agent/upi_detector.py for production detector.")

    async def process_risk(self, risk: RevenueRisk):
        """Process a detected risk through registered handlers."""
        handlers = self._handlers.get(risk.risk_type, [])
        for handler in handlers:
            await handler(risk)
