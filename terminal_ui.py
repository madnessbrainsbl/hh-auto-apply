import sys
import os
import re

# Enable ANSI virtual terminal processing on Windows cmd.exe / PowerShell
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'
UNDERLINE = '\033[4m'

# Bright ANSI Colors
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
MAGENTA = '\033[95m'
CYAN = '\033[96m'
WHITE = '\033[97m'
GRAY = '\033[90m'

def c_ok(text: str) -> str:
    return f"{GREEN}[OK]{RESET} {text}"

def c_err(text: str) -> str:
    return f"{RED}[X]{RESET} {text}"

def c_warn(text: str) -> str:
    return f"{YELLOW}[!]{RESET} {text}"

def c_info(text: str) -> str:
    return f"{CYAN}[ИНФО]{RESET} {text}"

def c_skip(text: str) -> str:
    # Жёлтый, а не серый: на тёмном терминале серый почти сливался с фоном,
    # и пропущенные вакансии терялись среди обычных строк INFO.
    return f"{YELLOW}[ПРОПУСК]{RESET} {text}"

def c_priority(priority: int) -> str:
    return f"{MAGENTA}{BOLD}[P{priority}]{RESET}"

def c_header(text: str) -> str:
    # Заголовки экранов красным: правило проекта. Раньше здесь был бирюзовый,
    # и разные файлы рисовали заголовки по-разному.
    return f"{RED}{BOLD}{text}{RESET}"

def c_company(text: str) -> str:
    return f"{BLUE}{text}{RESET}"

def c_title(text: str) -> str:
    return f"{BOLD}{WHITE}{text}{RESET}"

def c_accent(text: str) -> str:
    return f"{YELLOW}{BOLD}{text}{RESET}"

def colorize_text(line: str) -> str:
    """Подсвечивает ключевые маркеры статусов и тегов в строке."""
    if not line:
        return line

    line = re.sub(r'(\[OK\])', f"{GREEN}\\1{RESET}", line)
    line = re.sub(r'(\[X\])', f"{RED}\\1{RESET}", line)
    line = re.sub(r'(\[!\])', f"{YELLOW}\\1{RESET}", line)
    line = re.sub(r'(\[СТОП\])', f"{RED}{BOLD}\\1{RESET}", line)
    line = re.sub(r'(\[ПАУЗА\])', f"{YELLOW}{BOLD}\\1{RESET}", line)
    line = re.sub(r'(\[ПУСК\])', f"{GREEN}{BOLD}\\1{RESET}", line)
    line = re.sub(r'(\[ПРОПУСК\])', f"{YELLOW}\\1{RESET}", line)
    line = re.sub(r'(\[ИНФО\])', f"{CYAN}\\1{RESET}", line)
    # Всё, что делает ИИ, — синим. Просил пользователь: по цвету сразу видно,
    # где работала модель, а где шаблон.
    line = re.sub(r'(\[ИИ[^\]]*\])', f"{BLUE}{BOLD}\\1{RESET}", line)
    line = re.sub(r'(\[P\d\])', f"{MAGENTA}{BOLD}\\1{RESET}", line)
    # Раньше здесь стояло '[- ]' — с пробелом внутри скобок. В коде печатается
    # '[-]', поэтому правило не срабатывало ни разу.
    line = re.sub(r'(\[-\])', f"{GRAY}\\1{RESET}", line)
    # Метки, которые оставались без цвета: [i] [*] [+] [~] [?] и возобновление.
    line = re.sub(r'(\[i\]|\[\*\])', f"{CYAN}\\1{RESET}", line)
    line = re.sub(r'(\[\+\])', f"{GREEN}\\1{RESET}", line)
    line = re.sub(r'(\[~\]|\[\?\])', f"{YELLOW}\\1{RESET}", line)
    line = re.sub(r'(\[ВОЗОБНОВЛЕНИЕ\])', f"{GREEN}{BOLD}\\1{RESET}", line)
    return line


import logging

