"""
Unit tests for mock ProviderX News API.
"""
import pytest
from fastapi.testclient import TestClient
from meho_mock_systems.providerx_news_app import app, store


@pytest.fixture(autouse=True)
def reset_store():
    """Reset store before each test"""
    store.clear()
    from meho_mock_systems.providerx_news_app import seed_news
    seed_news()
    yield
    store.clear()


@pytest.mark.unit
def test_list_news():
    """Test listing news articles"""
    client = TestClient(app)
    response = client.get("/news")
    
    assert response.status_code == 200
    data = response.json()
    assert "news" in data
    assert data["total"] >= 4  # We seeded 4 articles


@pytest.mark.unit
def test_get_news():
    """Test getting a news article by ID"""
    client = TestClient(app)
    
    # Get first article
    list_response = client.get("/news")
    news_id = list_response.json()["news"][0]["id"]
    
    response = client.get(f"/news/{news_id}")
    
    assert response.status_code == 200
    assert response.json()["id"] == news_id


@pytest.mark.unit
def test_filter_news_by_category():
    """Test filtering news by category"""
    client = TestClient(app)
    
    response = client.get("/news?category=market")
    
    assert response.status_code == 200
    news_items = response.json()["news"]
    assert all(n["category"] == "market" for n in news_items)


@pytest.mark.unit
def test_news_sorted_by_date():
    """Test news is sorted by published_at descending"""
    client = TestClient(app)
    
    response = client.get("/news")
    
    assert response.status_code == 200
    news_items = response.json()["news"]
    
    # Should be in descending order (most recent first)
    for i in range(len(news_items) - 1):
        assert news_items[i]["published_at"] >= news_items[i + 1]["published_at"]


@pytest.mark.unit
def test_register_webhook():
    """Test registering a webhook"""
    client = TestClient(app)
    
    webhook = {
        "url": "https://example.com/webhook",
        "events": ["news.created", "news.updated"]
    }
    
    response = client.post("/webhooks/register", json=webhook)
    
    assert response.status_code == 201
    data = response.json()
    assert data["url"] == webhook["url"]
    assert data["events"] == webhook["events"]
    assert "id" in data


@pytest.mark.unit
def test_list_webhooks():
    """Test listing registered webhooks"""
    client = TestClient(app)
    
    # Register a webhook first
    webhook = {
        "url": "https://example.com/webhook",
        "events": ["news.created"]
    }
    client.post("/webhooks/register", json=webhook)
    
    # List webhooks
    response = client.get("/webhooks")
    
    assert response.status_code == 200
    webhooks = response.json()
    assert len(webhooks) >= 1

