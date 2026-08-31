"""
messaging.py — Outbound WhatsApp / SMS delivery (Twilio-backed)
================================================================
Drop this in as: src/integrations/messaging.py

Every intervention that wants to reach a customer's phone routes through
this ONE client instead of calling a provider SDK directly.

Design goal: the demo must work for ANYONE who clones the repo, with zero
setup — and upgrade to real delivery the moment Twilio credentials exist.

• No TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env -> "mock" mode.
  Nothing goes out over the wire. The exact message that WOULD have been
  sent is logged and returned, so demo.py / upi_demo.py / the dashboard
  all keep working out of the box for a judge who just clones and runs.

• Credentials present -> "live" mode. Real WhatsApp/SMS via Twilio.
  A provider failure (bad number, expired sandbox join, rate limit) is
  caught and logged, never raised — a flaky messaging API should never
  take down the recovery pipeline. That's itself a guardrail: the
  agent's decision-making must survive a channel outage.

WhatsApp uses the Twilio Sandbox by default (TWILIO_WHATSAPP_FROM =
whatsapp:+14155238886) — no Meta Business verification needed for a demo.
Swap in Interakt/Wati/a verified Meta number later by changing that one
env var; nothing else in the codebase needs to change.

SMS to Indian numbers additionally requires a TRAI DLT-registered sender
or the carrier silently drops it (see CHANNEL_COSTS note in
recovery_ledger.py, which already prices "sms" as DLT-registered). Until
you've registered, treat SMS as mock-only and demo live delivery on
WhatsApp instead.

DEMO_RECIPIENT_WHATSAPP / DEMO_RECIPIENT_SMS: if set, every LIVE send is
redirected there regardless of which (fake) demo customer triggered it.
Set this to your own WhatsApp-joined number so anyone running the repo
actually sees a message land on a real phone, instead of it silently
failing against a synthetic number from the sample dataset. Unset once
you're wired to real Razorpay webhooks carrying real customer phones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.config import settings
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Twilio's public WhatsApp Sandbox number — same for every developer;
# only the join code (shown in your Twilio console) differs per account.
DEFAULT_SANDBOX_WHATSAPP = "whatsapp:+14155238886"


def verify_twilio_signature(
    url: str,
    post_data: dict,
    twilio_signature: str,
    auth_token: str,
) -> bool:
    """
    Verify that an inbound webhook payload came from Twilio.

    Twilio signs requests using HMAC-SHA1 over the URL concatenated with
    alphabetically sorted POST parameters.
    Reference: https://www.twilio.com/docs/usage/security#validating-requests
    """
    if not auth_token or not twilio_signature:
        return False
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(auth_token)
        return validator.validate(url, post_data, twilio_signature)
    except Exception:
        import hmac
        import hashlib
        import base64
        s = url
        for k in sorted(post_data.keys()):
            s += f"{k}{post_data[k]}"
        expected = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, twilio_signature)


@dataclass
class MessageResult:
    channel: str                       # "whatsapp" | "sms"
    to: str
    body: str
    sent: bool                         # True only if Twilio actually accepted it
    mode: str                          # "mock" | "live"
    provider_sid: Optional[str] = None
    error: Optional[str] = None


class MessagingClient:
    """
    One client, two channels, safe by default.

    Usage:
        from src.integrations.messaging import messenger
        result = messenger.send_whatsapp(to="+919876543210", body="...")
    """

    def __init__(self, force_mock: bool = False):
        self.force_mock = force_mock
        self.account_sid = settings.twilio_account_sid.strip()
        self.auth_token = settings.twilio_auth_token.strip()
        self.whatsapp_from = settings.twilio_whatsapp_from.strip() or DEFAULT_SANDBOX_WHATSAPP
        self.sms_from = settings.twilio_sms_from.strip()
        self.demo_whatsapp_override = settings.demo_recipient_whatsapp.strip()
        self.demo_sms_override = settings.demo_recipient_sms.strip()

        self._client = None
        if not self.force_mock and self.account_sid and self.auth_token:
            try:
                from twilio.rest import Client  # lazy import: mock mode never needs the package
                self._client = Client(self.account_sid, self.auth_token)
                logger.info("MessagingClient started in LIVE mode (Twilio)")
            except Exception as e:
                logger.warning("Twilio credentials present but client init failed (%s) — using mock mode", e)
        else:
            logger.info("MessagingClient started in MOCK mode (no TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN or forced)")

    @property
    def is_live(self) -> bool:
        return self._client is not None and not self.force_mock

    def send_whatsapp(self, to: str, body: str) -> MessageResult:
        return self._send("whatsapp", to, body)

    def send_sms(self, to: str, body: str) -> MessageResult:
        return self._send("sms", to, body)

    def _send(self, channel: str, to: str, body: str) -> MessageResult:
        if not self.is_live:
            logger.info("[%s mock -> %s] %s", channel.upper(), to, body)
            return MessageResult(channel=channel, to=to, body=body, sent=False, mode="mock")

        # Redirect live sends to your own number for demo purposes, if set.
        override = self.demo_whatsapp_override if channel == "whatsapp" else self.demo_sms_override
        effective_to = override or to

        try:
            if channel == "whatsapp":
                from_number = self.whatsapp_from
                to_number = effective_to if effective_to.startswith("whatsapp:") else f"whatsapp:{effective_to}"
            else:
                if not self.sms_from:
                    raise ValueError("TWILIO_SMS_FROM not set — add a Twilio SMS-capable number to .env")
                from_number = self.sms_from
                to_number = effective_to

            msg = self._client.messages.create(body=body, from_=from_number, to=to_number)
            logger.info("[%s live -> %s] sid=%s", channel.upper(), effective_to, msg.sid)
            return MessageResult(channel=channel, to=effective_to, body=body, sent=True, mode="live", provider_sid=msg.sid)

        except Exception as e:
            # A messaging-provider hiccup must never break the recovery pipeline.
            logger.warning("[%s SEND FAILED -> %s] %s — recorded as mock", channel.upper(), effective_to, e)
            return MessageResult(channel=channel, to=effective_to, body=body, sent=False, mode="mock", error=str(e))


messenger = MessagingClient()
