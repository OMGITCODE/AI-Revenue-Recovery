"""Revenue Recovery Orchestrator.

Coordinates the full recovery workflow:
detect → diagnose → intervene → track.
"""

import structlog
from .detector import RevenueRiskDetector, RevenueRisk
from .diagnoser import RootCauseDiagnoser
from .interventions import BaseIntervention, InterventionResult

logger = structlog.get_logger()


class RecoveryOrchestrator:
    """Orchestrates the end-to-end revenue recovery pipeline."""

    def __init__(
        self,
        detector: RevenueRiskDetector,
        diagnoser: RootCauseDiagnoser,
        interventions: list[BaseIntervention],
    ):
        self.detector = detector
        self.diagnoser = diagnoser
        self.interventions = interventions
        self._results: list[InterventionResult] = []

    async def process_event(self, event: dict) -> InterventionResult | None:
        """Process a single event through the full recovery pipeline.

        Args:
            event: Raw event from payment/CRM system.

        Returns:
            InterventionResult if action was taken, None otherwise.
        """
        # Step 1: Detect risk
        risk = await self.detector.detect(event)
        if risk is None:
            logger.info("no_risk_detected", event=event)
            return None

        logger.info("risk_detected", risk_id=risk.id, risk_type=risk.risk_type.value)

        # Step 2: Diagnose root cause
        diagnosis = await self.diagnoser.diagnose(risk)
        logger.info(
            "diagnosis_complete",
            root_cause=diagnosis.root_cause.value,
            confidence=diagnosis.confidence,
        )

        # Step 3: Find and execute the right intervention
        for intervention in self.interventions:
            if intervention.can_handle(diagnosis):
                result = await intervention.execute(diagnosis)
                self._results.append(result)
                logger.info(
                    "intervention_executed",
                    type=result.intervention_type.value,
                    success=result.success,
                    amount_recovered=result.amount_recovered,
                )
                return result

        logger.warning("no_intervention_found", diagnosis=diagnosis)
        return None

    @property
    def total_recovered(self) -> float:
        """Total revenue recovered across all interventions."""
        return sum(r.amount_recovered for r in self._results if r.success)


if __name__ == "__main__":
    import asyncio

    async def main():
        detector = RevenueRiskDetector()
        diagnoser = RootCauseDiagnoser()
        orchestrator = RecoveryOrchestrator(detector, diagnoser, [])
        print("🔄 AI Revenue Recovery Agent initialized.")
        print("Ready to detect and recover revenue at risk.")

    asyncio.run(main())
