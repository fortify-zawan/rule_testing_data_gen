"""Loads schema.yml and exposes structured views used by LLM prompts and the validation engine."""
import os
from functools import lru_cache

import yaml

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.yml")


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_SCHEMA_PATH) as f:
        return yaml.safe_load(f)


# ── Attribute lookups ─────────────────────────────────────────────────────────

def all_attributes() -> dict:
    """Returns a merged dict of {canonical_name: {type, description, aliases}} across all entity types."""
    schema = _load()
    merged = {}
    for section in ("transaction_attributes", "user_attributes", "recipient_attributes"):
        merged.update(schema.get(section, {}))
    return merged


def canonical_name(raw: str) -> str:
    """
    Resolve a raw attribute name (possibly an alias) to its canonical name.
    Returns the raw name unchanged if no match is found.
    """
    attrs = all_attributes()
    raw_lower = raw.strip().lower()
    # Direct match
    if raw_lower in attrs:
        return raw_lower
    # Alias match
    for canonical, meta in attrs.items():
        if raw_lower in [a.lower() for a in meta.get("aliases", [])]:
            return canonical
    return raw  # unknown — return as-is


def get_by_type(attr_type: str) -> list[str]:
    """Return all canonical attribute names of a given type (numeric, categorical, datetime, boolean)."""
    return [name for name, meta in all_attributes().items() if meta.get("type") == attr_type]


# ── Aggregation lookups ───────────────────────────────────────────────────────

def supported_aggregations() -> dict:
    """Returns {aggregation_name: {applies_to, description}} from schema."""
    return _load().get("aggregations", {})


def aggregation_names() -> list[str]:
    return list(supported_aggregations().keys())


# ── Country value normalization ───────────────────────────────────────────────

_COUNTRY_FIELDS = {"receive_country_code", "send_country_code", "signup_send_country_code"}

# ISO 3166-1 alpha-2 → full name for countries commonly referenced in AML rules
_ISO_TO_NAME: dict[str, str] = {
    "AF": "Afghanistan", "AL": "Albania", "AO": "Angola", "AM": "Armenia",
    "AZ": "Azerbaijan", "BS": "Bahamas", "BH": "Bahrain", "BD": "Bangladesh",
    "BY": "Belarus", "BZ": "Belize", "BO": "Bolivia", "BA": "Bosnia and Herzegovina",
    "MM": "Myanmar", "KH": "Cambodia", "CM": "Cameroon", "CN": "China",
    "CO": "Colombia", "CD": "Congo", "CG": "Republic of Congo", "CR": "Costa Rica",
    "CU": "Cuba", "DO": "Dominican Republic", "EC": "Ecuador", "EG": "Egypt",
    "SV": "El Salvador", "ER": "Eritrea", "ET": "Ethiopia",
    "GN": "Guinea", "GW": "Guinea-Bissau", "GY": "Guyana", "HT": "Haiti",
    "HN": "Honduras", "IQ": "Iraq", "IR": "Iran", "JM": "Jamaica",
    "KZ": "Kazakhstan", "KG": "Kyrgyzstan", "LA": "Laos", "LB": "Lebanon",
    "LR": "Liberia", "LY": "Libya", "MK": "North Macedonia", "ML": "Mali",
    "MR": "Mauritania", "MX": "Mexico", "MD": "Moldova", "MN": "Mongolia",
    "MA": "Morocco", "MZ": "Mozambique", "NP": "Nepal", "NI": "Nicaragua",
    "NE": "Niger", "NG": "Nigeria", "KP": "North Korea", "OM": "Oman",
    "PK": "Pakistan", "PA": "Panama", "PY": "Paraguay", "PH": "Philippines",
    "PS": "Palestine", "QA": "Qatar", "RU": "Russia", "RW": "Rwanda",
    "SA": "Saudi Arabia", "SN": "Senegal", "SL": "Sierra Leone", "SO": "Somalia",
    "SS": "South Sudan", "SD": "Sudan", "SR": "Suriname", "SY": "Syria",
    "TJ": "Tajikistan", "TZ": "Tanzania", "TH": "Thailand", "TG": "Togo",
    "TT": "Trinidad and Tobago", "TN": "Tunisia", "TR": "Turkey",
    "TM": "Turkmenistan", "UG": "Uganda", "UA": "Ukraine",
    "AE": "United Arab Emirates", "UZ": "Uzbekistan", "VE": "Venezuela",
    "VN": "Vietnam", "YE": "Yemen", "ZM": "Zambia", "ZW": "Zimbabwe",
    # Common non-flagged countries
    "US": "United States", "GB": "United Kingdom", "DE": "Germany",
    "FR": "France", "IT": "Italy", "ES": "Spain", "NL": "Netherlands",
    "CA": "Canada", "AU": "Australia", "IN": "India", "JP": "Japan",
    "SG": "Singapore", "ZA": "South Africa",
}


def normalize_country_values(attrs: dict, high_risk_countries: list[str] | None = None) -> dict:
    """
    For country-type fields, map ISO 2-letter codes to full country names.
    Uses high_risk_countries (from the rule) as the preferred source of exact casing,
    falling back to the built-in mapping table.
    """
    # Build a lookup that prefers exact strings from the rule
    iso_map = dict(_ISO_TO_NAME)
    for name in (high_risk_countries or []):
        for iso, default_name in _ISO_TO_NAME.items():
            if default_name.lower() == name.lower():
                iso_map[iso] = name  # use exact casing from the rule

    result = {}
    for k, v in attrs.items():
        if k in _COUNTRY_FIELDS and isinstance(v, str) and len(v) == 2 and v.upper() in iso_map:
            result[k] = iso_map[v.upper()]
        else:
            result[k] = v
    return result


# ── Prompt formatting ─────────────────────────────────────────────────────────

def format_attributes_for_prompt(show_aliases: bool = True) -> str:
    """
    Returns a compact attribute reference table for injecting into LLM prompts.
    Groups by entity type and shows canonical name, type, and common aliases.
    Set show_aliases=False for generation prompts where aliases cause key confusion.
    """
    schema = _load()
    lines = ["CANONICAL ATTRIBUTE SCHEMA (use ONLY these exact names as JSON keys — do not use aliases or invent others):"]

    for section, label in [
        ("transaction_attributes", "Transaction"),
        ("user_attributes", "User"),
        ("recipient_attributes", "Recipient"),
    ]:
        attrs = schema.get(section, {})
        if not attrs:
            continue
        lines.append(f"\n{label} attributes:")
        for name, meta in attrs.items():
            if show_aliases:
                aliases = ", ".join(meta.get("aliases", [])[:3])
                alias_str = f" (also known as: {aliases})" if aliases else ""
            else:
                alias_str = ""
            lines.append(f"  {name} [{meta['type']}]{alias_str} — {meta['description']}")

    return "\n".join(lines)


def format_aggregations_for_prompt() -> str:
    """Returns a compact aggregation reference for injecting into LLM prompts."""
    aggs = supported_aggregations()
    lines = ["SUPPORTED AGGREGATIONS (use ONLY these — do not use average_per_day, std_dev, etc.):"]
    for name, meta in aggs.items():
        desc = str(meta.get("description", "")).replace("\n", " ").strip()
        lines.append(f"  {name} [{meta['applies_to']}] — {desc}")
    return "\n".join(lines)
