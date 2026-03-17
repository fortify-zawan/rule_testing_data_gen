"""Parse a natural language AML rule into a structured Rule object."""
from config.schema_loader import (
    canonical_name,
    format_aggregations_for_prompt,
    format_attributes_for_prompt,
)
from domain.models import DerivedAttr, Rule, RuleCondition
from llm.llm_wrapper import call_llm_json
from logging_config import get_logger
from prompts.rule_parser import PROMPT_TEMPLATE, SYSTEM

log = get_logger(__name__)


def parse_rule(description: str) -> Rule:
    log.info("parse_rule | input_chars=%d", len(description))
    prompt = PROMPT_TEMPLATE.format(
        schema_context=format_attributes_for_prompt(),
        aggregation_context=format_aggregations_for_prompt(),
        description=description,
    )
    data = call_llm_json(prompt, system=SYSTEM)

    def _coerce_filter_value(fop, fval):
        """Ensure filter_value is a list when operator is 'in'/'not_in'."""
        if fval is None:
            return None
        if fop in ("in", "not_in") and not isinstance(fval, list):
            return [fval]
        return fval

    def _hydrate_derived_attr(raw: dict) -> DerivedAttr:
        """Construct a DerivedAttr from a raw JSON sub-object."""
        fop = raw.get("filter_operator")
        return DerivedAttr(
            name=raw["name"],
            aggregation=raw.get("aggregation", "count"),
            attribute=canonical_name(raw["attribute"]) if raw.get("attribute") else "transaction_id",
            window=raw.get("window"),
            filter_attribute=canonical_name(raw["filter_attribute"]) if raw.get("filter_attribute") else None,
            filter_operator=fop,
            filter_value=_coerce_filter_value(fop, raw.get("filter_value")),
        )

    conditions = [
        RuleCondition(
            attribute=canonical_name(c["attribute"]) if c.get("attribute") else None,
            operator=c["operator"],
            value=c["value"],
            aggregation=c.get("aggregation"),
            window=c.get("window"),
            logical_connector=c.get("logical_connector", "AND"),
            filter_attribute=canonical_name(c["filter_attribute"]) if c.get("filter_attribute") else None,
            filter_operator=c.get("filter_operator"),
            filter_value=_coerce_filter_value(c.get("filter_operator"), c.get("filter_value")),
            derived_attributes=[_hydrate_derived_attr(da) for da in c["derived_attributes"]]
                                if c.get("derived_attributes") else None,
            derived_expression=c.get("derived_expression"),
            window_mode=c.get("window_mode"),
        )
        for c in data["conditions"]
    ]

    relevant_attributes = [canonical_name(a) for a in data["relevant_attributes"]]

    rule = Rule(
        description=description,
        rule_type=data["rule_type"],
        relevant_attributes=relevant_attributes,
        conditions=conditions,
        raw_expression=data["raw_expression"],
        high_risk_countries=data.get("high_risk_countries", []),
    )
    log.info(
        "parse_rule | result: rule_type=%s conditions=%d high_risk_countries=%s raw_expression=%r",
        rule.rule_type,
        len(rule.conditions),
        rule.high_risk_countries,
        rule.raw_expression,
    )
    log.debug("parse_rule | full Rule: %r", rule)
    return rule
