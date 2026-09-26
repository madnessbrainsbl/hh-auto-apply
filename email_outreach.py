"""Дублирование сопроводительного письма на почту работодателя.

Зачем: отклик на hh.ru часто лежит непрочитанным в общей куче, а письмо на
почту, которую работодатель сам указал в вакансии, попадает прямо к человеку.
Модуль включается только явно (email_outreach.enabled в конфиге) и работает
после успешного отклика: находит адрес в тексте вакансии и отправляет туда то
же письмо.

Главные предохранители:
- на один адрес пишем один раз и на одну вакансию один раз: повторные письма
  от одного кандидата читаются как спам и портят впечатление;
- дневной лимит писем, чтобы почтовый сервис не счёл ящик рассыльщиком и не
  заблокировал его;
- пароль никогда не попадает ни в журнал, ни в возвращаемые строки.
"""

import json
import logging
import os
import re
import smtplib
import ssl
import time
from email.headerregistry import Address
from email.message import EmailMessage
from typing import List, Optional

from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR
LOG_FILE_NAME = 'email_outreach_log.json'

DEFAULT_DAILY_LIMIT = 30
DEFAULT_SUBJECT = 'Отклик на вакансию «{vacancy_title}»'
SMTP_TIMEOUT = 20
DAY_SECONDS = 24 * 60 * 60

EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')

# Адреса самого hh.ru и служебные домены: писать туда бессмысленно, это не
# работодатель. Поддомены (например, m.hh.ru) отсекаются тем же списком.
EXCLUDED_DOMAINS = (
    'hh.ru', 'headhunter.ru', 'headhunter.com', 'hh.kz', 'hh.uz', 'hh.by',
    'example.com', 'example.org', 'example.net',
)
# В вёрстке встречаются имена картинок вида logo@2x.png: регулярка видит в них
# адрес, а «доменная зона» у такого адреса на деле расширение файла.
IMAGE_SUFFIXES = ('png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico')
# Сравниваем без точек, дефисов и подчёркиваний: no-reply, no_reply и noreply
# это один и тот же ящик, на который никто не ответит.
NOREPLY_PREFIXES = ('noreply', 'donotreply', 'mailerdaemon', 'postmaster')

# Почтовые сервисы, где обычный пароль для программ не подходит, и где
# создаётся отдельный пароль приложения.
APP_PASSWORD_HINTS = (
    ('mail.ru', 'Mail.ru: настройки почты, раздел «Безопасность», «Пароли для внешних приложений»'),
    ('yandex', 'Яндекс: id.yandex.ru, раздел «Безопасность», «Пароли приложений»'),
    ('gmail', 'Gmail: myaccount.google.com, раздел «Безопасность», «Пароли приложений» (нужна двухэтапная проверка)'),
    ('google', 'Gmail: myaccount.google.com, раздел «Безопасность», «Пароли приложений» (нужна двухэтапная проверка)'),
)


def _log_path() -> str:
    # Путь собираем при каждом вызове, а не при импорте: тесты подменяют
    # SCRIPT_DIR, и журнал должен уйти во временную папку.
    return os.path.join(SCRIPT_DIR, LOG_FILE_NAME)


def extract_emails(page_text: str, own_address: str = '') -> List[str]:
    """Возвращает подходящие адреса из текста вакансии в порядке появления."""
    own = (own_address or '').strip().lower()
    result = []
    for match in EMAIL_RE.findall(page_text or ''):
        email = match.strip('.').lower()
        local, _, domain = email.partition('@')
        if not local or not domain:
            continue
        if domain.rsplit('.', 1)[-1] in IMAGE_SUFFIXES:
            continue
        if any(domain == d or domain.endswith('.' + d) for d in EXCLUDED_DOMAINS):
            continue
        compact_local = re.sub(r'[._-]', '', local)
        if compact_local.startswith(NOREPLY_PREFIXES):
            continue
        if email == own or email in result:
            continue
        result.append(email)
    return result


def _load_log() -> Optional[list]:
    """Читает журнал отправленных писем.

    Отсутствующий файл это пустой журнал. Повреждённый файл возвращает None:
    без журнала нельзя проверить повторы, и лучше не отправить письмо, чем
    написать работодателю второй раз.
    """
    path = _log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else None
    except (OSError, ValueError):
        return None


def _save_log(entries: list) -> None:
    path = _log_path()
    temp_path = f'{path}.tmp'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _app_password_hint(host: str) -> str:
    host = (host or '').lower()
    for marker, hint in APP_PASSWORD_HINTS:
        if marker in host:
            return f' Нужен пароль приложения, а не обычный пароль от почты. Создать его: {hint}.'
    return ' Проверьте логин и пароль. Многие почтовые сервисы требуют отдельный пароль приложения.'


