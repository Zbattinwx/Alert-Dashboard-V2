
import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from backend.services.zone_geometry_service import ZoneGeometryService

@pytest.mark.asyncio
async def test_fetch_fallback_county_to_zone():
    """Test that a failed county fetch falls back to a zone fetch."""
    
    # Mock the NWSAPIClient
    mock_client = AsyncMock()
    
    # Setup fetch responses
    # 1. County fetch (TXC150) -> fails (returns None)
    mock_client.get_county_geometry.return_value = None
    
    # 2. Zone fetch (TXZ150) -> succeeds (returns mock geometry)
    mock_geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]
    }
    mock_client.get_zone_geometry.return_value = mock_geometry

    # Initialize service with mocked client
    service = ZoneGeometryService(nws_client=mock_client)
    
    # Perform the fetch
    result = await service.fetch_zone_geometry("TXC150")
    
    # Verification
    # 1. Verify fallback logic executed
    mock_client.get_county_geometry.assert_called_once_with("TXC150")
    mock_client.get_zone_geometry.assert_called_once_with("TXZ150")
    
    # 2. Verify result matches the zone geometry (parsed)
    assert result is not None
    assert len(result) == 1
    # Check swapped coordinates (lon,lat -> lat,lon)
    assert result[0][0] == [0, 0] # [lat, lon]

@pytest.mark.asyncio
async def test_fetch_no_fallback_for_zones():
    """Test that failed zone fetches do NOT fallback (only county -> zone)."""
    
    mock_client = AsyncMock()
    mock_client.get_zone_geometry.return_value = None
    
    service = ZoneGeometryService(nws_client=mock_client)
    
    result = await service.fetch_zone_geometry("TXZ150")
    
    # Only zone fetch attempted
    mock_client.get_zone_geometry.assert_called_once_with("TXZ150")
    mock_client.get_county_geometry.assert_not_called()
    
    assert result is None

@pytest.mark.asyncio
async def test_fetch_fallback_fails_too():
    """Test behavior when both primary and fallback fetches fail."""
    
    mock_client = AsyncMock()
    mock_client.get_county_geometry.return_value = None
    mock_client.get_zone_geometry.return_value = None
    
    service = ZoneGeometryService(nws_client=mock_client)
    
    result = await service.fetch_zone_geometry("TXC150")
    
    mock_client.get_county_geometry.assert_called_once_with("TXC150")
    mock_client.get_zone_geometry.assert_called_once_with("TXZ150")
    
    assert result is None
