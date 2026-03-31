"""
Unit tests for mock Orders API date filtering (bug fix verification).
"""
import pytest
from fastapi.testclient import TestClient
from meho_mock_systems.orders_app import app, store


@pytest.fixture(autouse=True)
def reset_store():
    """Reset store before each test"""
    store.clear()
    from meho_mock_systems.orders_app import seed_orders
    seed_orders()
    yield
    store.clear()


@pytest.mark.unit
def test_filter_orders_by_date_from():
    """Test filtering orders by date_from with Z suffix"""
    client = TestClient(app)
    
    # This should work with Z suffix (bug fix verification)
    response = client.get("/orders?date_from=2024-10-01T00:00:00Z")
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert "orders" in data
    # Should get orders after Oct 1, 2024
    assert len(data["orders"]) > 0


@pytest.mark.unit
def test_filter_orders_by_date_to():
    """Test filtering orders by date_to with Z suffix"""
    client = TestClient(app)
    
    # This should work with Z suffix
    response = client.get("/orders?date_to=2024-12-31T23:59:59Z")
    
    assert response.status_code == 200
    data = response.json()
    assert "orders" in data


@pytest.mark.unit
def test_filter_orders_by_date_range():
    """Test filtering orders by date range"""
    client = TestClient(app)
    
    # Filter for November 2024
    response = client.get("/orders?date_from=2024-11-01T00:00:00Z&date_to=2024-11-30T23:59:59Z")
    
    assert response.status_code == 200
    data = response.json()
    orders = data["orders"]
    
    # Should only get November orders
    # From seed data: ord-002 (Nov 1) and ord-003 (Nov 10)
    assert len(orders) == 2


@pytest.mark.unit
def test_filter_orders_date_without_z_suffix():
    """Test filtering also works without Z suffix (URL encoded)"""
    from urllib.parse import quote
    client = TestClient(app)
    
    # URL encode the + sign to %2B
    date_param = quote("2024-10-01T00:00:00+00:00", safe='')
    response = client.get(f"/orders?date_from={date_param}")
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["orders"]) > 0

