from unittest.mock import Mock

import pytest

from hh_selenium import HHSeleniumBot
import hh_selenium


@pytest.mark.parametrize('state,expected', [('success', 'confirmed'), ('ready', 'not_confirmed'),
                                           ('captcha', 'captcha_required'),
                                           ('restricted', 'access_restricted')])
def test_application_inspection_never_dispatches_or_rewrites_history(tmp_path, monkeypatch, state, expected):
    monkeypatch.setattr(hh_selenium, 'SCRIPT_DIR', str(tmp_path))
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda _: None)
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver = Mock()
    bot.driver.current_url = 'https://hh.ru/vacancy/138674234'
    bot.driver.execute_script.return_value = []
    bot.init_driver = Mock(return_value=True)
    bot.page_has_captcha = Mock(return_value=state == 'captcha')
    bot.vacancy_access_restricted = Mock(return_value=state == 'restricted')
    bot.detect_response_state = Mock(return_value=state)
    bot.get_response_control_texts = Mock(return_value=['вы откликнулись'] if state == 'success' else ['откликнуться'])
    bot.submit_open_response_modal = Mock(side_effect=AssertionError('No dispatch'))
    bot.save_applied = Mock(side_effect=AssertionError('No history rewrite'))
    bot.try_vision_captcha = Mock(side_effect=AssertionError('No CAPTCHA solving'))
    result = bot.inspect_application('138674234')
    assert result['status'] == expected
    bot.submit_open_response_modal.assert_not_called()
    bot.save_applied.assert_not_called()
    bot.try_vision_captcha.assert_not_called()


def test_application_inspection_rejects_arbitrary_urls():
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.init_driver = Mock()
    assert bot.inspect_application('https://example.test')['status'] == 'invalid_vacancy'
    bot.init_driver.assert_not_called()
