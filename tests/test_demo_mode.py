"""Demo hours switch and the volume factor (task 14.1)."""
import pytest

from skeleton.runtime.demo_mode import demo_speed, real_hours_from_env


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_real_hours_is_on_for_the_true_words(value, tmp_path):
    assert real_hours_from_env({"KANTOR_REAL_HOURS": value}, tmp_path / "none.env") is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_real_hours_is_off_for_the_false_words_and_when_unset(value, tmp_path):
    assert real_hours_from_env({"KANTOR_REAL_HOURS": value}, tmp_path / "none.env") is False
    assert real_hours_from_env({}, tmp_path / "none.env") is False


def test_a_bad_value_is_an_error_not_a_silent_default(tmp_path):
    with pytest.raises(ValueError, match="KANTOR_REAL_HOURS"):
        real_hours_from_env({"KANTOR_REAL_HOURS": "sometimes"}, tmp_path / "none.env")


def test_the_dotenv_file_is_read_and_the_environment_wins(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("KANTOR_REAL_HOURS=1\n", encoding="utf-8")
    assert real_hours_from_env({}, dotenv) is True
    assert real_hours_from_env({"KANTOR_REAL_HOURS": "0"}, dotenv) is False


def test_demo_speed_is_real_time_at_300_seconds_and_scales_below():
    assert demo_speed(None, 6.0) == demo_speed(300, 6.0) == demo_speed(900, 6.0) == 1.0
    assert demo_speed(8, 6.0) == pytest.approx(6.0 * 8 / 300)
    assert demo_speed(30, 6.0) == pytest.approx(0.6)
    assert demo_speed(60, 6.0) == 1.0  # capped
    # the faster the heartbeat, the smaller the factor, so volume per real minute stays about constant
    assert demo_speed(8, 6.0) < demo_speed(15, 6.0) < demo_speed(30, 6.0)
    assert demo_speed(0, 6.0) == 1.0
