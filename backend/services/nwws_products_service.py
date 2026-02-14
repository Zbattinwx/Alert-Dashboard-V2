"""
NWWS Products Service for Alert Dashboard V2.

Captures ALL raw NWWS products for monitoring and extracts
Area Forecast Discussions (AFDs) for display.

This service registers as a raw callback on NWWSAlertHandler to receive
every product that comes through the NWWS-OI feed, regardless of whether
it matches target phenomena or states.
"""

import re
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from ..models.alert import get_wfo_name

logger = logging.getLogger(__name__)

# Regex for WMO header line: TTAAii CCCC YYGGgg
WMO_HEADER_RE = re.compile(r'^([A-Z]{4}\d{2})\s+([A-Z0-9]{4})\s+(\d{6})\s*$')

# AWIPS ID: 3-6+ uppercase alpha characters on a line by itself
AWIPS_ID_RE = re.compile(r'^([A-Z]{3,8})$')

# AFD section headers (preceded by a dot, e.g. ".SYNOPSIS...")
AFD_SECTION_RE = re.compile(r'^\.([A-Z][A-Z\s/()0-9]+?)\.{3}\s*(.*)$')


class NWWSProductsService:
    """
    Captures ALL raw NWWS products for monitoring and extracts AFDs.
    """

    def __init__(self, max_products: int = 500, max_afds_per_office: int = 5):
        self._products: deque[dict] = deque(maxlen=max_products)
        self._afds: dict[str, deque] = {}
        self._max_afds_per_office = max_afds_per_office
        self._product_count: int = 0

    def on_raw_product(self, raw_text: str):
        """Callback registered on NWWSAlertHandler.add_raw_callback()."""
        try:
            product = self._parse_product_metadata(raw_text)
            if product:
                self._products.appendleft(product)
                self._product_count += 1

                # Check if this is an AFD
                awips = product.get("awips_id", "") or ""
                if awips.upper().startswith("AFD"):
                    self._store_afd(product, raw_text)
        except Exception as e:
            logger.error(f"Error processing raw NWWS product: {e}")

    def _parse_product_metadata(self, raw_text: str) -> Optional[dict]:
        """Extract WMO header, AWIPS ID, office, and preview from raw text."""
        lines = raw_text.strip().split('\n')
        if len(lines) < 2:
            return None

        wmo_header = None
        awips_id = None
        office = None
        product_type = None
        wmo_office = None

        # Find WMO header (usually first non-empty line)
        header_line_idx = None
        for i, line in enumerate(lines[:5]):
            line = line.strip()
            m = WMO_HEADER_RE.match(line)
            if m:
                wmo_header = line
                wmo_office = m.group(2)
                header_line_idx = i
                break

        # AWIPS ID is typically the next non-empty line after WMO header
        if header_line_idx is not None:
            for j in range(header_line_idx + 1, min(header_line_idx + 4, len(lines))):
                candidate = lines[j].strip()
                if candidate and AWIPS_ID_RE.match(candidate):
                    awips_id = candidate
                    break

        if awips_id and len(awips_id) >= 4:
            # AWIPS ID: first 3 chars = product type, rest = office
            product_type = awips_id[:3]
            office = awips_id[3:]
        elif wmo_office:
            # Fall back to WMO office (strip leading K for US stations)
            office = wmo_office[1:] if wmo_office.startswith("K") else wmo_office

        # Build preview from body text (skip header lines)
        body_start = (header_line_idx or 0) + 2
        preview_lines = []
        for line in lines[body_start:body_start + 5]:
            stripped = line.strip()
            if stripped and stripped != "$$":
                preview_lines.append(stripped)
            if len(preview_lines) >= 2:
                break
        preview = ' '.join(preview_lines)[:150]

        return {
            "wmo_header": wmo_header,
            "awips_id": awips_id,
            "office": office,
            "product_type": product_type,
            "preview": preview,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "raw_length": len(raw_text),
        }

    def _store_afd(self, product: dict, raw_text: str):
        """Store an AFD product."""
        office = (product.get("office") or "").upper()
        if not office:
            return

        sections = self._parse_afd_sections(raw_text)

        afd_entry = {
            "office": office,
            "wfo_name": get_wfo_name(office),
            "received_at": product["received_at"],
            "wmo_header": product.get("wmo_header"),
            "raw_text": raw_text,
            "sections": sections,
        }

        if office not in self._afds:
            self._afds[office] = deque(maxlen=self._max_afds_per_office)
        self._afds[office].appendleft(afd_entry)
        logger.info(f"Stored AFD from {office} ({len(self._afds[office])} cached)")

    def _parse_afd_sections(self, raw_text: str) -> dict[str, str]:
        """Parse AFD into named sections (.SYNOPSIS..., .NEAR TERM..., etc.)."""
        sections: dict[str, str] = {}
        current_section: Optional[str] = None
        current_lines: list[str] = []

        for line in raw_text.split('\n'):
            stripped = line.strip()

            # End of product
            if stripped == "$$" or stripped == "&&":
                if current_section:
                    sections[current_section] = '\n'.join(current_lines).strip()
                    current_section = None
                    current_lines = []
                continue

            m = AFD_SECTION_RE.match(stripped)
            if m:
                # Save previous section
                if current_section:
                    sections[current_section] = '\n'.join(current_lines).strip()
                current_section = m.group(1).strip()
                remainder = m.group(2).strip()
                current_lines = [remainder] if remainder else []
            elif current_section:
                current_lines.append(line.rstrip())

        # Save last section
        if current_section:
            sections[current_section] = '\n'.join(current_lines).strip()

        return sections

    # --- Public API methods ---

    def get_products(
        self,
        limit: int = 50,
        offset: int = 0,
        product_type: Optional[str] = None,
        office: Optional[str] = None,
    ) -> list[dict]:
        """Get recent products with optional filtering."""
        products = list(self._products)

        if product_type:
            pt = product_type.upper()
            products = [p for p in products if (p.get("product_type") or "").upper().startswith(pt)]
        if office:
            off = office.upper()
            products = [p for p in products if (p.get("office") or "").upper() == off]

        return products[offset:offset + limit]

    def get_product_count(self) -> int:
        """Total products received since startup."""
        return self._product_count

    def get_afd_offices(self) -> list[dict]:
        """List offices that have cached AFDs."""
        result = []
        for office, afds in self._afds.items():
            if afds:
                latest = afds[0]
                result.append({
                    "office": office,
                    "wfo_name": get_wfo_name(office),
                    "latest_received": latest["received_at"],
                    "count": len(afds),
                })
        result.sort(key=lambda x: x["office"])
        return result

    def get_afd(self, office: str, index: int = 0) -> Optional[dict]:
        """Get AFD for an office. index=0 is latest."""
        office = office.upper()
        if office.startswith("K") and len(office) == 4:
            office = office[1:]
        afds = self._afds.get(office)
        if afds and 0 <= index < len(afds):
            return afds[index]
        return None

    async def fetch_afd_from_api(self, office: str) -> Optional[dict]:
        """Fallback: fetch latest AFD from NWS API."""
        office = office.upper()
        if office.startswith("K") and len(office) == 4:
            office = office[1:]

        try:
            headers = {
                "User-Agent": "AlertDashboardV2/2.0 (alert-dashboard)",
                "Accept": "application/ld+json",
            }
            timeout = aiohttp.ClientTimeout(total=15)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Step 1: Get product list for this office
                list_url = f"https://api.weather.gov/products/types/AFD/locations/{office}"
                async with session.get(list_url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"AFD API list returned {resp.status} for {office}")
                        return None
                    data = await resp.json()

                graph = data.get("@graph", [])
                if not graph:
                    return None

                # Step 2: Fetch the latest product text
                product_url = graph[0].get("@id") or graph[0].get("id")
                if not product_url:
                    return None

                async with session.get(product_url, headers=headers) as resp2:
                    if resp2.status != 200:
                        return None
                    product_data = await resp2.json()

                raw_text = product_data.get("productText", "")
                if not raw_text:
                    return None

                sections = self._parse_afd_sections(raw_text)
                issuance_time = product_data.get("issuanceTime")

                return {
                    "office": office,
                    "wfo_name": get_wfo_name(office),
                    "received_at": issuance_time or datetime.now(timezone.utc).isoformat(),
                    "wmo_header": product_data.get("wmoCollectiveId"),
                    "raw_text": raw_text,
                    "sections": sections,
                    "source": "api",
                }

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching AFD from API for {office}")
            return None
        except Exception as e:
            logger.error(f"Error fetching AFD from API for {office}: {e}")
            return None

    def get_statistics(self) -> dict:
        """Service statistics."""
        return {
            "total_received": self._product_count,
            "buffer_size": len(self._products),
            "buffer_max": self._products.maxlen,
            "afd_offices": list(self._afds.keys()),
            "afd_count": sum(len(d) for d in self._afds.values()),
        }


# Singleton
_service: Optional[NWWSProductsService] = None


def get_nwws_products_service() -> NWWSProductsService:
    """Get the singleton NWWS products service instance."""
    global _service
    if _service is None:
        _service = NWWSProductsService()
    return _service


async def start_nwws_products_service():
    """Start the NWWS products service."""
    global _service
    _service = NWWSProductsService()
    logger.info("NWWS Products service started")


async def stop_nwws_products_service():
    """Stop the NWWS products service."""
    global _service
    _service = None
    logger.info("NWWS Products service stopped")
