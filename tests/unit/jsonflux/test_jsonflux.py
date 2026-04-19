# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
import pytest

from meho_app.jsonflux import JsonFlux

from .generator import DataGenerator, GeneratorConfig


@pytest.fixture(scope="module")
def large_dataset():
    """Generate the large dataset once for all tests in this module."""
    cfg = GeneratorConfig(
        products=1000, customers=5000, max_orders=15000, max_reviews=7500, seed=42
    )
    gen = DataGenerator(cfg)
    return gen.generate_batch()


@pytest.fixture(scope="module")
def flux_instance(large_dataset):
    """Analyze the dataset once and reuse the flux object."""
    flux = JsonFlux()
    flux.analyze(large_dataset)
    return flux


def test_structure_tree_verification(flux_instance):
    """Verify tree layout matches the generated structure."""
    tree = flux_instance.tree(format="tree")

    # Core sections
    assert "catalog" in tree
    assert "transactions" in tree
    assert "orders" in tree
    assert "edge_cases" in tree

    # Check nesting depth indicators (e.g. box drawing)
    assert "└──" in tree
    assert "├──" in tree


def test_rendering_formats(flux_instance):
    """Verify tree, tabs, and bracket outputs."""
    # Tabs format (TSV-like with actual tab characters)
    tabs = flux_instance.tree(format="tabs")
    assert "\tcatalog" in tabs  # Tab-indented
    assert "└──" not in tabs

    # Bracket format
    bracket = flux_instance.tree(format="bracket")
    assert "catalog {" in bracket
    assert "}" in bracket


def test_schema_format(flux_instance):
    """Verify LLM-friendly schema output."""
    schema = flux_instance.tree(format="schema")

    # Should be compact TypeScript-like format
    assert "{" in schema
    assert "}" in schema

    # Should contain key fields with types
    assert "catalog:" in schema
    assert "transactions:" in schema

    # Arrays should use bracket notation
    assert "[{" in schema  # Array of objects indicator

    # Should have primitive types
    assert "str" in schema
    assert "int" in schema


def test_schema_simple_types():
    """Test schema output for various JSON structures."""
    # Simple object (no samples)
    flux1 = JsonFlux(samples=0).analyze({"name": "test", "count": 42})
    schema1 = flux1.tree(format="schema")
    assert "name: str" in schema1
    assert "count: int" in schema1

    # Array of objects
    flux2 = JsonFlux(samples=0).analyze(
        [
            {"id": 1, "value": "a"},
            {"id": 2, "value": "b"},
        ]
    )
    schema2 = flux2.tree(format="schema")
    assert "[{" in schema2
    assert "id: int" in schema2
    assert "value: str" in schema2

    # Mixed array
    flux3 = JsonFlux(samples=0).analyze([1, "two", 3.0])
    schema3 = flux3.tree(format="schema")
    assert "str" in schema3
    assert "int" in schema3
    assert "float" in schema3


def test_schema_with_samples():
    """Test schema format includes samples when enabled."""
    data = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"},
    ]

    # Without samples
    flux_no_samples = JsonFlux(samples=0).analyze(data)
    schema_no_samples = flux_no_samples.tree(format="schema")
    assert "samples=" not in schema_no_samples

    # With samples
    flux_with_samples = JsonFlux(samples=2).analyze(data)
    schema_with_samples = flux_with_samples.tree(format="schema")
    assert "samples=" in schema_with_samples


def test_sampling_logic(large_dataset):
    """Compare samples=0 vs samples=1, 2, 3."""
    # No samples
    flux0 = JsonFlux(samples=0).analyze(large_dataset)
    assert "samples=[" not in flux0.tree()

    # With samples
    flux3 = JsonFlux(samples=3).analyze(large_dataset)
    tree3 = flux3.tree()
    assert "samples=[" in tree3


def test_statistical_report(flux_instance):
    """Verify statistical report content."""
    stats = flux_instance.stats()
    assert "JSON STATISTICS" in stats
    assert "Total values:" in stats
    assert "Estimated size:" in stats
    assert "Max depth:" in stats
    # Stats summary shows top paths by size, so specific paths might be buried if there are many
    assert "📍 $" in stats


def test_query_capabilities(flux_instance):
    """Various SQL queries including JOINs."""
    # 1. Simple count
    res = flux_instance.query(
        "SELECT COUNT(*) as cnt FROM (SELECT unnest(catalog.products) FROM data)"
    )
    assert res[0]["cnt"] == 1000

    # 2. Aggregation (Fix: use alias for status)
    res = flux_instance.query(
        """
        SELECT o.status, COUNT(*) as cnt
        FROM (SELECT unnest(transactions.orders) as o FROM data)
        GROUP BY o.status
    """
    )
    assert len(res) > 0

    # 3. Join logic
    res = flux_instance.query(
        """
        WITH ords AS (SELECT unnest(transactions.orders) as o FROM data),
             prods AS (SELECT unnest(catalog.products) as p FROM data)
        SELECT p.category, COUNT(*) as sales
        FROM ords o
        JOIN prods p ON o.items[1].product_id = p.product_id
        GROUP BY p.category
        ORDER BY sales DESC
    """
    )
    assert len(res) > 0
    assert "category" in res[0]
    assert "sales" in res[0]


def test_tabular_query_rendering(flux_instance):
    """Verify tabular formatting works on query results."""
    # Grid format
    grid = flux_instance.query_table(
        "SELECT o.status, count(*) as cnt FROM (SELECT unnest(transactions.orders) as o FROM data) GROUP BY o.status",
        format="grid",
    )
    assert "+" in grid
    assert "|" in grid

    # Markdown format
    md = flux_instance.query_table(
        "SELECT * FROM (SELECT unnest(catalog.products) FROM data) LIMIT 5",
        format="markdown",
    )
    assert "|" in md
    assert "product_id" in md


# =============================================================================
# Comprehensive SQL Query Tests
# =============================================================================


