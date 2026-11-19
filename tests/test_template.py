"""Consistency checks between the Zabbix template YAML and the pusher.

The template (``templates/template_certinext/7.0/template_certinext.yaml``)
is hand-written, and nothing at push time catches a key that exists on one
side only: Zabbix just rejects the value. These tests parse the YAML and
check it against the ``KEY_*`` constants in
:mod:`certinext_zabbix.zabbix_push`, plus the structural mistakes that make
a hand-written template fail to import (undefined macros, dangling trigger
dependencies, malformed UUIDs).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from certinext_zabbix import zabbix_push

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "templates" / "template_certinext" / "7.0" / "template_certinext.yaml"
)
TEMPLATE_NAME = "CertiNext DCV by Zabbix trapper"
SENDER_MACRO = "{$CERTINEXT.SENDER.ALLOWED}"

#: Matches a user macro, capturing its name without any ``:context``.
_MACRO_RE = re.compile(r"\{\$([A-Z0-9_.]+)(?::[^}]*)?\}")
#: Matches an LLD macro such as ``{#APEX}``.
_LLD_MACRO_RE = re.compile(r"\{#[A-Z0-9_.]+\}")
#: Matches the item key in a trigger function's ``/host/key`` reference.
_ITEM_REF_RE = re.compile(r"/" + re.escape(TEMPLATE_NAME) + r"/([a-z0-9_.]+(?:\[[^\]]*\])?)")


@pytest.fixture(scope="module")
def export() -> dict[str, Any]:
    """Parse the import file once per module.

    Returns:
        The ``zabbix_export`` mapping.
    """
    with TEMPLATE_PATH.open(encoding="utf-8") as fh:
        parsed: dict[str, Any] = yaml.safe_load(fh)["zabbix_export"]
    return parsed


@pytest.fixture(scope="module")
def template(export: dict[str, Any]) -> dict[str, Any]:
    """Return the single template definition from the import file.

    Args:
        export: The parsed ``zabbix_export`` mapping.

    Returns:
        The one entry of ``zabbix_export.templates``.
    """
    assert len(export["templates"]) == 1
    tpl: dict[str, Any] = export["templates"][0]
    return tpl


def _push_keys() -> dict[str, str]:
    """Collect the ``KEY_*`` constants the pusher sends.

    Returns:
        Mapping of constant name to item key.
    """
    return {
        name: value
        for name, value in vars(zabbix_push).items()
        if name.startswith("KEY_") and isinstance(value, str)
    }


def _discovery_rules(tpl: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the template's discovery rules (an empty list if none).

    Args:
        tpl: The template definition.

    Returns:
        The ``discovery_rules`` entries.
    """
    rules: list[dict[str, Any]] = tpl.get("discovery_rules", [])
    return rules


