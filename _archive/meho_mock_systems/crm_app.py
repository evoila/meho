"""
Mock CRM API for testing.

Provides customer management endpoints.
"""
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime
from meho_mock_systems.base import InMemoryStore, generate_id

# Initialize FastAPI app
app = FastAPI(
    title="Mock CRM API",
    description="Mock Customer Relationship Management API for MEHO testing",
    version="1.0.0"
)

# In-memory storage
store = InMemoryStore()


# ============================================================================
# Models
# ============================================================================

class Address(BaseModel):
    """Customer address"""
    street: str
    city: str
    country: str
    postal_code: str


class CustomerCreate(BaseModel):
    """Customer creation request"""
    name: str = Field(..., min_length=1, max_length=255)
    email: str
    status: Literal["active", "inactive", "suspended"] = "active"
    region: str = Field(..., description="Region (e.g., EU, US, APAC)")
    address: Optional[Address] = None


class Customer(CustomerCreate):
    """Customer with ID and timestamps"""
    id: str
    created_at: str
    updated_at: str


class CustomerSummary(BaseModel):
    """Aggregated customer information"""
    customer: Customer
    total_orders: int
    total_spent: float
    last_order_date: Optional[str] = None


class CustomerList(BaseModel):
    """Paginated customer list"""
    customers: List[Customer]
    total: int
    limit: int
    offset: int


# ============================================================================
# Seed Data
# ============================================================================

def seed_customers():
    """Populate with sample customers"""
    sample_customers = [
        {
            "id": "cust-001",
            "name": "Acme Corp",
            "email": "contact@acme.com",
            "status": "active",
            "region": "EU",
            "address": {
                "street": "123 Business St",
                "city": "Berlin",
                "country": "Germany",
                "postal_code": "10115"
            }
        },
        {
            "id": "cust-002",
            "name": "TechStart Inc",
            "email": "hello@techstart.com",
            "status": "active",
            "region": "US",
            "address": {
                "street": "456 Innovation Ave",
                "city": "San Francisco",
                "country": "USA",
                "postal_code": "94105"
            }
        },
        {
            "id": "cust-003",
            "name": "Global Solutions Ltd",
            "email": "info@globalsolutions.com",
            "status": "active",
            "region": "APAC",
            "address": {
                "street": "789 Enterprise Rd",
                "city": "Singapore",
                "country": "Singapore",
                "postal_code": "018956"
            }
        },
        {
            "id": "cust-004",
            "name": "EU Trading GmbH",
            "email": "trade@eutrading.de",
            "status": "inactive",
            "region": "EU",
            "address": {
                "street": "321 Commerce Blvd",
                "city": "Frankfurt",
                "country": "Germany",
                "postal_code": "60311"
            }
        },
        {
            "id": "cust-005",
            "name": "Pacific Ventures",
            "email": "contact@pacificventures.com",
            "status": "active",
            "region": "APAC",
            "address": {
                "street": "555 Ocean Dr",
                "city": "Sydney",
                "country": "Australia",
                "postal_code": "2000"
            }
        }
    ]
    
    for customer in sample_customers:
        store.create("customers", customer)


# Seed on startup
seed_customers()


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/customers", response_model=CustomerList, tags=["customers"])
def list_customers(
    region: Optional[str] = Query(None, description="Filter by region (EU, US, APAC)"),
    status: Optional[str] = Query(None, description="Filter by status"),
    created_after: Optional[str] = Query(None, description="Filter by creation date (ISO format)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    List customers with optional filters.
    
    Supports filtering by:
    - region: EU, US, APAC
    - status: active, inactive, suspended
    - created_after: ISO datetime string
    """
    def filter_fn(customer):
        if region and customer.get("region") != region:
            return False
        if status and customer.get("status") != status:
            return False
        if created_after:
            customer_date = datetime.fromisoformat(customer.get("created_at", ""))
            filter_date = datetime.fromisoformat(created_after)
            if customer_date <= filter_date:
                return False
        return True
    
    all_customers = store.list("customers", filter_fn if (region or status or created_after) else None)
    
    # Pagination
    total = len(all_customers)
    customers_page = all_customers[offset:offset + limit]
    
    return {
        "customers": customers_page,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/customers/{customer_id}", response_model=Customer, tags=["customers"])
def get_customer(
    customer_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Get a customer by ID"""
    customer = store.get("customers", customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@app.post("/customers", response_model=Customer, status_code=201, tags=["customers"])
def create_customer(
    customer: CustomerCreate,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Create a new customer"""
    customer_data = customer.model_dump()
    # Convert address to dict if present
    if customer_data.get("address"):
        customer_data["address"] = customer_data["address"]
    
    created = store.create("customers", customer_data)
    return created


@app.get("/customers/{customer_id}/summary", response_model=CustomerSummary, tags=["customers"])
def get_customer_summary(
    customer_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    Get aggregated customer summary.
    
    Includes order count and spending (mock data).
    """
    customer = store.get("customers", customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    # Mock aggregated data
    return {
        "customer": customer,
        "total_orders": 42,
        "total_spent": 125000.50,
        "last_order_date": "2024-11-01T10:30:00Z"
    }


@app.get("/health", tags=["system"])
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "mock-crm"}


# Run with: uvicorn meho_mock_systems.crm_app:app --port 8001 --reload

