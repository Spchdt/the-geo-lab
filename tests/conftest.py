"""Shared submission fixture for the pipeline tests."""

import json


def form():
    return {
        "product_name": "PocketVolt",
        "category": "wireless earbuds",
        "listing_title": "PocketVolt portable charger",
        "listing_description": "A compact charger for everyday use.",
        "price": "75.00",
        "attributes_json": json.dumps([
            {"name": "Battery life", "value": "32", "unit": "hours", "preference": "higher"},
            {"name": "Earbud weight", "value": "4.8", "unit": "g", "preference": "lower"},
            {"name": "Noise cancellation", "value": "Active", "unit": "", "preference": "exact"},
            {"name": "Warranty", "value": "24", "unit": "months", "preference": "higher"},
        ]),
    }