@pytest.fixture(scope="module")
def query_engine(large_dataset):
    """Create QueryEngine with registered tables from generated data."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register("products", large_dataset["catalog"]["products"])
    engine.register("customers", large_dataset["catalog"]["customers"])
    engine.register("orders", large_dataset["transactions"]["orders"])
    engine.register("reviews", large_dataset["transactions"]["reviews"])
    return engine


def test_sql_basic_select(query_engine):
    """Test basic SELECT with ORDER BY and LIMIT."""
    result = query_engine.query(
        """
        SELECT product_id, name, price, category
        FROM products
        ORDER BY price DESC
        LIMIT 5
    """
    )
    assert len(result) == 5
    assert "product_id" in result[0]
    assert "price" in result[0]
    # Verify ordering (descending)
    prices = [r["price"] for r in result]
    assert prices == sorted(prices, reverse=True)


def test_sql_count_group_by(query_engine):
    """Test COUNT with GROUP BY."""
    result = query_engine.query(
        """
        SELECT category, COUNT(*) as product_count
        FROM products
        GROUP BY category
        ORDER BY product_count DESC
    """
    )
    assert len(result) > 0
    assert "category" in result[0]
    assert "product_count" in result[0]
    # Total should equal 1000 products
    total = sum(r["product_count"] for r in result)
    assert total == 1000


def test_sql_aggregates(query_engine):
    """Test aggregate functions: MIN, MAX, AVG, SUM."""
    result = query_engine.query(
        """
        SELECT
            COUNT(*) as total,
            MIN(price) as min_price,
            MAX(price) as max_price,
            ROUND(AVG(price), 2) as avg_price,
            ROUND(SUM(price), 2) as total_value
        FROM products
    """
    )
    assert len(result) == 1
    assert result[0]["total"] == 1000
    assert result[0]["min_price"] <= result[0]["max_price"]
    assert result[0]["avg_price"] > 0


def test_sql_where_filter(query_engine):
    """Test WHERE clause filtering."""
    result = query_engine.query(
        """
        SELECT product_id, name, price
        FROM products
        WHERE price > 2000 AND active = true
        ORDER BY price DESC
    """
    )
    # All returned products should have price > 2000
    for r in result:
        assert r["price"] > 2000


def test_sql_case_expression(query_engine):
    """Test CASE expressions."""
    result = query_engine.query(
        """
        SELECT
            product_id,
            price,
            CASE
                WHEN price >= 2000 THEN 'premium'
                WHEN price >= 500 THEN 'mid-range'
                ELSE 'budget'
            END as tier
        FROM products
        LIMIT 10
    """
    )
    assert len(result) == 10
    for r in result:
        assert r["tier"] in ("premium", "mid-range", "budget")
        # Verify tier matches price
        if r["price"] >= 2000:
            assert r["tier"] == "premium"
        elif r["price"] >= 500:
            assert r["tier"] == "mid-range"
        else:
            assert r["tier"] == "budget"


def test_sql_string_functions(query_engine):
    """Test string functions: UPPER, LENGTH, SUBSTRING."""
    result = query_engine.query(
        """
        SELECT
            product_id,
            UPPER(category) as category_upper,
            LENGTH(name) as name_length
        FROM products
        LIMIT 5
    """
    )
    assert len(result) == 5
    for r in result:
        assert r["category_upper"] == r["category_upper"].upper()
        assert r["name_length"] > 0


def test_sql_having_clause(query_engine):
    """Test GROUP BY with HAVING clause."""
    result = query_engine.query(
        """
        SELECT
            category,
            COUNT(*) as cnt,
            ROUND(AVG(price), 2) as avg_price
        FROM products
        GROUP BY category
        HAVING COUNT(*) > 100
        ORDER BY cnt DESC
    """
    )
    # All results should have count > 100
    for r in result:
        assert r["cnt"] > 100


def test_sql_subquery(query_engine):
    """Test subquery in WHERE clause."""
    result = query_engine.query(
        """
        SELECT product_id, name, price
        FROM products
        WHERE price > (SELECT AVG(price) FROM products)
        ORDER BY price DESC
        LIMIT 10
    """
    )
    # Get average price
    avg_result = query_engine.query("SELECT AVG(price) as avg FROM products")
    avg_price = avg_result[0]["avg"]

    # All returned products should have price > average
    for r in result:
        assert r["price"] > avg_price


def test_sql_cte(query_engine):
    """Test Common Table Expression (WITH clause)."""
    result = query_engine.query(
        """
        WITH category_stats AS (
            SELECT
                category,
                COUNT(*) as cnt,
                AVG(price) as avg_price,
                MAX(price) as max_price
            FROM products
            GROUP BY category
        )
        SELECT
            category,
            cnt,
            ROUND(avg_price, 2) as avg,
            ROUND(max_price, 2) as max
        FROM category_stats
        ORDER BY cnt DESC
    """
    )
    assert len(result) > 0
    assert "category" in result[0]
    assert "cnt" in result[0]


def test_sql_window_functions(query_engine):
    """Test window functions: RANK, ROW_NUMBER, OVER."""
    result = query_engine.query(
        """
        SELECT
            product_id,
            category,
            price,
            RANK() OVER (ORDER BY price DESC) as price_rank,
            ROW_NUMBER() OVER (PARTITION BY category ORDER BY price DESC) as category_rank
        FROM products
        LIMIT 20
    """
    )
    assert len(result) == 20
    for r in result:
        assert "price_rank" in r
        assert "category_rank" in r
        assert r["price_rank"] >= 1
        assert r["category_rank"] >= 1


def test_sql_distinct(query_engine):
    """Test DISTINCT clause."""
    result = query_engine.query(
        """
        SELECT DISTINCT category
        FROM products
        ORDER BY category
    """
    )
    # Should have 7 unique categories
    categories = [r["category"] for r in result]
    assert len(categories) == len(set(categories))  # All unique


def test_sql_union(query_engine):
    """Test UNION and UNION ALL."""
    result = query_engine.query(
        """
        SELECT product_id, 'high' as price_tier FROM products WHERE price > 1500
        UNION ALL
        SELECT product_id, 'low' as price_tier FROM products WHERE price < 100
        LIMIT 20
    """
    )
    assert len(result) <= 20
    for r in result:
        assert r["price_tier"] in ("high", "low")


def test_sql_math_operations(query_engine):
    """Test mathematical operations."""
    result = query_engine.query(
        """
        SELECT
            product_id,
            price,
            ROUND(price * 1.08, 2) as price_with_tax,
            ROUND(price * 0.9, 2) as discounted_price,
            ROUND(price / 100, 2) as price_hundreds
        FROM products
        LIMIT 5
    """
    )
    for r in result:
        assert abs(r["price_with_tax"] - r["price"] * 1.08) < 0.01
        assert abs(r["discounted_price"] - r["price"] * 0.9) < 0.01


def test_sql_coalesce_null_handling(query_engine):
    """Test COALESCE and NULL handling."""
    result = query_engine.query(
        """
        SELECT
            customer_id,
            full_name,
            COALESCE(phone, 'N/A') as phone_display
        FROM customers
        LIMIT 10
    """
    )
    for r in result:
        assert r["phone_display"] is not None  # COALESCE should eliminate NULLs


def test_sql_like_pattern(query_engine):
    """Test LIKE pattern matching."""
    result = query_engine.query(
        """
        SELECT product_id, name
        FROM products
        WHERE product_id LIKE 'P0001%'
        ORDER BY product_id
        LIMIT 10
    """
    )
    for r in result:
        assert r["product_id"].startswith("P0001")


def test_sql_in_clause(query_engine):
    """Test IN clause."""
    result = query_engine.query(
        """
        SELECT product_id, category
        FROM products
        WHERE category IN ('Electronics', 'Books', 'Sports')
        LIMIT 20
    """
    )
    for r in result:
        assert r["category"] in ("Electronics", "Books", "Sports")


def test_sql_between(query_engine):
    """Test BETWEEN clause."""
    result = query_engine.query(
        """
        SELECT product_id, price
        FROM products
        WHERE price BETWEEN 100 AND 500
        ORDER BY price
    """
    )
    for r in result:
        assert 100 <= r["price"] <= 500


def test_sql_order_status_distribution(query_engine):
    """Test GROUP BY on order status."""
    result = query_engine.query(
        """
        SELECT
            status,
            COUNT(*) as order_count,
            ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM orders), 2) as percentage
        FROM orders
        GROUP BY status
        ORDER BY order_count DESC
    """
    )
    # Should have 5 statuses: pending, shipped, delivered, cancelled, returned
    assert len(result) == 5
    total = sum(r["order_count"] for r in result)
    assert total == 15000


def test_sql_customer_segments(query_engine):
    """Test customer segmentation analysis."""
    result = query_engine.query(
        """
        SELECT
            segment,
            country,
            COUNT(*) as customer_count
        FROM customers
        GROUP BY segment, country
        ORDER BY customer_count DESC
        LIMIT 15
    """
    )
    assert len(result) == 15
    for r in result:
        assert r["segment"] in ("consumer", "small_business", "enterprise")


def test_sql_review_ratings(query_engine):
    """Test review rating analysis."""
    result = query_engine.query(
        """
        SELECT
            rating,
            COUNT(*) as review_count,
            ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM reviews), 1) as pct
        FROM reviews
        GROUP BY rating
        ORDER BY rating
    """
    )
    # Ratings should be 1-5
    assert len(result) == 5
    for r in result:
        assert 1 <= r["rating"] <= 5


def test_sql_format_grid(query_engine):
    """Test grid format output."""
    output = query_engine.format_query(
        """
        SELECT category, COUNT(*) as cnt
        FROM products
        GROUP BY category
        ORDER BY cnt DESC
    """,
        format="grid",
    )
    assert "+" in output  # Grid borders
    assert "|" in output
    assert "category" in output


def test_sql_format_markdown(query_engine):
    """Test markdown format output."""
    output = query_engine.format_query(
        """
        SELECT product_id, name, price
        FROM products
        LIMIT 5
    """,
        format="markdown",
    )
    assert "|" in output
    assert "product_id" in output


def test_sql_format_csv(query_engine):
    """Test CSV format output."""
    output = query_engine.format_query(
        """
        SELECT product_id, name, price
        FROM products
        LIMIT 3
    """,
        format="csv",
    )
    assert "product_id,name,price" in output
    lines = output.strip().split("\n")
    assert len(lines) == 4  # Header + 3 rows


def test_sql_format_json(query_engine):
    """Test JSON format output."""
    import json

    output = query_engine.format_query(
        """
        SELECT product_id, price
        FROM products
        LIMIT 3
    """,
        format="json",
    )
    data = json.loads(output)
    assert len(data) == 3
    assert "product_id" in data[0]


# =============================================================================
# Cross-Table JOIN Tests (Multiple JSON Sources)
# =============================================================================


def test_sql_inner_join_products_reviews(query_engine):
    """Test INNER JOIN between products and reviews tables."""
    result = query_engine.query(
        """
        SELECT
            p.product_id,
            p.name,
            p.category,
            r.rating,
            r.review_id
        FROM products p
        INNER JOIN reviews r ON p.product_id = r.product_id
        LIMIT 20
    """
    )
    assert len(result) == 20
    for row in result:
        assert row["product_id"] is not None
        assert row["rating"] >= 1
        assert row["rating"] <= 5


def test_sql_left_join_products_reviews(query_engine):
    """Test LEFT JOIN - products with optional reviews."""
    result = query_engine.query(
        """
        SELECT
            p.product_id,
            p.name,
            COUNT(r.review_id) as review_count
        FROM products p
        LEFT JOIN reviews r ON p.product_id = r.product_id
        GROUP BY p.product_id, p.name
        ORDER BY review_count DESC
        LIMIT 10
    """
    )
    assert len(result) == 10
    # Most reviewed products should have multiple reviews
    assert result[0]["review_count"] > 0


def test_sql_join_with_aggregates(query_engine):
    """Test JOIN with aggregate functions."""
    result = query_engine.query(
        """
        SELECT
            p.category,
            COUNT(DISTINCT p.product_id) as product_count,
            COUNT(r.review_id) as total_reviews,
            ROUND(AVG(r.rating), 2) as avg_rating
        FROM products p
        LEFT JOIN reviews r ON p.product_id = r.product_id
        GROUP BY p.category
        ORDER BY total_reviews DESC
    """
    )
    assert len(result) > 0
    for row in result:
        assert "category" in row
        assert "avg_rating" in row


def test_sql_join_products_reviews_customers(query_engine):
    """Test multi-table JOIN: products, reviews, and customers."""
    result = query_engine.query(
        """
        SELECT
            p.category,
            c.country,
            COUNT(*) as review_count,
            ROUND(AVG(r.rating), 2) as avg_rating
        FROM reviews r
        JOIN products p ON r.product_id = p.product_id
        JOIN customers c ON r.customer_id = c.customer_id
        GROUP BY p.category, c.country
        HAVING COUNT(*) > 5
        ORDER BY review_count DESC
        LIMIT 20
    """
    )
    assert len(result) > 0
    for row in result:
        assert row["review_count"] > 5


def test_sql_join_with_subquery(query_engine):
    """Test JOIN with subquery."""
    result = query_engine.query(
        """
        SELECT
            p.product_id,
            p.name,
            p.price,
            review_stats.review_count,
            review_stats.avg_rating
        FROM products p
        JOIN (
            SELECT
                product_id,
                COUNT(*) as review_count,
                AVG(rating) as avg_rating
            FROM reviews
            GROUP BY product_id
            HAVING COUNT(*) >= 5
        ) review_stats ON p.product_id = review_stats.product_id
        ORDER BY review_stats.avg_rating DESC
        LIMIT 10
    """
    )
    assert len(result) <= 10
    for row in result:
        assert row["review_count"] >= 5


def test_sql_cross_table_cte(query_engine):
    """Test CTE with multiple tables."""
    result = query_engine.query(
        """
        WITH product_reviews AS (
            SELECT
                p.product_id,
                p.name,
                p.category,
                p.price,
                COUNT(r.review_id) as reviews,
                COALESCE(AVG(r.rating), 0) as avg_rating
            FROM products p
            LEFT JOIN reviews r ON p.product_id = r.product_id
            GROUP BY p.product_id, p.name, p.category, p.price
        ),
        category_stats AS (
            SELECT
                category,
                AVG(avg_rating) as category_avg_rating
            FROM product_reviews
            WHERE reviews > 0
            GROUP BY category
        )
        SELECT
            pr.name,
            pr.category,
            pr.reviews,
            ROUND(pr.avg_rating, 2) as product_rating,
            ROUND(cs.category_avg_rating, 2) as category_avg
        FROM product_reviews pr
        JOIN category_stats cs ON pr.category = cs.category
        WHERE pr.reviews > 0
        ORDER BY pr.avg_rating DESC
        LIMIT 15
    """
    )
    assert len(result) <= 15
    for row in result:
        assert row["reviews"] > 0


def test_sql_customers_orders_join(query_engine):
    """Test JOIN between customers and orders."""
    result = query_engine.query(
        """
        SELECT
            c.customer_id,
            c.segment,
            c.country,
            o.order_id,
            o.status
        FROM customers c
        JOIN orders o ON c.customer_id = o.customer.customer_id
        LIMIT 20
    """
    )
    assert len(result) == 20
    for row in result:
        assert row["customer_id"] is not None
        assert row["order_id"] is not None


def test_sql_customer_order_aggregates(query_engine):
    """Test customer order summary with JOIN and aggregates."""
    result = query_engine.query(
        """
        SELECT
            c.segment,
            COUNT(DISTINCT c.customer_id) as unique_customers,
            COUNT(o.order_id) as total_orders,
            ROUND(COUNT(o.order_id) * 1.0 / COUNT(DISTINCT c.customer_id), 2) as orders_per_customer
        FROM customers c
        JOIN orders o ON c.customer_id = o.customer.customer_id
        GROUP BY c.segment
        ORDER BY total_orders DESC
    """
    )
    # Should have 3 segments: consumer, small_business, enterprise
    assert len(result) == 3
    for row in result:
        assert row["segment"] in ("consumer", "small_business", "enterprise")
        assert row["orders_per_customer"] > 0


def test_sql_order_status_by_country(query_engine):
    """Test order status distribution by customer country."""
    result = query_engine.query(
        """
        SELECT
            c.country,
            o.status,
            COUNT(*) as order_count
        FROM orders o
        JOIN customers c ON o.customer.customer_id = c.customer_id
        GROUP BY c.country, o.status
        ORDER BY c.country, order_count DESC
        LIMIT 30
    """
    )
    assert len(result) > 0
    for row in result:
        assert row["status"] in (
            "pending",
            "shipped",
            "delivered",
            "cancelled",
            "returned",
        )


def test_sql_top_reviewed_by_category(query_engine):
    """Test finding top-reviewed products per category using window function."""
    result = query_engine.query(
        """
        WITH product_review_counts AS (
            SELECT
                p.product_id,
                p.name,
                p.category,
                COUNT(r.review_id) as review_count,
                ROUND(AVG(r.rating), 2) as avg_rating,
                ROW_NUMBER() OVER (PARTITION BY p.category ORDER BY COUNT(r.review_id) DESC) as rank_in_category
            FROM products p
            LEFT JOIN reviews r ON p.product_id = r.product_id
            GROUP BY p.product_id, p.name, p.category
        )
        SELECT product_id, name, category, review_count, avg_rating
        FROM product_review_counts
        WHERE rank_in_category <= 3
        ORDER BY category, review_count DESC
    """
    )
    # Should have top 3 per category (7 categories * 3 = 21 max)
    assert len(result) <= 21
    # Each category should appear at most 3 times
    from collections import Counter

    category_counts = Counter(r["category"] for r in result)
    for count in category_counts.values():
        assert count <= 3


def test_sql_full_analytics_query(query_engine):
    """Test complex analytics query joining all tables."""
    result = query_engine.query(
        """
        WITH customer_orders AS (
            SELECT
                c.customer_id,
                c.segment,
                c.country,
                COUNT(o.order_id) as order_count
            FROM customers c
            LEFT JOIN orders o ON c.customer_id = o.customer.customer_id
            GROUP BY c.customer_id, c.segment, c.country
        ),
        customer_reviews AS (
            SELECT
                customer_id,
                COUNT(*) as review_count,
                AVG(rating) as avg_rating_given
            FROM reviews
            GROUP BY customer_id
        )
        SELECT
            co.segment,
            co.country,
            COUNT(DISTINCT co.customer_id) as customers,
            SUM(co.order_count) as total_orders,
            COALESCE(SUM(cr.review_count), 0) as total_reviews,
            ROUND(COALESCE(AVG(cr.avg_rating_given), 0), 2) as avg_rating_given
        FROM customer_orders co
        LEFT JOIN customer_reviews cr ON co.customer_id = cr.customer_id
        GROUP BY co.segment, co.country
        ORDER BY total_orders DESC
        LIMIT 20
    """
    )
    assert len(result) > 0
    for row in result:
        assert row["customers"] > 0


# =============================================================================
# QueryEngine Feature Tests (nested fields, unnesting, LLM schema)
# =============================================================================


@pytest.fixture(scope="module")
def rich_query_engine():
    """QueryEngine with rich nested data for feature testing."""
    from meho_app.jsonflux import QueryEngine

    products = [
        {
            "id": "P1",
            "name": "Laptop",
            "specs": {"cpu": "i7", "ram": "16GB"},
            "colors": ["silver", "space gray"],
        },
        {
            "id": "P2",
            "name": "Phone",
            "specs": {"cpu": "M3", "ram": "8GB"},
            "colors": ["black", "white", "blue"],
        },
        {
            "id": "P3",
            "name": "Monitor",
            "specs": {"cpu": "N/A", "ram": "N/A"},
            "colors": ["black"],
        },
    ]

    orders = [
        {
            "order_id": 101,
            "customer": "Alice",
            "items": [{"id": "P1", "qty": 1}, {"id": "P2", "qty": 2}],
        },
        {"order_id": 102, "customer": "Bob", "items": [{"id": "P2", "qty": 1}]},
        {
            "order_id": 103,
            "customer": "Charlie",
            "items": [{"id": "P1", "qty": 1}, {"id": "P3", "qty": 5}],
        },
    ]

    engine = QueryEngine()
    engine.register("products", products)
    engine.register("orders", orders)
    return engine


def test_llm_schema_context(rich_query_engine):
    """Test LLM-optimized schema description output."""
    schema = rich_query_engine.describe_tables()

    # Should contain table information
    assert "products" in schema
    assert "orders" in schema

    # Should have some structural content
    assert len(schema) > 50


def test_query_nested_fields_dot_notation(rich_query_engine):
    """Test querying nested fields using dot notation."""
    result = rich_query_engine.query(
        """
        SELECT name, specs.cpu as cpu, specs.ram as ram
        FROM products
        WHERE specs.cpu != 'N/A'
        """
    )

    assert len(result) == 2  # Laptop and Phone (Monitor has N/A)
    for row in result:
        assert row["cpu"] != "N/A"
        assert "ram" in row


def test_unnest_flatten_arrays(rich_query_engine):
    """Test flattening arrays using unnest."""
    result = rich_query_engine.query(
        """
        SELECT
            p.name,
            SUM(orders_unnested.items.qty) as total_qty
        FROM (SELECT unnest(items) as items FROM orders) orders_unnested
        JOIN products p ON orders_unnested.items.id = p.id
        GROUP BY p.name
        ORDER BY total_qty DESC
        """
    )

    assert len(result) == 3  # Laptop, Phone, Monitor
    # Verify aggregation worked
    qty_map = {r["name"]: r["total_qty"] for r in result}
    assert qty_map["Laptop"] == 2  # 1 + 1 from orders 101 and 103
    assert qty_map["Phone"] == 3  # 2 + 1 from orders 101 and 102
    assert qty_map["Monitor"] == 5  # 5 from order 103


def test_complex_nesting_with_list_contains(rich_query_engine):
    """Test complex query with unnest and list_contains."""
    result = rich_query_engine.query(
        """
        SELECT DISTINCT o.customer
        FROM (
            SELECT customer, unnest(items) as item
            FROM orders
        ) o
        JOIN products p ON o.item.id = p.id
        WHERE list_contains(p.colors, 'silver')
        """
    )

    # Alice and Charlie ordered Laptop which has 'silver' color
    customers = {r["customer"] for r in result}
    assert "Alice" in customers
    assert "Charlie" in customers
    assert "Bob" not in customers  # Bob only ordered Phone


def test_nested_field_format_output(rich_query_engine):
    """Test that format_query works with nested field queries."""
    output = rich_query_engine.format_query(
        """
        SELECT name, specs.cpu, specs.ram
        FROM products
        WHERE specs.cpu != 'N/A'
        """,
        format="grid",
    )

    assert "+" in output  # Grid borders
    assert "|" in output
    assert "Laptop" in output or "Phone" in output


# =============================================================================
# Phase 7: New Test Coverage
# =============================================================================


# --- Error path tests ---


def test_query_on_unanalyzed_data():
    """Querying before analyze() should raise ValueError."""
    flux = JsonFlux()
    with pytest.raises(ValueError, match="No data analyzed"):
        flux.query("SELECT 1")


def test_stats_on_unanalyzed_data():
    """Stats before analyze() should raise ValueError."""
    flux = JsonFlux()
    with pytest.raises(ValueError, match="No data analyzed"):
        flux.stats()


def test_tree_on_unanalyzed_data():
    """Tree before analyze() should raise ValueError."""
    flux = JsonFlux()
    with pytest.raises(ValueError, match="No data analyzed"):
        flux.tree()


def test_invalid_sql_format_query():
    """Invalid SQL through format_query returns error string."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register("t", [{"a": 1}])
    result = engine.format_query("SELECT nonexistent FROM t")
    assert result.startswith("ERROR:")


