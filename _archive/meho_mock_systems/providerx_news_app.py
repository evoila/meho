"""
Mock ProviderX News API for testing.

Provides news articles and webhook registration.
"""
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
from meho_mock_systems.base import InMemoryStore, generate_id

app = FastAPI(
    title="Mock ProviderX News API",
    description="Mock News Provider API for MEHO testing",
    version="1.0.0"
)

store = InMemoryStore()


# ============================================================================
# Models
# ============================================================================

class NewsItem(BaseModel):
    """News article"""
    id: str
    title: str
    content: str
    category: str = Field(..., description="Category (e.g., market, regulation, technology)")
    source: str
    published_at: str
    tags: List[str] = Field(default_factory=list)


class NewsWebhook(BaseModel):
    """Webhook registration"""
    url: str
    events: List[str] = Field(..., description="Events to subscribe to (e.g., news.created)")


class NewsWebhookRegistration(BaseModel):
    """Webhook registration response"""
    id: str
    url: str
    events: List[str]
    created_at: str


class NewsList(BaseModel):
    """Paginated news list"""
    news: List[NewsItem]
    total: int
    limit: int
    offset: int


# ============================================================================
# Seed Data
# ============================================================================

def seed_news():
    """Populate with sample news articles"""
    from datetime import UTC
    base_date = datetime.now(UTC)
    
    sample_news = [
        {
            "id": "news-001",
            "title": "Market Rally Continues",
            "content": "Stock markets around the world continued their upward trend today...",
            "category": "market",
            "source": "ProviderX Financial",
            "published_at": (base_date - timedelta(days=1)).isoformat() + "Z",
            "tags": ["markets", "stocks", "rally"]
        },
        {
            "id": "news-002",
            "title": "New Financial Regulations Announced",
            "content": "Regulatory bodies announced new compliance requirements for financial institutions...",
            "category": "regulation",
            "source": "ProviderX Regulatory",
            "published_at": (base_date - timedelta(days=3)).isoformat() + "Z",
            "tags": ["regulation", "compliance"]
        },
        {
            "id": "news-003",
            "title": "AI Technology Disrupting Finance",
            "content": "Artificial intelligence is transforming how financial services operate...",
            "category": "technology",
            "source": "ProviderX Tech",
            "published_at": (base_date - timedelta(hours=6)).isoformat() + "Z",
            "tags": ["technology", "ai", "fintech"]
        },
        {
            "id": "news-004",
            "title": "Q4 Earnings Season Kicks Off",
            "content": "Major corporations begin reporting Q4 2024 earnings this week...",
            "category": "market",
            "source": "ProviderX Financial",
            "published_at": (base_date - timedelta(hours=2)).isoformat() + "Z",
            "tags": ["earnings", "q4", "markets"]
        }
    ]
    
    for news in sample_news:
        store.create("news", news)


seed_news()


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/news", response_model=NewsList, tags=["news"])
def list_news(
    category: Optional[str] = Query(None, description="Filter by category"),
    date_from: Optional[str] = Query(None, description="Filter by start date (ISO format)"),
    date_to: Optional[str] = Query(None, description="Filter by end date (ISO format)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """List news articles with optional filters"""
    def filter_fn(article):
        if category and article.get("category") != category:
            return False
        if date_from:
            article_date = datetime.fromisoformat(article.get("published_at", "").replace("Z", ""))
            if article_date < datetime.fromisoformat(date_from):
                return False
        if date_to:
            article_date = datetime.fromisoformat(article.get("published_at", "").replace("Z", ""))
            if article_date > datetime.fromisoformat(date_to):
                return False
        return True
    
    all_news = store.list("news", filter_fn if (category or date_from or date_to) else None)
    
    # Sort by published_at descending (most recent first)
    all_news.sort(
        key=lambda x: x.get("published_at", ""),
        reverse=True
    )
    
    total = len(all_news)
    news_page = all_news[offset:offset + limit]
    
    return {
        "news": news_page,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/news/{news_id}", response_model=NewsItem, tags=["news"])
def get_news(
    news_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Get a news article by ID"""
    news = store.get("news", news_id)
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    return news


@app.post("/webhooks/register", response_model=NewsWebhookRegistration, status_code=201, tags=["webhooks"])
def register_webhook(
    webhook: NewsWebhook,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Register a webhook for news events"""
    webhook_data = webhook.model_dump()
    created = store.create("webhooks", webhook_data)
    return created


@app.get("/webhooks", response_model=List[NewsWebhookRegistration], tags=["webhooks"])
def list_webhooks(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """List registered webhooks"""
    return store.list("webhooks")


@app.get("/health", tags=["system"])
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "mock-providerx-news"}


# Run with: uvicorn meho_mock_systems.providerx_news_app:app --port 8004 --reload

