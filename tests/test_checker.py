"""Test offline: python -m unittest discover tests"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import volotea_checker as v  # noqa: E402

CFG = {
    "origin": "FLR", "horizon_days": 60, "min_stay_days": 5, "max_stay_days": 10,
    "top_n": 5, "max_per_destination": 1, "passengers": 1,
    "destinations": [{"code": "SVQ", "name": "Siviglia", "weekdays": [0, 4]},
                     {"code": "BER", "name": "Berlino", "weekdays": []}],
}


def fare(day, price):
    return v.Fare(day=day, price=price)


class CombosTest(unittest.TestCase):
    def test_stay_window_and_ranking(self):
        fares = {
            "SVQ": {"out": {"2026-10-05": fare("2026-10-05", 20)},
                    "in": {"2026-10-09": fare("2026-10-09", 1),     # 4 notti: escluso
                           "2026-10-12": fare("2026-10-12", 30),    # 7 notti
                           "2026-10-16": fare("2026-10-16", 1)}},   # 11 notti: escluso
            "BER": {"out": {"2026-10-06": fare("2026-10-06", 10)},
                    "in": {"2026-10-11": fare("2026-10-11", 15), "2026-10-13": fare("2026-10-13", 12)}},
        }
        combos = v.best_combos(CFG, fares)
        self.assertEqual([(c.dest_code, c.total, c.nights) for c in combos], [("BER", 22, 7), ("SVQ", 50, 7)])

    def test_candidate_days_respects_weekdays(self):
        days = v.candidate_days(date(2026, 10, 5), date(2026, 10, 11), [0, 4])
        self.assertEqual([d.isoformat() for d in days], ["2026-10-05", "2026-10-09"])

    def test_mock_end_to_end(self):
        provider = v.MockProvider(CFG)
        with mock.patch.object(v.FareCache, "save"):
            fares = v.scan(CFG, provider, v.FareCache(Path("/nonexistent/x.json"), 1), date(2026, 9, 26))
        combos = v.best_combos(CFG, fares)
        self.assertEqual(len(combos), 2)  # max 1 per destinazione, 2 destinazioni
        subject, text, body = v.build_report(CFG, combos, v.cheapest_per_destination(CFG, fares), provider, date(2026, 9, 26))
        self.assertIn("Siviglia", text)
        self.assertIn("<table", body)


class GoogleParsingTest(unittest.TestCase):
    def test_keeps_cheapest_direct_volotea(self):
        from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight

        def f(price, airlines, legs=1):
            leg = SingleFlight(Airport("", "FLR"), Airport("", "SVQ"), SimpleDatetime((2026, 10, 5), (7, 5)),
                               SimpleDatetime((2026, 10, 5), (9, 30)), 145, "A320")
            return Flights("", price, airlines, [leg] * legs, CarbonEmission(0, 0))

        results = [f(15, ["Vueling"]), f(18, ["Volotea"], legs=2), f(45, ["Volotea"]), f(39, ["Volotea"])]
        p = v.GoogleFlightsProvider({"request_delay_seconds": 0})
        with mock.patch("fast_flights.get_flights", return_value=results):
            out = p.fetch("FLR", "SVQ", [date(2026, 10, 5)])
        self.assertEqual(out["2026-10-05"].price, 39)
        self.assertEqual(out["2026-10-05"].dep_time, "07:05")


if __name__ == "__main__":
    unittest.main()
