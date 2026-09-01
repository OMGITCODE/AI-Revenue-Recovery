"""
Spend Pattern & Historical Anomaly Analyzer.

Retrieves and tracks the historical transaction spend pattern for a given UPI address (VPA),
customer ID, or phone number by resolving to a unified customer identity. Computes baseline
statistical distributions (mean, median, min-max range, std dev), and determines whether an
incoming transaction is a sudden upward spike requiring critical handling.

Examples:
  - Typical spend range ₹10,000–₹50,000, current = ₹60,000:
      Spike ratio: ~1.2x (within expected margin) -> NOT critical.
  - Typical spend ~₹100, current = ₹70,000:
      Spike ratio: 700x (massive upward anomaly) -> CRITICAL.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from ..models.risk_models import RiskSeverity
from ..utils.logger import get_logger
from .customer_identity import customer_identity_registry, normalize_identifier

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class SpendProfile:
    """Statistical summary of a customer's historical spend pattern."""
    vpa:             str
    transaction_count: int
    min_amount:      float
    max_amount:      float
    mean_amount:     float
    median_amount:   float
    std_dev:         float
    typical_range:   Tuple[float, float]
    history:         List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "vpa":               self.vpa,
            "transaction_count": self.transaction_count,
            "min_amount":        round(self.min_amount, 2),
            "max_amount":        round(self.max_amount, 2),
            "mean_amount":       round(self.mean_amount, 2),
            "median_amount":     round(self.median_amount, 2),
            "std_dev":           round(self.std_dev, 2),
            "typical_range":     [round(self.typical_range[0], 2), round(self.typical_range[1], 2)],
            "history":           [round(x, 2) for x in self.history],
        }


@dataclass
class PatternAnalysisResult:
    """Outcome of analyzing a transaction against historical spend pattern."""
    vpa:               str
    current_amount:    float
    is_critical:       bool
    is_spike:          bool
    severity:          RiskSeverity
    spike_ratio:       float
    baseline_mean:     float
    baseline_median:   float
    typical_range:     Tuple[float, float]
    z_score:           float
    confidence:        float
    explanation:       str
    recommendation:    str
    profile:           Optional[SpendProfile] = None

    def to_dict(self) -> dict:
        return {
            "vpa":             self.vpa,
            "current_amount":  round(self.current_amount, 2),
            "is_critical":     self.is_critical,
            "is_spike":        self.is_spike,
            "severity":        self.severity.value,
            "spike_ratio":     round(self.spike_ratio, 2),
            "baseline_mean":   round(self.baseline_mean, 2),
            "baseline_median": round(self.baseline_median, 2),
            "typical_range":   [round(self.typical_range[0], 2), round(self.typical_range[1], 2)],
            "z_score":         round(self.z_score, 2),
            "confidence":      round(self.confidence, 2),
            "explanation":     self.explanation,
            "recommendation":  self.recommendation,
            "profile":         self.profile.to_dict() if self.profile else None,
        }


