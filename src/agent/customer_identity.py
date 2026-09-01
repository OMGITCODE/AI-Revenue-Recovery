"""
customer_identity.py — Customer Identity Resolution & Unified Behavioral History Engine
========================================================================================
Resolves and links disparate transaction identifiers (Customer ID, UPI VPA,
Phone Number, Email Address) into a single canonical customer profile.

Ensures the agent treats all interactions, touches, retry histories, spend baselines,
promises, and suppression states of the same person as a coherent, unified behavioral
history rather than isolated events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def normalize_identifier(identifier: Optional[str]) -> str:
    """Normalizes an identifier string (strips whitespace, lowercases, formats phone)."""
    if not identifier:
        return ""
    val = str(identifier).strip()
    if not val:
        return ""

    # Already canonical
    if val.startswith("cust:"):
        return val.lower()

    # Phone normalization (+91 / 0 prefix handling)
    digits = re.sub(r"[^\d+]", "", val)
    if digits.startswith("+91") and len(digits) == 13:
        return digits
    elif digits.startswith("91") and len(digits) == 12:
        return f"+{digits}"
    elif digits.startswith("0") and len(digits) == 11 and digits[1:].isdigit():
        return f"+91{digits[1:]}"
    elif len(digits) == 10 and digits.isdigit():
        return f"+91{digits}"

    # VPA / Email / Customer ID -> lowercased
    return val.lower()


@dataclass
class CustomerProfile:
    """
    Unified 360-degree behavioral profile of an individual customer across all aliases.
    """
    canonical_id:      str                         # Unique internal identifier (e.g. cust:rahul@oksbi)
    primary_name:      str = ""                    # Display name if available
    customer_ids:      Set[str] = field(default_factory=set) # e.g. {"CUST-SBI-001", "CUST-SPIKE-007"}
    vpas:              Set[str] = field(default_factory=set) # e.g. {"rahul@oksbi"}
    phones:            Set[str] = field(default_factory=set) # e.g. {"+919800000001"}
    emails:            Set[str] = field(default_factory=set) # e.g. {"rahul.sharma@example.com"}
    aliases:           Set[str] = field(default_factory=set) # All resolved identifier strings
    daily_touches:     List[str] = field(default_factory=list) # Timestamps of outbound contacts today
    retry_timestamps:  List[datetime] = field(default_factory=list) # Timestamps of retry attempts
    created_at:        datetime = field(default_factory=lambda: datetime.now(IST))
    updated_at:        datetime = field(default_factory=lambda: datetime.now(IST))

    def to_dict(self) -> dict:
        return {
            "canonical_id":      self.canonical_id,
            "primary_name":      self.primary_name or self.canonical_id,
            "customer_ids":      sorted(list(self.customer_ids)),
            "vpas":              sorted(list(self.vpas)),
            "phones":            sorted(list(self.phones)),
            "emails":            sorted(list(self.emails)),
            "aliases":           sorted(list(self.aliases)),
            "daily_touches_today": len(self.daily_touches),
            "retries_30d":       len(self.retry_timestamps),
            "created_at":        self.created_at.strftime("%Y-%m-%d %H:%M:%S IST"),
            "updated_at":        self.updated_at.strftime("%Y-%m-%d %H:%M:%S IST"),
        }

    def record_touch(self) -> None:
        """Record an outbound touch timestamp."""
        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        self.daily_touches.append(now_str)
        self.updated_at = datetime.now(IST)

    def get_daily_touches_count(self) -> int:
        """Return number of touches made today (IST date)."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        return sum(1 for t in self.daily_touches if t.startswith(today_str))

    def record_retry(self) -> None:
        """Record a retry attempt timestamp."""
        now = datetime.now(IST)
        self.retry_timestamps.append(now)
        self.updated_at = now

    def get_retry_count_30d(self) -> int:
        """Return count of retries in the last 30 days."""
        now = datetime.now(IST)
        cutoff = now - timedelta(days=30)
        return sum(1 for t in self.retry_timestamps if t >= cutoff)


