# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

import random
import string
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int = 42
    products: int = 1000
    customers: int = 5000
    max_orders: int = 15000
    max_reviews: int = 7500
    indent: int = 2


class DataGenerator:
    """A faithful in-memory recreation of the original sales_data_generator.py."""

    def __init__(self, config: GeneratorConfig | None = None):
        if config is None:
            config = GeneratorConfig()
        self.config = config
        self.rng = random.Random(config.seed)

    def rand_bool(self) -> bool:
        return bool(self.rng.getrandbits(1))

    def rand_int(self, lo: int, hi: int) -> int:
        return self.rng.randint(lo, hi)

    def rand_float_money(self, lo: float = 1.0, hi: float = 9999.99) -> float:
        return round(self.rng.uniform(lo, hi), 2)

    def rand_string(self, prefix: str, length: int = 8) -> str:
        suffix = "".join(self.rng.choices(string.ascii_uppercase + string.digits, k=length))
        return f"{prefix}_{suffix}"

    def rand_text(self, words: int = 10) -> str:
        vocab = [
            "fast",
            "slow",
            "reliable",
            "excellent",
            "average",
            "poor",
            "durable",
            "cheap",
            "premium",
            "value",
            "quality",
            "shipping",
            "service",
            "packaging",
            "easy",
            "smooth",
            "recommend",
            "issue",
            "refund",
            "support",
            "happy",
            "satisfied",
        ]
        return (" ".join(self.rng.choices(vocab, k=words))).capitalize()

    def maybe_null(self, value: Any, p_null: float = 0.1) -> Any:
        return None if self.rng.random() < p_null else value

    def rand_unicode_string(self) -> str:
        samples = [
            "Ñoño España",
            "日本語テスト",
            "Привет мир",
            "مرحبا بالعالم",
            "🎉 Sale! 50% off 🔥",
            "Ümlauts: äöü ÄÖÜ ß",
            "Cześć świat",
            "Ελληνικά",
            "עברית",
            "ไทย",
            "Line1\nLine2\tTabbed",
            'Quote: "hello"',
            "Backslash: C:\\Users\\data",
            "Mixed: Tëst™ © 2025 — Pro·duct",
            "Emoji combo: 👨‍👩‍👧‍👦 🏳️‍🌈",
            "Zero-width: a\u200bb\u200cc",
            "Control: \u0001\u0002",
        ]
        return self.rng.choice(samples)

    def rand_edge_number(self) -> Any:
        edge_cases = [
            0,
            -0,
            1,
            -1,
            42,
            -42,
            0.0,
            -0.0,
            0.1,
            -0.1,
            0.000001,
            -0.000001,
            999999999999999,
            -999999999999999,
            1e10,
            -1e10,
            1e-10,
            -1e-10,
            3.141592653589793,
            2.718281828459045,
            1.7976931348623157e308,
            -1.7976931348623157e308,
            2.2250738585072014e-308,
        ]
        return self.rng.choice(edge_cases)

    def make_product(self, idx: int) -> dict[str, Any]:
        categories = [
            "Electronics",
            "Books",
            "Home",
            "Fashion",
            "Sports",
            "Toys",
            "Grocery",
        ]
        brands = ["Acme", "Nimbus", "Orion", "Kite", "Solace", "Vertex", "Cedar"]
        category = self.rng.choice(categories)
        return {
            "product_id": f"P{idx:06d}",
            "name": self.rand_string("Product", 6),
            "brand": self.rng.choice(brands),
            "category": category,
            "price": self.rand_float_money(2.0, 2500.0),
            "active": self.rand_bool(),
            "attributes": {
                "color": self.rng.choice(["black", "white", "red", "blue", "green", None]),
                "size": self.rng.choice(["XS", "S", "M", "L", "XL", None]),
                "weight_grams": self.rand_int(50, 8000),
                "dimensions_cm": [
                    round(self.rng.uniform(1.0, 120.0), 1),
                    round(self.rng.uniform(1.0, 120.0), 1),
                    round(self.rng.uniform(1.0, 120.0), 1),
                ],
                "tags": self.rng.sample(categories, k=self.rng.randint(1, 3)),
            },
            "inventory": {
                "warehouse_bins": [
                    {
                        "bin": f"A-{self.rng.randint(1, 40)}",
                        "qty": self.rand_int(0, 300),
                    },
                    {
                        "bin": f"B-{self.rng.randint(1, 40)}",
                        "qty": self.rand_int(0, 300),
                    },
                ],
                "backorder_allowed": self.rand_bool(),
            },
            "localized_names": {
                "en": self.rand_string("Product", 6),
                "es": f"Producto_{self.rand_string('ES', 4)} — España",
                "ja": f"製品_{self.rand_string('JP', 4)}",
                "de": f"Produkt_{self.rand_string('DE', 4)} für Österreich",
            },
        }

    def make_customer(self, idx: int) -> dict[str, Any]:
        countries = [
            "US",
            "UK",
            "DE",
            "FR",
            "ES",
            "IT",
            "NL",
            "SE",
            "JP",
            "CA",
            "AU",
            "BA",
        ]
        segments = ["consumer", "small_business", "enterprise"]
        return {
            "customer_id": f"C{idx:06d}",
            "full_name": self.rand_string("Customer", 6),
            "email": f"user{idx}@example.com",
            "country": self.rng.choice(countries),
            "segment": self.rng.choice(segments),
            "marketing_opt_in": self.rand_bool(),
            "loyalty": {
                "tier": self.rng.choice(["none", "silver", "gold", "platinum"]),
                "points": self.rand_int(0, 50_000),
            },
            "address_book": {
                "default_shipping": {
                    "street": self.rand_string("Street", 5),
                    "city": self.rand_string("City", 4),
                    "postal_code": f"{self.rng.randint(10000, 99999)}",
                },
                "saved_addresses": [
                    {
                        "label": self.rng.choice(["home", "work", "gift"]),
                        "street": self.rand_string("Street", 5),
                        "city": self.rand_string("City", 4),
                        "postal_code": f"{self.rng.randint(10000, 99999)}",
                    }
                    for _ in range(self.rng.randint(0, 3))
                ],
            },
            **self._make_sparse_customer_fields(),
        }

    def _make_sparse_customer_fields(self) -> dict[str, Any]:
        fields = {}
        if self.rng.random() < 0.3:
            fields["phone"] = (
                f"+1-{self.rng.randint(100, 999)}-{self.rng.randint(100, 999)}-{self.rng.randint(1000, 9999)}"
            )
        if self.rng.random() < 0.2:
            fields["company_name"] = self.rand_string("Company", 8)
            fields["vat_number"] = f"VAT{self.rng.randint(100000000, 999999999)}"
        if self.rng.random() < 0.4:
            fields["preferences"] = {
                "newsletter": self.rand_bool(),
                "sms_alerts": self.rand_bool(),
                "language": self.rng.choice(["en", "es", "de", "fr", "ja"]),
            }
        if self.rng.random() < 0.1:
            fields["notes"] = self.rand_unicode_string()
        return fields

    def make_order(
        self, idx: int, customer: dict[str, Any], products: list[dict[str, Any]]
    ) -> dict[str, Any]:
        status = self.rng.choice(["pending", "shipped", "delivered", "cancelled", "returned"])
        item_count = self.rand_int(1, 10)
        chosen_products = [self.rng.choice(products) for _ in range(item_count)]

        items = []
        for p in chosen_products:
            qty = self.rand_int(1, 8)
            unit_price = float(p["price"])
            discount_rate = round(self.rng.uniform(0.0, 0.30), 2)
            items.append(
                {
                    "product_id": p["product_id"],
                    "product_name": p["name"],
                    "quantity": qty,
                    "unit_price": unit_price,
                    "discount_rate": discount_rate,
                    "line_total": round(qty * unit_price * (1.0 - discount_rate), 2),
                }
            )

        subtotal = round(sum(i["line_total"] for i in items), 2)
        tax = round(subtotal * 0.08, 2)
        shipping = round(self.rng.uniform(0.0, 35.0), 2)
        grand_total = round(subtotal + tax + shipping, 2)

        return {
            "order_id": f"O{idx:08d}",
            "customer": {
                "customer_id": customer["customer_id"],
                "segment": customer["segment"],
                "country": customer["country"],
            },
            "status": status,
            "items": items,
            "invoice": {
                "amounts": {"grand_total": grand_total},
                "breakdown": {"tax": tax, "shipping": shipping},
            },
            "edge_cases": {
                "deep_nest": (self.make_deeply_nested(32) if self.rng.random() < 0.1 else None),
                "polymorphic": (self.make_polymorphic_array() if self.rng.random() < 0.1 else None),
            },
        }

    def make_deeply_nested(self, depth: int) -> dict[str, Any]:
        if depth <= 0:
            return {"leaf": True, "val": self.rand_int(1, 100)}
        return {"level": depth, "nested": self.make_deeply_nested(depth - 1)}

    def make_polymorphic_array(self) -> list[dict[str, Any]]:
        return [
            {"type": "A", "val": self.rand_int(1, 100)},
            {"type": "B", "tags": ["x", "y"]},
            {"type": "C", "meta": {"active": self.rand_bool()}},
        ]

    def generate_batch(self) -> dict[str, Any]:
        """Generate the full dataset as a dictionary."""
        products = [self.make_product(i) for i in range(1, self.config.products + 1)]
        customers = [self.make_customer(i) for i in range(1, self.config.customers + 1)]

        orders = [
            self.make_order(i, self.rng.choice(customers), products)
            for i in range(1, self.config.max_orders + 1)
        ]

        reviews = [
            {
                "review_id": self.rand_string("REV", 10),
                "product_id": self.rng.choice(products)["product_id"],
                "customer_id": self.rng.choice(customers)["customer_id"],
                "rating": self.rand_int(1, 5),
            }
            for _ in range(1, self.config.max_reviews + 1)
        ]

        return {
            "metadata": {"seed": self.config.seed, "counts": vars(self.config)},
            "catalog": {"products": products, "customers": customers},
            "transactions": {"orders": orders, "reviews": reviews},
            "edge_cases": {
                "primitives": ["string", 42, 3.14, True, None],
                "all_numbers": [self.rand_edge_number() for _ in range(10)],
                "empty_obj": {},
                "empty_list": [],
            },
        }
