import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from ai_assistant import AIAssistant
from app_paths import profile_directory
import hh_selenium
from hh_selenium import HHSeleniumBot
from hh import profile_lock, schedule


def assistant(mode='heavy'):
    ai = AIAssistant({'ai_config': {'enabled': False},
                      'ai_filter': {'mode': mode, 'prompt': 'Только Python'},
                      'candidate_profile': {'skills': ['Python'], 'about': 'Backend',
                                            'contacts': {'email': 'private@example.test'}}})
    ai._call_llm = MagicMock(return_value='{"suitable": false, "reason": "Другой стек"}')
    return ai


def test_filter_modes_and_invalid_responses():
    for mode in ('light', 'heavy', 'custom'):
        ai = assistant(mode)
        assert ai.filter_vacancy('Python', 'Полное описание', ['SQL']) == (False, 'Другой стек')
        payload, prompt = ai._call_llm.call_args.args
        assert ('Полное описание' in payload) == (mode != 'light')
        assert ('Только Python' in prompt) == (mode == 'custom')
        assert 'private@example.test' not in payload
        for raw in (None, 'not json', '{"suitable":"false","reason":"no"}',
                    '{"suitable":true}', '[]'):
            ai._call_llm.return_value = raw
            assert ai.filter_vacancy('Python')[0] is None
        ai._call_llm.return_value = '```json\n{"suitable":true,"reason":"Совпадает стек"}\n```'
        assert ai.filter_vacancy('Python')[0] is True
    ai = assistant('off')
    assert ai.filter_vacancy('Anything') == (True, '')
    ai._call_llm.assert_not_called()


@pytest.mark.parametrize('decision', [False, None])
def test_filter_blocks_before_letter_or_response_click(decision, monkeypatch):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'ai_filter': {'mode': 'heavy'}, 'skip_keywords': []}
    bot.skipped = 0
    bot.driver = MagicMock()
    bot.db = MagicMock()
    bot.ai_assistant = MagicMock()
    bot.ai_assistant.filter_vacancy.return_value = decision, 'Причина'
    bot.save_applied = MagicMock()
    for name, value in [('check_interactive_controls', None), ('maybe_bump_resume', None),
                        ('random_delay', None), ('detect_response_state', 'ready'),
                        ('page_has_captcha', False), ('page_says_archived', False),
                        ('get_vacancy_page_company', 'Company'),
                        ('get_vacancy_page_description', 'Description'),
                        ('get_vacancy_page_skills', ['Python'])]:
        setattr(bot, name, MagicMock(return_value=value))
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda _: None)
    success, reason = bot.apply_to_vacancy('https://hh.ru/vacancy/123', 'Python')
    assert not success and reason.startswith('AI-фильтр:')
    bot.ai_assistant.generate_cover_letter.assert_not_called()
    bot.driver.find_elements.assert_not_called()
    assert bot.skipped == 1
    assert bot.save_applied.call_count == (1 if decision is False else 0)
    assert bot.db.record_skipped_vacancy.call_args.kwargs['reason'] == (
        'ai_filter' if decision is False else 'ai_unavailable')


@pytest.mark.parametrize('provider', ['gemini', 'openai'])
def test_vision_request_contains_only_crop(provider, monkeypatch):
    ai = assistant()
    ai.config['captcha'] = {'enabled': True, 'provider': provider, 'model': 'vision-test', 'api_key': 'test-key'}
    response = MagicMock()
    response.json.return_value = ({'candidates': [{'content': {'parts': [{'text': 'Ab123'}]}}]}
                                 if provider == 'gemini' else {'choices': [{'message': {'content': 'Ab123'}}]})
    post = MagicMock(return_value=response)
    monkeypatch.setattr('requests.post', post)
    assert ai.recognize_captcha(b'png-crop') == 'Ab123'
    body = json.dumps(post.call_args.kwargs['json'])
    assert 'private@example.test' not in body
    assert 'cG5nLWNyb3A=' in body
    assert post.call_args.kwargs['timeout'] == 20
    post.side_effect = RuntimeError('provider failed')
    assert ai.recognize_captcha(b'png-crop') is None


