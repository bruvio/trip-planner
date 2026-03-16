"""Tests for trip_planner.py utility functions."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trip_planner as tp


class TestHaversine:
    def test_same_point_returns_zero(self):
        assert tp.haversine(51.75, -1.26, 51.75, -1.26) == 0.0

    def test_known_distance(self):
        # Oxford to London ~ 90km
        dist = tp.haversine(51.75, -1.26, 51.51, -0.13)
        assert 80 < dist < 100

    def test_symmetry(self):
        d1 = tp.haversine(51.75, -1.26, 45.76, 4.83)
        d2 = tp.haversine(45.76, 4.83, 51.75, -1.26)
        assert abs(d1 - d2) < 0.001


class TestFormatHelpers:
    def test_fmt_dist(self):
        result = tp.fmt_dist(100000)
        assert "100 km" in result
        assert "62 mi" in result

    def test_fmt_time_hours(self):
        result = tp.fmt_time(7200)
        assert result == "2h 00m"

    def test_fmt_time_minutes_only(self):
        result = tp.fmt_time(1800)
        assert result == "30m"

    def test_fmt_cost(self):
        assert tp.fmt_cost("£", 12.5) == "£12.50"


class TestSimplifyPolyline:
    def test_short_polyline_unchanged(self):
        coords = [[0, 0], [1, 1], [2, 2]]
        result = tp.simplify_polyline(coords, max_points=10)
        assert len(result) == 3

    def test_long_polyline_reduced(self):
        coords = [[i, i] for i in range(1000)]
        result = tp.simplify_polyline(coords, max_points=50)
        assert len(result) == 50

    def test_preserves_endpoints(self):
        coords = [[i, i] for i in range(500)]
        result = tp.simplify_polyline(coords, max_points=20)
        assert result[0] == [0, 0]
        assert result[-1] == [499, 499]


class TestClassifyElements:
    def test_classifies_fuel(self):
        elements = [{"tags": {"amenity": "fuel", "name": "Shell"}}]
        result = tp._classify_elements(elements)
        assert len(result["fuel"]) == 1
        assert len(result["ev"]) == 0

    def test_classifies_ev(self):
        elements = [{"tags": {"amenity": "charging_station"}}]
        result = tp._classify_elements(elements)
        assert len(result["ev"]) == 1

    def test_classifies_hotel(self):
        elements = [{"tags": {"tourism": "hotel"}}]
        result = tp._classify_elements(elements)
        assert len(result["hotels"]) == 1

    def test_classifies_rest(self):
        elements = [{"tags": {"highway": "services"}}]
        result = tp._classify_elements(elements)
        assert len(result["rest"]) == 1

    def test_unknown_tags_ignored(self):
        elements = [{"tags": {"shop": "bakery"}}]
        result = tp._classify_elements(elements)
        assert all(len(v) == 0 for v in result.values())


class TestElemHelpers:
    def test_elem_name_prefers_name(self):
        el = {"tags": {"name": "BP", "brand": "BP", "operator": "BP PLC"}}
        assert tp.elem_name(el) == "BP"

    def test_elem_name_falls_back(self):
        el = {"tags": {"brand": "Shell"}}
        assert tp.elem_name(el) == "Shell"

    def test_elem_name_unnamed(self):
        el = {"tags": {}}
        assert tp.elem_name(el) == "Unnamed"

    def test_elem_center_node(self):
        el = {"type": "node", "lat": 51.5, "lon": -0.1}
        assert tp.elem_center(el) == (51.5, -0.1)

    def test_elem_center_way(self):
        el = {"type": "way", "center": {"lat": 51.5, "lon": -0.1}}
        assert tp.elem_center(el) == (51.5, -0.1)


class TestCalcCosts:
    class FakeArgs:
        def __init__(self, **kwargs):
            defaults = {
                "fuel_type": "diesel",
                "efficiency": 6.5,
                "tank": 60,
                "fuel_price": 1.45,
                "kwh": 18,
                "kwh_price": 0.35,
                "tolls": 0,
                "currency": "GBP",
            }
            defaults.update(kwargs)
            for k, v in defaults.items():
                setattr(self, k, v)

    def test_diesel_cost(self):
        args = self.FakeArgs(fuel_type="diesel", efficiency=6.5, fuel_price=1.45)
        result = tp.calc_costs(100, args)
        expected_fuel = (100 / 100) * 6.5 * 1.45
        assert abs(result["fuel_cost"] - expected_fuel) < 0.01

    def test_electric_cost(self):
        args = self.FakeArgs(fuel_type="electric", kwh=18, kwh_price=0.35)
        result = tp.calc_costs(200, args)
        expected = (200 / 100) * 18 * 0.35
        assert abs(result["ev_cost"] - expected) < 0.01
        assert result["fuel_cost"] == 0

    def test_tolls_included(self):
        args = self.FakeArgs(tolls=50)
        result = tp.calc_costs(100, args)
        assert result["toll"] == 50
        assert result["total"] == result["fuel_cost"] + 50


class TestAutoFilename:
    def test_generates_filename(self):
        waypoints = [{"short": "Oxford"}, {"short": "London"}]
        result = tp.auto_filename(waypoints, "md")
        assert result.startswith("trip_Oxford_London_")
        assert result.endswith(".md")

    def test_html_extension(self):
        waypoints = [{"short": "A"}, {"short": "B"}]
        result = tp.auto_filename(waypoints, "html")
        assert result.endswith(".html")
