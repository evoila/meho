"""Unit tests for Jinja2 custom filters."""

import json

import pytest
from jinja2 import Environment

from meho_core.templating.filters import (
    avg_attr,
    get_nested_attr,
    groupby_attr,
    json_dumps,
    register_custom_filters,
    selectattr_custom,
    sum_attr,
)


class TestGetNestedAttr:
    """Tests for get_nested_attr helper."""

    def test_simple_dict_access(self):
        """Should access simple dict keys."""
        obj = {"name": "test", "value": 42}
        assert get_nested_attr(obj, "name") == "test"
        assert get_nested_attr(obj, "value") == 42

    def test_nested_dict_access(self):
        """Should access nested dict keys."""
        obj = {
            "capacity": {
                "cpu": {
                    "total": {"value": 100}
                }
            }
        }
        assert get_nested_attr(obj, "capacity.cpu.total.value") == 100

    def test_missing_key_returns_none(self):
        """Should return None for missing keys."""
        obj = {"a": 1}
        assert get_nested_attr(obj, "b") is None
        assert get_nested_attr(obj, "a.b.c") is None

    def test_object_attribute_access(self):
        """Should access object attributes."""
        class MockObj:
            def __init__(self):
                self.name = "test"
                self.nested = MockObj2()
        
        class MockObj2:
            def __init__(self):
                self.value = 42
        
        obj = MockObj()
        assert get_nested_attr(obj, "name") == "test"
        assert get_nested_attr(obj, "nested.value") == 42


class TestSumAttr:
    """Tests for sum_attr filter."""

    def test_sum_simple_attr(self):
        """Should sum simple numeric attribute."""
        items = [
            {"value": 10},
            {"value": 20},
            {"value": 30},
        ]
        assert sum_attr(items, "value") == 60

    def test_sum_nested_attr(self):
        """Should sum nested attribute."""
        items = [
            {"capacity": {"cpu": 10.5}},
            {"capacity": {"cpu": 20.0}},
            {"capacity": {"cpu": 5.5}},
        ]
        assert sum_attr(items, "capacity.cpu") == 36.0

    def test_sum_empty_list(self):
        """Should return 0 for empty list."""
        assert sum_attr([], "value") == 0.0

    def test_sum_missing_attr(self):
        """Should use default for missing attributes."""
        items = [
            {"value": 10},
            {"other": 20},  # Missing 'value'
            {"value": 30},
        ]
        assert sum_attr(items, "value", default=0) == 40

    def test_sum_non_numeric_attr(self):
        """Should use default for non-numeric values."""
        items = [
            {"value": 10},
            {"value": "invalid"},
            {"value": 30},
        ]
        assert sum_attr(items, "value", default=0) == 40


class TestAvgAttr:
    """Tests for avg_attr filter."""

    def test_avg_simple_attr(self):
        """Should average simple numeric attribute."""
        items = [
            {"value": 10},
            {"value": 20},
            {"value": 30},
        ]
        assert avg_attr(items, "value") == 20.0

    def test_avg_nested_attr(self):
        """Should average nested attribute."""
        items = [
            {"util": {"cpu": 50.0}},
            {"util": {"cpu": 75.0}},
            {"util": {"cpu": 25.0}},
        ]
        assert avg_attr(items, "util.cpu") == 50.0

    def test_avg_empty_list(self):
        """Should return 0 for empty list."""
        assert avg_attr([], "value") == 0.0


