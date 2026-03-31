"""
Unit tests for mock Orders API.
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
def test_list_orders():
    """Test listing orders"""
    client = TestClient(app)
    response = client.get("/orders")
    
    assert response.status_code == 200
    data = response.json()
    assert "orders" in data
    assert data["total"] >= 3  # We seeded 3 orders


@pytest.mark.unit
def test_get_order():
    """Test getting an order by ID"""
    client = TestClient(app)
    
    # Get first order
    list_response = client.get("/orders")
    order_id = list_response.json()["orders"][0]["id"]
    
    response = client.get(f"/orders/{order_id}")
    
    assert response.status_code == 200
    assert response.json()["id"] == order_id


@pytest.mark.unit
def test_create_order():
    """Test creating an order"""
    client = TestClient(app)
    
    new_order = {
        "customer_id": "cust-001",
        "items": [
            {
                "product_id": "prod-001",
                "product_name": "Test Product",
                "quantity": 2,
                "unit_price": 50.00
            }
        ]
    }
    
    response = client.post("/orders", json=new_order)
    
    assert response.status_code == 201
    data = response.json()
    assert data["customer_id"] == "cust-001"
    assert data["total_amount"] == 100.00  # 2 * 50
    assert data["status"] == "pending"
    assert "id" in data


@pytest.mark.unit
def test_filter_orders_by_customer():
    """Test filtering orders by customer_id"""
    client = TestClient(app)
    
    response = client.get("/orders?customer_id=cust-001")
    
    assert response.status_code == 200
    orders = response.json()["orders"]
    assert all(o["customer_id"] == "cust-001" for o in orders)


@pytest.mark.unit
def test_filter_orders_by_status():
    """Test filtering orders by status"""
    client = TestClient(app)
    
    response = client.get("/orders?status=completed")
    
    assert response.status_code == 200
    orders = response.json()["orders"]
    assert all(o["status"] == "completed" for o in orders)


@pytest.mark.unit
def test_order_stats():
    """Test order statistics endpoint"""
    client = TestClient(app)
    
    response = client.get("/orders/stats")
    
    assert response.status_code == 200
    stats = response.json()
    assert "total_count" in stats
    assert "total_amount" in stats
    assert "avg_order_value" in stats
    assert "by_status" in stats
    assert stats["total_count"] >= 3


@pytest.mark.unit
def test_order_stats_by_customer():
    """Test order statistics filtered by customer"""
    client = TestClient(app)
    
    response = client.get("/orders/stats?customer_id=cust-001")
    
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_count"] >= 0