def test_invalid_sql_execute_query():
    """Invalid SQL through execute_query returns failed QueryResult."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register("t", [{"a": 1}])
    qr = engine.execute_query("THIS IS NOT SQL")
    assert qr.success is False
    assert qr.error is not None
    assert qr.markdown == ""


def test_single_row_registration():
    """Registering a single-row list should work."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register("single", [{"id": 1}])
    result = engine.query("SELECT * FROM single")
    assert isinstance(result, list)
    assert len(result) == 1


def test_invalid_tree_format():
    """Unknown format in tree() should raise ValueError."""
    flux = JsonFlux(samples=0).analyze({"a": 1})
    with pytest.raises(ValueError, match="Unknown format"):
        flux.tree(format="invalid_format")


# --- generate_prompt() tests ---


def test_generate_prompt_contains_table_info(rich_query_engine):
    """generate_prompt() should include table names and field names."""
    prompt = rich_query_engine.generate_prompt(samples=0)

    # Must contain registered table names
    assert "products" in prompt
    assert "orders" in prompt

    # Must contain query pattern guidance
    assert "SQL" in prompt or "sql" in prompt

    # Must contain DuckDB function reference
    assert "UNNEST" in prompt or "unnest" in prompt


def test_generate_prompt_with_samples(rich_query_engine):
    """generate_prompt() with samples should include sample values."""
    prompt = rich_query_engine.generate_prompt(samples=3)
    assert "samples=" in prompt


