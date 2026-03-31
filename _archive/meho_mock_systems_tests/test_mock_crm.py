"""
Unit tests for mock CRM API.
"""
import pytest
from fastapi.testclient import TestClient
from meho_mock_systems.crm_app import app, store


@pytest.fixture(autouse=True)
def reset_store():
    """Reset store before each test"""
    store.clear()
    from meho_mock_systems.crm_app import seed_customers
    seed_customers()
    yield
    store.clear()


@pytest.mark.unit
def test_list_customers():
    """Test listing customers"""
    client = TestClient(app)
    response = client.get("/customers")
    
    assert response.status_code == 200
    data = response.json()
    assert "customers" in data
    assert data["total"] >= 5  # We seeded 5 customers


@pytest.mark.unit
def test_get_customer():
    """Test getting a customer by ID"""
    client = TestClient(app)
    
    # Get first customer
    list_response = client.get("/customers")
    customers = list_response.json()["customers"]
    customer_id = customers[0]["id"]
    
    # Get specific customer
    response = client.get(f"/customers/{customer_id}")
    
    assert response.status_code == 200
    assert response.json()["id"] == customer_id


@pytest.mark.unit
def test_get_customer_not_found():
    """Test getting non-existent customer returns 404"""
    client = TestClient(app)
    response = client.get("/customers/nonexistent")
    
    assert response.status_code == 404


@pytest.mark.unit
def test_create_customer():
    """Test creating a customer"""
    client = TestClient(app)
    
    new_customer = {
        "name": "Test Corp",
        "email": "test@example.com",
        "status": "active",
        "region": "EU"
    }
    
    response = client.post("/customers", json=new_customer)
    
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Corp"
    assert data["email"] == "test@example.com"
    assert "id" in data
    assert "created_at" in data


@pytest.mark.unit
def test_filter_customers_by_region():
    """Test filtering customers by region"""
    client = TestClient(app)
    
    response = client.get("/customers?region=EU")
    
    assert response.status_code == 200
    customers = response.json()["customers"]
    assert all(c["region"] == "EU" for c in customers)


@pytest.mark.unit
def test_filter_customers_by_status():
    """Test filtering customers by status"""
    client = TestClient(app)
    
    response = client.get("/customers?status=active")
    
    assert response.status_code == 200
    customers = response.json()["customers"]
    assert all(c["status"] == "active" for c in customers)


@pytest.mark.unit
def test_get_customer_summary():
    """Test getting customer summary"""
    client = TestClient(app)
    
    # Get first customer
    list_response = client.get("/customers")
    customer_id = list_response.json()["customers"][0]["id"]
    
    # Get summary
    response = client.get(f"/customers/{customer_id}/summary")
    
    assert response.status_code == 200
    data = response.json()
    assert "customer" in data
    assert "total_orders" in data
    assert "total_spent" in data
    assert data["customer"]["id"] == customer_id


@pytest.mark.unit
def test_openapi_spec_available():
    """Test that OpenAPI spec is accessible"""
    client = TestClient(app)
    response = client.get("/openapi.json")
    
    assert response.status_code == 200
    spec = response.json()
    assert "openapi" in spec
    assert "paths" in spec
    assert "/customers" in spec["paths"]

