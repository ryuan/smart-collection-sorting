from __future__ import annotations

import json
import math


def override(base, changes):
    result = base.copy()
    for key, value in changes.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            value = override(result[key], value)
        result[key] = value
    return result


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("default"), dict):
        raise ValueError("Scoring configuration needs a default entry.")
    profiles = {}
    for name, changes in config.items():
        if not isinstance(changes, dict):
            raise ValueError(f"Expected settings for product type {name}.")
        settings = override(config["default"], changes)
        validate(settings, name)
        profiles[name] = settings
    return profiles


def validate(settings, name):
    sections = {"stock_reward", "value_reward", "decaying_reward", "repeat_value_penalty"}
    if settings.keys() != sections or not all(isinstance(v, dict) for v in settings.values()):
        raise ValueError(f"{name}: expected the four scoring sections.")

    def number(value, field):
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError(f"{name}: expected a finite number for {field}.")
        return value

    for section, rules in settings.items():
        for field, rule in rules.items():
            if type(rule) in (int, float) and rule == 0:
                continue
            if section == "repeat_value_penalty":
                if number(rule, field) > 0:
                    raise ValueError(f"{name}: repeat penalty for {field} must be negative or zero.")
            elif section == "decaying_reward":
                if field not in ("createdAt", "sales") or not isinstance(rule, dict) or rule.keys() != {"reward", "decay_period"}:
                    raise ValueError(f"{name}: expected reward and decay_period for createdAt or sales.")
                number(rule["reward"], field)
                if number(rule["decay_period"], field) <= 0:
                    raise ValueError(f"{name}: decay_period for {field} must be positive.")
            elif section == "stock_reward" and field in ("totalInventory", "inventoryQuantity"):
                number(rule, field)
            elif isinstance(rule, dict) and (section == "value_reward" or "." in field):
                for value, weight in rule.items():
                    number(weight, f"{field}.{value}")
            else:
                raise ValueError(f"{name}: invalid {section} rule for {field}.")


def values(node, field):
    value = node.get(field)
    if isinstance(value, frozenset):
        return value
    return frozenset() if value is None or value == "" else frozenset((str(value),))


def product_values(product, field):
    if field.startswith("__"):
        if field != "__title_first_word":
            raise ValueError(f"Unknown program-generated field {field!r}.")
        words = product.get("title", "").split()
        return frozenset((words[0].casefold(),)) if words else frozenset()
    if field in product:
        return values(product, field)
    return frozenset().union(*(values(v, field) for v in product["variants"]))


def decay(age, rule):
    return rule["reward"] * max(0, 1 - max(age, 0) / rule["decay_period"])


def product_score(product, settings, sales, as_of):
    stocked = [v for v in product["variants"] if v["inventoryQuantity"] > 0]
    stock, value, decaying = {}, {}, {}
    stocked_values = {}
    for field, rule in settings["stock_reward"].items():
        if rule == 0:
            stock[field] = 0
        elif field == "totalInventory":
            stock[field] = rule if product[field] > 0 else 0
        elif field == "inventoryQuantity":
            stock[field] = rule * len(stocked)
        else:
            present = frozenset().union(*(values(v, field) for v in stocked))
            stocked_values[field] = present
            stock[field] = sum(weight for key, weight in rule.items() if key in present)
    for field, rule in settings["value_reward"].items():
        if rule == 0:
            value[field] = 0
            continue
        present = product_values(product, field)
        value[field] = sum(weight for key, weight in rule.items() if key in present)
    for field, rule in settings["decaying_reward"].items():
        if rule == 0:
            decaying[field] = 0
        elif field == "sales":
            decaying[field] = math.fsum(units * decay((as_of - day).days, rule) for day, units in sales.items())
        else:
            decaying[field] = decay((as_of - product[field]).days, rule)

    points = {"stock_reward": stock, "value_reward": value, "decaying_reward": decaying}
    return {"points": points, "base": sum(sum(v.values()) for v in points.values()),
            "stocked": len(stocked), "stocked_values": stocked_values, "units": sum(sales.values())}


def rank_products(products, profiles, sales, as_of):
    settings = {gid: profiles.get(p["productType"], profiles["default"]) for gid, p in products.items()}
    scores = {gid: product_score(p, settings[gid], sales.get(gid, {}), as_of) for gid, p in products.items()}
    fields = {field for profile in settings.values() for field in profile["repeat_value_penalty"]}
    attributes = {gid: {field: product_values(p, field) for field in fields} for gid, p in products.items()}

    # A penalty can only lower a score. Stop scanning when later candidates cannot win.
    remaining = sorted(products, key=lambda gid: (-scores[gid]["base"], gid))
    ranked = []
    while remaining:
        recent = [attributes[gid] for gid in ranked[-12:]]
        best = None
        for index, gid in enumerate(remaining):
            base = scores[gid]["base"]
            if best is not None and base < best:
                break
            penalties = {field: weight for field, weight in settings[gid]["repeat_value_penalty"].items()
                         if weight and attributes[gid][field]
                         and any(attributes[gid][field] == previous[field] for previous in recent)}
            adjusted = base + sum(penalties.values())
            if best is None or adjusted > best:
                best, winner, applied = adjusted, index, penalties
                if best == base:
                    break
        gid = remaining.pop(winner)
        scores[gid]["points"]["repeat_value_penalty"] = applied
        scores[gid]["adjusted"] = best
        ranked.append(gid)
    return ranked, scores
