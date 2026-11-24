"""Controlled vocabularies for transaction data.

Shared by ingestion validation, the feature pipeline, detection rules and the synthetic
data generator, so every layer agrees on the allowed values. The database enforces the
same lists with CHECK constraints (migration 0002); a test asserts they stay in sync.
"""

from __future__ import annotations

from enum import StrEnum

# ISO 4217 codes accepted by the simulation.
CURRENCIES: frozenset[str] = frozenset({"ZAR", "USD", "EUR", "GBP"})


class TxnType(StrEnum):
    PURCHASE = "purchase"
    ATM_WITHDRAWAL = "atm_withdrawal"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    REFUND = "refund"


class Channel(StrEnum):
    CARD_PRESENT = "card_present"
    ONLINE = "online"
    MOBILE_APP = "mobile_app"
    ATM = "atm"
    BANK_TRANSFER = "bank_transfer"


class TxnStatus(StrEnum):
    COMPLETED = "completed"
    DECLINED = "declined"
    FAILED = "failed"


class MerchantCategory(StrEnum):
    GROCERIES = "groceries"
    RESTAURANTS = "restaurants"
    FUEL = "fuel"
    RETAIL_CLOTHING = "retail_clothing"
    ELECTRONICS = "electronics"
    HOME_FURNITURE = "home_furniture"
    TRAVEL_AIRLINE = "travel_airline"
    TRAVEL_HOTEL = "travel_hotel"
    ENTERTAINMENT = "entertainment"
    UTILITIES = "utilities"
    HEALTH_PHARMACY = "health_pharmacy"
    ONLINE_MARKETPLACE = "online_marketplace"
    SUBSCRIPTIONS = "subscriptions"
    GIFT_CARDS = "gift_cards"
    CRYPTO_EXCHANGE = "crypto_exchange"
    MONEY_TRANSFER = "money_transfer"
    ATM_CASH = "atm_cash"
    PAYROLL = "payroll"


# Categories commonly used to cash out stolen funds; used by rules and features.
HIGH_RISK_CATEGORIES: frozenset[MerchantCategory] = frozenset(
    {
        MerchantCategory.GIFT_CARDS,
        MerchantCategory.CRYPTO_EXCHANGE,
        MerchantCategory.MONEY_TRANSFER,
    }
)
