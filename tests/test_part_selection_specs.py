"""Passive checks use synthetic stock and saved fields, without CAD or I/O."""

from copy import deepcopy

import pytest

from part_selection_specs import validate_passive_specs


@pytest.fixture
def validate():
    return validate_passive_specs


def row(ref="C1", value="0.1uF", footprint="DSMU:0603", **extra):
    return {"refs": ref, "value": value, "footprint": footprint, **extra}


def stock(detail="Capacitance: 100nF", package="0603", **extra):
    return {"ok": True, "c_code": "C123", "param_detail": detail, "package": package, **extra}


def test_equivalent_nominal_and_package_match(validate):
    assert validate(row(), stock()) == {
        "status": "matched", "issues": [], "unknown_fields": [],
    }


def test_explanations_name_source_and_original_unrecognized_value(validate):
    result = validate(row(value='MODEL, 1nF, 250V, C0G'), stock('Capacitance: 1nF'), explain=True)
    assert result['status'] == 'unknown'
    details = result['unknown_details']
    assert any(d['field']=='value' and d['source']=='schematic' and 'MODEL, 1nF' in d['message'] for d in details)
    assert any(d['field']=='dielectric' and 'C0G' in d['message'] for d in details)


def test_verified_model_prefix_does_not_hide_nominal_or_missing_dielectric(validate):
    result = validate(row('C1,C2', value='MODEL-C0G, 1nF, 250V, C0G'),
        stock('Capacitance: 1nF; 温度系数: -', model='MODEL-C0G'), explain=True)
    assert result['unknown_fields'] == ['dielectric']
    assert result['unknown_details'][0]['refs'] == ['C1','C2']
    assert '温度系数' in result['unknown_details'][0]['message']
    assert '未提供' in result['unknown_details'][0]['message']
    assert validate(row(value='MODEL-C0G'), stock(model='MODEL-C0G'))['status'] == 'unknown'


def test_unrecognized_stock_and_complex_package_explain_separate_causes(validate):
    result = validate(row(footprint='Local:SMD-2G'), stock('Capacitance: -', package='SMD,D5xL5.4mm'), explain=True)
    assert any(d['source']=='stock' and '容值' in d['message'] for d in result['unknown_details'])
    assert any(d['source']=='schematic' and 'SMD-2G' in d['message'] for d in result['unknown_details'])
    assert any(d['source']=='stock' and 'SMD,D5xL5.4mm' in d['message'] for d in result['unknown_details'])


def test_through_hole_product_size_explains_missing_footprint_geometry(validate):
    result = validate(row(ref='R618', value='MMFR2568B500KV9'),
                      stock('Resistance: 5kΩ', package='插件,D2.5xL6.8mm'), explain=True)
    messages = [d['message'] for d in result['unknown_details']
                if d['source'] == 'stock' and d['field'] == 'footprint']
    assert messages
    assert '仅提供本体外形尺寸' in messages[0]
    assert '焊盘间距、钻孔直径和引脚方向未提供' in messages[0]


def test_explicit_dielectric_mismatch_and_np0_equivalence(validate):
    assert validate(row(value='1nF, C0G'), stock('Capacitance: 1nF; 温度系数: NP0'))['status'] == 'matched'
    result = validate(row(value='1nF, C0G'), stock('Capacitance: 1nF; 温度系数: X7R'))
    assert result['status'] == 'mismatch'
    assert result['issues'][0]['field'] == 'dielectric'


@pytest.mark.parametrize('detail,reason', [('Capacitance: -', '未提供'),
    ('Capacitance: 1nF; Capacitance: 2nF', '多项冲突'), ('Capacitance: 50V', '无法唯一识别')])
def test_stock_nominal_reasons_are_specific(validate, detail, reason):
    result = validate(row(), stock(detail), explain=True)
    assert reason in result['unknown_details'][0]['message']


@pytest.mark.parametrize('model,value', [('OTHER', 'MODEL, 100nF'), ('MODEL', 'MODEL100nF'),
    ('MODEL', 'MODEL'), ('MODEL', 'MODEL, 100nF / 1uF')])
def test_model_prefix_cannot_guess_or_hide_ambiguous_nominal(validate, model, value):
    assert validate(row(value=value), stock(model=model))['status'] == 'unknown'


def test_missing_saved_field_and_failed_lookup_are_explained_without_edit_fallback(validate):
    result = validate(row(), stock(), {'C1': {'footprint': 'DSMU:0603'}}, explain=True)
    assert result['unknown_details'][0]['source'] == 'schematic'
    assert 'Value 未填写' in result['unknown_details'][0]['message']
    result = validate(row(), stock(ok=False, error='timeout'), explain=True)
    assert {d['field'] for d in result['unknown_details']} == {'value', 'footprint'}
    assert all('timeout' in d['message'] for d in result['unknown_details'])


