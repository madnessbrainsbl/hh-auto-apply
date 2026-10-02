"""Тесты дублирования письма на почту работодателя. Настоящая почта не трогается:
классы smtplib подменены, журнал пишется во временную папку."""

import json
import smtplib
import time
from unittest.mock import MagicMock

import pytest

import email_outreach

PASSWORD = "fixture-password-123"


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(email_outreach, 'SCRIPT_DIR', str(tmp_path))
    return tmp_path


def fake_smtp(monkeypatch, name, login_error=None):
    server = MagicMock()
    server.__enter__.return_value = server
    if login_error:
        server.login.side_effect = login_error
    cls = MagicMock(return_value=server)
    monkeypatch.setattr(email_outreach.smtplib, name, cls)
    return cls, server


def make_config(**overrides):
    section = {
        'enabled': True, 'dry_run': False,
        'smtp_host': 'smtp.mail.ru', 'smtp_port': 465,
        'login': 'me@mail.ru', 'password': PASSWORD, 'from_name': 'Иван Иванов',
        'daily_limit': 30,
        'subject_template': 'Отклик на вакансию «{vacancy_title}»',
    }
    section.update(overrides)
    return {'email_outreach': section}


def send(config, vacancy_id='1', text='Пишите на HR@Company.ru'):
    return email_outreach.maybe_send_application_email(
        config, vacancy_id, 'Пентестер', 'ООО Ромашка', text, 'Добрый день! Мой отклик.')


def read_log(tmp_path):
    return json.loads((tmp_path / email_outreach.LOG_FILE_NAME).read_text(encoding='utf-8'))


@pytest.mark.parametrize('config', [
    {},
    make_config(enabled=False),
    make_config(login=''),
    make_config(password=''),
])
def test_disabled_or_incomplete_returns_none(monkeypatch, config):
    cls, _ = fake_smtp(monkeypatch, 'SMTP_SSL')
    assert send(config) is None
    cls.assert_not_called()


def test_extraction_exclusions():
    text = ('logo@2x.png support@hh.ru job@spb.hh.ru a@headhunter.ru no-reply@corp.ru '
            'do_not_reply@corp.ru test@example.com me@mail.ru HR@Corp.ru. hr@corp.ru jobs@firm.io')
    assert email_outreach.extract_emails(text, own_address='Me@Mail.ru') == ['hr@corp.ru', 'jobs@firm.io']


def test_success_ssl_records_and_builds_message(monkeypatch, sandbox):
    cls, server = fake_smtp(monkeypatch, 'SMTP_SSL')
    status = send(make_config())
    assert status == 'Письмо продублировано на почту работодателя: hr@company.ru'
    cls.assert_called_once()
    assert cls.call_args.args[:2] == ('smtp.mail.ru', 465)
    assert cls.call_args.kwargs['timeout'] == 20
    server.starttls.assert_not_called()
    server.login.assert_called_once_with('me@mail.ru', PASSWORD)
    msg = server.send_message.call_args.args[0]
    assert msg['To'] == 'hr@company.ru'
    assert msg['Subject'] == 'Отклик на вакансию «Пентестер»'
    assert 'Иван Иванов' in str(msg['From']) and 'me@mail.ru' in str(msg['From'])
    assert msg.get_content().strip() == 'Добрый день! Мой отклик.'
    log = read_log(sandbox)
    assert log[0]['email'] == 'hr@company.ru' and log[0]['vacancy_id'] == '1'


def test_starttls_branch(monkeypatch):
    ssl_cls, _ = fake_smtp(monkeypatch, 'SMTP_SSL')
    cls, server = fake_smtp(monkeypatch, 'SMTP')
    assert send(make_config(smtp_port=587)).startswith('Письмо продублировано')
    ssl_cls.assert_not_called()
    assert cls.call_args.args[:2] == ('smtp.mail.ru', 587)
    server.starttls.assert_called_once()
    server.send_message.assert_called_once()