def _all_triggers(export: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every trigger and trigger prototype in the import file.

    Covers single-item triggers nested under items and item prototypes,
    multi-item trigger prototypes on a discovery rule, and the top-level
    multi-item ``triggers`` list.

    Args:
        export: The parsed ``zabbix_export`` mapping.

    Yields:
        Each trigger or trigger-prototype mapping.
    """
    tpl = export["templates"][0]
    for item in tpl.get("items", []):
        yield from item.get("triggers", [])
    for rule in _discovery_rules(tpl):
        for proto in rule.get("item_prototypes", []):
            yield from proto.get("trigger_prototypes", [])
        yield from rule.get("trigger_prototypes", [])
    yield from export.get("triggers", [])


def _walk(node: Any) -> Iterator[Any]:
    """Yield every scalar and mapping in a parsed YAML tree.

    Args:
        node: Any parsed YAML node.

    Yields:
        Each nested node, depth first.
    """
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class TestKeySync:
    """The pusher's ``KEY_*`` constants and the template's trapper keys match."""

    def test_every_push_key_is_in_the_template(self, template: dict[str, Any]) -> None:
        """Each ``KEY_*`` value is a trapper item key or a discovery rule key."""
        trap_keys = {i["key"] for i in template["items"] if i["type"] == "TRAP"}
        rule_keys = {r["key"] for r in _discovery_rules(template) if r["type"] == "TRAP"}
        missing = {n: k for n, k in _push_keys().items() if k not in trap_keys | rule_keys}
        assert not missing, f"KEY_* constants with no trapper item/rule in the template: {missing}"

    def test_every_trapper_key_has_a_push_constant(self, template: dict[str, Any]) -> None:
        """No trapper item or rule waits for a key the pusher never sends."""
        sent = set(_push_keys().values())
        trap_keys = {i["key"] for i in template["items"] if i["type"] == "TRAP"}
        trap_keys |= {r["key"] for r in _discovery_rules(template) if r["type"] == "TRAP"}
        assert not trap_keys - sent, f"template trapper keys with no KEY_* constant: {trap_keys - sent}"

    def test_trapper_keys_are_bare(self, template: dict[str, Any]) -> None:
        """Trapper keys carry no parameter: the trapper does not expand macros in keys."""
        bracketed = [i["key"] for i in template["items"] if i["type"] == "TRAP" and "[" in i["key"]]
        assert not bracketed

    def test_persist_discovery_is_a_discovery_rule(self, template: dict[str, Any]) -> None:
        """The apex list feeds an LLD rule, not a plain item."""
        rule_keys = [r["key"] for r in _discovery_rules(template)]
        assert zabbix_push.KEY_PERSIST_DISCOVERY in rule_keys

    def test_trappers_restrict_senders(self, template: dict[str, Any]) -> None:
        """Every trapper item and rule sets allowed_hosts (empty rejects all senders)."""
        trappers = [i for i in template["items"] if i["type"] == "TRAP"]
        trappers += [r for r in _discovery_rules(template) if r["type"] == "TRAP"]
        assert all(t.get("allowed_hosts") == SENDER_MACRO for t in trappers)


class TestMacros:
    """Template macros are defined, and the new ones exist with safe defaults."""

    @pytest.mark.parametrize("name", [
        "{$CERTINEXT.ENV}",
        "{$CERTINEXT.DNS.RESOLVER.1}",
        "{$CERTINEXT.DNS.RESOLVER.2}",
        "{$CERTINEXT.DNS.INTERVAL.LOCAL}",
        "{$CERTINEXT.DNS.INTERVAL.EXTERNAL}",
        "{$CERTINEXT.DNS.NODATA}",
        "{$CERTINEXT.ORDER.DAYS_SINCE_ISSUED_MAX}",
    ])
    def test_new_macro_defined(self, template: dict[str, Any], name: str) -> None:
        """Each macro this change introduced is declared on the template."""
        assert name in {m["macro"] for m in template["macros"]}

    def test_every_referenced_macro_is_defined(
        self, export: dict[str, Any], template: dict[str, Any],
    ) -> None:
        """No name, key, expression or description uses an undeclared macro."""
        defined = {m["macro"][2:-1] for m in template["macros"]}
        used = {
            m.group(1)
            for node in _walk(export) if isinstance(node, str)
            for m in _MACRO_RE.finditer(node)
        }
        assert not used - defined, f"undeclared macros: {used - defined}"

    def test_no_hand_set_persist_macro(self, export: dict[str, Any]) -> None:
        """The expected TXT value comes from the pusher, never a user macro (#12)."""
        hits = [n for n in _walk(export) if isinstance(n, str) and "{$CERTINEXT.PERSIST.EXPECTED" in n]
        assert not hits

    def test_macros_sorted(self, template: dict[str, Any]) -> None:
        """Macros are in Zabbix's export order, so a re-import doesn't report a change."""
        names = [m["macro"] for m in template["macros"]]
        assert names == sorted(names)


class TestTriggers:
    """Trigger expressions and dependencies resolve against this template."""

    def test_expressions_reference_existing_keys(
        self, export: dict[str, Any], template: dict[str, Any],
    ) -> None:
        """Every ``/template/key`` in an expression is an item or item prototype."""
        keys = {i["key"] for i in template["items"]}
        for rule in _discovery_rules(template):
            keys |= {p["key"] for p in rule.get("item_prototypes", [])}
        for trig in _all_triggers(export):
            refs = _ITEM_REF_RE.findall(trig["expression"])
            assert refs, f"no item reference in {trig['name']!r}"
            assert set(refs) <= keys, f"{trig['name']!r} references unknown keys {set(refs) - keys}"

    def test_dependencies_match_a_trigger(self, export: dict[str, Any]) -> None:
        """Each dependency's name and expression match a defined trigger exactly."""
        defined = {(t["name"], t["expression"]) for t in _all_triggers(export)}
        for trig in _all_triggers(export):
            for dep in trig.get("dependencies", []):
                assert (dep["name"], dep["expression"]) in defined, f"dangling dependency in {trig['name']!r}"

    def test_days_left_triggers_gated_on_token_domains(self, export: dict[str, Any]) -> None:
        """Every min_days_left trigger also requires token_domains > 0."""
        gated = [t for t in _all_triggers(export) if zabbix_push.KEY_MIN_DAYS_LEFT + ")" in t["expression"]]
        assert len(gated) == 4
        gate = f"last(/{TEMPLATE_NAME}/{zabbix_push.KEY_TOKEN_DOMAINS})>0"
        assert all(gate in t["expression"] for t in gated)

    def test_no_lld_macro_in_unquoted_macro_context(self, export: dict[str, Any]) -> None:
        """No user macro carries an LLD macro as its (unquoted) context.

        Zabbix ends an unquoted context at the first ``}``, so
        ``{$M:{#APEX}}`` parses as context ``{#APEX`` plus a stray ``}``:
        LLD never substitutes it and a ``find()`` pattern built from it
        can never match (0.2.0rc1, every apex alerted on first lookup).
        """
        bad = [
            node for node in _walk(export)
            if isinstance(node, str) and re.search(r"\{\$[A-Z0-9_.]+:\{#", node)
        ]
        assert not bad, f"LLD macro in an unquoted user-macro context: {bad}"

    def test_persist_find_gated_on_full_window(self, export: dict[str, Any]) -> None:
        """Each dns-persist ``find(item,#N)`` is paired with ``count(item,#N)>=N``.

        ``find(…,#3)`` evaluates on however many values exist, so without
        the gate a freshly discovered apex alerts on its first miss.
        """
        finds = [
            (t["name"], t["expression"]) for t in _all_triggers(export)
            if "_validation-persist" in t["expression"] and "find(" in t["expression"]
        ]
        assert len(finds) == 2
        for name, expr in finds:
            calls = re.findall(r"find\((/[^/]+/[a-z0-9_.]+\[[^\]]*\]),#(\d+),", expr)
            assert calls, name
            for ref, n in calls:
                gate = f"count({ref},#{n})>={n}"
                assert gate in expr, f"{name!r}: no full-window gate for find({ref},#{n})"

    def test_misroute_guard_compares_env(self, export: dict[str, Any]) -> None:
        """A run sent to the wrong environment's host raises a HIGH problem."""
        guard = [
            t for t in _all_triggers(export)
            if f"/{zabbix_push.KEY_ENV})" in t["expression"]
        ]
        assert len(guard) == 1
        assert '<>"{$CERTINEXT.ENV}"' in guard[0]["expression"]
        assert guard[0]["priority"] == "HIGH"

    def test_persist_find_matches_pusher_value(self, export: dict[str, Any]) -> None:
        """Every dns-persist ``find()`` matches the quoted ``{#PERSIST.EXPECTED}`` (#12).

        The pusher sends the CA's value per discovery row; the template adds
        the TXT quotes, so the pattern is the whole quoted string.
        """
        patterns = [
            m.group(1)
            for t in _all_triggers(export) if "_validation-persist" in t["expression"]
            for m in re.finditer(r'find\([^)]*,"like",("(?:\\.|[^"\\])*")\)', t["expression"])
        ]
        assert len(patterns) == 3  # record missing, 2 x not visible publicly
        assert set(patterns) == {r'"\"{#PERSIST.EXPECTED}\""'}

    def test_persist_find_alerts_on_absence(self, export: dict[str, Any]) -> None:
        """Every dns-persist ``find()`` is compared ``=0``: alert when the value is absent.

        ``=1`` would alert while the record is healthy and stay quiet when
        it's gone, and the pattern test above would still pass (Codex R3
        finding 3). Dependency copies are held equal to their trigger by
        ``test_dependencies_match_a_trigger``.
        """
        comparisons = [
            m.group(1)
            for t in _all_triggers(export) if "_validation-persist" in t["expression"]
            for m in re.finditer(r'find\([^)]*,"like","(?:\\.|[^"\\])*"\)([<>=]*\d*)', t["expression"])
        ]
        assert len(comparisons) == 3
        assert comparisons == ["=0"] * 3


class TestStructure:
    """Import-file details that Zabbix enforces on a hand-written file."""

    def test_uuids_are_unique_and_well_formed(self, export: dict[str, Any]) -> None:
        """Every uuid is 32 lowercase hex characters and appears once."""
        uuids = [n["uuid"] for n in _walk(export) if isinstance(n, dict) and "uuid" in n]
        assert all(re.fullmatch(r"[0-9a-f]{32}", u) for u in uuids)
        assert len(uuids) == len(set(uuids))

    def test_everything_has_a_uuid(
        self, export: dict[str, Any], template: dict[str, Any],
    ) -> None:
        """Items, triggers, rules and prototypes all carry a uuid."""
        nodes: list[dict[str, Any]] = [*template["items"], *_all_triggers(export)]
        for rule in _discovery_rules(template):
            nodes += [rule, *rule.get("item_prototypes", [])]
        assert all("uuid" in n for n in nodes)

    def test_lost_apexes_deleted_not_disabled(self, template: dict[str, Any]) -> None:
        """The persist rule keeps lost apexes enabled and deletes them after 3 days."""
        rule = next(r for r in _discovery_rules(template) if r["key"] == zabbix_push.KEY_PERSIST_DISCOVERY)
        assert rule["lifetime"] == "3d"
        assert rule["enabled_lifetime_type"] == "DISABLE_NEVER"

    def test_missing_record_maps_to_empty_value(self, template: dict[str, Any]) -> None:
        """Only "Cannot perform DNS query" becomes "": scope 0 = error matches (7.0 API).

        Scope 1 would invert it: a missing record stays unsupported while a
        resolver timeout reads as a missing record (Codex R1 finding 1).
        """
        rule = next(r for r in _discovery_rules(template) if r["key"] == zabbix_push.KEY_PERSIST_DISCOVERY)
        for proto in rule["item_prototypes"]:
            (step,) = proto["preprocessing"]
            assert step["type"] == "CHECK_NOT_SUPPORTED"
            assert step["parameters"] == ["0", "^Cannot perform DNS query"]
            assert step["error_handler"] == "CUSTOM_VALUE"
            assert step["error_handler_params"] == ""

    def test_lld_macros_are_ones_the_pusher_sends(self, template: dict[str, Any]) -> None:
        """Every {#MACRO} in the persist rule is a column of the pusher's discovery rows."""
        row = json.loads(zabbix_push.persist_discovery_value({"example.edu": "ssl.com"}))[0]
        rule = next(r for r in _discovery_rules(template) if r["key"] == zabbix_push.KEY_PERSIST_DISCOVERY)
        used = {m for n in _walk(rule) if isinstance(n, str) for m in _LLD_MACRO_RE.findall(n)}
        assert used, "no LLD macros found in the persist rule"
        assert used <= set(row), f"LLD macros the pusher never sends: {used - set(row)}"

    def test_apex_drop_window_outlasts_lifetime(self, template: dict[str, Any]) -> None:
        """The count-drop trigger looks back further than the rule keeps lost apexes.

        Otherwise a partial list that persists would resolve the problem
        before Zabbix deletes the missing apexes' checks.
        """
        item = next(i for i in template["items"] if i["key"] == zabbix_push.KEY_PERSIST_APEXES)
        (trig,) = item["triggers"]
        window = re.search(r"max\(/[^,]+,(\d+)d\)", trig["expression"])
        assert window, trig["expression"]
        rule = next(r for r in _discovery_rules(template) if r["key"] == zabbix_push.KEY_PERSIST_DISCOVERY)
        assert int(window.group(1)) > int(rule["lifetime"].removesuffix("d"))

    def test_persist_prototypes_check_three_resolvers(self, template: dict[str, Any]) -> None:
        """One local and two external TXT lookups per discovered apex."""
        rule = next(r for r in _discovery_rules(template) if r["key"] == zabbix_push.KEY_PERSIST_DISCOVERY)
        keys = sorted(p["key"] for p in rule["item_prototypes"])
        assert keys == sorted([
            "net.dns.record[,_validation-persist.{#APEX},TXT]",
            "net.dns.record[{$CERTINEXT.DNS.RESOLVER.1},_validation-persist.{#APEX},TXT]",
            "net.dns.record[{$CERTINEXT.DNS.RESOLVER.2},_validation-persist.{#APEX},TXT]",
        ])


#: Width of a Zabbix 7.0 dashboard grid, in columns.
_DASHBOARD_COLUMNS = 72


def _widgets(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every widget on every page of the template's dashboards.

    Args:
        template: The template definition.

    Returns:
        The widgets, in file order.
    """
    return [
        w
        for dash in template.get("dashboards", [])
        for page in dash["pages"]
        for w in page["widgets"]
    ]


class TestDashboard:
    """The template dashboard: Zabbix shows it on each linked host (one per environment)."""

    def test_one_dashboard(self, template: dict[str, Any]) -> None:
        """The template ships exactly one dashboard, with widgets."""
        assert len(template["dashboards"]) == 1
        assert _widgets(template)

    def test_item_fields_reference_template_items(self, template: dict[str, Any]) -> None:
        """Every ITEM field names this template and one of its own item keys.

        A wrong host or key fails the import, or silently drops the widget's data.
        """
        keys = {i["key"] for i in template["items"]}
        refs = [
            f["value"]
            for w in _widgets(template)
            for f in w.get("fields", [])
            if f["type"] == "ITEM"
        ]
        assert refs
        for ref in refs:
            assert ref["host"] == TEMPLATE_NAME
            assert ref["key"] in keys, f"unknown item key {ref['key']!r}"

    def test_widgets_fit_the_grid_without_overlap(self, template: dict[str, Any]) -> None:
        """Widgets stay inside the 72-column grid and no two share a cell."""
        cells: set[tuple[int, int]] = set()
        for w in _widgets(template):
            x, y = int(w.get("x", 0)), int(w.get("y", 0))
            width, height = int(w["width"]), int(w["height"])
            assert x + width <= _DASHBOARD_COLUMNS, w["name"]
            mine = {(cx, cy) for cx in range(x, x + width) for cy in range(y, y + height)}
            assert not cells & mine, f"{w['name']!r} overlaps another widget"
            cells |= mine

    def test_graph_references_unique(self, template: dict[str, Any]) -> None:
        """Each SVG graph has a unique 5-letter ``reference`` (7.0 requires one)."""
        refs = [
            f["value"]
            for w in _widgets(template) if w["type"] == "svggraph"
            for f in w["fields"] if f["name"] == "reference"
        ]
        assert len(refs) == sum(1 for w in _widgets(template) if w["type"] == "svggraph")
        assert all(re.fullmatch(r"[A-Z]{5}", r) for r in refs)
        assert len(refs) == len(set(refs))

    def test_every_graph_item_has_a_color(self, template: dict[str, Any]) -> None:
        """An SVG data set has one color per item, so no series falls back to a default."""
        for w in _widgets(template):
            if w["type"] != "svggraph":
                continue
            names = [f["name"] for f in w["fields"]]
            items = [n for n in names if re.fullmatch(r"ds\.0\.itemids\.\d+", n)]
            colors = [n for n in names if re.fullmatch(r"ds\.0\.color\.\d+", n)]
            assert len(items) == len(colors), w["name"]

    def test_value_decimals_match_item_type(self, template: dict[str, Any]) -> None:
        """Whole-number items show 0 decimals; float items keep the default.

        The Item value widget defaults to 2 decimals, which rendered counts
        as "21.00" on the first live import (2026-11-19).
        """
        float_keys = {i["key"] for i in template["items"] if i.get("value_type") == "FLOAT"}
        for w in _widgets(template):
            if w["type"] != "item":
                continue
            fields = {f["name"]: f for f in w["fields"]}
            key = fields["itemid.0"]["value"]["key"]
            decimals = fields.get("decimal_places", {}).get("value")
            if key in float_keys:
                assert decimals is None, f"{w['name']!r}: float item, keep the default decimals"
            else:
                assert decimals == "0", f"{w['name']!r}: count item needs decimal_places 0"

    def test_fields_in_canonical_order(self, template: dict[str, Any]) -> None:
        """Fields are sorted by name, as Zabbix exports them.

        Out-of-order fields import fine but make every later import report
        a change.
        """
        for w in _widgets(template):
            names = [f["name"] for f in w.get("fields", [])]
            assert names == sorted(names), w["name"]