def test_unknown_details_remain_when_another_field_is_mismatched(validate):
    result = validate(row('C1,C2', value='100nF, C0G'), stock('Capacitance: 1uF'), explain=True)
    assert result['status'] == 'mismatch'
    assert result['issues'][0]['field'] == 'value'
    assert result['unknown_fields'] == ['dielectric']
    assert result['unknown_details'][0]['refs'] == ['C1', 'C2']


@pytest.mark.parametrize('detail', ['温度系数: -', '温度系数: X7R; Dielectric: C0G', '温度系数: C0G/X7R'])
def test_missing_or_conflicting_dielectric_is_not_matched(validate, detail):
    result = validate(row(value='1nF, C0G'), stock('Capacitance: 1nF; ' + detail), explain=True)
    assert result['status'] == 'unknown'
    assert result['unknown_fields'] == ['dielectric']


def test_no_dielectric_required_for_electrolytic(validate):
    assert validate(row(value='22uF, 35V'), stock('Capacitance: 22uF'))['status'] == 'matched'


@pytest.mark.parametrize('value', ['5.1k, 5ppm', '5.1kΩ, ±5ppm/℃',
    '5.1k, +/-5 ppm/°C, ±0.1%, 100V, 100mW', '5.1k, TCR: ±5ppm/K'])
def test_resistor_tcr_is_a_qualifier_not_another_resistance(validate, value):
    assert validate(row('R1262', value), stock('Resistance: 5.1kΩ'))['status'] == 'matched'
    assert validate(row('R1262', value), stock('Resistance: 10kΩ'))['status'] == 'mismatch'


@pytest.mark.parametrize('value', ['5.1k, 5ppm 10k', '5.1k, 5ppm/model',
    '5.1k, 5ppm/invalid', '5.1k, 5ppms', '5.1k, TCR: not specified'])
def test_tcr_does_not_hide_ambiguous_or_unrecognized_tails(validate, value):
    assert validate(row('R1', value), stock('Resistance: 5.1kΩ'))['status'] == 'unknown'


def test_resistor_tcr_is_not_a_capacitor_qualifier(validate):
    assert validate(row(value='100nF, 5ppm'), stock())['status'] == 'unknown'


@pytest.mark.parametrize("value", [
    "4.7uH, Rated Current: 800mA, Saturation Current: 1A",
    "4.7uH, Rated Current: 800mA, Saturation Current: not specified",
])
def test_inductor_current_labels_preserve_nominal_comparison(validate, value):
    assert validate(row("L1", value), stock("Inductance: 4.7uH"))["status"] == "matched"


def test_inductor_current_numbers_cannot_hide_wrong_inductance(validate):
    value = "100uH, Rated Current: 32A, Saturation Current: 45A"
    result = validate(row("L1", value), stock("Inductance: 100nH"))
    assert result["status"] == "mismatch"
    assert result["issues"][0]["expected"] == value


@pytest.mark.parametrize('value', ['10uH, DCR: 50mΩ, Type: 屏蔽电感',
    '10uH, DCR: 0.05ohm, Type: Wirewound', '10uH, Type: 一体成型电感'])
def test_inductor_copied_dcr_and_type_preserve_nominal_check(validate, value):
    assert validate(row('L1', value), stock('Inductance: 10uH'))['status'] == 'matched'
    assert validate(row('L1', value), stock('Inductance: 100uH'))['status'] == 'mismatch'


@pytest.mark.parametrize('value', ['10uH, DCR: 50uH', '10uH, Type: 2x10uH',
    '10uH, Type: Shielded 20uH', '10uH, Type: Shielded/20uH'])
def test_inductor_labels_cannot_hide_another_inductance(validate, value):
    assert validate(row('L1', value), stock('Inductance: 10uH'))['status'] == 'unknown'


@pytest.mark.parametrize("ref,value,detail", [
    ("L1", "4.7uH, Saturation Current: 1uH", "Inductance: 4.7uH"),
    ("C1", "4.7uF, Rated Current: 1A", "Capacitance: 4.7uF"),
])
def test_current_labels_do_not_swallow_other_quantities(validate, ref, value, detail):
    assert validate(row(ref, value), stock(detail))["status"] == "unknown"


