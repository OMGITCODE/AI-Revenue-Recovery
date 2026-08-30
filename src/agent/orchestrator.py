"""
Revenue Recovery Orchestrator  [REFERENCE / V1 PROTOTYPE]
==========================================================
⚠️  THIS FILE IS AN EARLIER ITERATION.

The live, production orchestration layer is api/main.py
────────────────────────────────────────────────────────
api/main.py is the real orchestrator. It wires together:
  • UPIAutopayDetector          (src/agent/upi_detector.py)
  • DecisionEngine + Guardrails (src/agent/decision_engine.py)
  • Thompson Sampling Bandit    (src/agent/bandit.py)
  • Promise-to-Pay Tracker      (src/agent/promise_tracker.py)
  • Recovery Audit Ledger       (src/agent/recovery_ledger.py)
  • Setu Account Aggregator     (src/integrations/setu_aa.py)
  • B2B Receivables Chaser      (src/agent/b2b_chaser.py)
  • Checkout Drop-off Recovery  (src/agent/checkout_recovery.py)

It exposes 25+ REST endpoints (see /docs) and a live SSE stream.

This file (orchestrator.py) is the generic v1 prototype built
before the UPI-specific pipeline was completed. It is kept here
for reference and to show the architectural evolution from the
generic detect → diagnose → intervene loop to the full
NPCI-aware, guardrail-enforced, Bayesian MAB pipeline in
api/main.py.

To run the project:
    uvicorn api.main:app --host 127.0.0.1 --port 8000
    # then open http://localhost:8000
"""

from .detector import RevenueRiskDetector, RevenueRisk
from .diagnoser import RootCauseDiagnoser
from .interventions import BaseIntervention, InterventionResult
from ..utils.logger import get_logger

logger = get_logger(__name__)


class RecoveryOrchestrator:
    """
    Generic v1 orchestrator — detect → diagnose → intervene loop.

    NOTE: For the full production pipeline (UPI Autopay, NPCI codes,
    RBI guardrails, Thompson Sampling, audit ledger), see api/main.py.
    """

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
        print("⚠️  orchestrator.py is the v1 prototype.")
        print("   The live pipeline is: uvicorn api.main:app --port 8000")
        print("   Dashboard: http://localhost:8000")

    asyncio.run(main())

