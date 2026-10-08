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
    # Преподавание и сертификация остаются в ИБ; продажи и пресейл исключены.
    'Преподаватель информационной безопасности': True,
    'Пресейл инженер по информационной безопасности': False,
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


@pytest.mark.parametrize('title', [
    'Пресейл-архитектор',
    'Пресейл-инженер (ИБ)',
    'Пресейл инженер по информационной безопасности',
    'Пре-сейл архитектор AppSec',
    'Pre-Sale\u00a0направления Soft/Cybersecurity',
    'Presales Security Engineer',
    'Pre\u2011sales DevOps Engineer',
    'Sales Engineer (Cybersecurity)',
    'Territory Enterprise Manager (Mid-Market, NGFW / Кибербезопасность)',
    'Key Account Manager (Information Security)',
    'Account Executive / AppSec',
    'Менеджер по продажам СЗИ',
    'Business Development Manager (Security)',
])
@pytest.mark.parametrize('fallback', [False, True])
def test_commercial_security_roles_cannot_bypass_custom_excludes(title, fallback, monkeypatch):
    import config_manager
    monkeypatch.setattr(config_manager, 'get_active_preset', lambda: {'id': 'security'})
    monkeypatch.setattr(menu, 'get_active_preset', lambda: {'id': 'security'})
    monkeypatch.setattr(menu, 'title_excludes', lambda: ('фотограф',))
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'keywords_include': ['security'], 'keywords_exclude': ['фотограф'],
                  'allow_technical_fallback': fallback}
    assert menu.validate_apply_title(title, allow_technical_fallback=fallback)[0] is False
    assert bot.validate_security_title(title)[0] is False
    assert bot.is_api_vacancy_suitable({'name': title})[0] is False
    applicant = menu.HHAutoApplicant.__new__(menu.HHAutoApplicant)
    applicant.allow_technical_fallback = fallback
    assert applicant.is_vacancy_suitable({'name': title}) is False


@pytest.mark.parametrize('title', [
    'Application Security Engineer', 'Эксперт по сетевой безопасности',
    'Менеджер по управлению уязвимостями', 'Security Solutions Architect',
    'DevSecOps Engineer / Salesforce', 'Разработчик Salesforce',
])
def test_noncommercial_security_and_technical_roles_still_allowed(title, monkeypatch):
    import config_manager
    monkeypatch.setattr(config_manager, 'get_active_preset', lambda: {'id': 'security'})
    monkeypatch.setattr(menu, 'get_active_preset', lambda: {'id': 'security'})
    monkeypatch.setattr(menu, 'title_excludes', lambda: ('фотограф',))
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'keywords_exclude': ['фотограф'], 'allow_technical_fallback': True}
    assert menu.validate_apply_title(title)[0] is True
    assert bot.validate_security_title(title)[0] is True


@pytest.mark.parametrize('preset_id', ['python', 'custom'])
def test_commercial_block_does_not_override_other_search_directions(preset_id, monkeypatch):
    import config_manager
    preset = {'id': preset_id, 'keywords_include': ['python']}
    monkeypatch.setattr(config_manager, 'get_active_preset', lambda: preset)
    monkeypatch.setattr(menu, 'get_active_preset', lambda: preset)
    monkeypatch.setattr(menu, 'title_excludes', lambda: ('фотограф',))
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'keywords_exclude': ['фотограф'], 'keywords_include': ['python']}
    assert menu.validate_apply_title('Python Sales Engineer')[0] is True
    assert bot.validate_security_title('Python Sales Engineer')[0] is True