def test_vision_captcha_submits_and_verifies(monkeypatch):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'captcha': {'enabled': True}}
    bot.driver = MagicMock()
    bot.driver.current_url = 'https://hh.ru/account/captcha'
    bot.check_interactive_controls = MagicMock(return_value=None)
    bot.page_has_captcha = MagicMock(return_value=False)
    picture, field = MagicMock(), MagicMock()
    picture.screenshot_as_png = b'crop'
    bot.driver.find_elements.side_effect = [[picture], [field], []]
    bot.ai_assistant = MagicMock()
    bot.ai_assistant.recognize_captcha.return_value = 'Ab123'
    assert bot.try_vision_captcha() is True
    bot.ai_assistant.recognize_captcha.assert_called_once_with(b'crop')
    field.send_keys.assert_any_call('Ab123')
    assert field.send_keys.call_count == 2
    bot.driver.current_url = 'https://example.test/'
    assert bot.try_vision_captcha() is False


def test_vision_retries_are_bounded(monkeypatch):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'captcha': {'enabled': True, 'max_attempts': 100}}
    bot.driver = MagicMock()
    bot.driver.current_url = 'https://hh.ru/account/captcha'
    bot.driver.find_elements.return_value = [MagicMock()]
    bot.check_interactive_controls = MagicMock(return_value=None)
    bot.ai_assistant = MagicMock()
    bot.ai_assistant.recognize_captcha.return_value = '1234'
    wait = MagicMock()
    wait.until.side_effect = hh_selenium.TimeoutException()
    monkeypatch.setattr(hh_selenium, 'WebDriverWait', lambda *_: wait)
    assert bot.try_vision_captcha() is False
    assert bot.ai_assistant.recognize_captcha.call_count == 3


def test_profile_paths_and_lock(tmp_path):
    assert profile_directory('default', tmp_path) == tmp_path
    assert profile_directory('second', tmp_path) == tmp_path / 'profiles' / 'second'
    for invalid in ('../escape', '', 'CON', 'a/b', 'a\\b', 'x' * 65):
        with pytest.raises(ValueError):
            profile_directory(invalid, tmp_path)
    with profile_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with profile_lock(tmp_path):
                pass
    with profile_lock(tmp_path):
        pass


def test_profile_isolation_in_real_processes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = """
import json, pathlib
import app_paths, config_manager, hh_selenium, db_manager, resume_updater, rejection_analyzer, email_outreach
base = pathlib.Path(app_paths.DATA_DIR)
for module in (config_manager, hh_selenium, db_manager, resume_updater, rejection_analyzer, email_outreach):
    assert pathlib.Path(module.SCRIPT_DIR) == base
config = config_manager.load_config()
assert config['candidate_profile'] == {} and config['resume_id'] == ''
config['resume_id'] = app_paths.PROFILE_ID
assert config_manager.save_config(config)
db = db_manager.DatabaseManager()
assert pathlib.Path(db.db_path).parent == base
bot = hh_selenium.HHSeleniumBot(pause_before_close=False)
assert bot.config['resume_id'] == app_paths.PROFILE_ID
assert pathlib.Path(bot.applied_file).parent == base
try:
    hh_selenium.resolve_workspace_path('../outside.json')
except ValueError:
    pass
else:
    raise AssertionError('Cache escaped the profile')
"""
    for name in ('first', 'second'):
        env = {**os.environ, 'HH_DATA_DIR': str(tmp_path), 'HH_PROFILE_ID': name}
        result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    for name in ('first', 'second'):
        config = json.loads((tmp_path / 'profiles' / name / 'hh_selenium_config.json').read_text(encoding='utf-8'))
        assert config['resume_id'] == name
    assert not (tmp_path / 'hh_selenium_config.json').exists()


