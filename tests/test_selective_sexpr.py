import pytest
from kicad_sexpr import parse_sexpr


def test_selective_board_parse_retains_exact_footprint_offsets():
    text = '(kicad_pcb (version 1) (zone (pts (xy 1 2))) (footprint "A" (property "Reference" "R1") (pad "1" (at 1 2))))'
    full = parse_sexpr(text, 'kicad_pcb').children_named('footprint')
    selected = parse_sexpr(text, 'kicad_pcb', root_children={'footprint'})
    assert selected.children_named('footprint') == full
    assert [n.head for n in selected.children[1:]] == ['footprint']


@pytest.mark.parametrize('text', ['(kicad_pcb (zone (pts))', '(kicad_pcb (zone "unclosed))',
    '(kicad_pcb (zone)) extra', '(kicad_pcb (zone (pts))) )'])
def test_selective_parser_still_rejects_malformed_skipped_content(text):
    with pytest.raises(ValueError):
        parse_sexpr(text, 'kicad_pcb', root_children={'footprint'})


def test_selective_parser_skips_strings_with_parentheses_and_escapes():
    text = r'(kicad_pcb (zone "a(\"b)" (x 1)) (footprint "A"))'
    assert parse_sexpr(text, 'kicad_pcb', root_children={'footprint'}).children_named('footprint')[0].arg() == 'A'


def test_selective_parser_preserves_quoted_heads():
    text = '(kicad_pcb ("footprint" "A") (zone 1))'
    assert parse_sexpr(text, 'kicad_pcb', root_children={'footprint'}).children_named('footprint') == parse_sexpr(text, 'kicad_pcb').children_named('footprint')


def test_selective_parser_matches_full_parser_on_generated_escaping_and_damage():
    import json
    import random
    rng = random.Random(42)
    for _ in range(200):
        name = ''.join(rng.choices('abc ()\\"\n\t', k=20))
        text = '(kicad_pcb (zone (name ' + json.dumps(name) + ') (xy 1 2)) (footprint "A" (pad "1")))'
        for candidate in (text, text[:-1], text + ')', text + ' tail'):
            try:
                full = parse_sexpr(candidate, 'kicad_pcb')
            except ValueError:
                with pytest.raises(ValueError):
                    parse_sexpr(candidate, 'kicad_pcb', root_children={'footprint'})
            else:
                selected = parse_sexpr(candidate, 'kicad_pcb', root_children={'footprint'})
                assert selected.children_named('footprint') == full.children_named('footprint')