# ── Seed / Default Historical Profiles for Archetypes ─────────────────────────
_DEFAULT_SPEND_HISTORIES: Dict[str, List[float]] = {
    # Archetype 1: Micro-ticket OTT & Daily User (Rahul Sharma, ~₹999 normal)
    "cust:rahul@oksbi": [999.0, 999.0, 899.0, 1099.0, 999.0, 949.0],
    "rahul@oksbi": [999.0, 999.0, 899.0, 1099.0, 999.0, 949.0],
    "rahul.sharma@oksbi": [999.0, 999.0, 899.0, 1099.0, 999.0, 949.0],
    "cust-sbi-001": [999.0, 999.0, 899.0, 1099.0, 999.0, 949.0],
    "cust-a1": [999.0, 999.0, 899.0, 1099.0, 999.0, 949.0],

    # Spike Test Archetype: Low Base Aarav Kapoor (~₹100 normal, tests 700x spike to ₹70k)
    "cust:aarav@oksbi": [99.0, 149.0, 110.0, 100.0, 89.0, 129.0, 99.0, 105.0],
    "aarav@oksbi": [99.0, 149.0, 110.0, 100.0, 89.0, 129.0, 99.0, 105.0],
    "cust-spike-007": [99.0, 149.0, 110.0, 100.0, 89.0, 129.0, 99.0, 105.0],

    # Archetype 2: Cloud Infrastructure & Server Payer (Arjun Nair, ~₹4,500 normal)
    "cust:arjun@okicici": [4500.0, 4500.0, 4200.0, 4800.0, 4500.0, 4600.0],
    "arjun@okicici": [4500.0, 4500.0, 4200.0, 4800.0, 4500.0, 4600.0],
    "arjun.nair@okicici": [4500.0, 4500.0, 4200.0, 4800.0, 4500.0, 4600.0],
    "cust-icici-004": [4500.0, 4500.0, 4200.0, 4800.0, 4500.0, 4600.0],
    "cust-icici-003": [4500.0, 4500.0, 4200.0, 4800.0, 4500.0, 4600.0],
    "cust-normal-008": [4500.0, 4500.0, 4200.0, 4800.0, 4500.0, 4600.0],

    # Archetype 3: SaaS Pro Subscriber (Priya Mehta, ~₹1,499 normal)
    "cust:priya@okhdfcbank": [1499.0, 1499.0, 1299.0, 1599.0, 1499.0, 1450.0],
    "priya@okhdfcbank": [1499.0, 1499.0, 1299.0, 1599.0, 1499.0, 1450.0],
    "priya.mehta@okhdfcbank": [1499.0, 1499.0, 1299.0, 1599.0, 1499.0, 1450.0],
    "priya@hdfc": [1499.0, 1499.0, 1299.0, 1599.0, 1499.0, 1450.0],
    "cust-hdfc-002": [1499.0, 1499.0, 1299.0, 1599.0, 1499.0, 1450.0],
    "cust-c3": [1499.0, 1499.0, 1299.0, 1599.0, 1499.0, 1450.0],

    # Archetype 4: EdTech Upskilling Learner (Meera Iyer, ~₹1,250 normal)
    "cust:meera@okaxis": [1250.0, 1250.0, 1100.0, 1400.0, 1250.0, 1300.0],
    "meera@okaxis": [1250.0, 1250.0, 1100.0, 1400.0, 1250.0, 1300.0],
    "meera.iyer@okaxis": [1250.0, 1250.0, 1100.0, 1400.0, 1250.0, 1300.0],
    "cust-axis-005": [1250.0, 1250.0, 1100.0, 1400.0, 1250.0, 1300.0],
    "cust-axis-004": [1250.0, 1250.0, 1100.0, 1400.0, 1250.0, 1300.0],
    "cust-b2": [1250.0, 1250.0, 1100.0, 1400.0, 1250.0, 1300.0],

    # Archetype 5: Fitness Gold Member (Vikram Patel, ~₹2,999 normal)
    "cust:vikram@ybl": [2999.0, 2999.0, 2800.0, 3100.0, 2999.0, 3050.0],
    "vikram@ybl": [2999.0, 2999.0, 2800.0, 3100.0, 2999.0, 3050.0],
    "vikram.patel@ybl": [2999.0, 2999.0, 2800.0, 3100.0, 2999.0, 3050.0],
    "cust-ybl-003": [2999.0, 2999.0, 2800.0, 3100.0, 2999.0, 3050.0],
    "cust-ybl-005": [2999.0, 2999.0, 2800.0, 3100.0, 2999.0, 3050.0],
    "cust-d4": [2999.0, 2999.0, 2800.0, 3100.0, 2999.0, 3050.0],

    # Archetype 6: Music & Podcast Subscriber (Deepak Joshi, ~₹899 normal)
    "cust:deepak@okkotak": [899.0, 899.0, 799.0, 999.0, 899.0, 850.0],
    "deepak@okkotak": [899.0, 899.0, 799.0, 999.0, 899.0, 850.0],
    "deepak.joshi@kotak": [899.0, 899.0, 799.0, 999.0, 899.0, 850.0],
    "cust-kotak-006": [899.0, 899.0, 799.0, 999.0, 899.0, 850.0],

    # Archetype 7: Insurance & Health Rider Payer (Ananya Sen, ~₹3,499 normal)
    "cust:ananya@oksbi": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0, 3500.0],
    "ananya@oksbi": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0, 3500.0],
    "ananya.sen@oksbi": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0, 3500.0],
    "cust-sbi-007": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0, 3500.0],

    # Archetype 8: B2B Developer API Tier (Rohit Verma, ~₹1,999 normal)
    "cust:rohit@okhdfcbank": [1999.0, 1999.0, 1800.0, 2200.0, 1999.0, 2100.0],
    "rohit@okhdfcbank": [1999.0, 1999.0, 1800.0, 2200.0, 1999.0, 2100.0],
    "rohit.verma@okhdfcbank": [1999.0, 1999.0, 1800.0, 2200.0, 1999.0, 2100.0],
    "cust-hdfc-008": [1999.0, 1999.0, 1800.0, 2200.0, 1999.0, 2100.0],

    # Archetype 9: Micro-ticket Fitness (Anita Roy, ~₹299 normal)
    "cust:anita@paytm": [299.0, 299.0, 349.0, 299.0, 499.0],
    "anita@paytm": [299.0, 299.0, 349.0, 299.0, 499.0],
    "anita.roy@paytm": [299.0, 299.0, 349.0, 299.0, 499.0],
    "cust-ptm-006": [299.0, 299.0, 349.0, 299.0, 499.0],
    "cust-e5": [299.0, 299.0, 349.0, 299.0, 499.0],

    # Archetype 10: Education & Coaching Mandates (Kavita Kotak, ~₹3,499 normal)
    "cust:kavita@okkotak": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0],
    "kavita@okkotak": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0],
    "cust-kotak-010": [3499.0, 3499.0, 3200.0, 3600.0, 3499.0],

    # Archetype 11: High-Velocity B2B & Pre-Debit Rules (Rohan Gupta, ~₹18,500)
    "cust:rohan@okhdfcbank": [18500.0, 18500.0, 18500.0, 18500.0],
    "rohan@okhdfcbank": [18500.0, 18500.0, 18500.0, 18500.0],
    "cust-hdfc-016": [18500.0, 18500.0, 18500.0, 18500.0],
}


