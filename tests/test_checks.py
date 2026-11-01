from wprdc.checks import CHECKS, Check


def test_check_ids_unique():
    ids = [c.id for c in CHECKS]
    assert len(ids) == len(set(ids)) == 9


def test_version_ignores_whitespace_action_and_description():
    a = Check("x", "d1", "flag", "record_date  IS NULL")
    b = Check("x", "d2", "quarantine", "record_date IS\n NULL")
    assert a.version == b.version and len(a.version) == 12


def test_version_changes_with_logic():
    assert Check("x", "d", "flag", "a IS NULL").version != Check("x", "d", "flag", "b IS NULL").version


def test_initial_actions():
    by_id = {c.id: c for c in CHECKS}
    assert by_id["key_not_unique"].alert == "any"
    for new in ("sale_code_unmapped", "parcel_location_varies"):
        assert (by_id[new].action, by_id[new].alert) == ("flag", "any")


def test_checks_refer_to_typed_table_by_alias():
    # Naming the table would tie each version to where the table lives, so a
    # rename would look like a rule change.
    assert not [c.id for c in CHECKS if "property_sales" in c.where]
