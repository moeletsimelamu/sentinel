"""Static reference data for the generator: simulated geography and spending profiles.

City coordinates are real (public), but the data attached to them is synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    name: str
    country: str
    lat: float
    lon: float
    currency: str
    home_weight: float  # 0 = never a home city


CITIES: tuple[City, ...] = (
    City("Johannesburg", "ZA", -26.2041, 28.0473, "ZAR", 0.22),
    City("Pretoria", "ZA", -25.7479, 28.2293, "ZAR", 0.12),
    City("Cape Town", "ZA", -33.9249, 18.4241, "ZAR", 0.18),
    City("Durban", "ZA", -29.8587, 31.0218, "ZAR", 0.12),
    City("Gqeberha", "ZA", -33.9608, 25.6022, "ZAR", 0.05),
    City("Bloemfontein", "ZA", -29.0852, 26.1596, "ZAR", 0.04),
    City("East London", "ZA", -33.0153, 27.9116, "ZAR", 0.03),
    City("Polokwane", "ZA", -23.9045, 29.4689, "ZAR", 0.03),
    City("Mbombela", "ZA", -25.4753, 30.9694, "ZAR", 0.03),
    City("Kimberley", "ZA", -28.7282, 24.7499, "ZAR", 0.02),
    City("Pietermaritzburg", "ZA", -29.6006, 30.3794, "ZAR", 0.03),
    City("Rustenburg", "ZA", -25.6676, 27.2421, "ZAR", 0.02),
    City("Stellenbosch", "ZA", -33.9321, 18.8602, "ZAR", 0.03),
    City("George", "ZA", -33.9630, 22.4617, "ZAR", 0.02),
    City("Midrand", "ZA", -25.9992, 28.1263, "ZAR", 0.06),
    City("Gaborone", "BW", -24.6282, 25.9231, "USD", 0.0),
    City("Windhoek", "NA", -22.5609, 17.0658, "USD", 0.0),
    City("Maputo", "MZ", -25.9692, 32.5732, "USD", 0.0),
    City("Nairobi", "KE", -1.2921, 36.8219, "USD", 0.0),
    City("Lagos", "NG", 6.5244, 3.3792, "USD", 0.0),
    City("Dubai", "AE", 25.2048, 55.2708, "USD", 0.0),
    City("London", "GB", 51.5074, -0.1278, "GBP", 0.0),
    City("Frankfurt", "DE", 50.1109, 8.6821, "EUR", 0.0),
    City("Lisbon", "PT", 38.7223, -9.1393, "EUR", 0.0),
    City("New York", "US", 40.7128, -74.0060, "USD", 0.0),
)
HOME_CITIES = tuple(c for c in CITIES if c.home_weight > 0)
FOREIGN_CITIES = tuple(c for c in CITIES if c.country != "ZA")
FX_FROM_ZAR = {"ZAR": 1.0, "USD": 1 / 18.0, "EUR": 1 / 19.5, "GBP": 1 / 23.0}

# Median amount in ZAR and log-normal sigma for each spending category.
AMOUNTS: dict[str, tuple[float, float]] = {
    "groceries": (350, 0.6),
    "restaurants": (280, 0.5),
    "fuel": (650, 0.35),
    "retail_clothing": (900, 0.6),
    "electronics": (3500, 0.8),
    "home_furniture": (4500, 0.7),
    "travel_airline": (4000, 0.5),
    "travel_hotel": (2200, 0.5),
    "entertainment": (250, 0.6),
    "utilities": (900, 0.4),
    "health_pharmacy": (300, 0.6),
    "online_marketplace": (600, 0.8),
    "subscriptions": (150, 0.4),
    "gift_cards": (500, 0.5),
    "crypto_exchange": (2000, 0.9),
    "money_transfer": (1500, 0.8),
    "atm_cash": (800, 0.5),
}
SPEND_CATEGORIES: tuple[str, ...] = tuple(AMOUNTS)

# Categories bought in person by default (card_present); the rest are card-not-present.
IN_PERSON = frozenset(
    {
        "groceries",
        "restaurants",
        "fuel",
        "retail_clothing",
        "electronics",
        "home_furniture",
        "entertainment",
        "health_pharmacy",
    }
)
NEVER_DIGITAL = frozenset({"fuel"})
DIGITAL_ONLY = frozenset(
    {"online_marketplace", "subscriptions", "gift_cards", "crypto_exchange", "utilities"}
)
TRAVEL = ("travel_airline", "travel_hotel", "restaurants")

SEGMENTS: dict[str, dict[str, float]] = {
    # mix share, mean daily transactions, amount scale, share of digital purchases
    "everyday": {"share": 0.45, "rate": 0.30, "scale": 1.0, "digital": 0.35},
    "frequent_spender": {"share": 0.20, "rate": 0.75, "scale": 1.3, "digital": 0.5},
    "small_business": {"share": 0.10, "rate": 1.00, "scale": 1.8, "digital": 0.6},
    "traveller": {"share": 0.10, "rate": 0.45, "scale": 1.5, "digital": 0.55},
    "low_activity": {"share": 0.15, "rate": 0.08, "scale": 0.8, "digital": 0.25},
}

# Dirichlet concentration per category (higher = more commonly preferred).
CATEGORY_ALPHA: dict[str, float] = {
    "groceries": 4.0,
    "restaurants": 2.5,
    "fuel": 2.0,
    "retail_clothing": 1.2,
    "electronics": 0.4,
    "home_furniture": 0.2,
    "travel_airline": 0.15,
    "travel_hotel": 0.15,
    "entertainment": 1.0,
    "utilities": 0.8,
    "health_pharmacy": 1.0,
    "online_marketplace": 1.2,
    "subscriptions": 0.8,
    "gift_cards": 0.12,  # benign use exists, so the high-risk rule sees false positives
    "crypto_exchange": 0.05,
    "money_transfer": 0.4,
    "atm_cash": 1.0,
}
