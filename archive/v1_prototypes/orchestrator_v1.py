"""V1 Prototype: Generic Orchestrator (Archived).

Preserved for reference. Production orchestration is in api/main.py and api/simulator.py.
"""

from src.models.risk_models import RevenueRisk
from src.agent.diagnoser import RootCauseDiagnoser
from .interventions_v1 import BaseIntervention, InterventionResult
from src.utils.logger import get_logger

logger = get_logger(__name__)


class RecoveryOrchestrator:
    """Generic v1 orchestrator — reference prototype."""

    def __init__(
        self,
        detector,
        diagnoser: RootCauseDiagnoser,
        interventions: list[BaseIntervention],
    ):
        self.detector = detector
        self.diagnoser = diagnoser
        self.interventions = interventions
        self._results: list[InterventionResult] = []

    async def process_event(self, event: dict) -> InterventionResult | None:
        risk = await self.detector.detect(event)
        if risk is None:
            return None

        diagnosis = await self.diagnoser.diagnose(risk)
        for intervention in self.interventions:
            if intervention.can_handle(diagnosis):
                result = await intervention.execute(diagnosis)
                self._results.append(result)
                return result
        return None

    @property
    def total_recovered(self) -> float:
        return sum(r.amount_recovered for r in self._results if r.success)