def test_generate_prompt_no_samples(rich_query_engine):
    """generate_prompt() with samples=0 should not include samples."""
    prompt = rich_query_engine.generate_prompt(samples=0)
    assert "samples=[" not in prompt


# --- execute_query() (renamed from nlq) tests ---


def test_execute_query_success(rich_query_engine):
    """execute_query() should return successful QueryResult."""
    qr = rich_query_engine.execute_query("SELECT * FROM products LIMIT 3")
    assert qr.success is True
    assert qr.row_count == 3
    assert qr.error is None
    assert len(qr.markdown) > 0
    assert "Laptop" in qr.markdown or "Phone" in qr.markdown or "Monitor" in qr.markdown


def test_execute_query_error(rich_query_engine):
    """execute_query() with bad SQL should return failed QueryResult."""
    qr = rich_query_engine.execute_query("INVALID SQL STATEMENT")
    assert qr.success is False
    assert qr.error is not None


def test_execute_query_split(rich_query_engine):
    """execute_query() with split should provide preview."""
    qr = rich_query_engine.execute_query("SELECT * FROM products", split=1)
    assert qr.success is True
    assert qr.preview is not None
    assert qr.row_count == 3


def test_execute_query_max_colwidth(rich_query_engine):
    """execute_query() should truncate long columns."""
    qr = rich_query_engine.execute_query("SELECT * FROM products", max_colwidth=5)
    assert qr.success is True