class ColoredConsoleFormatter(logging.Formatter):
    """Цветной форматировщик для логов в терминале без временных меток."""
    
    # WARNING — красным: это предупреждение, его надо замечать. Жёлтый в этом
    # логе уже занят пометкой [ПРОПУСК], и уровни с ней сливались.
    LEVEL_COLORS = {
        logging.DEBUG: GRAY,
        logging.INFO: CYAN,
        logging.WARNING: RED,
        logging.ERROR: f"{RED}{BOLD}",
        logging.CRITICAL: f"{RED}{BOLD}",
    }

    def __init__(self, fmt=None, datefmt=None, style='%'):
        if fmt is None:
            fmt = '%(levelname)s - %(message)s'
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)

    def format(self, record):
        levelname = record.levelname
        color = self.LEVEL_COLORS.get(record.levelno, RESET)
        record.levelname = f"{color}{levelname}{RESET}"
        message = super().format(record)
        return colorize_text(message)

# ---------------------------------------------------------------------------
# Перевод технических ошибок в человеческий текст.
#
# Консольный обработчик висит на корневом логгере, поэтому КАЖДЫЙ logging.info
# и logging.warning виден пользователю. Подстановка сырого `{e}` вываливала ему
# «Message: invalid session id», «UnboundLocalError» и куски JSON.
# ---------------------------------------------------------------------------

_ERROR_HINTS = (
    # (что искать в тексте ошибки, что показать пользователю)
    ('err_name_not_resolved', 'не удалось определить адрес hh.ru — проверьте интернет или отключите VPN'),
    ('err_internet_disconnected', 'нет подключения к интернету'),
    ('err_connection_timed_out', 'hh.ru не отвечает — попробуйте позже'),
    ('err_connection_reset', 'соединение с hh.ru оборвалось'),
    ('err_proxy_connection_failed', 'не удалось подключиться через прокси'),
    # Так пишет клиент Groq/OpenAI при обрыве сети — раньше это был «неизвестный сбой».
    ('connection error', 'нет связи с сервисом ИИ — проверьте интернет или VPN'),
    ('invalid session id', 'окно браузера закрыто'),
    ('no such window', 'окно браузера закрыто'),
    ('target window already closed', 'окно браузера закрыто'),
    ('browser has closed', 'браузер закрыт'),
    ('session not created', 'браузер не запустился — закройте все окна Chrome и повторите'),
    ('chrome failed to start', 'браузер не запустился — закройте все окна Chrome и повторите'),
    ('user data directory is already in use', 'браузер уже запущен в другом окне'),
    ('element click intercepted', 'кнопку закрыло всплывающее окно'),
    ('element not interactable', 'элемент страницы оказался недоступен для нажатия'),
    ('stale element', 'страница обновилась во время работы'),
    ('timeout', 'страница не успела загрузиться'),
    ('429', 'слишком много запросов — сервис попросил подождать'),
    ('api key not valid', 'ключ недействителен — введите новый в меню'),
    ('api_key_invalid', 'ключ недействителен — введите новый в меню'),
    ('rate limit', 'слишком много запросов — сервис попросил подождать'),
    ('quota', 'дневной лимит запросов исчерпан'),
    ('401', 'сервис требует повторной авторизации'),
    ('403', 'сервис отказал в доступе'),
    ('404', 'запрошенная страница или модель не найдена'),
    ('5 0 2', 'сервис ответил ошибкой'),
    ('permission denied', 'нет доступа к файлу'),
    ('no such file', 'файл не найден'),
    ('json', 'файл повреждён'),
)


def explain_error(error) -> str:
    """Человеческое объяснение ошибки вместо её технического текста.

    Если ошибка незнакомая — возвращает короткую обезличенную фразу, а не
    питоновский текст: подробности всё равно попадают в файл журнала.
    """
    text = str(error or '').strip()
    if not text:
        return 'причина не определена'
    low = text.lower()
    for marker, human in _ERROR_HINTS:
        if marker in low:
            return human
    # Незнакомая ошибка: одна короткая строка без стека и без английского.
    first = text.splitlines()[0].strip()
    if first and len(first) <= 90 and not any(ch in first for ch in '{}<>'):
        # Русский текст показать можно, латиницу и разметку — нет.
        if sum(c.isalpha() and c.lower() in 'абвгдежзийклмнопрстуфхцчшщъыьэюя' for c in first) > len(first) * 0.3:
            return first
    # Обещание «записаны в журнал» раньше не выполнялось: текст ошибки терялся.
    logging.getLogger('terminal_ui').debug(f'Сбой без объяснения: {text[:1000]}')
    return 'неизвестный сбой, подробности записаны в журнал'