@pytest.mark.parametrize("ref,actual,detail", [
    ("C1", "0.1uF", "Capacitance: 100nF"),
    ("C1", "0.1\u03bcF", "\u5bb9\u503c: 100nF"),
    ("C1", "0.1\u00b5F", "\u7535\u5bb9\u91cf: 100nF"),
    ("C1", "100\u7eb3\u6cd5", "\u7535\u5bb9\u503c: 0.1\u5fae\u6cd5"),
    ("C1", "100000pF", "Capacity: 100nF"),
    ("C1", "100nF", "\u5bb9\u91cf: 0.1uF"),
    ("C1", "100nF", "\u7535\u5bb9\u5bb9\u91cf: 0.1uF"),
    ("C1", "4n7", "Capacitance: 4.7nF"),
    ("R1", "100K", "Resistance: 100k"),
    ("R1", "100", "\u963b\u503c: 100R"),
    ("R1", "4k7", "Resistance: 4.7kOhm"),
    ("R1", "4R7", "Resistance: 4.7\u03a9"),
    ("R1", "0R", "Resistance: 0 ohms"),
    ("R1", "1M", "Resistance: 1000k\u03a9"),
    ("R1", "100m\u03a9", "Resistance: 0.1ohm"),
    ("R1", "100\u5343\u6b27", "\u7535\u963b\u503c: 0.1\u5146\u6b27\u59c6"),
    ("L1", "4u7H", "Inductance: 4700nH"),
    ("L1", "10\u5fae\u4ea8", "\u611f\u503c: 0.01mH"),
    ("L1", "1mH", "\u7535\u611f\u91cf: 1000uH"),
])
def test_numeric_equivalences(validate, ref, actual, detail):
    assert validate(row(ref, actual), stock(detail))["status"] == "matched"


@pytest.mark.parametrize("ref,value,detail", [
    ("R1", "1m", "Resistance: 1M"),
    ("C1", "1mF", "Capacitance: 1MF"),
    ("L1", "1mH", "Inductance: 1MH"),
    ("C1", "1uF", "Capacitance: 100nF"),
])
def test_nominal_mismatch_including_mega_vs_milli(validate, ref, value, detail):
    result = validate(row(ref, value), stock(detail))
    assert result["status"] == "mismatch"
    assert [issue["field"] for issue in result["issues"]] == ["value"]
    assert result["issues"][0]["refs"] == [ref]
    assert result["issues"][0]["expected"] == value
    assert result["issues"][0]["actual"]


@pytest.mark.parametrize("footprint", [
    "DSMU:0603", "Resistor_SMD:R_0603_1608Metric", "R0603",
    "Capacitor_SMD:C_0603_1608Metric", "1608Metric", "0603",
])
def test_known_simple_package_aliases(validate, footprint):
    assert validate(row(footprint=footprint), stock())["status"] == "matched"


@pytest.mark.parametrize("footprint", [
    "DSMU:1808", "1808", "C1808", "Capacitor_SMD:C_1808_4520Metric", "4520Metric",
])
def test_high_voltage_capacitor_1808_package_is_comparable(validate, footprint):
    actual = {ref: {"value": "100pF, 3kV, C0G", "footprint": footprint}
              for ref in ("C1", "C2")}
    result = validate(row("C1,C2", "100pF, 3kV, C0G", footprint),
                      stock("Capacitance: 100pF; Rated Voltage: 3kV; Dielectric: C0G", package="1808"), actual)
    assert result == {"status": "matched", "issues": [], "unknown_fields": []}


def test_1808_cannot_match_1812_or_inconsistent_metric_alias(validate):
    result = validate(row(footprint="DSMU:1808"), stock(package="1812"))
    assert result["status"] == "mismatch"
    assert result["issues"][0]["field"] == "footprint"
    assert validate(row(footprint="C_1808_4532Metric"), stock(package="1808"))["status"] == "unknown"


def test_package_mismatch_is_actionable(validate):
    result = validate(row(footprint="DSMU:0805"), stock())
    assert result == {"status": "mismatch", "issues": [{
        "field": "footprint", "refs": ["C1"],
        "expected": "DSMU:0805", "actual": "0603",
    }], "unknown_fields": []}


@pytest.mark.parametrize("footprint,package", [
    ("Custom:Special_0603_Array", "0603"),
    ("Custom:Special_0603_Array", "Custom:Special_0603_Array"),
    ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "0603"),
    ("Resistor_SMD:R_0603_1608Metric_Pad1.08x0.95mm_HandSolder", "0603"),
    ("DSMU:0603", "1608"),
    ("DSMU:0603", "0603/0805"),
    ("DSMU:0603", "SOT-23-6"),
    ("DSMU:0603", ""),
    ("", "0603"),
])
def test_unknown_complex_or_ambiguous_package_is_not_compared(validate, footprint, package):
    assert validate(row(footprint=footprint), stock(package=package)) == {
        "status": "unknown", "issues": [], "unknown_fields": ["footprint"],
    }


