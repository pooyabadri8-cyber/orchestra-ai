from orchestra.content.numbers import scan


def test_karat_is_a_name_not_a_figure():
    assert scan("قیمت طلای ۱۸ عیار امروز بالا رفت.", []) == []
    assert scan("طلای 24عیار", []) == []
    assert scan("طلای ۱۸ عیار (هر گرم)", []) == []


def test_other_numbers_still_blocked():
    found = scan("طلای ۱۸ عیار ۵ درصد بالا رفت و ۱۱۸ عیار هم نیست.", [])
    assert [u.text for u in found] == ["۵", "۱۱۸"]
    assert [u.text for u in scan("۱۸ گرم طلا", [])] == ["۱۸"]
