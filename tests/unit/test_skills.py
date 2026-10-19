from app.skills.loader import match_skill, skill_by_name


def test_match_skill_by_trigger() -> None:
    skill = match_skill("这件衣服能退货吗")
    assert skill is not None
    assert skill["name"] == "return_handling"
    assert "create_return_request" in skill["tools"]


def test_skill_loaded_explicitly_by_name_without_intent() -> None:
    skill = skill_by_name("order_tracking")
    assert skill is not None
    assert skill["name"] == "order_tracking"


def test_no_skill_for_chitchat() -> None:
    assert match_skill("今天天气真好") is None
