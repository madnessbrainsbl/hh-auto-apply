# -*- coding: utf-8 -*-
"""Оба фильтра названий (test.py и hh_selenium) решают одинаково.

Копии фильтра расходились: вакансия проходила в одном месте и отсеивалась
в другом. Короткие слова-исключения ловятся только целым словом: «hr» не
должно срабатывать внутри «Chrome».
"""
from unittest.mock import patch

import pytest

import test as menu
from config_manager import SEARCH_PRESETS
from hh_selenium import HHSeleniumBot

PRESET = dict(SEARCH_PRESETS['python'], keywords_exclude=[])

TITLES = {
    'Senior Python Developer': True,
    'Backend-разработчик (Python, FastAPI)': True,
    'Python-разработчик в команду Chrome Extensions': True,
    'Разработчик Django (Гибрид)': True,
    'Менеджер по продажам Python-курсов': False,
    'HR BP': False,
    'Бухгалтер': False,
    'Java Developer': False,
    'Автор студенческих работ по Python': False,
}


@pytest.mark.parametrize('title,want', TITLES.items())
def test_both_title_filters_agree(title, want):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'keywords_include': PRESET['keywords_include'], 'keywords_exclude': []}
    with patch.object(menu, 'get_active_preset', return_value=PRESET), \
         patch('config_manager.get_active_preset', return_value=PRESET):
        assert menu.validate_apply_title(title)[0] is want
        assert bot.validate_title(title)[0] is want