@pytest.mark.parametrize("value,detail,param", [
    ("0.1uF 50V +/-10%", "Capacitance: 100nF; Voltage: 50V; Tolerance: 10%", ""),
    ("100nF/50V", "\u5bb9\u503c: 100nF; \u989d\u5b9a\u7535\u538b: 50V", ""),
    ("100nF", "Tolerance: 10%; Voltage: 50V", "CAP CER 100nF 50V X7R 0603"),
    ("100nF", "", "100nF 50V"),
    ("100nF", "\u7cbe\u5ea6: \u00b110%; \u6e29\u5ea6\u7cfb\u6570: X7R", "CAP CER 100nF 50V X7R 0805"),
    ("100nF \u00b110%", "Capacitance: 100nF \u00b110%; Voltage: 50V", ""),
    ("100nF \u00b110nF", "Capacitance: 100nF", ""),
    ("100nF, 50V", "Capacitance: 100nF", ""),
])
def test_qualifiers_and_conservative_summary(validate, value, detail, param):
    assert validate(row(value=value), stock(detail, param=param))["status"] == "matched"


@pytest.mark.parametrize("detail,param", [
    ("Voltage: 50V; Tolerance: 10%", ""),
    ("Voltage: 100; Tolerance: 1%", ""),
    ("Resistance: 100", ""),
    ("Voltage: 100nF", ""),
    ("Capacitance: unavailable", "CAP CER 100nF 50V"),
    ("Capacitance: 50V", "CAP CER 100nF 50V"),
    ("Capacitance: 100nF; Capacitance: 1uF", ""),
    ("100nF", "1uF"),
    ("", "GRM188R71H104KA93D"),
    ("", "CAP CER 50V 0603"),
    ("", "RES SMD 100nF"),
    ("", "Rated voltage 100nF"),
    ("", ""),
])
def test_wrong_missing_or_conflicting_nominal_params_are_unknown(validate, detail, param):
    assert validate(row(), stock(detail, param=param, model="100nF")) == {
        "status": "unknown", "issues": [], "unknown_fields": ["value"],
    }


@pytest.mark.parametrize("value", [
    "100nF/1uF", "100nF, 1uF", "100nF 1uF", "100nF100nF",
    "10-100nF", "10nF-100nF", "10~100nF", "10 to 100nF",
    "2x100nF", "100nF x 4", "100nF array", "50V", "50", "",
])
def test_ambiguous_or_non_value_schematic_values_are_unknown(validate, value):
    assert validate(row(value=value), stock())["unknown_fields"] == ["value"]


@pytest.mark.parametrize("detail", [
    "Capacitance: 100nF/1uF", "Capacitance: 100nF to 1uF",
    "Capacitance: 2x100nF", "Capacitance: 100nF array",
    "Capacitance: 10-100nF",
])
def test_ambiguous_stock_values_are_unknown(validate, detail):
    assert validate(row(), stock(detail))["status"] == "unknown"


def test_saved_fields_override_edited_fields_for_each_reference(validate):
    edited = row("C2,C1,C3", "100nF", refs_list=["C1", "C2", "C3"])
    actual = {
        "C1": {"value": "1uF", "footprint": "DSMU:0603"},
        "C2": {"value": "0.1uF", "footprint": "DSMU:0805"},
        "C3": {"value": "1uF", "footprint": "DSMU:0603"},
    }
    result = validate(edited, stock(), actual)
    assert result["status"] == "mismatch"
    assert result["issues"] == [
        {"field": "value", "refs": ["C1", "C3"], "expected": "1uF", "actual": "100nF"},
        {"field": "footprint", "refs": ["C2"], "expected": "DSMU:0805", "actual": "0603"},
    ]
    assert result["unknown_fields"] == []


def test_pending_edits_do_not_create_false_mismatch(validate):
    actual = {"C1": {"value": "100nF", "footprint": "DSMU:0603"}}
    assert validate(row(value="1uF", footprint="DSMU:0805"), stock(), actual)["status"] == "matched"


@pytest.mark.parametrize("actual", [{}, {"C1": {}}, {"C2": {"value": "1uF"}}])
def test_missing_saved_fields_do_not_fall_back_to_edits(validate, actual):
    assert validate(row(), stock(), actual) == {
        "status": "unknown", "issues": [], "unknown_fields": ["value", "footprint"],
    }


