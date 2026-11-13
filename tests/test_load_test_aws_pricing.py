import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from quiz_app.load_test_aws.aws import AwsApi
from quiz_app.load_test_aws.pricing import (
    PRICE_SPECS,
    PriceSpec,
    PricingEvidence,
    ProductsResponse,
    _price_line,
    refresh_pricing,
)

NOW = datetime(2026, 9, 20, tzinfo=UTC)
PRICES = ("0.4464", "0.2232", "0.05", "0.049", "0.096", "0.138", "0.075", "0.005")


def _offer(spec: PriceSpec, price: str) -> dict[str, object]:
    return {
        "serviceCode": spec.service,
        "product": {
            "sku": "SKU",
            "attributes": {
                "regionCode": "ap-northeast-1",
                "usagetype": "usage",
                **spec.filters,
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "sku": "SKU",
                    "effectiveDate": "2026-09-01T00:00:00Z",
                    "priceDimensions": {
                        "dimension": {
                            "unit": spec.unit,
                            "beginRange": "0",
                            "endRange": "Inf",
                            "appliesTo": [],
                            "pricePerUnit": {"USD": price},
                        }
                    },
                }
            },
            "Reserved": {"must-not-use": {"pricePerUnit": {"USD": "0"}}},
        },
    }


def _response(spec: PriceSpec, price: str) -> ProductsResponse:
    return ProductsResponse.model_validate({"PriceList": [json.dumps(_offer(spec, price))]})


def _evidence() -> PricingEvidence:
    return PricingEvidence(
        fetched_at=NOW,
        lines=tuple(
            _price_line(_response(spec, price), spec, NOW)
            for spec, price in zip(PRICE_SPECS, PRICES, strict=True)
        ),
    )


def test_quote_includes_four_hours_full_cpu_credits_storage_tax_and_reserve() -> None:
    evidence = _evidence()
    assert evidence.estimated_total_jpy == Decimal("944.63")
    evidence.assert_within_budget(NOW + timedelta(minutes=30))
    assert evidence.lines[6].four_hour_usd == Decimal("0.6")
    assert evidence.lines[4].four_hour_usd == Decimal("0.064")


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(minutes=30, seconds=1)])
def test_stale_or_future_quote_cannot_authorize_creation(offset: timedelta) -> None:
    with pytest.raises(ValueError, match="pricing_stale"):
        _evidence().assert_within_budget(NOW + offset)


def test_price_increase_blocks_creation() -> None:
    original = _evidence()
    line = original.lines[0].model_copy(update={"unit_usd": Decimal("0.6")})
    quote = original.model_copy(update={"lines": (line, *original.lines[1:])})
    with pytest.raises(ValueError, match="budget_exceeded"):
        quote.assert_within_budget(NOW)


@pytest.mark.parametrize("values", [[], ["duplicate", "duplicate"]])
def test_missing_and_ambiguous_prices_fail_closed(values: list[str]) -> None:
    response = ProductsResponse.model_validate(
        {
            "PriceList": [json.dumps(_offer(PRICE_SPECS[0], PRICES[0])) for _ in values],
        }
    )
    with pytest.raises(ValueError, match="price_not_unique"):
        _price_line(response, PRICE_SPECS[0], NOW)


def test_next_page_is_not_silently_ignored() -> None:
    response = _response(PRICE_SPECS[0], PRICES[0]).model_copy(update={"next_token": "next"})
    with pytest.raises(ValueError, match="price_not_unique"):
        _price_line(response, PRICE_SPECS[0], NOW)


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity"])
def test_nonpositive_or_nonfinite_unit_prices_are_rejected(price: str) -> None:
    with pytest.raises(ValidationError):
        _response(PRICE_SPECS[0], price)


def test_unrequested_product_does_not_satisfy_price_filter() -> None:
    with pytest.raises(ValueError, match="product_mismatch"):
        _price_line(_response(PRICE_SPECS[1], PRICES[1]), PRICE_SPECS[0], NOW)


def test_incomplete_or_reduced_configuration_is_rejected() -> None:
    quote = _evidence()
    with pytest.raises(ValidationError, match="items_missing"):
        PricingEvidence(fetched_at=NOW, lines=quote.lines[:-1])
    with pytest.raises(ValidationError, match="item_mismatch"):
        PricingEvidence(
            fetched_at=NOW,
            lines=(
                quote.lines[0].model_copy(update={"quantity": Decimal("0.5")}),
                *quote.lines[1:],
            ),
        )


def test_refresh_uses_eight_exact_regional_queries_and_ignores_reserved() -> None:
    class FakeApi:
        calls: int = 0

        def call(
            self, service: str, operation: str, model: type[ProductsResponse], **parameters: object
        ) -> ProductsResponse:
            spec = PRICE_SPECS[self.calls]
            price = PRICES[self.calls]
            self.calls += 1
            assert (service, operation, model) == ("pricing", "get_products", ProductsResponse)
            assert parameters["ServiceCode"] == spec.service
            assert parameters["Filters"] == [
                {"Type": "TERM_MATCH", "Field": key, "Value": value}
                for key, value in {"regionCode": "ap-northeast-1", **spec.filters}.items()
            ]
            return _response(spec, price)

    api = FakeApi()
    quote = refresh_pricing(cast(AwsApi, api), now=NOW)
    assert api.calls == 8
    assert quote == _evidence()