def _build_subject(template: str, vacancy_title: str, company: str) -> str:
    values = {'vacancy_title': vacancy_title or '', 'company': company or ''}
    try:
        return (template or DEFAULT_SUBJECT).format(**values)
    except (KeyError, IndexError, ValueError):
        # Опечатка в шаблоне темы не должна срывать письмо: берём стандартную тему.
        return DEFAULT_SUBJECT.format(**values)


def _send(settings: dict, message: EmailMessage) -> None:
    host = settings.get('smtp_host') or 'smtp.mail.ru'
    port = int(settings.get('smtp_port') or 465)
    context = ssl.create_default_context()
    # 465 это шифрование сразу при подключении, остальные порты (обычно 587)
    # поднимают шифрование командой STARTTLS уже внутри соединения.
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    with server:
        if port != 465:
            server.starttls(context=context)
        server.login(settings['login'], settings['password'])
        server.send_message(message)


def maybe_send_application_email(config: dict, vacancy_id: str, vacancy_title: str,
                                 company: str, page_text: str, cover_letter: str,
                                 logger=None) -> Optional[str]:
    """Returns a short human-readable Russian status line for the log, or None if nothing to do."""
    log = logger or logging.getLogger(__name__)
    settings = (config or {}).get('email_outreach') or {}
    login = (settings.get('login') or '').strip()
    password = settings.get('password') or ''
    if not settings.get('enabled') or not login or not password:
        return None
    if not (cover_letter or '').strip():
        return None

    emails = extract_emails(page_text, own_address=login)
    if not emails:
        return None
    email = emails[0]
    vacancy_id = str(vacancy_id or '')

    entries = _load_log()
    if entries is None:
        return ('Письмо на почту не отправлено: журнал отправленных писем '
                f'{LOG_FILE_NAME} повреждён, без него нельзя проверить повторы')
    if vacancy_id and any(str(e.get('vacancy_id')) == vacancy_id for e in entries):
        return f'Письмо на почту не отправлено: по вакансии {vacancy_id} письмо уже было'
    if any(e.get('email') == email for e in entries):
        return f'Письмо на почту не отправлено: на адрес {email} уже писали раньше'

    try:
        daily_limit = int(settings.get('daily_limit') or DEFAULT_DAILY_LIMIT)
    except (TypeError, ValueError):
        daily_limit = DEFAULT_DAILY_LIMIT
    now = time.time()
    sent_last_day = sum(1 for e in entries if now - float(e.get('time') or 0) < DAY_SECONDS)
    if sent_last_day >= daily_limit:
        return f'Письмо на почту не отправлено: дневной лимит {daily_limit} писем исчерпан'

    message = EmailMessage()
    message['Subject'] = _build_subject(settings.get('subject_template'), vacancy_title, company)
    from_name = (settings.get('from_name') or '').strip()
    message['From'] = Address(display_name=from_name, addr_spec=login) if from_name else login
    message['To'] = email
    message.set_content(cover_letter, charset='utf-8')

    if settings.get('dry_run'):
        # Пробный режим ничего не записывает в журнал: иначе настоящий прогон
        # посчитал бы адрес уже использованным и не отправил письмо.
        status = f'Пробный режим: письмо на {email} подготовлено, но не отправлено'
        log.info(status)
        return status

    try:
        _send(settings, message)
    except smtplib.SMTPAuthenticationError:
        status = ('Письмо на почту не отправлено: почтовый сервер не принял логин и пароль.'
                  + _app_password_hint(settings.get('smtp_host')))
        log.warning(status)
        return status
    except (smtplib.SMTPException, OSError) as e:
        # Текст ошибки сервера пароля не содержит, но вычищаем его на всякий
        # случай: журнал бота читают и пересылают.
        reason = str(e).replace(password, '***') if password else str(e)
        status = f'Письмо на почту не отправлено: ошибка почтового сервера ({type(e).__name__}: {reason})'
        log.warning(status)
        return status

    entries.append({
        'time': now,
        'date': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'email': email,
        'vacancy_id': vacancy_id,
        'vacancy_title': vacancy_title or '',
        'company': company or '',
    })
    try:
        _save_log(entries)
    except OSError as e:
        log.warning(f'Письмо отправлено, но журнал {LOG_FILE_NAME} не записан: {e}')

    status = f'Письмо продублировано на почту работодателя: {email}'
    log.info(status)
    return status
