"""
Mock Trading API for testing.

Provides trading account, position, and order endpoints.
"""
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from meho_mock_systems.base import InMemoryStore, generate_id

app = FastAPI(
    title="Mock Trading API",
    description="Mock Trading Platform API for MEHO testing",
    version="1.0.0"
)

store = InMemoryStore()


# ============================================================================
# Models
# ============================================================================

class TradingAccount(BaseModel):
    """Trading account"""
    id: str
    name: str
    account_type: Literal["cash", "margin"]
    balance: float
    currency: str = "USD"
    created_at: str
    updated_at: str


class Position(BaseModel):
    """Trading position"""
    id: str
    account_id: str
    symbol: str
    quantity: float
    avg_price: float
    current_price: float
    unrealized_pnl: float


class TradingOrderCreate(BaseModel):
    """Create trading order"""
    account_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float = Field(..., gt=0)
    order_type: Literal["market", "limit"]
    limit_price: Optional[float] = None


class TradingOrder(BaseModel):
    """Trading order"""
    id: str
    account_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    order_type: Literal["market", "limit"]
    limit_price: Optional[float]
    status: Literal["pending", "filled", "cancelled"]
    created_at: str


class RiskSummary(BaseModel):
    """Risk metrics for an account"""
    account_id: str
    total_exposure: float
    leverage: float
    margin_used: float
    margin_available: float
    positions_count: int


# ============================================================================
# Seed Data
# ============================================================================

def seed_trading_data():
    """Populate with sample trading data"""
    # Accounts
    accounts = [
        {
            "id": "acc-001",
            "name": "Main Trading Account",
            "account_type": "margin",
            "balance": 100000.00,
            "currency": "USD"
        },
        {
            "id": "acc-002",
            "name": "Conservative Account",
            "account_type": "cash",
            "balance": 50000.00,
            "currency": "USD"
        }
    ]
    
    for acc in accounts:
        store.create("accounts", acc)
    
    # Positions
    positions = [
        {
            "id": "pos-001",
            "account_id": "acc-001",
            "symbol": "AAPL",
            "quantity": 100,
            "avg_price": 150.00,
            "current_price": 155.00,
            "unrealized_pnl": 500.00
        },
        {
            "id": "pos-002",
            "account_id": "acc-001",
            "symbol": "GOOGL",
            "quantity": 50,
            "avg_price": 2800.00,
            "current_price": 2750.00,
            "unrealized_pnl": -2500.00
        }
    ]
    
    for pos in positions:
        store.create("positions", pos)
    
    # Orders
    orders = [
        {
            "id": "tord-001",
            "account_id": "acc-001",
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 100,
            "order_type": "market",
            "limit_price": None,
            "status": "filled"
        }
    ]
    
    for order in orders:
        store.create("orders", order)


seed_trading_data()


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/accounts", response_model=List[TradingAccount], tags=["accounts"])
def list_accounts(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """List all trading accounts"""
    return store.list("accounts")


@app.get("/positions", response_model=List[Position], tags=["positions"])
def list_positions(
    account_id: Optional[str] = Query(None, description="Filter by account ID"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """List positions"""
    def filter_fn(pos):
        return account_id is None or pos.get("account_id") == account_id
    
    return store.list("positions", filter_fn if account_id else None)


@app.get("/orders", response_model=List[TradingOrder], tags=["orders"])
def list_orders(
    account_id: Optional[str] = Query(None, description="Filter by account ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """List trading orders"""
    def filter_fn(order):
        if account_id and order.get("account_id") != account_id:
            return False
        if status and order.get("status") != status:
            return False
        return True
    
    return store.list("orders", filter_fn if (account_id or status) else None)


@app.post("/orders", response_model=TradingOrder, status_code=201, tags=["orders"])
def place_order(
    order: TradingOrderCreate,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Place a new trading order"""
    order_data = order.model_dump()
    order_data["status"] = "pending"
    
    created = store.create("orders", order_data)
    return created


@app.get("/risk/summary", response_model=RiskSummary, tags=["risk"])
def get_risk_summary(
    account_id: str = Query(..., description="Account ID"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Get risk summary for an account"""
    account = store.get("accounts", account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Get positions for account
    positions = store.list("positions", lambda p: p.get("account_id") == account_id)
    
    # Calculate metrics
    total_exposure = sum(p.get("quantity", 0) * p.get("current_price", 0) for p in positions)
    balance = account.get("balance", 0)
    leverage = total_exposure / balance if balance > 0 else 0
    
    # Simplified margin calculation
    margin_used = total_exposure * 0.1  # 10% margin requirement
    margin_available = balance - margin_used
    
    return {
        "account_id": account_id,
        "total_exposure": round(total_exposure, 2),
        "leverage": round(leverage, 2),
        "margin_used": round(margin_used, 2),
        "margin_available": round(margin_available, 2),
        "positions_count": len(positions)
    }


@app.get("/health", tags=["system"])
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "mock-trading"}


# Run with: uvicorn meho_mock_systems.trading_app:app --port 8003 --reload