def test_dedupe_by_vacancy_and_address(monkeypatch):
    _, server = fake_smtp(monkeypatch, 'SMTP_SSL')
    config = make_config()
    assert send(config, vacancy_id='1').startswith('Письмо продублировано')
    # Та же вакансия, другой адрес: второй раз не пишем.
    assert 'уже было' in send(config, vacancy_id='1', text='other@firm.ru')
    # Другая вакансия, тот же адрес: тоже не пишем.
    assert 'уже писали' in send(config, vacancy_id='2')
    assert server.send_message.call_count == 1


def test_daily_limit(monkeypatch, sandbox):
    _, server = fake_smtp(monkeypatch, 'SMTP_SSL')
    now = time.time()
    old = [{'time': now - 2 * 86400, 'email': f'old{i}@x.ru', 'vacancy_id': f'o{i}'} for i in range(5)]
    fresh = [{'time': now - 3600, 'email': f'f{i}@x.ru', 'vacancy_id': f'f{i}'} for i in range(2)]
    (sandbox / email_outreach.LOG_FILE_NAME).write_text(json.dumps(old + fresh), encoding='utf-8')
    assert send(make_config(daily_limit=2)) == \
        'Письмо на почту не отправлено: дневной лимит 2 писем исчерпан'
    server.send_message.assert_not_called()
    # Старые письма за сутки не считаются: при лимите 3 одно место ещё есть.
    assert send(make_config(daily_limit=3)).startswith('Письмо продублировано')


def test_dry_run_does_not_send_or_record(monkeypatch, sandbox):
    cls, _ = fake_smtp(monkeypatch, 'SMTP_SSL')
    status = send(make_config(dry_run=True))
    assert 'Пробный режим' in status and 'hr@company.ru' in status
    cls.assert_not_called()
    assert not (sandbox / email_outreach.LOG_FILE_NAME).exists()


def test_auth_error_hint_without_password(monkeypatch, sandbox):
    fake_smtp(monkeypatch, 'SMTP_SSL',
              login_error=smtplib.SMTPAuthenticationError(535, b'bad credentials'))
    records = []
    logger = MagicMock()
    logger.warning.side_effect = records.append
    status = email_outreach.maybe_send_application_email(
        make_config(), '1', 'Пентестер', 'ООО', 'hr@corp.ru', 'Письмо', logger=logger)
    assert 'пароль приложения' in status and 'Mail.ru' in status
    assert PASSWORD not in status and all(PASSWORD not in r for r in records)
    assert not (sandbox / email_outreach.LOG_FILE_NAME).exists()


def test_broken_log_blocks_sending(monkeypatch, sandbox):
    cls, _ = fake_smtp(monkeypatch, 'SMTP_SSL')
    (sandbox / email_outreach.LOG_FILE_NAME).write_text('{broken', encoding='utf-8')
    assert 'повреждён' in send(make_config())
    cls.assert_not_called()


def test_bot_hook_passes_vacancy_text_and_letter(monkeypatch, tmp_path, caplog):
    """После успешного отклика бот передаёт модулю текст вакансии и письмо."""
    import logging
    import email_outreach
    import hh_selenium as m
    monkeypatch.setattr(email_outreach, 'SCRIPT_DIR', str(tmp_path))
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    bot.config = {'email_outreach': {'enabled': True, 'dry_run': True,
                                     'login': 'me@mail.ru', 'password': 'secret'}}
    bot.last_application_meta = {
        'company': 'Компания', 'cover_letter': 'Здравствуйте! Откликаюсь на вакансию.',
        'page_text': 'Требования... Резюме присылайте на HR@Company.ru',
    }
    with caplog.at_level(logging.INFO):
        bot.maybe_email_employer('123', 'Аналитик SOC')
    assert 'hr@company.ru' in caplog.text
    assert 'Пробный режим' in caplog.text
    assert 'secret' not in caplog.text