def test_cli_settings_and_missing_resume(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HH_DATA_DIR': str(tmp_path)}
    command = [sys.executable, str(root / 'hh.py'), '--profile-id', 'second']
    def invoke(*args):
        return subprocess.run(command + list(args), cwd=root, env=env,
                              capture_output=True, encoding='utf-8', timeout=20)
    result = invoke('settings', '--ai-filter', 'custom')
    assert result.returncode == 1
    path = tmp_path / 'profiles' / 'second' / 'hh_selenium_config.json'
    assert json.loads(path.read_text(encoding='utf-8'))['ai_filter']['mode'] == 'off'
    result = invoke('settings', '--ai-filter', 'custom', '--filter-prompt', 'Python only', '--captcha', 'on')
    assert result.returncode == 0, result.stderr
    cfg = json.loads(path.read_text(encoding='utf-8'))
    assert cfg['ai_filter'] == {'mode': 'custom', 'prompt': 'Python only'}
    assert cfg['captcha']['enabled'] is True
    result = invoke('run', '--headless')
    assert result.returncode == 1 and 'выберите резюме' in result.stderr
    assert not (path.parent / 'chrome_profile').exists()


def test_relative_data_root_survives_changing_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HH_DATA_DIR': 'data', 'HH_PROFILE_ID': 'second', 'PYTHONPATH': str(root)}
    code = """
import os, subprocess, sys
from app_paths import DATA_DIR
assert os.path.isabs(os.environ['HH_DATA_DIR'])
os.chdir(DATA_DIR)
child = subprocess.check_output([sys.executable, '-c', 'from app_paths import DATA_DIR; print(DATA_DIR)'], encoding='utf-8')
assert child.strip() == DATA_DIR
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('interval', [0, -1, float('nan'), float('inf')])
def test_scheduler_rejects_invalid_interval(interval):
    from argparse import Namespace
    with pytest.raises(ValueError):
        schedule(Namespace(every_minutes=interval))


def test_scheduler_waits_between_completed_runs(monkeypatch):
    from argparse import Namespace
    import hh
    class Stop:
        cycles = 0
        def is_set(self):
            return self.cycles == 2
        def set(self):
            self.cycles = 2
        def wait(self, seconds):
            assert seconds == 240 * 60
            self.cycles += 1
    child = MagicMock()
    child.poll.return_value = 0
    child.returncode = 0
    popen = MagicMock(return_value=child)
    monkeypatch.setattr(hh.threading, 'Event', Stop)
    monkeypatch.setattr(hh.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(hh.subprocess, 'Popen', popen)
    assert schedule(Namespace(every_minutes=240, profile_id='second', headless=True, limit=10)) == 0
    assert popen.call_count == 2
    command = popen.call_args.args[0]
    assert command[-3:] == ['--headless', '--limit', '10']
    assert command[2:5] == ['--profile-id', 'second', 'run']
    child.terminate.assert_not_called()


@pytest.mark.skipif(os.environ.get('HH_BROWSER_SMOKE') != '1', reason='Run explicitly with HH_BROWSER_SMOKE=1')
def test_captcha_in_real_chrome(tmp_path, monkeypatch):
    from urllib.parse import quote
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-background-networking')
    options.add_argument('--window-size=1280,900')
    options.add_argument(f'--user-data-dir={tmp_path / "chrome"}')
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="60"><rect width="160" height="60" fill="white"/><text x="10" y="40" font-size="28">Ab123</text></svg>'
    html = ('<form onsubmit="event.preventDefault(); if(this.elements[0].value === \'Ab123\') this.remove()">'
            '<img data-qa="account-captcha-picture" src="data:image/svg+xml,' + quote(svg) + '">'
            '<input data-qa="account-captcha-input"><button type="submit">OK</button></form>')
    driver = webdriver.Chrome(options=options)
    try:
        driver.get('data:text/html;charset=utf-8,' + quote(html))
        monkeypatch.setattr(hh_selenium, 'is_allowed_hh_url', lambda url: url.startswith('data:text/html;'))
        bot = HHSeleniumBot.__new__(HHSeleniumBot)
        bot.config = {'captcha': {'enabled': True}}
        bot.driver = driver
        bot.check_interactive_controls = lambda: None
        bot.ai_assistant = MagicMock()
        def recognize(png):
            assert png.startswith(b'\x89PNG') and len(png) > 100
            return 'Ab123'
        bot.ai_assistant.recognize_captcha.side_effect = recognize
        assert bot.try_vision_captcha() is True
        assert bot.page_has_captcha() is False
    finally:
        driver.quit()