def test_known_mismatch_wins_over_missing_group_member(validate):
    result = validate(row("C1,C2"), stock(), {
        "C1": {"value": "1uF", "footprint": "DSMU:0603"},
    })
    assert result["status"] == "mismatch"
    assert result["issues"][0]["refs"] == ["C1"]
    assert result["unknown_fields"] == ["value", "footprint"]


@pytest.mark.parametrize("reference_fields", [
    {"ref": "C1"}, {"refs": "C1,C2"}, {"refs": ["C1", "C2"]},
    {"refs_list": ["C1", "C2"]},
    {"ref_links": [{"ref": "C1"}, {"ref": "C2"}]},
])
def test_supported_reference_sources(validate, reference_fields):
    result = validate({**row(value="1uF"), "refs": "", **reference_fields}, stock())
    assert result["status"] == "mismatch"
    assert result["issues"][0]["refs"] == (["C1"] if "ref" in reference_fields else ["C1", "C2"])


@pytest.mark.parametrize("ref,comment,value,detail,status", [
    ("U1", "amplifier", "100nF", "Capacitance: 100nF", "not_applicable"),
    ("J1", "connector", "100", "Resistance: 100R", "not_applicable"),
    ("X1", "capacitor", "100nF", "Capacitance: 100nF", "matched"),
    ("X1", "\u7535\u963b", "100", "Resistance: 100R", "matched"),
    ("RN1", "resistor array", "100", "Resistance: 100R", "unknown"),
    ("R1", "NTC thermistor", "10k", "Resistance: 10k", "unknown"),
])
def test_only_obvious_single_passives_are_validated(validate, ref, comment, value, detail, status):
    result = validate(row(ref, value, comment=comment), stock(detail))
    assert result["status"] == status
    assert result["issues"] == []


def test_saved_comment_is_used_instead_of_edited_kind(validate):
    result = validate(row("X1", comment="capacitor"), stock(), {
        "X1": {"comment": "amplifier", "value": "100nF", "footprint": "DSMU:0603"},
    })
    assert result["status"] == "not_applicable"


def test_structured_parameter_objects(validate):
    result = validate(row(), stock({"Capacitance": "100nF", "Voltage": "50V"}))
    assert result["status"] == "matched"


def test_parameter_lists_and_chinese_separators(validate):
    assert validate(row(), stock([
        {"paramName": "\u5bb9\u91cf", "paramValue": "100nF"},
        {"name": "Voltage", "value": "50V"},
    ]))["status"] == "matched"
    assert validate(row(), stock("\u5bb9\u91cf\uff1a100nF\uff1b\u7535\u538b\uff1a50V"))["status"] == "matched"


def test_structured_nominal_takes_priority_over_summary(validate):
    result = validate(row(), stock("Capacitance: 100nF", param="CAP CER 1uF 50V 0603"))
    assert result["status"] == "matched"


def test_duplicate_equivalent_parameters_are_not_a_multivalue_spec(validate):
    result = validate(row(), stock("Capacitance: 100nF", param="Capacity: 0.1uF"))
    assert result["status"] == "matched"


def test_empty_actual_map_without_refs_does_not_use_row_comment(validate):
    result = validate({"comment": "capacitor", "value": "100nF", "footprint": "0603"}, stock(), {})
    assert result == {"status": "unknown", "issues": [], "unknown_fields": ["value", "footprint"]}


@pytest.mark.parametrize("bad_stock", [
    None, {}, [], "100nF 0603", {"ok": True},
    {"c_code": "C123", "param_detail": "Capacitance: 100nF", "package": "0603"},
    stock(ok=False), stock(ok=None), stock(ok="false"), stock(ok="true"),
])
def test_failed_incomplete_or_malformed_stock_never_matches(validate, bad_stock):
    assert validate(row(), bad_stock) == {
        "status": "unknown", "issues": [], "unknown_fields": ["value", "footprint"],
    }


@pytest.mark.parametrize("value,detail,param", [
    ("100", "Resistance: 100R", "RES ARRAY 100R 0603"),
    ("10k", "Resistance: 10k", "NTC thermistor 10k"),
])
def test_stock_arrays_and_special_resistors_are_unknown(validate, value, detail, param):
    result = validate(row("R1", value), stock(detail, param=param))
    assert result["status"] == "unknown"
    assert "value" in result["unknown_fields"]


def test_inputs_are_unchanged_and_repeated_results_are_independent(validate):
    args = [row("C1,C2"), stock(), {"C1": {"value": "1uF", "footprint": "DSMU:0603"}}]
    before = deepcopy(args)
    result = validate(*args)
    result["issues"][0]["refs"].append("C99")
    result["unknown_fields"].clear()
    assert args == before
    assert validate(*args)["issues"][0]["refs"] == ["C1"]
