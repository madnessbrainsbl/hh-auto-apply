import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from unittest.mock import MagicMock, patch
import hh_selenium
from hh_selenium import HHSeleniumBot

# Флаги паузы/стопа бот ищет в hh_selenium.SCRIPT_DIR, а conftest уводит эту
# константу во временную папку, чтобы тест не сорил файлами в корне проекта.
# Поэтому путь берём через модуль, а не копией значения на момент импорта.
def flag_path(name):
    return os.path.join(hh_selenium.SCRIPT_DIR, name)

@pytest.fixture
def clean_flags():
    for f in ('pause.flag', 'stop.flag'):
        path = flag_path(f)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    yield
    for f in ('pause.flag', 'stop.flag'):
        path = flag_path(f)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

def test_init_cleans_stale_flags():
    pause_file = flag_path('pause.flag')
    stop_file = flag_path('stop.flag')
    with open(pause_file, 'w') as f:
        f.write('1')
    with open(stop_file, 'w') as f:
        f.write('1')
    
    assert os.path.exists(pause_file)
    assert os.path.exists(stop_file)

    with patch('hh_selenium.DatabaseManager'), patch('hh_selenium.AIAssistant'):
        bot = HHSeleniumBot()
        assert not os.path.exists(pause_file)
        assert not os.path.exists(stop_file)
        assert bot.is_paused is False
        assert bot.stop_requested is False

def test_check_interactive_controls_stop_flag(clean_flags):
    with patch('hh_selenium.DatabaseManager'), patch('hh_selenium.AIAssistant'):
        bot = HHSeleniumBot()
        stop_file = flag_path('stop.flag')
        with open(stop_file, 'w') as f:
            f.write('1')

        status = bot.check_interactive_controls()
        assert status == 'stop'
        assert bot.stop_requested is True
        assert not os.path.exists(stop_file)

def test_check_interactive_controls_pause_flag_toggle(clean_flags):
    with patch('hh_selenium.DatabaseManager'), patch('hh_selenium.AIAssistant'):
        bot = HHSeleniumBot()
        pause_file = flag_path('pause.flag')
        
        with open(pause_file, 'w') as f:
            f.write('1')
        
        with patch('time.sleep', side_effect=lambda s: setattr(bot, 'is_paused', False)):
            bot.check_interactive_controls()
            assert not os.path.exists(pause_file)

def test_check_interactive_controls_normal_continue(clean_flags):
    with patch('hh_selenium.DatabaseManager'), patch('hh_selenium.AIAssistant'):
        bot = HHSeleniumBot()
        with patch('msvcrt.kbhit', return_value=False):
            status = bot.check_interactive_controls()
            assert status == 'continue'
            assert bot.stop_requested is False
            assert bot.is_paused is False

def test_random_delay_stops_when_stop_requested(clean_flags):
    with patch('hh_selenium.DatabaseManager'), patch('hh_selenium.AIAssistant'):
        bot = HHSeleniumBot()
        bot.stop_requested = True
        bot.random_delay((5, 10))
        assert bot.stop_requested is True