def kill_profile_chrome(profile_dir: str, attempts: int = 4) -> int:
    """Завершает все процессы Chrome, держащие профиль, и ждёт, пока они исчезнут.

    05.10 одиночный проход оставлял главный процесс осиротевшего Chrome живым:
    новый запуск отдавал профиль ему и сразу «падал» (DevToolsActivePort не
    создан), в консоли — «закройте все окна Chrome». Возвращает число
    процессов, которые удалось завершить.
    """
    import subprocess
    import time as _time
    try:
        import psutil
    except Exception:
        return 0
    wanted = f'--user-data-dir={profile_dir}'.lower()
    killed = 0
    for _ in range(attempts):
        found = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'chrome' not in (proc.info.get('name') or '').lower():
                    continue
                if any(str(a).lower().strip(chr(34)) == wanted for a in (proc.info.get('cmdline') or [])):
                    found.append(proc)
            except Exception:
                continue
        if not found:
            break
        for proc in found:
            try:
                proc.kill()
                killed += 1
            except Exception:
                # taskkill /T добивает и дерево процессов, если psutil не справился
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        psutil.wait_procs(found, timeout=3)
        _time.sleep(0.5)
    return killed


def chrome_service(log_dir: str = ''):
    """Service для chromedriver: без консоли и со служебным выводом в файл.

    Возвращает None, если selenium недоступен — вызывающий код тогда
    запускает браузер как раньше.

    Зачем не DEVNULL: строки вроде «DevTools listening on ws://...» полезны при
    разборе, просто им не место на экране пользователя. Складываем их в
    `browser.log` рядом с программой.

    Зачем CREATE_NO_WINDOW: на Windows Chrome подключается к консоли
    родительского процесса и пишет в неё напрямую, поэтому одного
    перенаправления потоков не хватает — нужно, чтобы консоли не было вовсе.
    """
    import subprocess
    try:
        from selenium.webdriver.chrome.service import Service
    except Exception:
        return None

    stream = subprocess.DEVNULL
    try:
        base = log_dir or os.path.dirname(os.path.abspath(__file__))
        stream = open(os.path.join(base, 'browser.log'), 'a', encoding='utf-8',
                      errors='replace', buffering=1)
    except Exception:
        pass

    try:
        driver_path = os.environ.get('CHROMEDRIVER')
        service = Service(executable_path=driver_path, log_output=stream) if driver_path else Service(log_output=stream)
    except Exception:
        return None

    # Без своей консоли Chrome физически некуда писать поверх наших потоков.
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if flags:
        try:
            service.creation_flags = flags
        except Exception:
            pass
    return service

# Логгеры чужих библиотек: в файл пишем, на экран не пускаем. Адреса запросов,
# коды ответов и внутренние повторы пользователю ничего не говорят, а при
# каждом письме повторяются заново.
_TECHNICAL_LOGGERS = (
    'httpx', 'httpcore', 'openai', 'google', 'urllib3', 'selenium',
    'websockets', 'asyncio', 'PIL', 'charset_normalizer', 'filelock',
)


class _TechnicalNoiseFilter(logging.Filter):
    def filter(self, record):
        name = record.name or ''
        return not any(name == lib or name.startswith(lib + '.')
                       for lib in _TECHNICAL_LOGGERS)


def hide_technical_from_console(handler) -> None:
    """Вешает фильтр на консольный обработчик. В файле всё остаётся."""
    try:
        handler.addFilter(_TechnicalNoiseFilter())
    except Exception:
        pass

HH_RESUMES_URL = 'https://hh.ru/applicant/resumes'
HH_LOGIN_POLL_SECONDS = 2


