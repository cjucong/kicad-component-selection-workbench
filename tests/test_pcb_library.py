from part_selection_pcb_library import cache_key


def test_library_cache_separates_two_nicknames_for_one_directory():
    assert cache_key('same.kicad_mod', 'hash', False, 'First:1206') != cache_key('same.kicad_mod', 'hash', False, 'Second:1206')