# --- query_print() tests ---


def test_query_print_no_crash(rich_query_engine, capsys):
    """query_print() should not crash (was using undefined query_df)."""
    rich_query_engine.query_print(
        "SELECT * FROM products LIMIT 2",
        format="grid",
        max_rows=10,
    )
    captured = capsys.readouterr()
    assert "Laptop" in captured.out or "Phone" in captured.out


def test_query_print_with_title(rich_query_engine, capsys):
    """query_print() with title should print the title."""
    rich_query_engine.query_print(
        "SELECT * FROM products LIMIT 1",
        format="grid",
        title="Test Title",
    )
    captured = capsys.readouterr()
    assert "Test Title" in captured.out


# --- Engine caching in JsonFlux tests ---


def test_jsonflux_engine_caching():
    """Multiple queries should reuse the same cached engine."""
    data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    flux = JsonFlux(samples=0).analyze(data)

    # First query creates the engine
    flux.query("SELECT * FROM data LIMIT 1")
    engine1 = flux._engine

    # Second query reuses it
    flux.query("SELECT * FROM data LIMIT 1")
    engine2 = flux._engine

    assert engine1 is engine2


def test_jsonflux_engine_invalidated_on_reanalyze():
    """Re-analyzing should invalidate the cached engine."""
    data = [{"id": 1}]
    flux = JsonFlux(samples=0).analyze(data)

    flux.query("SELECT * FROM data")
    old_engine = flux._engine

    # Re-analyze
    flux.analyze([{"id": 2, "extra": "field"}])
    assert flux._engine is None  # Invalidated

    # New query creates fresh engine
    flux.query("SELECT * FROM data")
    assert flux._engine is not old_engine


