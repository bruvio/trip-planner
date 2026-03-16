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


class TestPointCountry:
    def test_france(self):
        assert tp._point_country(48.8, 2.3) == "FR"  # Paris

    def test_italy(self):
        assert tp._point_country(41.9, 12.5) == "IT"  # Rome

    def test_uk(self):
        assert tp._point_country(51.5, -0.1) == "GB"  # London

    def test_unknown(self):
        assert tp._point_country(0, 0) is None  # middle of ocean


class TestAnalyzeRoute:
    def test_empty_route(self):
        route = {"legs": []}
        result = tp.analyze_route(route)
        assert result["has_toll"] is False
        assert result["has_ferry"] is False

    def test_ferry_detection(self):
        route = {
            "distance": 50000,
            "duration": 5400,
            "geometry": {"coordinates": [[-1.2, 51.8], [1.8, 48.8]]},
            "has_ferry": True,
            "has_toll": False,
            "toll_km": 0,
            "ferry_segments": [
                {"name": "Dover-Calais", "distance_km": 50, "duration_min": 90},
            ],
        }
        result = tp.analyze_route(route)
        assert result["has_ferry"] is True
        assert len(result["ferry_segments"]) == 1
        assert result["ferry_segments"][0]["name"] == "Dover-Calais"
        assert result["is_channel_crossing"] is True
        assert "GB" in result["countries"]
        assert "FR" in result["countries"]


class TestEstimateTollCost:
    def test_french_tolls(self):
        analysis = {"toll_km_by_country": {"FR": 100}, "countries": {"FR"}}
        cost = tp.estimate_toll_cost(analysis, "EUR")
        assert abs(cost - 9.0) < 0.01  # 100km * 0.09 EUR/km

    def test_no_tolls(self):
        analysis = {"toll_km_by_country": {}, "countries": set()}
        cost = tp.estimate_toll_cost(analysis, "GBP")
        assert cost == 0

    def test_vignette_added(self):
        analysis = {"toll_km_by_country": {}, "countries": {"CH"}}
        cost = tp.estimate_toll_cost(analysis, "EUR")
        assert cost == 40.0  # Swiss vignette


class TestDeduplicateRoutes:
    def test_removes_duplicates(self):
        routes = [
            ("A", {"distance": 1000}, {}),
            ("B", {"distance": 1005}, {}),  # within 1%
            ("C", {"distance": 2000}, {}),
        ]
        result = tp.deduplicate_routes(routes)
        assert len(result) == 2

    def test_keeps_different(self):
        routes = [
            ("A", {"distance": 1000}, {}),
            ("B", {"distance": 1500}, {}),
        ]
        result = tp.deduplicate_routes(routes)
        assert len(result) == 2


class TestCalcCostsWithTollEstimate:
    class FakeArgs:
        def __init__(self, **kwargs):
            defaults = {
                "fuel_type": "diesel", "efficiency": 6.5, "tank": 60,
                "fuel_price": 1.45, "kwh": 18, "kwh_price": 0.35,
                "tolls": 0, "currency": "GBP",
            }
            defaults.update(kwargs)
            for k, v in defaults.items():
                setattr(self, k, v)

    def test_auto_toll_used_when_no_manual(self):
        args = self.FakeArgs(tolls=0)
        result = tp.calc_costs(100, args, toll_estimate=25.0)
        assert result["toll"] == 25.0

    def test_manual_toll_overrides(self):
        args = self.FakeArgs(tolls=50)
        result = tp.calc_costs(100, args, toll_estimate=25.0)
        assert result["toll"] == 50


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
