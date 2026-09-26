# -*- coding: utf-8 -*-
"""Подписи вариантов анкеты в настоящем Chrome (отдельный временный профиль).

25.09 модели уходили id вариантов «395387587 | 395387588» вместо «Москва |
Другая локация», и анкеты оставались незаполненными.
"""
import pathlib
import pytest



PAGE = '''<html><body>
<div class="q1">Укажите город проживания
  <label class="magritte"><input type="checkbox" value="395387587"><span class="mark"></span></label><span>Москва</span>
  <label class="magritte"><input type="checkbox" value="395387588"><span class="mark"></span></label><span>Другая локация</span>
</div>
<div class="q2">Офис?
  <div class="w"><label><input type="radio" name="r" value="1"><div class="mark"></div></label><div class="t">Да</div></div>
  <div class="w"><label><input type="radio" name="r" value="2"><div class="mark"></div></label><div class="t">Нет</div></div>
</div>
<div class="q3">
  <label class="bloko-radio"><input type="radio" name="b" value="7"><span class="bloko-radio__text">Готов</span></label>
  <label class="bloko-radio"><input type="radio" name="b" value="8"><span class="bloko-radio__text">Не готов</span></label>
</div>
<div class="q4"><input type="radio" name="f" id="f1" value="9"><label for="f1">Удалённо</label></div>
</body></html>'''


def test_labels_are_read_from_any_markup(tmp_path):
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from hh_selenium import HHSeleniumBot
    page = tmp_path / 'q.html'
    page.write_text(PAGE, encoding='utf-8')
    opts = webdriver.ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument(f'--user-data-dir={tmp_path / "profile"}')
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as e:
        pytest.skip(f'Chrome недоступен: {e}')
    try:
        driver.get(page.as_uri())
        bot = HHSeleniumBot.__new__(HHSeleniumBot)
        got = [bot.get_option_label(el) for el in
               driver.find_elements(By.CSS_SELECTOR, 'input[type=radio],input[type=checkbox]')]
    finally:
        driver.quit()
    assert got == ['Москва', 'Другая локация', 'Да', 'Нет', 'Готов', 'Не готов', 'Удалённо']


MAGRITTE_FORM = '''<html><head><style>
 .hidden-input { position:absolute; opacity:0; width:1px; height:1px; margin:0; }
 label { position:relative; display:inline-block; padding:12px 24px; border:1px solid #999; }
</style></head><body>
<div class="q" id="q1">Укажите город проживания
  <div><label><input class="hidden-input" type="checkbox" value="395387587"><span>Москва</span></label></div>
  <div><label><input class="hidden-input" type="checkbox" value="395387588"><span>Другая локация</span></label></div>
</div>
<div class="q" id="q2">Готовы ли рассмотреть офисный формат работы?
  <div><label><input class="hidden-input" type="radio" name="o" value="1"><span>Да</span></label></div>
  <div><label><input class="hidden-input" type="radio" name="o" value="2"><span>Нет</span></label></div>
</div>
</body></html>'''


def test_hidden_magritte_choices_get_checked(tmp_path):
    """25.09: у Magritte input скрыт — бот считал блок пустым и ничего не отмечал."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from hh_selenium import HHSeleniumBot
    page = tmp_path / 'form.html'
    page.write_text(MAGRITTE_FORM, encoding='utf-8')
    opts = webdriver.ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument(f'--user-data-dir={tmp_path / "profile"}')
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as e:
        pytest.skip(f'Chrome недоступен: {e}')
    try:
        driver.get(page.as_uri())
        bot = HHSeleniumBot.__new__(HHSeleniumBot)
        bot.driver = driver
        bot.config = {'question_answers': {}}
        bot.is_disabled_element = lambda el: False
        bot.click_viewport_coordinates = lambda x, y: driver.execute_script(
            'document.elementFromPoint(arguments[0], arguments[1]).click(); return true;', x, y)
        handled = set()
        results = []
        for block_id, answer in (('q1', 'Другая локация'), ('q2', 'Нет')):
            block = driver.find_element(By.ID, block_id)
            bot._batch_answers = {' '.join(block.text.split()): answer}
            bot._batch_answers_by_block = {}
            results.append(bot.answer_single_question(block, block.text, {}, handled))
        checked = [el.get_attribute('value') for el in
                   driver.find_elements(By.CSS_SELECTOR, 'input:checked')]
    finally:
        driver.quit()
    assert [r[1] for r in results] == [None, None]
    assert checked == ['395387588', '2']


OWN_VARIANT_FORM = '''<html><head><style>
 .hidden-input { position:absolute; opacity:0; width:1px; height:1px; margin:0; }
 label { position:relative; display:inline-block; padding:12px 24px; border:1px solid #999; }
</style></head><body>
<div class="q" id="q1">Я имею опыт настройки сетевого оборудования:
  <div><label><input class="hidden-input" type="checkbox" value="1"><span>Cisco</span></label></div>
  <div><label><input class="hidden-input" type="checkbox" value="2"><span>Mikrotik</span></label></div>
  <div><label><input class="hidden-input" type="checkbox" value="3" id="own"
       onchange="document.getElementById('t').style.display = this.checked ? 'block' : 'none'"><span>Свой вариант</span></label></div>
  <textarea id="t" style="display:none"></textarea>
</div>
</body></html>'''


def test_own_variant_gets_its_text_field_filled(tmp_path):
    """25.09 Ресурс-Медиа: «Свой вариант» отмечен, поле к нему пустое — «Не заполнены вопросы»."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from hh_selenium import HHSeleniumBot
    page = tmp_path / 'own.html'
    page.write_text(OWN_VARIANT_FORM, encoding='utf-8')
    opts = webdriver.ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument(f'--user-data-dir={tmp_path / "profile"}')
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as e:
        pytest.skip(f'Chrome недоступен: {e}')
    try:
        driver.get(page.as_uri())
        bot = HHSeleniumBot.__new__(HHSeleniumBot)
        bot.driver = driver
        bot.config = {'question_answers': {}}
        bot.is_disabled_element = lambda el: False
        bot.click_viewport_coordinates = lambda x, y: driver.execute_script(
            'document.elementFromPoint(arguments[0], arguments[1]).click(); return true;', x, y)
        bot.get_answer_for_question = lambda q, d=None: 'Работал с Kafka и RabbitMQ'
        block = driver.find_element(By.ID, 'q1')
        bot._batch_answers = {' '.join(block.text.split()): 'Свой вариант'}
        bot._batch_answers_by_block = {}
        answered, unresolved = bot.answer_single_question(block, block.text, {}, set())
        text = driver.find_element(By.ID, 't').get_attribute('value')
    finally:
        driver.quit()
    assert unresolved is None
    assert text == 'Работал с Kafka и RabbitMQ'
