from tl.plugin_config import ConfigLoader


def test_scheduler_v2_string_false_stays_disabled():
    config = ConfigLoader({"retry_settings": {"scheduler_v2": "false"}}).load()
    assert config.scheduler_v2 is False