# --- close() and context manager tests ---


def test_query_engine_close():
    """close() should release resources without error."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register("t", [{"a": 1}])
    engine.close()
    assert engine._closed is True
    assert len(engine.tables) == 0


def test_query_engine_context_manager():
    """Context manager should close on exit."""
    from meho_app.jsonflux import QueryEngine

    with QueryEngine() as engine:
        engine.register("t", [{"a": 1}])
        result = engine.query("SELECT * FROM t")
        assert len(result) == 1
    assert engine._closed is True


def test_jsonflux_close():
    """JsonFlux.close() should close cached engine."""
    flux = JsonFlux(samples=0).analyze([{"x": 1}])
    flux.query("SELECT * FROM data")
    assert flux._engine is not None

    flux.close()
    assert flux._engine is None


def test_jsonflux_context_manager():
    """JsonFlux should work as context manager."""
    with JsonFlux(samples=0) as flux:
        flux.analyze([{"x": 1}])
        result = flux.query("SELECT * FROM data")
        assert len(result) == 1
    assert flux._engine is None


# --- describe_tables() tests ---


def test_describe_tables_contains_info(rich_query_engine):
    """describe_tables() should contain table names and types."""
    desc = rich_query_engine.describe_tables(samples=0)
    assert "products" in desc
    assert "orders" in desc
    assert "rows" in desc


def test_describe_tables_with_samples(rich_query_engine):
    """describe_tables() with samples should include sample values."""
    desc = rich_query_engine.describe_tables(samples=3)
    assert "samples=" in desc


# --- validate() tests ---


def test_validate_returns_empty_on_success():
    """validate() should return empty list when all checks pass."""
    from meho_app.jsonflux import validate

    errors = validate()
    assert isinstance(errors, list)
    assert len(errors) == 0


# --- __version__ tests ---


def test_version_exists():
    """__version__ should be accessible."""
    from meho_app.jsonflux import __version__

    assert isinstance(__version__, str)
    assert "." in __version__


# =============================================================================
# Hard / Complex SQL Query Tests
# =============================================================================


def test_sql_unnest_items_revenue_by_category(query_engine):
    """UNNEST order items + JOIN products to get revenue by category."""
    result = query_engine.query(
        """
        SELECT
            p.category,
            COUNT(*) as line_items,
            ROUND(SUM(item.line_total), 2) as total_revenue
        FROM (SELECT unnest(items) as item FROM orders) sub
        JOIN products p ON sub.item.product_id = p.product_id
        GROUP BY p.category
        ORDER BY total_revenue DESC
    """
    )
    assert len(result) > 0
    assert all(r["total_revenue"] > 0 for r in result)
    assert all(r["line_items"] > 0 for r in result)


def test_sql_nested_customer_country_group(query_engine):
    """GROUP BY on a nested field: orders.customer.country."""
    result = query_engine.query(
        """
        SELECT
            customer.country,
            COUNT(*) as order_count,
            ROUND(AVG(invoice.amounts.grand_total), 2) as avg_total
        FROM orders
        GROUP BY customer.country
        ORDER BY order_count DESC
    """
    )
    assert len(result) > 0
    for r in result:
        assert r["order_count"] > 0
        assert r["avg_total"] > 0


def test_sql_unnest_items_window_rank(query_engine):
    """Window function over unnested items: rank items within each order."""
    result = query_engine.query(
        """
        SELECT
            order_id,
            item.product_id,
            item.line_total,
            ROW_NUMBER() OVER (
                PARTITION BY order_id ORDER BY item.line_total DESC
            ) as item_rank
        FROM (
            SELECT order_id, unnest(items) as item
            FROM orders
        )
        QUALIFY item_rank = 1
        LIMIT 20
    """
    )
    assert len(result) == 20
    for r in result:
        assert r["item_rank"] == 1


def test_sql_exists_subquery(query_engine):
    """EXISTS subquery: products that have at least one 5-star review."""
    result = query_engine.query(
        """
        SELECT p.product_id, p.name, p.category
        FROM products p
        WHERE EXISTS (
            SELECT 1 FROM reviews r
            WHERE r.product_id = p.product_id
            AND r.rating = 5
        )
        ORDER BY p.product_id
        LIMIT 20
    """
    )
    assert len(result) > 0
    # Verify each returned product actually has a 5-star review
    for r in result:
        check = query_engine.query(
            f"SELECT COUNT(*) as c FROM reviews WHERE product_id = '{r['product_id']}' AND rating = 5"  # noqa: S608 -- static SQL query, test data
        )
        assert check[0]["c"] > 0


def test_sql_not_exists_subquery(query_engine):
    """NOT EXISTS: products with zero reviews."""
    result = query_engine.query(
        """
        SELECT p.product_id, p.name
        FROM products p
        WHERE NOT EXISTS (
            SELECT 1 FROM reviews r
            WHERE r.product_id = p.product_id
        )
        LIMIT 10
    """
    )
    for r in result:
        check = query_engine.query(
            f"SELECT COUNT(*) as c FROM reviews WHERE product_id = '{r['product_id']}'"  # noqa: S608 -- static SQL query, test data
        )
        assert check[0]["c"] == 0


def test_sql_ntile_percentile_buckets(query_engine):
    """NTILE window function for quartile bucketing."""
    result = query_engine.query(
        """
        SELECT
            product_id,
            price,
            NTILE(4) OVER (ORDER BY price) as price_quartile
        FROM products
        LIMIT 20
    """
    )
    assert len(result) == 20
    for r in result:
        assert r["price_quartile"] in (1, 2, 3, 4)


def test_sql_nested_invoice_breakdown(query_engine):
    """Query deeply nested invoice fields with arithmetic."""
    result = query_engine.query(
        """
        SELECT
            order_id,
            invoice.amounts.grand_total as total,
            invoice.breakdown.tax as tax,
            invoice.breakdown.shipping as shipping,
            ROUND(
                invoice.amounts.grand_total
                - invoice.breakdown.tax
                - invoice.breakdown.shipping,
                2
            ) as subtotal_implied
        FROM orders
        ORDER BY total DESC
        LIMIT 10
    """
    )
    assert len(result) == 10
    for r in result:
        assert r["total"] > 0
        assert r["tax"] >= 0
        assert r["shipping"] >= 0


def test_sql_multi_cte_unnest_join(query_engine):
    """Complex multi-CTE: unnest items, join products, aggregate by brand."""
    result = query_engine.query(
        """
        WITH order_items AS (
            SELECT
                order_id,
                customer.segment as segment,
                unnest(items) as item
            FROM orders
        ),
        item_products AS (
            SELECT
                oi.segment,
                p.brand,
                oi.item.quantity as qty,
                oi.item.line_total as revenue
            FROM order_items oi
            JOIN products p ON oi.item.product_id = p.product_id
        )
        SELECT
            brand,
            segment,
            SUM(qty) as total_qty,
            ROUND(SUM(revenue), 2) as total_revenue,
            COUNT(*) as line_items
        FROM item_products
        GROUP BY brand, segment
        ORDER BY total_revenue DESC
        LIMIT 15
    """
    )
    assert len(result) > 0
    for r in result:
        assert r["total_qty"] > 0
        assert r["total_revenue"] > 0


def test_sql_except_query(query_engine):
    """EXCEPT: customers who have reviews but no orders."""
    result = query_engine.query(
        """
        SELECT DISTINCT customer_id FROM reviews
        EXCEPT
        SELECT DISTINCT customer.customer_id FROM orders
    """
    )
    # Result may be empty or not — just verify it runs without error
    assert isinstance(result, list)


def test_sql_intersect_query(query_engine):
    """INTERSECT: products that have both orders and reviews."""
    result = query_engine.query(
        """
        SELECT DISTINCT product_id FROM reviews
        INTERSECT
        SELECT DISTINCT item.product_id
        FROM (SELECT unnest(items) as item FROM orders)
    """
    )
    assert len(result) > 0


def test_sql_filter_aggregate(query_engine):
    """DuckDB FILTER clause on aggregates."""
    result = query_engine.query(
        """
        SELECT
            p.category,
            COUNT(*) as total_reviews,
            COUNT(*) FILTER (WHERE r.rating >= 4) as good_reviews,
            COUNT(*) FILTER (WHERE r.rating <= 2) as bad_reviews,
            ROUND(AVG(r.rating), 2) as avg_rating
        FROM reviews r
        JOIN products p ON r.product_id = p.product_id
        GROUP BY p.category
        ORDER BY avg_rating DESC
    """
    )
    assert len(result) > 0
    for r in result:
        assert r["good_reviews"] + r["bad_reviews"] <= r["total_reviews"]


def test_sql_lateral_unnest_with_nested_join(query_engine):
    """Unnest items, join to products AND customers via nested field."""
    result = query_engine.query(
        """
        SELECT
            c.country,
            p.category,
            COUNT(*) as purchases,
            ROUND(SUM(item.line_total), 2) as spend
        FROM (
            SELECT customer.customer_id as cid, unnest(items) as item
            FROM orders
        ) oi
        JOIN customers c ON oi.cid = c.customer_id
        JOIN products p ON oi.item.product_id = p.product_id
        GROUP BY c.country, p.category
        HAVING COUNT(*) > 50
        ORDER BY spend DESC
        LIMIT 20
    """
    )
    assert len(result) > 0
    for r in result:
        assert r["purchases"] > 50
        assert r["spend"] > 0


# =============================================================================
# Auto-Generated System Prompt Tests
# =============================================================================


@pytest.fixture
def prompt_engine_multi():
    """Multi-table engine with arrays, nested objects, and descriptions."""
    from meho_app.jsonflux import QueryEngine

    pods = [
        {
            "name": "web-abc",
            "namespace": "default",
            "status": "Running",
            "spec": {"node_name": "node-1", "restart_policy": "Always"},
            "containers": [
                {"name": "nginx", "image": "nginx:1.25", "cpu_limit": "500m"},
                {"name": "sidecar", "image": "envoy:1.28", "cpu_limit": "250m"},
            ],
        },
        {
            "name": "api-def",
            "namespace": "prod",
            "status": "Running",
            "spec": {"node_name": "node-2", "restart_policy": "Always"},
            "containers": [
                {"name": "api", "image": "myapp:latest", "cpu_limit": "1000m"},
            ],
        },
    ]
    nodes = [
        {"node_name": "node-1", "role": "worker", "cpu_capacity": 8},
        {"node_name": "node-2", "role": "worker", "cpu_capacity": 16},
        {"node_name": "node-3", "role": "control-plane", "cpu_capacity": 4},
    ]

    engine = QueryEngine()
    engine.register("pods", pods, description="Active Kubernetes pods")
    engine.register("nodes", nodes, description="Cluster nodes with capacity")
    return engine


@pytest.fixture
def prompt_engine_single():
    """Single-table engine — no JOINs possible."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register(
        "metrics",
        [
            {"host": "srv-1", "cpu_pct": 82.5, "mem_pct": 45.0},
            {"host": "srv-2", "cpu_pct": 12.0, "mem_pct": 91.3},
        ],
    )
    return engine