def is_hh_logged_in(driver) -> bool:
    """Подтверждает вход по меню аккаунта, а не отсутствию кнопки входа."""
    from urllib.parse import urlparse
    from selenium.webdriver.common.by import By

    url = urlparse(driver.current_url or '')
    if not url.hostname or not (url.hostname == 'hh.ru' or url.hostname.endswith('.hh.ru')):
        return False
    if url.path.startswith(('/account/login', '/account/signup', '/oauth')):
        return False
    for button in driver.find_elements(By.CSS_SELECTOR, '[data-qa="login"]'):
        if button.is_displayed():
            return False
    if url.path.rstrip('/') == '/applicant/profile/me':
        markers = ('applicant-profile-common-name', 'applicant-profile-common-edit')
        if all(any(element.is_displayed() for element in driver.find_elements(
                By.CSS_SELECTOR, f'[data-qa="{marker}"]')) for marker in markers):
            return True
    for link in driver.find_elements(
        By.CSS_SELECTOR,
        '[data-qa="mainmenu_profile-link"], a[href*="/applicant/resumes"], '
        'a[href*="/applicant/negotiations"], a[href*="/account/logout"]',
    ):
        if not link.is_displayed():
            continue
        path = urlparse(link.get_attribute('href') or '').path
        if path == '/applicant/resumes' or path.startswith(('/applicant/negotiations', '/account/logout')):
            return True
        if link.get_attribute('data-qa') == 'mainmenu_profile-link' and path.startswith('/applicant/'):
            return True
    return False


def wait_for_hh_login(driver, should_stop=None, log=None) -> bool:
    """Ждёт вход, не обновляя страницу с телефоном, паролем или кодом."""
    import time
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        InvalidSessionIdException, NoSuchWindowException, WebDriverException,
    )

    log = log or logging.getLogger(__name__)
    log.warning('Войдите в аккаунт hh.ru в открытом браузере. Бот дождётся входа.')
    while True:
        if should_stop and should_stop():
            return False
        try:
            if is_hh_logged_in(driver):
                log.info('[OK] Вход в браузере подтверждён')
                return True
            # Новая форма hh сначала предлагает выбрать тип аккаунта.
            applicants = driver.find_elements(By.CSS_SELECTOR, 'input[data-qa^="account-type-card-APPLICANT"]')
            if applicants:
                applicant = applicants[0]
                label = applicant.find_element(By.XPATH, './ancestor::label')
                if label.is_displayed():
                    if not applicant.is_selected():
                        label.click()
                    for submit in driver.find_elements(By.CSS_SELECTOR, '[data-qa="submit-button"]'):
                        if submit.is_displayed() and submit.is_enabled():
                            submit.click()
                            break
        except (InvalidSessionIdException, NoSuchWindowException):
            log.info('Окно входа закрыто. Ожидание завершено.')
            return False
        except WebDriverException as error:
            log.debug(f'Форма входа пока недоступна, продолжаю ждать: {explain_error(error)}')
        time.sleep(HH_LOGIN_POLL_SECONDS)


def ensure_hh_login(driver, headless=False, should_stop=None, log=None) -> bool:
    """Проверяет защищённую страницу перед операциями с аккаунтом."""
    from selenium.common.exceptions import WebDriverException

    log = log or logging.getLogger(__name__)
    try:
        driver.get(HH_RESUMES_URL)
        if is_hh_logged_in(driver):
            return True
        if headless:
            log.warning('Нет входа в hh.ru. Запустите бот с окном браузера и войдите в аккаунт.')
            return False
        return wait_for_hh_login(driver, should_stop, log)
    except WebDriverException as error:
        log.warning(f'Не удалось проверить вход в hh.ru: {explain_error(error)}')
        return False