class SpendPatternTracker:
    """
    Tracks and retrieves transaction spend history per customer, resolving across
    all known aliases (VPA, customer ID, phone), and analyzes whether an incoming
    amount constitutes a sudden upward spike / critical anomaly.
    """

    def __init__(self):
        self._history: Dict[str, List[float]] = {
            k.lower(): list(amounts) for k, amounts in _DEFAULT_SPEND_HISTORIES.items()
        }

    def _resolve_key(self, *identifiers: Optional[str]) -> str:
        """Resolves one or more identifiers to the canonical customer key."""
        valid = [i for i in identifiers if i]
        if not valid:
            return "cust:anonymous"
        return customer_identity_registry.resolve_canonical_id(*valid)

    def record_transaction(
        self,
        vpa: str,
        amount: float,
        skip_outliers: bool = True,
        customer_id: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> None:
        """Add a transaction amount to the customer's canonical historical profile."""
        if (not vpa and not customer_id) or amount <= 0:
            return
        
        cid = self._resolve_key(vpa, customer_id, phone)
        norm_vpa = normalize_identifier(vpa) if vpa else ""

        if cid not in self._history:
            # Check if an alias was previously registered
            if norm_vpa and norm_vpa in self._history:
                self._history[cid] = list(self._history[norm_vpa])
            else:
                self._history[cid] = []

        current_hist = self._history[cid]

        # Outlier filtering: exclude extreme anomalies & sudden upward spikes from mutating standard baseline
        if skip_outliers and len(current_hist) >= 2:
            base_mean = statistics.mean(current_hist)
            base_max = max(current_hist)
            if base_mean > 0:
                ratio = amount / base_mean
                if (
                    (ratio >= 5.0 and (amount - base_max) >= 3000.0)
                    or (ratio >= 10.0)
                    or (amount >= 50000.0 and base_mean <= 1000.0)
                ):
                    logger.info("Outlier transaction ₹%.2f excluded from normal baseline profile for %s (%s)", amount, vpa, cid)
                    return

        current_hist.append(float(amount))
        # Keep last 50 transactions max
        if len(current_hist) > 50:
            self._history[cid] = current_hist[-50:]

        # Mirror canonical history to all aliases for fast direct lookups
        if norm_vpa:
            self._history[norm_vpa] = self._history[cid]
        if customer_id:
            self._history[normalize_identifier(customer_id)] = self._history[cid]

    def reset_history(self, vpa: Optional[str] = None) -> None:
        """Reset historical spend profiles to default seeds."""
        if vpa:
            cid = self._resolve_key(vpa)
            norm = normalize_identifier(vpa)
            if cid in _DEFAULT_SPEND_HISTORIES:
                self._history[cid] = list(_DEFAULT_SPEND_HISTORIES[cid])
            elif norm in _DEFAULT_SPEND_HISTORIES:
                self._history[cid] = list(_DEFAULT_SPEND_HISTORIES[norm])
            elif cid in self._history:
                del self._history[cid]
            if norm in self._history:
                del self._history[norm]
        else:
            self._history = {
                k.lower(): list(amounts) for k, amounts in _DEFAULT_SPEND_HISTORIES.items()
            }

    def get_history(self, vpa: str, customer_id: Optional[str] = None) -> List[float]:
        """Retrieve historical transaction amounts for a customer by VPA or customer ID."""
        if not vpa and not customer_id:
            return []
        
        cid = self._resolve_key(vpa, customer_id)
        if cid in self._history:
            return list(self._history[cid])
        
        norm_vpa = normalize_identifier(vpa) if vpa else ""
        if norm_vpa and norm_vpa in self._history:
            return list(self._history[norm_vpa])

        norm_cid = normalize_identifier(customer_id) if customer_id else ""
        if norm_cid and norm_cid in self._history:
            return list(self._history[norm_cid])

        return []

    def set_history(self, vpa: str, history: List[float], customer_id: Optional[str] = None) -> None:
        """Explicitly set history for testing or profile initialization across customer aliases."""
        cid = self._resolve_key(vpa, customer_id)
        cleaned = [float(x) for x in history if float(x) > 0]
        self._history[cid] = cleaned
        if vpa:
            self._history[normalize_identifier(vpa)] = cleaned
        if customer_id:
            self._history[normalize_identifier(customer_id)] = cleaned

    def get_profile(
        self,
        vpa: str,
        custom_history: Optional[List[float]] = None,
        customer_id: Optional[str] = None,
    ) -> SpendProfile:
        """Compute statistical spend profile for a customer or custom history."""
        hist = custom_history if custom_history is not None else self.get_history(vpa, customer_id)
        display_vpa = vpa or customer_id or "anonymous"
        if not hist:
            return SpendProfile(
                vpa=display_vpa,
                transaction_count=0,
                min_amount=0.0,
                max_amount=0.0,
                mean_amount=0.0,
                median_amount=0.0,
                std_dev=0.0,
                typical_range=(0.0, 0.0),
                history=[],
            )

        n = len(hist)
        min_val = min(hist)
        max_val = max(hist)
        mean_val = statistics.mean(hist)
        med_val = statistics.median(hist)
        std_val = statistics.stdev(hist) if n > 1 else 0.0
        typical_range = (min_val, max_val)

        return SpendProfile(
            vpa=display_vpa,
            transaction_count=n,
            min_amount=min_val,
            max_amount=max_val,
            mean_amount=mean_val,
            median_amount=med_val,
            std_dev=std_val,
            typical_range=typical_range,
            history=hist,
        )

    def analyze(
        self,
        vpa: str,
        current_amount: float,
        custom_history: Optional[List[float]] = None,
        customer_id: Optional[str] = None,
    ) -> PatternAnalysisResult:
        """
        Evaluates whether current_amount represents a sudden upward spike / critical anomaly
        relative to the customer's unified historical spending pattern.
        """
        profile = self.get_profile(vpa, custom_history, customer_id=customer_id)
        amt = float(current_amount)
        display_vpa = vpa or customer_id or "customer"

        # Case A: No history available (0 prior transactions)
        if profile.transaction_count == 0:
            severity = (
                RiskSeverity.LOW if amt < 1000.0 else
                (RiskSeverity.MEDIUM if amt <= 50000.0 else RiskSeverity.HIGH)
            )
            return PatternAnalysisResult(
                vpa=display_vpa,
                current_amount=amt,
                is_critical=False,
                is_spike=False,
                severity=severity,
                spike_ratio=1.0,
                baseline_mean=amt,
                baseline_median=amt,
                typical_range=(amt, amt),
                z_score=0.0,
                confidence=0.50,
                explanation=f"First transaction for {display_vpa}. Initial baseline seeded at ₹{amt:,.2f}.",
                recommendation="Proceed with standard failure workflow and record to profile.",
                profile=profile,
            )

        # Case B: Single prior transaction (baseline known, establishing variance)
        if profile.transaction_count == 1:
            baseline = max(profile.mean_amount, 1.0)
            spike_ratio = amt / baseline
            upper_ratio = (amt / profile.max_amount) if profile.max_amount > 0 else 1.0

            # Extreme spike check even with 1 prior transaction
            if (spike_ratio >= 10.0 and amt >= 15000.0) or (profile.max_amount <= 500.0 and amt >= 15000.0):
                is_critical = True
                is_spike = True
                severity = RiskSeverity.CRITICAL
                explanation = (
                    f"🚨 CRITICAL SPEND SPIKE: Transaction ₹{amt:,.2f} is a {spike_ratio:.1f}x sudden upward spike "
                    f"above customer initial baseline of ₹{profile.mean_amount:,.2f}."
                )
                recommendation = "BLOCK blind automatic retry. Elevate to explicit customer confirmation."
            elif spike_ratio >= 4.0 and upper_ratio >= 2.5 and (amt - profile.max_amount) >= 500.0:
                is_critical = False
                is_spike = True
                severity = RiskSeverity.HIGH
                explanation = (
                    f"⚠️ ELEVATED SPEND: Transaction ₹{amt:,.2f} is {spike_ratio:.1f}x higher than previous "
                    f"transaction of ₹{profile.mean_amount:,.2f}."
                )
                recommendation = "Customer confirmation nudge recommended prior to final retry."
            else:
                is_critical = False
                is_spike = False
                severity = self._normal_severity_for_baseline(profile.mean_amount)
                explanation = (
                    f"✓ NORMAL PATTERN: Transaction ₹{amt:,.2f} aligns with prior transaction of "
                    f"₹{profile.mean_amount:,.2f} ({spike_ratio:.2f}x)."
                )
                recommendation = "Execute standard automated recovery playbook."

            return PatternAnalysisResult(
                vpa=display_vpa,
                current_amount=amt,
                is_critical=is_critical,
                is_spike=is_spike,
                severity=severity,
                spike_ratio=spike_ratio,
                baseline_mean=profile.mean_amount,
                baseline_median=profile.median_amount,
                typical_range=profile.typical_range,
                z_score=0.0,
                confidence=0.65,
                explanation=explanation,
                recommendation=recommendation,
                profile=profile,
            )

        # Case C: 2+ transactions available (Full Statistical Profile)
        baseline = max(profile.median_amount, profile.mean_amount, 1.0)
        spike_ratio = amt / baseline
        upper_ratio = (amt / profile.max_amount) if profile.max_amount > 0 else 1.0

        z_score = 0.0
        if profile.std_dev > 0:
            z_score = (amt - profile.mean_amount) / profile.std_dev

        is_critical = False
        is_spike = False

        # Critical Spike Conditions
        if (
            (spike_ratio >= 8.0 and upper_ratio >= 2.0 and (amt - profile.max_amount) >= 5000.0)
            or (spike_ratio >= 5.0 and upper_ratio >= 2.5 and (amt - profile.max_amount) >= 15000.0)
            or (spike_ratio >= 15.0 and amt >= 5000.0)
            or (z_score >= 4.0 and profile.transaction_count >= 4 and upper_ratio >= 2.0 and amt >= 5000.0)
            or (profile.max_amount <= 500.0 and amt >= 10_000.0)
            or (spike_ratio >= 4.0 and amt >= 100_000.0 and upper_ratio >= 2.0)
        ):
            is_critical = True
            is_spike = True
            severity = RiskSeverity.CRITICAL
            explanation = (
                f"🚨 CRITICAL SPEND SPIKE: Transaction ₹{amt:,.2f} is a {spike_ratio:.1f}x sudden upward spike "
                f"above customer baseline (mean: ₹{profile.mean_amount:,.2f}, normal range: ₹{profile.min_amount:,.2f}–₹{profile.max_amount:,.2f}). "
                f"Exceeds historical maximum by {upper_ratio:.1f}x."
            )
            recommendation = (
                "BLOCK blind automatic retry. Elevate to explicit customer consent channel (WhatsApp interactive / 2FA) "
                "or fraud/anomaly verification to protect payer from unexpected account depletion."
            )
            confidence = min(0.98, 0.85 + min(0.13, (spike_ratio / 100.0)))

        elif (
            (spike_ratio >= 4.0 and upper_ratio >= 2.0 and (amt - profile.max_amount) >= 500.0)
            or (z_score >= 2.5 and profile.transaction_count >= 3 and amt >= 1000.0)
        ):
            is_critical = False
            is_spike = True
            severity = RiskSeverity.HIGH
            explanation = (
                f"⚠️ ELEVATED SPEND: Transaction ₹{amt:,.2f} is {spike_ratio:.1f}x higher than baseline "
                f"(mean: ₹{profile.mean_amount:,.2f}, normal range: ₹{profile.min_amount:,.2f}–₹{profile.max_amount:,.2f}). "
                f"Moderate deviation from typical pattern."
            )
            recommendation = "Customer confirmation nudge recommended prior to final retry attempt."
            confidence = 0.85

        elif (
            (spike_ratio >= 1.75 and upper_ratio >= 1.50 and (amt - profile.max_amount) >= 100.0)
            or (z_score >= 1.75 and upper_ratio >= 1.50 and profile.transaction_count >= 3 and amt >= 500.0)
        ):
            is_critical = False
            is_spike = True
            severity = RiskSeverity.MEDIUM
            explanation = (
                f"MEDIUM SPEND DRIFT: Transaction ₹{amt:,.2f} is above the usual pattern "
                f"(mean: ₹{profile.mean_amount:,.2f}, normal range: ₹{profile.min_amount:,.2f}–₹{profile.max_amount:,.2f}). "
                f"It is unusual, but not large enough to block automated recovery."
            )
            recommendation = "Proceed with recovery, preferably with a customer-visible confirmation path."
            confidence = 0.78

        else:
            is_critical = False
            is_spike = False
            severity = self._normal_severity_for_baseline(profile.mean_amount)

            explanation = (
                f"✓ NORMAL PATTERN: Transaction ₹{amt:,.2f} aligns with historical spend baseline "
                f"(range: ₹{profile.min_amount:,.2f}–₹{profile.max_amount:,.2f}, mean: ₹{profile.mean_amount:,.2f}, ratio: {upper_ratio:.2f}x max). "
                f"No sudden spike detected."
            )
            recommendation = "Execute standard automated recovery playbook."
            confidence = 0.92

        logger.info(
            "SpendPatternAnalysis → VPA=%s | Amount=₹%.2f | Baseline=₹%.2f | Ratio=%.2fx | Spike=%s | Critical=%s | Severity=%s",
            display_vpa, amt, baseline, spike_ratio, is_spike, is_critical, severity.value,
        )

        return PatternAnalysisResult(
            vpa=display_vpa,
            current_amount=amt,
            is_critical=is_critical,
            is_spike=is_spike,
            severity=severity,
            spike_ratio=spike_ratio,
            baseline_mean=profile.mean_amount,
            baseline_median=profile.median_amount,
            typical_range=profile.typical_range,
            z_score=z_score,
            confidence=confidence,
            explanation=explanation,
            recommendation=recommendation,
            profile=profile,
        )

    @staticmethod
    def _normal_severity_for_baseline(baseline_amount: float) -> RiskSeverity:
        """Base severity for non-anomalous transactions, based on the user's usual spend band."""
        if baseline_amount < 1000.0:
            return RiskSeverity.LOW
        if baseline_amount <= 50_000.0:
            return RiskSeverity.MEDIUM
        return RiskSeverity.HIGH


# Global singleton
spend_pattern_tracker = SpendPatternTracker()