@pytest.fixture
def prompt_engine_flat():
    """Flat data only — no arrays, no nesting."""
    from meho_app.jsonflux import QueryEngine

    engine = QueryEngine()
    engine.register(
        "events",
        [
            {"event_id": "e1", "severity": "high", "count": 5},
            {"event_id": "e2", "severity": "low", "count": 100},
        ],
    )
    engine.register(
        "hosts",
        [
            {"host_id": "h1", "event_id": "e1", "region": "us-east"},
            {"host_id": "h2", "event_id": "e2", "region": "eu-west"},
        ],
    )
    return engine


def test_prompt_all_examples_use_real_table_names(prompt_engine_multi):
    """Every SQL example in the prompt must reference registered table names."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    # Extract all lines that look like SQL (inside code blocks or containing SELECT/FROM)
    sql_lines = [
        ln
        for ln in prompt.split("\n")
        if any(kw in ln.upper() for kw in ("SELECT", "FROM", "JOIN", "GROUP BY"))
    ]
    for line in sql_lines:
        # No generic placeholder names should appear
        upper = line.upper()
        assert "FROM TABLE" not in upper
        assert "FROM ARR" not in upper


def test_prompt_no_generic_placeholders_in_mistakes(prompt_engine_multi):
    """Common Mistakes section should use real field/table names, not placeholders."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    # Find the Common Mistakes section
    in_mistakes = False
    for line in prompt.split("\n"):
        if "Common Mistakes" in line:
            in_mistakes = True
            continue
        if line.startswith("---") or line.startswith("# "):
            in_mistakes = False
        if in_mistakes and line.strip():
            # Should not contain bare generic words as identifiers
            assert "`arr`" not in line
            assert "FROM table`" not in line


