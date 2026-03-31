"""
Unit tests for mock Trading API.
"""
import pytest
from fastapi.testclient import TestClient
from meho_mock_systems.trading_app import app, store


@pytest.fixture(autouse=True)
def reset_store():
    """Reset store before each test"""
    store.clear()
    from meho_mock_systems.trading_app import seed_trading_data
    seed_trading_data()
    yield
    store.clear()


@pytest.mark.unit
def test_list_accounts():
    """Test listing trading accounts"""
    client = TestClient(app)
    response = client.get("/accounts")
    
    assert response.status_code == 200
    accounts = response.json()
    assert len(accounts) >= 2


@pytest.mark.unit
def test_list_positions():
    """Test listing positions"""
    client = TestClient(app)
    response = client.get("/positions")
    
    assert response.status_code == 200
    positions = response.json()
    assert len(positions) >= 2


@pytest.mark.unit
def test_filter_positions_by_account():
    """Test filtering positions by account_id"""
    client = TestClient(app)
    
    response = client.get("/positions?account_id=acc-001")
    
    assert response.status_code == 200
    positions = response.json()
    assert all(p["account_id"] == "acc-001" for p in positions)


@pytest.mark.unit
def test_place_order():
    """Test placing a trading order"""
    client = TestClient(app)
    
    new_order = {
        "account_id": "acc-001",
        "symbol": "TSLA",
        "side": "buy",
        "quantity": 10,
        "order_type": "market"
    }
    
    response = client.post("/orders", json=new_order)
    
    assert response.status_code == 201
    data = response.json()
    assert data["symbol"] == "TSLA"
    assert data["quantity"] == 10
    assert data["status"] == "pending"


@pytest.mark.unit
def test_get_risk_summary():
    """Test getting risk summary"""
    client = TestClient(app)
    
    response = client.get("/risk/summary?account_id=acc-001")
    
    assert response.status_code == 200
    summary = response.json()
    assert "total_exposure" in summary
    assert "leverage" in summary
    assert "margin_used" in summary
    assert "margin_available" in summary
    assert "positions_count" in summary
    assert summary["positions_count"] >= 0


@pytest.mark.unit
def test_risk_summary_nonexistent_account():
    """Test risk summary for non-existent account returns 404"""
    client = TestClient(app)
    
    response = client.get("/risk/summary?account_id=nonexistent")
    
    assert response.status_code == 404