def ensure_russian_interface(driver, log=None) -> bool:
    """Переключает интерфейс hh на русский, если он на другом языке.

    Все селекторы бота завязаны на русские надписи. На английском интерфейсе
    поле письма не находится, а кнопка отправки «недоступна» — отклики уходят
    без письма или не уходят вовсе.

    Язык хранится в аккаунте hh, поэтому одного переключения хватает для всех
    следующих страниц. Возвращает True, если интерфейс русский.
    """
    import time as _time
    log = log or logging.getLogger(__name__)
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.action_chains import ActionChains
    except Exception:
        return True

    def current_lang():
        try:
            return (driver.execute_script(
                'return document.documentElement.lang') or '').lower()
        except Exception:
            return ''

    try:
        if 'hh.ru' not in (driver.current_url or ''):
            driver.get('https://hh.ru/')
            _time.sleep(2)
    except Exception:
        return True

    lang = current_lang()
    if not lang or lang.startswith('ru'):
        return True

    log.warning(f"Интерфейс hh переключён на другой язык ({lang}). Бот ищет "
                f"русские надписи — возвращаю русский.")
    try:
        # Только ActionChains: JS-клик React в вёрстке hh не принимает.
        button = driver.find_element(By.CSS_SELECTOR, '[data-qa="lang-switch-button"]')
        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", button)
        _time.sleep(0.4)
        ActionChains(driver).move_to_element(button).pause(0.2).click().perform()
        _time.sleep(1.0)
        option = driver.find_element(By.CSS_SELECTOR, '[data-qa="magritte-select-option-RU"]')
        ActionChains(driver).move_to_element(option).pause(0.2).click().perform()
    except Exception as e:
        log.error(f"Не удалось переключить язык hh: {explain_error(e)}. "
                  f"Переключите вручную: внизу страницы hh -> «Русский». "
                  f"На другом языке письма к откликам не прикладываются.")
        return False

    for _ in range(10):
        _time.sleep(0.8)
        if current_lang().startswith('ru'):
            log.warning("Интерфейс hh снова на русском")
            return True

    log.error("Язык hh не переключился. Переключите вручную: внизу страницы "
              "hh -> «Русский». На другом языке письма к откликам не прикладываются.")
    return False

# Временные сетевые сбои, которые лечатся ожиданием. Первая строка — как их
# пишет Chrome, вторая — как их пишут Python-библиотеки (requests, клиенты ИИ).
NETWORK_ERROR_MARKERS = (
    'err_name_not_resolved', 'err_internet_disconnected', 'err_connection_reset',
    'err_connection_refused', 'err_connection_timed_out', 'err_network_changed',
    'err_proxy_connection_failed', 'err_address_unreachable', 'timeout',
    'connection error', 'max retries exceeded', 'nameresolutionerror', 'getaddrinfo',
    'connectionerror', 'remotedisconnected', 'connection aborted', 'network is unreachable',
)


def is_network_error(e) -> bool:
    """True для временных сетевых сбоев, которые лечатся повтором."""
    low = str(e or '').lower()
    return any(m in low for m in NETWORK_ERROR_MARKERS)


def network_is_up(host: str = 'hh.ru', timeout: float = 5.0) -> bool:
    """Есть ли связь: адрес определяется и соединение открывается."""
    import socket
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network(max_seconds: int = 600, log=None, should_stop=None) -> bool:
    """Ждёт, пока вернётся сеть. True — вернулась, False — не дождались.

    Короткий обрыв (VPN переподключился, Wi-Fi моргнул) — не повод бросать
    прогон: раньше бот за секунду проваливал всё подряд. Проверяем раз в 5 с.
    should_stop — функция, которая вернёт True, если пользователь нажал «стоп».
    """
    import time as _time
    log = log or logging.getLogger(__name__)
    if network_is_up():
        return True
    minutes = max(1, max_seconds // 60)
    log.warning(f"Нет связи с интернетом — жду, пока вернётся (до {minutes} мин). "
                f"Обычно это VPN или Wi-Fi.")
    started = _time.monotonic()
    while _time.monotonic() - started < max_seconds:
        if should_stop and should_stop():
            return False
        _time.sleep(5)
        if network_is_up():
            log.warning(f"Связь вернулась через {int(_time.monotonic() - started)} с — продолжаю")
            return True
    log.warning(f"Связь не вернулась за {minutes} мин.")
    return False