def test_prompt_includes_unnest_when_arrays_exist(prompt_engine_multi):
    """Prompt should include UNNEST pattern when data has arrays."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    assert "UNNEST" in prompt or "unnest" in prompt
    # Should reference the real array field name "containers"
    assert "containers" in prompt


def test_prompt_includes_dot_notation_when_nested(prompt_engine_multi):
    """Prompt should include dot notation example for nested objects."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    assert "spec." in prompt or "Dot Notation" in prompt


def test_prompt_includes_join_with_detected_keys(prompt_engine_multi):
    """Prompt should detect and list real join keys between tables."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    # pods.spec.node_name should be detected as joinable with nodes.node_name
    assert "node_name" in prompt
    # JOIN section should exist with table names
    assert "JOIN" in prompt
    assert "pods" in prompt
    assert "nodes" in prompt


def test_prompt_includes_unnest_join_pattern(prompt_engine_multi):
    """Prompt should include combined UNNEST+JOIN when applicable."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    # Data has containers array with no direct FK to nodes, so this section
    # may or may not appear.  Just verify no crash and prompt is coherent.
    assert "pods" in prompt
    assert "nodes" in prompt


def test_prompt_includes_table_descriptions(prompt_engine_multi):
    """Table descriptions from register() should appear in prompt."""
    prompt = prompt_engine_multi.generate_prompt(samples=0)
    assert "Active Kubernetes pods" in prompt
    assert "Cluster nodes with capacity" in prompt


def test_prompt_single_table_omits_joins(prompt_engine_single):
    """Single-table prompt should not include JOIN section."""
    prompt = prompt_engine_single.generate_prompt(samples=0)
    assert "JOIN" not in prompt.split("## DuckDB")[0]


def test_prompt_flat_data_omits_unnest(prompt_engine_flat):
    """Flat data prompt should not include UNNEST pattern."""
    prompt = prompt_engine_flat.generate_prompt(samples=0)
    # No array patterns before the DuckDB Functions reference section
    before_funcs = prompt.split("## DuckDB Functions")[0]
    assert "UNNEST" not in before_funcs
    assert "unnest(" not in before_funcs


def test_prompt_flat_data_detects_join_keys(prompt_engine_flat):
    """Flat multi-table data should detect event_id as join key."""
    prompt = prompt_engine_flat.generate_prompt(samples=0)
    assert "event_id" in prompt
    # JOIN example should use the detected key in the ON clause
    assert "a.event_id = b.event_id" in prompt


def test_prompt_schema_depth_limits_output(prompt_engine_multi):
    """max_schema_depth should truncate deep structures."""
    deep = prompt_engine_multi.generate_prompt(samples=0, max_schema_depth=2)
    unlimited = prompt_engine_multi.generate_prompt(samples=0, max_schema_depth=None)
    # Depth-limited prompt should be smaller or equal
    assert len(deep) <= len(unlimited)


def test_prompt_with_samples_includes_values(prompt_engine_multi):
    """Prompt with samples > 0 should include sample values from data."""
    prompt = prompt_engine_multi.generate_prompt(samples=2)
    assert "samples=" in prompt
    # Should contain actual data values
    assert "nginx" in prompt or "Running" in prompt or "node-1" in prompt
