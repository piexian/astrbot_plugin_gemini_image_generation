from tl.plugin_config import ConfigLoader


def test_scheduler_v2_string_false_stays_disabled():
    config = ConfigLoader({"retry_settings": {"scheduler_v2": "false"}}).load()
    assert config.scheduler_v2 is False


def test_scheduler_v2_sessions_cleaned_to_ordered_tuple():
    config = ConfigLoader(
        {
            "retry_settings": {
                "scheduler_v2": True,
                "scheduler_v2_sessions": [
                    "platform:GroupMessage:1",
                    "  platform:GroupMessage:2  ",
                    "",
                    "platform:GroupMessage:1",
                    3,
                ],
            }
        }
    ).load()
    assert config.scheduler_v2 is True
    assert config.scheduler_v2_sessions == (
        "platform:GroupMessage:1",
        "platform:GroupMessage:2",
        "3",
    )


def test_scheduler_v2_sessions_tolerates_non_list_input():
    assert (
        ConfigLoader({"retry_settings": {"scheduler_v2_sessions": {"a": 1}}})
        .load()
        .scheduler_v2_sessions
        == ()
    )
    assert ConfigLoader({"retry_settings": {}}).load().scheduler_v2_sessions == ()
    assert ConfigLoader(
        {"retry_settings": {"scheduler_v2_sessions": "platform:friend:u1"}}
    ).load().scheduler_v2_sessions == ("platform:friend:u1",)