class TestSelectAttrCustom:
    """Tests for selectattr_custom filter."""

    def test_filter_eq(self):
        """Should filter by equality."""
        items = [
            {"status": "ACTIVE"},
            {"status": "INACTIVE"},
            {"status": "ACTIVE"},
        ]
        result = selectattr_custom(items, "status", "eq", "ACTIVE")
        assert len(result) == 2
        assert all(item["status"] == "ACTIVE" for item in result)

    def test_filter_ne(self):
        """Should filter by inequality."""
        items = [
            {"status": "ACTIVE"},
            {"status": "INACTIVE"},
        ]
        result = selectattr_custom(items, "status", "ne", "ACTIVE")
        assert len(result) == 1
        assert result[0]["status"] == "INACTIVE"

    def test_filter_gt(self):
        """Should filter by greater than."""
        items = [
            {"util": 50},
            {"util": 80},
            {"util": 90},
            {"util": 60},
        ]
        result = selectattr_custom(items, "util", "gt", 75)
        assert len(result) == 2
        assert all(item["util"] > 75 for item in result)

    def test_filter_lt(self):
        """Should filter by less than."""
        items = [
            {"util": 50},
            {"util": 80},
            {"util": 20},
        ]
        result = selectattr_custom(items, "util", "lt", 60)
        assert len(result) == 2

    def test_filter_in(self):
        """Should filter by membership."""
        items = [
            {"tag": "prod"},
            {"tag": "dev"},
            {"tag": "staging"},
        ]
        result = selectattr_custom(items, "tag", "in", ["prod", "staging"])
        assert len(result) == 2

    def test_filter_contains(self):
        """Should filter by contains."""
        items = [
            {"name": "cluster-01"},
            {"name": "cluster-02"},
            {"name": "host-01"},
        ]
        result = selectattr_custom(items, "name", "contains", "cluster")
        assert len(result) == 2

    def test_filter_unknown_operator(self):
        """Should raise error for unknown operator."""
        items = [{"x": 1}]
        with pytest.raises(ValueError, match="Unknown operator"):
            selectattr_custom(items, "x", "unknown", 1)

    def test_filter_empty_list(self):
        """Should handle empty list."""
        result = selectattr_custom([], "x", "eq", 1)
        assert result == []

    def test_filter_missing_attr(self):
        """Should skip items with missing attribute."""
        items = [
            {"value": 10},
            {"other": 20},  # Missing 'value'
            {"value": 30},
        ]
        result = selectattr_custom(items, "value", "eq", 10)
        assert len(result) == 1


class TestGroupbyAttr:
    """Tests for groupby_attr filter."""

    def test_group_simple_attr(self):
        """Should group by simple attribute."""
        items = [
            {"type": "web", "name": "server1"},
            {"type": "db", "name": "db1"},
            {"type": "web", "name": "server2"},
        ]
        result = groupby_attr(items, "type")
        
        assert len(result) == 2
        assert len(result["web"]) == 2
        assert len(result["db"]) == 1

    def test_group_nested_attr(self):
        """Should group by nested attribute."""
        items = [
            {"cluster": {"domain": "d1"}, "name": "c1"},
            {"cluster": {"domain": "d2"}, "name": "c2"},
            {"cluster": {"domain": "d1"}, "name": "c3"},
        ]
        result = groupby_attr(items, "cluster.domain")
        
        assert len(result) == 2
        assert len(result["d1"]) == 2
        assert len(result["d2"]) == 1

    def test_group_empty_list(self):
        """Should handle empty list."""
        result = groupby_attr([], "type")
        assert result == {}

    def test_group_missing_attr(self):
        """Should skip items with missing attribute."""
        items = [
            {"type": "web"},
            {"other": "x"},  # Missing 'type'
            {"type": "db"},
        ]
        result = groupby_attr(items, "type")
        
        assert len(result) == 2
        assert "web" in result
        assert "db" in result


class TestJsonDumps:
    """Tests for json_dumps filter."""

    def test_json_dumps_dict(self):
        """Should serialize dict to JSON."""
        obj = {"name": "test", "value": 42}
        result = json_dumps(obj)
        
        assert isinstance(result, str)
        assert json.loads(result) == obj

    def test_json_dumps_list(self):
        """Should serialize list to JSON."""
        obj = [1, 2, 3]
        result = json_dumps(obj)
        
        assert isinstance(result, str)
        assert json.loads(result) == obj

    def test_json_dumps_with_indent(self):
        """Should format with indentation."""
        obj = {"x": 1}
        result = json_dumps(obj, indent=2)
        
        assert "\n" in result
        assert "  " in result

    def test_json_dumps_compact(self):
        """Should format compactly without indent."""
        obj = {"x": 1, "y": 2}
        result = json_dumps(obj, indent=None)
        
        assert "\n" not in result


class TestRegisterCustomFilters:
    """Tests for filter registration."""

    def test_register_filters(self):
        """Should register all custom filters."""
        env = Environment()
        register_custom_filters(env)
        
        # Check long names
        assert "sum_attr" in env.filters
        assert "avg_attr" in env.filters
        assert "selectattr_custom" in env.filters
        assert "groupby_attr" in env.filters
        assert "json_dumps" in env.filters
        
        # Check short aliases
        assert "sum" in env.filters
        assert "avg" in env.filters
        assert "selectattr" in env.filters
        assert "groupby" in env.filters
        assert "json" in env.filters

    def test_filters_work_in_templates(self):
        """Should work when used in templates."""
        env = Environment()
        register_custom_filters(env)
        
        # Test sum filter
        template = env.from_string("{{ items | sum('value') }}")
        result = template.render(items=[{"value": 10}, {"value": 20}])
        assert result == "30.0"
        
        # Test json filter
        template = env.from_string("{{ data | json }}")
        result = template.render(data={"x": 1})
        parsed = json.loads(result)
        assert parsed == {"x": 1}

