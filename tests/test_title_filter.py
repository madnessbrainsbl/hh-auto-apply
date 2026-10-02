# -*- coding: utf-8 -*-
"""Оба фильтра названий (test.py и hh_selenium) решают одинаково и не теряют ИБ.

24.09 отсеивались «...тестирования на проникновение», «InfraSec», «Threat
Intelligence» (исключение «hr» внутри «Threat»), «OSINT», «компьютерных инцидентов».
"""
import pytest

import test as menu
from hh_selenium import HHSeleniumBot

TITLES = {
    'Ведущий инженер-проектировщик (прикладные и хостовые СрЗИ)': True,
    'Эксперт SOAR': True,
    # 28.09: РОСКОСМОС — ПДИТР отсеивался, а это ТЗИ-роль. «Геологоразведка» — не она.
    'Главный специалист по противодействию иностранным техническим разведкам': True,
    'Главный специалист ПД ИТР и ТЗИ': True,
    'Инженер геологоразведки': False,
    'Специалист направления анализа защищенности и тестирования на проникновение (Гибрид)': True,
    'Senior InfraSec engineer': True,
    'Аналитик Threat Intelligence / Analyst (Darkweb)': True,
    'Младший аналитик Threat Intelligence, исследование сложных угроз APT': True,
    'Эксперт по расследованию компьютерных инцидентов': True,
    'Analyst OSINT': True,
    'Инженер по сетевой безопасности': True,
    'Специалист по антитеррористической защищенности объектов': False,
    'Специалист по промышленной безопасности': False,
    'Инженер по охране труда': False,
    'HR BP': False,
    'Бухгалтер': False,
    # 25.09: подаёмся массово — преподавание, пресейл, сертификация СЗИ это тоже ИБ.
    'Преподаватель информационной безопасности': True,
    'Пресейл инженер по информационной безопасности': True,
    'Менеджер по продажам СЗИ': False,
    'Преподаватель математики': False,
    # 25.09 вечер
    'Младший инженер по исследованию атак на веб-ресурсы.': True,
    'Руководитель проектов (Информационная безопасность)': True,
    'ИТ-аудитор, служба внутреннего аудита, Ozon Банк': True,
    'PCI DSS Specialist': True,
    'Администратор антивирусной защиты': True,
    'Системный архитектор SoC (СнК)': False,
}


@pytest.mark.parametrize('title,want', TITLES.items())
def test_both_title_filters_agree(title, want):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'keywords_include': ['appsec'], 'keywords_exclude': ['hr', 'продаж', 'снк']}
    assert menu.validate_apply_title(title, allow_technical_fallback=False)[0] is want
    assert bot.validate_security_title(title)[0] is want