class CustomerIdentityRegistry:
    """
    Central identity graph resolving aliases to canonical customer profiles.
    Maintains bi-directional lookup tables and merges customer records as new aliases
    (VPA, customer_id, phone, email) are discovered.
    """

    def __init__(self):
        self._profiles: Dict[str, CustomerProfile] = {}         # canonical_id -> CustomerProfile
        self._alias_to_canonical: Dict[str, str] = {}           # normalized_alias -> canonical_id
        self._seed_archetypes()

    def _seed_archetypes(self):
        """Seed known simulator archetypes so multi-alias scenarios automatically link."""
        archetypes = [
            CustomerProfile(
                canonical_id="cust:rahul@oksbi",
                primary_name="Rahul Sharma",
                customer_ids={"CUST-SBI-001", "cust-sbi-001", "cust_rahul_01"},
                vpas={"rahul@oksbi", "rahul.sharma@okaxis"},
                phones={"+919800000001"},
                emails={"rahul.sharma@example.com"},
                aliases={"cust:rahul@oksbi", "CUST-SBI-001", "cust-sbi-001", "cust_rahul_01", "rahul@oksbi", "rahul.sharma@okaxis", "+919800000001", "rahul.sharma@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:aarav@oksbi",
                primary_name="Aarav Kapoor",
                customer_ids={"CUST-SPIKE-007", "cust-spike-007"},
                vpas={"aarav@oksbi"},
                phones={"+919800000021"},
                emails={"aarav.kapoor@example.com"},
                aliases={"cust:aarav@oksbi", "CUST-SPIKE-007", "cust-spike-007", "aarav@oksbi", "+919800000021", "aarav.kapoor@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:priya@okhdfcbank",
                primary_name="Priya Mehta",
                customer_ids={"CUST-HDFC-002", "cust-hdfc-002", "cust_priya_02"},
                vpas={"priya@okhdfcbank", "priya.m@paytm"},
                phones={"+919800000002"},
                emails={"priya.mehta@example.com"},
                aliases={"cust:priya@okhdfcbank", "CUST-HDFC-002", "cust-hdfc-002", "cust_priya_02", "priya@okhdfcbank", "priya.m@paytm", "+919800000002", "priya.mehta@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:arjun@okicici",
                primary_name="Arjun Nair",
                customer_ids={"CUST-ICICI-004", "cust-icici-003", "cust-normal-008", "cust_arjun_03"},
                vpas={"arjun@okicici", "arjun.nair@okicici"},
                phones={"+919800000003"},
                emails={"arjun.nair@example.com"},
                aliases={"cust:arjun@okicici", "CUST-ICICI-004", "cust-icici-003", "cust-normal-008", "cust_arjun_03", "arjun@okicici", "arjun.nair@okicici", "+919800000003", "arjun.nair@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:meera@okaxis",
                primary_name="Meera Iyer",
                customer_ids={"CUST-AXIS-005", "cust-axis-004", "cust_meera_04"},
                vpas={"meera@okaxis", "meera.iyer@okaxis"},
                phones={"+919800000004"},
                emails={"meera.iyer@example.com"},
                aliases={"cust:meera@okaxis", "CUST-AXIS-005", "cust-axis-004", "cust_meera_04", "meera@okaxis", "meera.iyer@okaxis", "+919800000004", "meera.iyer@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:vikram@ybl",
                primary_name="Vikram Patel",
                customer_ids={"CUST-YBL-003", "cust-ybl-005", "cust_vikram_05"},
                vpas={"vikram@ybl", "vikram.patel@okhdfcbank"},
                phones={"+919800000005"},
                emails={"vikram.patel@example.com"},
                aliases={"cust:vikram@ybl", "CUST-YBL-003", "cust-ybl-005", "cust_vikram_05", "vikram@ybl", "vikram.patel@okhdfcbank", "+919800000005", "vikram.patel@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:deepak@okkotak",
                primary_name="Deepak Joshi",
                customer_ids={"CUST-KOTAK-006", "cust-kotak-006"},
                vpas={"deepak@okkotak", "deepak.joshi@kotak"},
                phones={"+919800000006"},
                emails={"deepak.joshi@example.com"},
                aliases={"cust:deepak@okkotak", "CUST-KOTAK-006", "cust-kotak-006", "deepak@okkotak", "deepak.joshi@kotak", "+919800000006", "deepak.joshi@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:ananya@oksbi",
                primary_name="Ananya Sen",
                customer_ids={"CUST-SBI-007"},
                vpas={"ananya@oksbi", "ananya.sen@oksbi"},
                phones={"+919800000007"},
                emails={"ananya.sen@example.com"},
                aliases={"cust:ananya@oksbi", "CUST-SBI-007", "ananya@oksbi", "ananya.sen@oksbi", "+919800000007", "ananya.sen@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:rohit@okhdfcbank",
                primary_name="Rohit Verma",
                customer_ids={"CUST-HDFC-008"},
                vpas={"rohit@okhdfcbank", "rohit.verma@okhdfcbank"},
                phones={"+919800000008"},
                emails={"rohit.verma@example.com"},
                aliases={"cust:rohit@okhdfcbank", "CUST-HDFC-008", "rohit@okhdfcbank", "rohit.verma@okhdfcbank", "+919800000008", "rohit.verma@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:anita@paytm",
                primary_name="Anita Roy",
                customer_ids={"cust-ptm-006", "cust_anita_06"},
                vpas={"anita@paytm"},
                phones={"+919800000006"},
                emails={"anita.roy@example.com"},
                aliases={"cust:anita@paytm", "cust-ptm-006", "cust_anita_06", "anita@paytm", "+919800000006", "anita.roy@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:sunita@oksbi",
                primary_name="Sunita Rao",
                customer_ids={"cust-sunita-009"},
                vpas={"sunita@oksbi"},
                phones={"+919800000009"},
                emails={"sunita.rao@example.com"},
                aliases={"cust:sunita@oksbi", "cust-sunita-009", "sunita@oksbi", "+919800000009", "sunita.rao@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:kavita@okkotak",
                primary_name="Kavita Roy",
                customer_ids={"cust-kotak-010"},
                vpas={"kavita@okkotak"},
                phones={"+919800000010"},
                emails={"kavita.roy@example.com"},
                aliases={"cust:kavita@okkotak", "cust-kotak-010", "kavita@okkotak", "+919800000010", "kavita.roy@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:ramesh@okpnb",
                primary_name="Ramesh Gupta",
                customer_ids={"cust-pnb-011"},
                vpas={"ramesh@okpnb"},
                phones={"+919800000011"},
                emails={"ramesh.gupta@example.com"},
                aliases={"cust:ramesh@okpnb", "cust-pnb-011", "ramesh@okpnb", "+919800000011", "ramesh.gupta@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:sneha@okunion",
                primary_name="Sneha Joshi",
                customer_ids={"cust-union-012"},
                vpas={"sneha@okunion"},
                phones={"+919800000012"},
                emails={"sneha.joshi@example.com"},
                aliases={"cust:sneha@okunion", "cust-union-012", "sneha@okunion", "+919800000012", "sneha.joshi@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:deepak@paytm",
                primary_name="Deepak Verma",
                customer_ids={"cust-idfc-013"},
                vpas={"deepak@paytm"},
                phones={"+919800000013"},
                emails={"deepak.verma@example.com"},
                aliases={"cust:deepak@paytm", "cust-idfc-013", "deepak@paytm", "+919800000013", "deepak.verma@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:sunil@okcanara",
                primary_name="Sunil Nair",
                customer_ids={"cust-canara-014"},
                vpas={"sunil@okcanara"},
                phones={"+919800000014"},
                emails={"sunil.nair@example.com"},
                aliases={"cust:sunil@okcanara", "cust-canara-014", "sunil@okcanara", "+919800000014", "sunil.nair@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:pooja@oksbi",
                primary_name="Pooja Sharma",
                customer_ids={"cust-sbi-015"},
                vpas={"pooja@oksbi"},
                phones={"+919800000015"},
                emails={"pooja.sharma@example.com"},
                aliases={"cust:pooja@oksbi", "cust-sbi-015", "pooja@oksbi", "+919800000015", "pooja.sharma@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:rohan@okhdfcbank",
                primary_name="Rohan Enterprises",
                customer_ids={"cust-hdfc-016"},
                vpas={"rohan@okhdfcbank"},
                phones={"+919800000016"},
                emails={"rohan.ent@example.com"},
                aliases={"cust:rohan@okhdfcbank", "cust-hdfc-016", "rohan@okhdfcbank", "+919800000016", "rohan.ent@example.com"},
            ),
            CustomerProfile(
                canonical_id="cust:aditya@okicici",
                primary_name="Aditya Verma",
                customer_ids={"cust-icici-017"},
                vpas={"aditya@okicici"},
                phones={"+919800000017"},
                emails={"aditya.verma@example.com"},
                aliases={"cust:aditya@okicici", "cust-icici-017", "aditya@okicici", "+919800000017", "aditya.verma@example.com"},
            ),
        ]
        for p in archetypes:
            self._profiles[p.canonical_id] = p
            self._alias_to_canonical[p.canonical_id] = p.canonical_id
            for alias in p.aliases:
                self._alias_to_canonical[alias] = p.canonical_id

    def resolve_canonical_id(self, *identifiers: Optional[str]) -> str:
        """
        Takes one or more identifiers for a transaction/interaction (e.g. customer_vpa,
        customer_id, phone, email) and returns the single canonical customer ID.
        If multiple existing identities are matched, merges them into one unified identity.
        """
        valid_norms: List[str] = []
        for ident in identifiers:
            norm = normalize_identifier(ident)
            if norm and norm not in valid_norms:
                valid_norms.append(norm)

        if not valid_norms:
            if "cust:anonymous" not in self._profiles:
                anon = CustomerProfile(canonical_id="cust:anonymous", primary_name="Anonymous Customer", aliases={"cust:anonymous", "anonymous"})
                self._profiles["cust:anonymous"] = anon
                self._alias_to_canonical["cust:anonymous"] = "cust:anonymous"
                self._alias_to_canonical["anonymous"] = "cust:anonymous"
            return "cust:anonymous"

        # Check existing canonical mappings
        existing_cids: Set[str] = set()
        for n in valid_norms:
            if n.startswith("cust:") and n in self._profiles:
                existing_cids.add(n)
            elif n in self._alias_to_canonical:
                existing_cids.add(self._alias_to_canonical[n])

        if len(existing_cids) == 1:
            cid = next(iter(existing_cids))
            prof = self._profiles[cid]
            # Add any new aliases
            for n in valid_norms:
                self._classify_and_add_alias(prof, n)
                self._alias_to_canonical[n] = cid
            return cid

        if len(existing_cids) > 1:
            # Merge multiple profiles into the first one
            cids_list = list(existing_cids)
            primary_cid = cids_list[0]
            primary_prof = self._profiles[primary_cid]

            for secondary_cid in cids_list[1:]:
                sec_prof = self._profiles.pop(secondary_cid, None)
                if sec_prof:
                    primary_prof.aliases.update(sec_prof.aliases)
                    primary_prof.customer_ids.update(sec_prof.customer_ids)
                    primary_prof.vpas.update(sec_prof.vpas)
                    primary_prof.phones.update(sec_prof.phones)
                    primary_prof.emails.update(sec_prof.emails)
                    primary_prof.daily_touches.extend(sec_prof.daily_touches)
                    primary_prof.retry_timestamps.extend(sec_prof.retry_timestamps)
                    for alias in sec_prof.aliases:
                        self._alias_to_canonical[alias] = primary_cid

            for n in valid_norms:
                self._classify_and_add_alias(primary_prof, n)
                self._alias_to_canonical[n] = primary_cid
            return primary_cid

        # Brand new customer identity
        primary_ident = valid_norms[0]
        cid = primary_ident if primary_ident.startswith("cust:") else f"cust:{primary_ident}"
        prof = CustomerProfile(canonical_id=cid)
        self._profiles[cid] = prof
        self._alias_to_canonical[cid] = cid
        for n in valid_norms:
            self._classify_and_add_alias(prof, n)
            self._alias_to_canonical[n] = cid

        logger.info("New customer identity established: %s with aliases %s", cid, valid_norms)
        return cid

    def _classify_and_add_alias(self, prof: CustomerProfile, norm: str) -> None:
        prof.aliases.add(norm)
        if norm.startswith("cust:"):
            return
        if "@" in norm and not norm.endswith("@upi") and "." in norm.split("@")[-1] and not any(norm.endswith(x) for x in ("oksbi", "okhdfcbank", "okicici", "okaxis", "ybl", "paytm", "hdfc")):
            prof.emails.add(norm)
        elif "@" in norm:
            prof.vpas.add(norm)
        elif norm.startswith("+") or norm.isdigit():
            prof.phones.add(norm)
        else:
            prof.customer_ids.add(norm)
        prof.updated_at = datetime.now(IST)

    def link_identifiers(self, *identifiers: Optional[str]) -> CustomerProfile:
        """Explicitly links multiple identifiers together as the same person."""
        cid = self.resolve_canonical_id(*identifiers)
        return self._profiles[cid]

    def get_profile(self, identifier: str) -> Optional[CustomerProfile]:
        """Retrieve the customer profile by any known alias."""
        if not identifier:
            return None
        norm = normalize_identifier(identifier)
        if norm in self._profiles:
            return self._profiles[norm]
        cid = self._alias_to_canonical.get(norm)
        if cid and cid in self._profiles:
            return self._profiles[cid]
        return None

    def get_or_create_profile(self, *identifiers: Optional[str]) -> CustomerProfile:
        """Get or create customer profile resolving all provided identifiers."""
        clean_ids = [normalize_identifier(i) for i in identifiers if i]
        if not clean_ids:
            if "cust:anonymous" not in self._profiles:
                anon = CustomerProfile(canonical_id="cust:anonymous", primary_name="Anonymous Customer", aliases={"cust:anonymous", "anonymous"})
                self._profiles["cust:anonymous"] = anon
                self._alias_to_canonical["cust:anonymous"] = "cust:anonymous"
                self._alias_to_canonical["anonymous"] = "cust:anonymous"
            return self._profiles["cust:anonymous"]
        cid = self.resolve_canonical_id(*identifiers)
        return self._profiles[cid]

    def get_all_aliases(self, identifier: str) -> Set[str]:
        """Returns all known aliases for a person."""
        prof = self.get_profile(identifier)
        if prof:
            return set(prof.aliases)
        norm = normalize_identifier(identifier)
        return {norm} if norm else set()

    def is_same_person(self, id1: str, id2: str) -> bool:
        """Returns True if two identifiers map to the exact same customer profile."""
        if not id1 or not id2:
            return False
        n1 = normalize_identifier(id1)
        n2 = normalize_identifier(id2)
        if n1 == n2:
            return True
        c1 = self._alias_to_canonical.get(n1) or (n1 if n1 in self._profiles else None)
        c2 = self._alias_to_canonical.get(n2) or (n2 if n2 in self._profiles else None)
        if c1 and c2 and c1 == c2:
            return True
        if c1 and c1 == n2:
            return True
        if c2 and c2 == n1:
            return True
        return False

    def record_touch(self, *identifiers: Optional[str]) -> int:
        """Record an outbound touch for this customer and return today's total touches."""
        if not any(identifiers):
            return 0
        prof = self.get_or_create_profile(*identifiers)
        prof.record_touch()
        return prof.get_daily_touches_count()

    def get_daily_touches(self, *identifiers: Optional[str]) -> int:
        """Get number of touches sent to this customer today."""
        if not any(identifiers):
            return 0
        prof = self.get_or_create_profile(*identifiers)
        return prof.get_daily_touches_count()

    def record_retry(self, *identifiers: Optional[str]) -> int:
        """Record a retry for this customer and return retries in the last 30 days."""
        if not any(identifiers):
            return 0
        prof = self.get_or_create_profile(*identifiers)
        prof.record_retry()
        return prof.get_retry_count_30d()

    def get_retry_count(self, *identifiers: Optional[str]) -> int:
        """Get number of retries executed for this customer in the last 30 days."""
        if not any(identifiers):
            return 0
        prof = self.get_or_create_profile(*identifiers)
        return prof.get_retry_count_30d()

    def all_profiles(self) -> List[CustomerProfile]:
        return list(self._profiles.values())

    def reset(self) -> None:
        self._profiles.clear()
        self._alias_to_canonical.clear()
        self._seed_archetypes()


customer_identity_registry = CustomerIdentityRegistry()
