"""Parse a natural language AML rule into a structured Rule object."""
from config.schema_loader import (
    canonical_name,
    format_aggregations_for_prompt,
    format_attributes_for_prompt,
)
from domain.models import ComputedAttr, DerivedAttr, FilterClause, Rule, RuleCondition
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
    log.debug("parse_rule | raw LLM response: %s", data)

    def _coerce_filter_value(fop, fval):
        """Ensure filter_value is a list when operator is 'in'/'not_in'."""
        if fval is None:
            return None
        if fop in ("in", "not_in") and not isinstance(fval, list):
            return [fval]
        return fval

    def _hydrate_filter_clause(raw: dict) -> FilterClause:
        """Construct a FilterClause from a raw JSON object."""
        op = raw.get("operator", "==")
        val = _coerce_filter_value(op, raw.get("value"))
        return FilterClause(
            attribute=canonical_name(raw["attribute"]) if raw.get("attribute") else "",
            operator=op,
            value=val,
            value_field=canonical_name(raw["value_field"]) if raw.get("value_field") else None,
            connector=raw.get("connector", "AND"),
        )

    def _build_filters(raw: dict) -> list[FilterClause] | None:
        """Build filters list from a condition/DA dict.

        Supports both new format (filters array) and old scalar fields
        (filter_attribute/filter_operator/filter_value) for backward compat.
        """
        if raw.get("filters"):
            return [_hydrate_filter_clause(f) for f in raw["filters"]]
        # Backward compat: old single-filter scalar fields
        if raw.get("filter_attribute"):
            fop = raw.get("filter_operator")
            return [FilterClause(
                attribute=canonical_name(raw["filter_attribute"]),
                operator=fop or "==",
                value=_coerce_filter_value(fop, raw.get("filter_value")),
            )]
        return None

    def _hydrate_derived_attr(raw: dict) -> DerivedAttr:
        """Construct a DerivedAttr from a raw JSON sub-object."""
        return DerivedAttr(
            name=raw["name"],
            aggregation=raw.get("aggregation", "count"),
            attribute=canonical_name(raw["attribute"]) if raw.get("attribute") else "transaction_id",
            window=raw.get("window"),
            filters=_build_filters(raw),
        )

    def _hydrate_computed_attr(raw: dict) -> ComputedAttr:
        """Construct a ComputedAttr from a raw JSON sub-object."""
        return ComputedAttr(
            name=raw["name"],
            aggregation=raw.get("aggregation", "count"),
            attribute=canonical_name(raw["attribute"]) if raw.get("attribute") else "transaction_id",
            filters=_build_filters(raw),
            group_by=canonical_name(raw["group_by"]) if raw.get("group_by") else None,
            window=raw.get("window"),
            window_exclude=raw.get("window_exclude"),
            derived_from=raw.get("derived_from"),  # list of 2 CA names for ratio/difference
            link_attribute=[canonical_name(la) for la in raw["link_attribute"]] if raw.get("link_attribute") else None,
        )

    computed_attrs = [
        _hydrate_computed_attr(ca)
        for ca in (data.get("computed_attrs") or [])
    ]

    conditions = [
        RuleCondition(
            attribute=canonical_name(c["attribute"]) if c.get("attribute") else None,
            operator=c["operator"],
            value=c["value"],
            aggregation=c.get("aggregation"),
            window=c.get("window"),
            logical_connector=c.get("logical_connector", "AND"),
            filters=_build_filters(c),
            group_by=canonical_name(c["group_by"]) if c.get("group_by") else None,
            group_mode=c.get("group_mode", "any"),
            link_attribute=[canonical_name(la) for la in c["link_attribute"]] if c.get("link_attribute") else None,
            derived_attributes=[_hydrate_derived_attr(da) for da in c["derived_attributes"]]
                                if c.get("derived_attributes") else None,
            derived_expression=c.get("derived_expression"),
            window_mode=c.get("window_mode"),
            condition_group=c.get("condition_group", 0),
            condition_group_connector=c.get("condition_group_connector", "OR"),
            computed_attr_name=c.get("computed_attr_name"),
        )
        for c in (data.get("conditions") or [])
    ]

    relevant_attributes = [canonical_name(a) for a in (data.get("relevant_attributes") or [])]

    if data.get("rule_type") == "behavioral":
        log.info(
            "parse_rule | hydrated conditions computed_attr_name values: %s",
            [(i, c.computed_attr_name) for i, c in enumerate(conditions)],
        )
        bad = [i for i, c in enumerate(conditions) if not c.computed_attr_name]
        if bad:
            log.warning(
                "parse_rule | behavioral conditions at indices %s have computed_attr_name=None "
                "(LLM did not link them to a CA). Raw condition dicts: %s",
                bad,
                [data["conditions"][i] for i in bad],
            )

    rule = Rule(
        description=description,
        rule_type=data["rule_type"],
        relevant_attributes=relevant_attributes,
        conditions=conditions,
        raw_expression=data["raw_expression"],
        high_risk_countries=data.get("high_risk_countries") or [],
        computed_attrs=computed_attrs,
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
