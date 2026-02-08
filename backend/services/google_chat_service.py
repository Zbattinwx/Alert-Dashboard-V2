"""
Google Chat Notification Service for Alert Dashboard V2.

Sends weather alert notifications to Google Chat via webhooks.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

from ..config import get_settings
from ..models.alert import Alert, AlertPriority

logger = logging.getLogger(__name__)


# WFO to timezone mapping for local time display
WFO_TIMEZONES = {
    # Eastern
    "CLE": "America/New_York", "ILN": "America/New_York", "PBZ": "America/New_York",
    "CTP": "America/New_York", "PHI": "America/New_York", "BGM": "America/New_York",
    "ALY": "America/New_York", "BUF": "America/New_York", "OKX": "America/New_York",
    "BOX": "America/New_York", "GYX": "America/New_York", "CAR": "America/New_York",
    "BTV": "America/New_York", "RLX": "America/New_York", "LWX": "America/New_York",
    "AKQ": "America/New_York", "RNK": "America/New_York", "JKL": "America/New_York",
    "LMK": "America/New_York", "MRX": "America/New_York", "GSP": "America/New_York",
    "FFC": "America/New_York", "CAE": "America/New_York", "CHS": "America/New_York",
    "ILM": "America/New_York", "MHX": "America/New_York", "RAH": "America/New_York",
    "JAX": "America/New_York", "MLB": "America/New_York", "TBW": "America/New_York",
    "DTX": "America/New_York", "GRR": "America/New_York", "APX": "America/New_York",
    # Central
    "IND": "America/Indiana/Indianapolis", "IWX": "America/Indiana/Indianapolis",
    "LOT": "America/Chicago", "ILX": "America/Chicago", "DVN": "America/Chicago",
    "DMX": "America/Chicago", "ARX": "America/Chicago", "MKX": "America/Chicago",
    "GRB": "America/Chicago", "DLH": "America/Chicago", "MPX": "America/Chicago",
    "FSD": "America/Chicago", "ABR": "America/Chicago", "BIS": "America/Chicago",
    "FGF": "America/Chicago", "PAH": "America/Chicago", "OHX": "America/Chicago",
    "MEG": "America/Chicago", "HUN": "America/Chicago", "BMX": "America/Chicago",
    "MOB": "America/Chicago", "LIX": "America/Chicago", "LCH": "America/Chicago",
    "SHV": "America/Chicago", "JAN": "America/Chicago", "LZK": "America/Chicago",
    "TSA": "America/Chicago", "OUN": "America/Chicago", "FWD": "America/Chicago",
    "HGX": "America/Chicago", "CRP": "America/Chicago", "BRO": "America/Chicago",
    "EWX": "America/Chicago", "SJT": "America/Chicago", "AMA": "America/Chicago",
    "LUB": "America/Chicago", "MAF": "America/Chicago", "MQT": "America/Chicago",
    "TAE": "America/Chicago",
    # Mountain
    "UNR": "America/Denver", "BOU": "America/Denver", "PUB": "America/Denver",
    "GJT": "America/Denver", "ABQ": "America/Denver", "EPZ": "America/Denver",
    "RIW": "America/Denver", "CYS": "America/Denver", "BYZ": "America/Denver",
    "GGW": "America/Denver", "TFX": "America/Denver", "MSO": "America/Denver",
    "PIH": "America/Denver", "BOI": "America/Denver", "SLC": "America/Denver",
    "PHX": "America/Phoenix", "FGZ": "America/Phoenix", "TWC": "America/Phoenix",
    # Pacific
    "VEF": "America/Los_Angeles", "LKN": "America/Los_Angeles", "REV": "America/Los_Angeles",
    "PDT": "America/Los_Angeles", "PQR": "America/Los_Angeles", "MFR": "America/Los_Angeles",
    "SEW": "America/Los_Angeles", "OTX": "America/Los_Angeles", "SGX": "America/Los_Angeles",
    "LOX": "America/Los_Angeles", "HNX": "America/Los_Angeles", "STO": "America/Los_Angeles",
    "MTR": "America/Los_Angeles", "EKA": "America/Los_Angeles",
    # Other
    "MFL": "America/New_York",
    "AFC": "America/Anchorage", "AFG": "America/Anchorage", "AJK": "America/Anchorage",
    "HFO": "Pacific/Honolulu",
}


def get_timezone_for_office(office_code: str) -> ZoneInfo:
    """Get timezone for a WFO office code."""
    code = office_code.upper()
    if code.startswith("K") and len(code) == 4:
        code = code[1:]
    tz_name = WFO_TIMEZONES.get(code, "America/New_York")
    return ZoneInfo(tz_name)


def format_expiration_time(dt: Optional[datetime], office: str) -> str:
    """Format expiration time in the local timezone of the issuing office."""
    if not dt:
        return "Unknown"

    try:
        tz = get_timezone_for_office(office)
        local_time = dt.astimezone(tz)
        return local_time.strftime("%-I:%M %p %Z")
    except Exception:
        # Fallback to UTC
        return dt.strftime("%H:%M UTC")


def build_threat_summary(alert: Alert) -> str:
    """Build a summary of threats from the alert."""
    threats = []
    threat = alert.threat

    # Tornado
    if threat.tornado_detection:
        tornado_str = f"Tornado: {threat.tornado_detection}"
        if threat.tornado_damage_threat:
            tornado_str += f" ({threat.tornado_damage_threat})"
        threats.append(tornado_str)

    # Wind
    if threat.max_wind_gust_mph:
        wind_str = f"Wind: {threat.max_wind_gust_mph} MPH"
        if threat.wind_damage_threat:
            wind_str += f" ({threat.wind_damage_threat})"
        threats.append(wind_str)

    # Hail
    if threat.max_hail_size_inches:
        hail_str = f"Hail: {threat.max_hail_size_inches}\" diameter"
        if threat.hail_damage_threat:
            hail_str += f" ({threat.hail_damage_threat})"
        threats.append(hail_str)

    # Snow
    if threat.snow_amount_max_inches:
        if threat.snow_amount_min_inches:
            snow_str = f"Snow: {threat.snow_amount_min_inches}-{threat.snow_amount_max_inches}\""
        else:
            snow_str = f"Snow: Up to {threat.snow_amount_max_inches}\""
        threats.append(snow_str)

    # Ice
    if threat.ice_accumulation_inches:
        threats.append(f"Ice: {threat.ice_accumulation_inches}\"")

    # Flash Flood
    if threat.flash_flood_detection:
        flood_str = f"Flooding: {threat.flash_flood_detection}"
        if threat.flash_flood_damage_threat:
            flood_str += f" ({threat.flash_flood_damage_threat})"
        threats.append(flood_str)

    return ", ".join(threats) if threats else "N/A"


def get_alert_title(alert: Alert) -> str:
    """Get formatted alert title with priority indicators."""
    title = alert.event_name

    # Check for special conditions
    threat = alert.threat

    # Tornado Emergency
    if (alert.phenomenon == "TO" and
        threat.tornado_damage_threat == "CATASTROPHIC"):
        return f"🚨 TORNADO EMERGENCY 🚨"

    # PDS (Particularly Dangerous Situation)
    if threat.is_pds:
        if threat.tornado_damage_threat == "CONSIDERABLE":
            return f"⚠️ PDS {title}"
        if threat.wind_damage_threat in ("DESTRUCTIVE", "CATASTROPHIC"):
            return f"⚠️ DESTRUCTIVE {title}"

    # Observed tornado
    if threat.tornado_detection == "OBSERVED":
        return f"🌪️ CONFIRMED {title}"

    # Radar indicated tornado
    if threat.tornado_detection == "RADAR INDICATED":
        return f"🌀 {title}"

    return title


def build_google_chat_message(alert: Alert) -> dict:
    """Build a Google Chat card message for an alert."""
    title = get_alert_title(alert)
    expires_text = format_expiration_time(alert.expiration_time, alert.sender_office)
    threat_summary = build_threat_summary(alert)

    # Build the card message using cardsV2 format
    message = {
        "cardsV2": [{
            "cardId": f"weatherAlert_{alert.product_id}",
            "card": {
                "header": {
                    "title": title,
                    "subtitle": f"📍 {alert.display_locations or 'Multiple counties'}",
                    "imageUrl": "https://www.weather.gov/images/nws/nws_logo.png",
                    "imageType": "CIRCLE"
                },
                "sections": [
                    {
                        "header": "Alert Details",
                        "collapsible": False,
                        "widgets": [
                            {
                                "decoratedText": {
                                    "topLabel": "Expires",
                                    "text": expires_text,
                                    "startIcon": {
                                        "knownIcon": "CLOCK"
                                    }
                                }
                            },
                            {
                                "decoratedText": {
                                    "topLabel": "Issuing Office",
                                    "text": alert.sender_name or alert.sender_office,
                                    "startIcon": {
                                        "knownIcon": "BOOKMARK"
                                    }
                                }
                            }
                        ]
                    },
                    {
                        "header": "Threats",
                        "collapsible": False,
                        "widgets": [
                            {
                                "textParagraph": {
                                    "text": f"<b>{threat_summary}</b>"
                                }
                            }
                        ]
                    }
                ]
            }
        }]
    }

    # Add headline if available
    if alert.headline:
        headline_widget = {
            "textParagraph": {
                "text": alert.headline[:500]  # Limit length
            }
        }
        message["cardsV2"][0]["card"]["sections"].insert(0, {
            "widgets": [headline_widget]
        })

    return message


async def send_alert_to_google_chat(alert: Alert) -> bool:
    """
    Send an alert notification to Google Chat.

    Args:
        alert: The Alert to send

    Returns:
        True if sent successfully, False otherwise
    """
    settings = get_settings()

    # Check if Google Chat is enabled
    if not settings.google_chat_enabled:
        logger.debug("Google Chat notifications disabled")
        return False

    # Check webhook URL
    webhook_url = settings.google_chat_webhook_url
    if not webhook_url:
        logger.warning("Google Chat webhook URL not configured")
        return False

    # Check if this alert type should be sent
    if alert.phenomenon not in settings.google_chat_phenomena:
        logger.debug(f"Alert phenomenon {alert.phenomenon} not in Google Chat filter")
        return False

    # Build the message
    message = build_google_chat_message(alert)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=message,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    logger.info(f"Google Chat notification sent for {alert.event_name}: {alert.product_id}")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"Google Chat webhook error {response.status}: {error_text}")
                    return False

    except asyncio.TimeoutError:
        logger.error("Google Chat webhook timed out")
        return False
    except aiohttp.ClientError as e:
        logger.error(f"Google Chat webhook connection error: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error sending to Google Chat: {e}")
        return False


class GoogleChatService:
    """Service for managing Google Chat notifications."""

    def __init__(self):
        self._sent_alerts: set[str] = set()  # Track sent alert IDs to avoid duplicates
        self._startup_complete: bool = False  # Don't send notifications until startup is complete
        self._stats = {
            "total_sent": 0,
            "total_failed": 0,
            "total_skipped_startup": 0,  # Track alerts skipped during startup
            "last_sent": None,
        }

    async def notify_new_alert(self, alert: Alert) -> bool:
        """
        Send notification for a new alert.

        Only sends if:
        - Startup is complete (don't notify for alerts loaded on startup)
        - Google Chat is enabled
        - Alert hasn't been sent before
        - Alert is a NEW action (not update/continuation)
        """
        # Skip notifications during startup (alerts loaded from NWS API on app start)
        if not self._startup_complete:
            logger.debug(f"Skipping Google Chat during startup: {alert.product_id}")
            self._stats["total_skipped_startup"] += 1
            # Still track it as "sent" so we don't re-notify if it comes through NWWS later
            self._sent_alerts.add(alert.product_id)
            return False

        # Check if already sent
        if alert.product_id in self._sent_alerts:
            logger.debug(f"Alert {alert.product_id} already sent to Google Chat")
            return False

        # Check if this is an update (CON action)
        if alert.vtec and alert.vtec.is_update:
            logger.debug(f"Skipping Google Chat for alert update: {alert.product_id}")
            return False

        # Send the notification
        success = await send_alert_to_google_chat(alert)

        if success:
            self._sent_alerts.add(alert.product_id)
            self._stats["total_sent"] += 1
            self._stats["last_sent"] = datetime.now(timezone.utc).isoformat()
        else:
            self._stats["total_failed"] += 1

        return success

    def get_statistics(self) -> dict:
        """Get service statistics."""
        settings = get_settings()
        return {
            "enabled": settings.google_chat_enabled,
            "webhook_configured": bool(settings.google_chat_webhook_url),
            "phenomena_filter": settings.google_chat_phenomena,
            "alerts_sent": self._stats["total_sent"],
            "alerts_failed": self._stats["total_failed"],
            "alerts_skipped_startup": self._stats["total_skipped_startup"],
            "last_sent": self._stats["last_sent"],
            "tracked_alerts": len(self._sent_alerts),
            "startup_complete": self._startup_complete,
        }

    def mark_startup_complete(self):
        """
        Mark startup as complete, enabling Google Chat notifications.

        Should be called after initial alerts are loaded from NWS API.
        """
        self._startup_complete = True
        logger.info(
            f"Google Chat startup complete - skipped {self._stats['total_skipped_startup']} "
            f"existing alerts, now accepting new alert notifications"
        )

    def clear_sent_alerts(self):
        """Clear the set of sent alerts (useful for testing)."""
        self._sent_alerts.clear()


# Global service instance
_service: Optional[GoogleChatService] = None


def get_google_chat_service() -> GoogleChatService:
    """Get the global GoogleChatService instance."""
    global _service
    if _service is None:
        _service = GoogleChatService()
    return _service


async def start_google_chat_service() -> bool:
    """
    Start the Google Chat service.

    Returns:
        True if service is enabled and configured, False otherwise
    """
    global _service
    settings = get_settings()

    _service = GoogleChatService()

    if settings.google_chat_enabled:
        if settings.google_chat_webhook_url:
            logger.info("Google Chat notification service started")
            return True
        else:
            logger.warning("Google Chat enabled but webhook URL not configured")
            return False
    else:
        logger.info("Google Chat notifications disabled")
        return False


async def stop_google_chat_service():
    """Stop the Google Chat service."""
    global _service
    if _service:
        logger.info(f"Google Chat service stopped. Stats: {_service.get_statistics()}")
        _service = None
