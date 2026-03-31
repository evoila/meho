"""
Mock Orders API for testing.

Provides order management and statistics endpoints.
"""
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict
from datetime import datetime
from meho_mock_systems.base import InMemoryStore, generate_id


def normalize_iso_datetime(dt_string: str) -> str:
    """
    Normalize ISO datetime string for fromisoformat compatibility.
    Handles both Z suffix and +00:00 format.
    
    Args:
        dt_string: ISO datetime string (e.g., "2024-10-15T10:00:00Z" or "2024-10-15T10:00:00+00:00")
    
    Returns:
        Normalized string compatible with datetime.fromisoformat()
    """
    # Replace Z with +00:00 only if it ends with Z
    if dt_string.endswith("Z"):
        return dt_string[:-1] + "+00:00"
    return dt_string

app = FastAPI(
    title="Mock Orders API",
    description="Mock Orders Management API for MEHO testing",
    version="1.0.0"
)

store = InMemoryStore()


# ============================================================================
# Models
# ============================================================================

class OrderItem(BaseModel):
    """Order line item"""
    product_id: str
    product_name: str
    quantity: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0)


class OrderCreate(BaseModel):
    """Order creation request"""
    customer_id: str
    items: List[OrderItem]
    notes: Optional[str] = None


class Order(BaseModel):
    """Order with ID and computed fields"""
    id: str
    customer_id: str
    items: List[OrderItem]
    total_amount: float
    status: Literal["pending", "processing", "completed", "cancelled"]
    notes: Optional[str] = None
    created_at: str
    updated_at: str


class OrderStats(BaseModel):
    """Order statistics"""
    total_count: int
    total_amount: float
    avg_order_value: float
    by_status: Dict[str, int]


class OrderList(BaseModel):
    """Paginated order list"""
    orders: List[Order]
    total: int
    limit: int
    offset: int


# ============================================================================
# Seed Data
# ============================================================================

def seed_orders():
    """Populate with sample orders"""
    sample_orders = [
        {
            "id": "ord-001",
            "customer_id": "cust-001",
            "items": [
                {"product_id": "prod-001", "product_name": "Widget A", "quantity": 10, "unit_price": 99.99},
                {"product_id": "prod-002", "product_name": "Widget B", "quantity": 5, "unit_price": 149.99}
            ],
            "total_amount": 1749.85,
            "status": "completed",
            "notes": "Bulk order",
            "created_at": "2024-10-15T10:00:00Z",
            "updated_at": "2024-10-15T10:00:00Z"
        },
        {
            "id": "ord-002",
            "customer_id": "cust-002",
            "items": [
                {"product_id": "prod-003", "product_name": "Service Package", "quantity": 1, "unit_price": 999.00}
            ],
            "total_amount": 999.00,
            "status": "processing",
            "created_at": "2024-11-01T14:30:00Z",
            "updated_at": "2024-11-01T14:30:00Z"
        },
        {
            "id": "ord-003",
            "customer_id": "cust-001",
            "items": [
                {"product_id": "prod-001", "product_name": "Widget A", "quantity": 20, "unit_price": 99.99}
            ],
            "total_amount": 1999.80,
            "status": "completed",
            "created_at": "2024-11-10T09:15:00Z",
            "updated_at": "2024-11-10T09:15:00Z"
        }
    ]
    
    for order in sample_orders:
        store.create("orders", order)


seed_orders()


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/orders", response_model=OrderList, tags=["orders"])
def list_orders(
    customer_id: Optional[str] = Query(None, description="Filter by customer ID"),
    date_from: Optional[str] = Query(None, description="Filter by start date (ISO format)"),
    date_to: Optional[str] = Query(None, description="Filter by end date (ISO format)"),
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """List orders with optional filters"""
    def filter_fn(order):
        if customer_id and order.get("customer_id") != customer_id:
            return False
        if status and order.get("status") != status:
            return False
        if date_from:
            # Normalize both timestamps for fromisoformat compatibility
            order_date_str = normalize_iso_datetime(order.get("created_at", ""))
            filter_date_str = normalize_iso_datetime(date_from)
            order_date = datetime.fromisoformat(order_date_str)
            if order_date < datetime.fromisoformat(filter_date_str):
                return False
        if date_to:
            # Normalize both timestamps for fromisoformat compatibility
            order_date_str = normalize_iso_datetime(order.get("created_at", ""))
            filter_date_str = normalize_iso_datetime(date_to)
            order_date = datetime.fromisoformat(order_date_str)
            if order_date > datetime.fromisoformat(filter_date_str):
                return False
        return True
    
    all_orders = store.list("orders", filter_fn if (customer_id or date_from or date_to or status) else None)
    
    total = len(all_orders)
    orders_page = all_orders[offset:offset + limit]
    
    return {
        "orders": orders_page,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/orders/stats", response_model=OrderStats, tags=["orders"])
def get_order_stats(
    customer_id: Optional[str] = Query(None, description="Filter by customer ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Get aggregated order statistics"""
    def filter_fn(order):
        if customer_id and order.get("customer_id") != customer_id:
            return False
        if status and order.get("status") != status:
            return False
        return True
    
    orders = store.list("orders", filter_fn if (customer_id or status) else None)
    
    total_count = len(orders)
    total_amount = sum(o.get("total_amount", 0) for o in orders)
    avg_order_value = total_amount / total_count if total_count > 0 else 0
    
    # Count by status
    by_status = {}
    for order in orders:
        status_val = order.get("status", "unknown")
        by_status[status_val] = by_status.get(status_val, 0) + 1
    
    return {
        "total_count": total_count,
        "total_amount": round(total_amount, 2),
        "avg_order_value": round(avg_order_value, 2),
        "by_status": by_status
    }


@app.get("/orders/{order_id}", response_model=Order, tags=["orders"])
def get_order(
    order_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Get an order by ID"""
    order = store.get("orders", order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/orders", response_model=Order, status_code=201, tags=["orders"])
def create_order(
    order: OrderCreate,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Create a new order"""
    order_data = order.model_dump()
    
    # Calculate total amount
    total = sum(item["quantity"] * item["unit_price"] for item in order_data["items"])
    order_data["total_amount"] = round(total, 2)
    order_data["status"] = "pending"
    
    created = store.create("orders", order_data)
    return created


@app.get("/health", tags=["system"])
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "mock-orders"}


# Run with: uvicorn meho_mock_systems.orders_app:app --port 8002 --reload

