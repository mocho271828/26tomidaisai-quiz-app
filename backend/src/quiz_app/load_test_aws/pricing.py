"""東京の承認構成を、作成直前の公式OnDemand単価で再見積もりする。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from typing import Literal

from pydantic import Field, Json, field_validator, model_validator

from quiz_app.load_test_aws.aws import ApiModel, AwsApi
from quiz_app.load_test_aws.models import BUDGET_JPY, REGION, StrictModel, utc_datetime

QUOTE_MAX_AGE = timedelta(minutes=30)
RESERVE_USD = Decimal("0.50")
HOURS = Decimal(4)
JPY_PER_USD = Decimal(200)
TAX_MULTIPLIER = Decimal("1.1")
MONTHLY_HOURS = Decimal(720)
PRICE_SOURCE = "https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_pricing_GetProducts.html"


class PriceSpec(StrictModel):
    name: str
    service: str
    filters: dict[str, str]
    unit: Literal["Hrs", "GB-Mo", "vCPU-Hours"]
    quantity: Decimal


_EC2_FILTERS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
}
PRICE_SPECS = (
    PriceSpec(
        name="application",
        service="AmazonEC2",
        unit="Hrs",
        quantity=1,
        filters={**_EC2_FILTERS, "instanceType": "m6a.2xlarge"},
    ),
    PriceSpec(
        name="runner",
        service="AmazonEC2",
        unit="Hrs",
        quantity=1,
        filters={**_EC2_FILTERS, "instanceType": "m6a.xlarge"},
    ),
    PriceSpec(
        name="database",
        service="AmazonRDS",
        unit="Hrs",
        quantity=1,
        filters={
            "instanceType": "db.t4g.small",
            "databaseEngine": "PostgreSQL",
            "deploymentOption": "Single-AZ",
        },
    ),
    PriceSpec(
        name="redis",
        service="AmazonElastiCache",
        unit="Hrs",
        quantity=1,
        filters={
            "instanceType": "cache.t4g.small",
            "cacheEngine": "Redis",
            "usagetype": "APN1-NodeUsage:cache.t4g.small",
        },
    ),
    PriceSpec(
        name="ebs",
        service="AmazonEC2",
        unit="GB-Mo",
        quantity=120,
        filters={"usagetype": "APN1-EBS:VolumeUsage.gp3"},
    ),
    PriceSpec(
        name="database_storage",
        service="AmazonRDS",
        unit="GB-Mo",
        quantity=20,
        filters={
            "usagetype": "APN1-RDS:GP3-Storage",
            "databaseEngine": "PostgreSQL",
            "deploymentOption": "Single-AZ",
        },
    ),
    # t4g.smallの2 vCPUを4時間フル稼働した場合まで保守的に含める。
    PriceSpec(
        name="database_cpu_credits",
        service="AmazonRDS",
        unit="vCPU-Hours",
        quantity=2,
        filters={"usagetype": "APN1-CPUCredits:db.t4g", "databaseEngine": "PostgreSQL"},
    ),
    PriceSpec(
        name="public_ipv4",
        service="AmazonVPC",
        unit="Hrs",
        quantity=2,
        filters={"usagetype": "APN1-PublicIPv4:InUseAddress"},
    ),
)


class ProductAttributes(ApiModel):
    region_code: str = Field(alias="regionCode")
    instance_type: str | None = Field(default=None, alias="instanceType")
    operating_system: str | None = Field(default=None, alias="operatingSystem")
    tenancy: str | None = None
    preinstalled_software: str | None = Field(default=None, alias="preInstalledSw")
    capacity_status: str | None = Field(default=None, alias="capacitystatus")
    database_engine: str | None = Field(default=None, alias="databaseEngine")
    deployment_option: str | None = Field(default=None, alias="deploymentOption")
    cache_engine: str | None = Field(default=None, alias="cacheEngine")
    usage_type: str = Field(alias="usagetype")


class Product(ApiModel):
    sku: str
    attributes: ProductAttributes


class UnitPrice(ApiModel):
    usd: Decimal = Field(alias="USD", gt=0, allow_inf_nan=False)


class PriceDimension(ApiModel):
    unit: str
    begin_range: Decimal = Field(alias="beginRange")
    end_range: str = Field(alias="endRange")
    applies_to: tuple[str, ...] = Field(alias="appliesTo")
    price: UnitPrice = Field(alias="pricePerUnit")


class OnDemandTerm(ApiModel):
    sku: str
    effective_at: datetime = Field(alias="effectiveDate")
    dimensions: dict[str, PriceDimension] = Field(alias="priceDimensions")


class OfferTerms(ApiModel):
    on_demand: dict[str, OnDemandTerm] = Field(alias="OnDemand")


class Offer(ApiModel):
    product: Product
    service: str = Field(alias="serviceCode")
    terms: OfferTerms


class ProductsResponse(ApiModel):
    # AWS Query APIはJSON文字列の配列を返す。外部dictを下流へ渡さない。
    price_list: tuple[Json[Offer], ...] = Field(alias="PriceList", max_length=100)
    next_token: str | None = Field(default=None, alias="NextToken")


class PriceLine(StrictModel):
    name: str
    sku: str
    service: str
    unit: str
    unit_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    effective_at: datetime
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _time = field_validator("effective_at")(utc_datetime)

    @property
    def four_hour_usd(self) -> Decimal:
        divisor = MONTHLY_HOURS if self.unit == "GB-Mo" else Decimal(1)
        return self.unit_usd * self.quantity * HOURS / divisor


class PricingEvidence(StrictModel):
    schema_version: Literal["aws-load-test-pricing-v1"] = "aws-load-test-pricing-v1"
    region: Literal["ap-northeast-1"] = REGION
    fetched_at: datetime
    lines: tuple[PriceLine, ...]
    reserve_usd: Literal["0.50"] = "0.50"
    hours: Literal[4] = 4
    jpy_per_usd: Literal[200] = 200
    tax_multiplier: Literal["1.1"] = "1.1"

    _time = field_validator("fetched_at")(utc_datetime)

    @model_validator(mode="after")
    def validate_fixed_configuration(self) -> PricingEvidence:
        if len(self.lines) != len(PRICE_SPECS):
            raise ValueError("aws_load_test_price_items_missing")
        for line, spec in zip(self.lines, PRICE_SPECS, strict=True):
            if (line.name, line.service, line.unit, line.quantity) != (
                spec.name,
                spec.service,
                spec.unit,
                spec.quantity,
            ) or line.effective_at > self.fetched_at:
                raise ValueError("aws_load_test_price_item_mismatch")
        return self

    @property
    def estimated_total_jpy(self) -> Decimal:
        total = sum((line.four_hour_usd for line in self.lines), RESERVE_USD)
        return (total * JPY_PER_USD * TAX_MULTIPLIER).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )

    def assert_within_budget(self, now: datetime) -> None:
        """古い見積り、将来取得時刻、上限超過を課金作成の直前に拒否する。"""
        if not timedelta(0) <= utc_datetime(now) - self.fetched_at <= QUOTE_MAX_AGE:
            raise ValueError("aws_load_test_pricing_stale")
        if self.estimated_total_jpy > BUDGET_JPY:
            raise ValueError("aws_load_test_budget_exceeded")


def _price_line(response: ProductsResponse, spec: PriceSpec, now: datetime) -> PriceLine:
    if response.next_token or len(response.price_list) != 1:
        raise ValueError("aws_load_test_price_not_unique")
    offer = response.price_list[0]
    attributes = offer.product.attributes.model_dump(by_alias=True, exclude_none=True)
    if offer.service != spec.service or any(
        attributes.get(key) != value
        for key, value in {"regionCode": REGION, **spec.filters}.items()
    ):
        raise ValueError("aws_load_test_price_product_mismatch")
    terms = tuple(offer.terms.on_demand.values())
    if len(terms) != 1 or len(terms[0].dimensions) != 1:
        raise ValueError("aws_load_test_price_term_not_unique")
    term = terms[0]
    dimension = next(iter(term.dimensions.values()))
    if (
        term.sku != offer.product.sku
        or utc_datetime(term.effective_at) > now
        or dimension.unit != spec.unit
        or dimension.begin_range != 0
        or dimension.end_range != "Inf"
        or dimension.applies_to
    ):
        raise ValueError("aws_load_test_price_dimension_invalid")
    return PriceLine(
        name=spec.name,
        sku=offer.product.sku,
        service=spec.service,
        unit=spec.unit,
        unit_usd=dimension.price.usd,
        quantity=spec.quantity,
        effective_at=term.effective_at,
        response_sha256=sha256(offer.model_dump_json().encode()).hexdigest(),
    )


def refresh_pricing(api: AwsApi, *, now: datetime) -> PricingEvidence:
    """公式Price List Query APIから固定8品目のOnDemand単価だけを取得する。"""
    utc_datetime(now)
    lines = []
    for spec in PRICE_SPECS:
        response = api.call(
            "pricing",
            "get_products",
            ProductsResponse,
            ServiceCode=spec.service,
            FormatVersion="aws_v1",
            MaxResults=100,
            Filters=[
                {"Type": "TERM_MATCH", "Field": key, "Value": value}
                for key, value in {"regionCode": REGION, **spec.filters}.items()
            ],
        )
        lines.append(_price_line(response, spec, now))
    evidence = PricingEvidence(fetched_at=now, lines=tuple(lines))
    evidence.assert_within_budget(now)
    return evidence
