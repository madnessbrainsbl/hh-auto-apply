"""
Автоматизация откликов на hh.ru через Selenium
Работает через браузер, обходя ограничения API
"""

import json
import time
import random
import os
import re
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from application_history import count_recent_applications

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from urllib.parse import urlparse, quote_plus
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, 
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException
)

HH_ALLOWED_HOST_SUFFIX = 'hh.ru'
DEFAULT_API_CACHE_FILE = 'vacancies_cache.json'
APPLICATION_LIMIT_WINDOW_HOURS = 24
APPLY_CONFIRM_ATTEMPTS = 2
# Подряд идущие провалы подтверждения отклика — признак троттлинга hh; после
# стольких провалов прогон останавливается, чтобы не молоть ~75 сек на каждой.
APPLY_FAILURE_STREAK_LIMIT = 6
# Маркер сообщения о неподтверждённом отклике (см. confirm_response_submission)
APPLY_NOT_CONFIRMED_MARKER = 'Статус отклика не изменился'
RESPONSE_STATE_WAIT_SECONDS = 7
STATUS_SENT = 'sent'
STATUS_SENT_WRONG_FILTER = 'sent_wrong_filter'
STATUS_ALREADY_APPLIED = 'already_applied'
STATUS_DENIED = 'denied'
STATUS_PENDING_CONFIRMATION = 'pending_confirmation'
STATUS_SKIPPED_NO_SAFE_URL = 'skipped_no_safe_url'
STATUS_SKIPPED_TEST = 'skipped_test'
STATUS_SKIPPED_FILTER = 'skipped_filter'
# Отдельный статус, а не skipped_filter: тот перепроверяется при каждом
# запуске, и архивная вакансия с подходящим названием вернулась бы в работу.
STATUS_ARCHIVED = 'archived'
# hh временно прячет резюме от откликов и просит «поменять видимость», хотя
# она уже нужная. Через несколько минут проходит само.
HIDDEN_RESUME_TEXT = 'поменяйте видимость резюме'
HIDDEN_RESUME_MESSAGE = 'hh временно скрыл резюме от откликов'
HIDDEN_RESUME_PAUSE_SECONDS = 300
# Сколько ждать письмо от ИИ (все провайдеры по цепочке, включая CLI).
LETTER_WAIT_SECONDS = 240
NETWORK_MESSAGE = 'Нет связи с интернетом'
NETWORK_WAIT_SECONDS = 600
SHARED_APPLIED_STATUSES = (STATUS_SENT, STATUS_SENT_WRONG_FILTER)
RESPONSE_SUCCESS_TEXTS = (
    'отклик отправлен',
    'вы откликнулись',
    'ваш отклик',
    'you applied',
    'you have applied',
    'application sent',
    'response sent',
    'apply with another resume',
)
RESPONSE_DENIED_TEXTS = (
    'вам отказали',
    'работодатель отказал',
    'application denied',
    'employer rejected',
    'you were rejected',
)
RESPONSE_ALREADY_TEXTS = (
    'уже откликнулись',
    'отклик уже отправлен',
    'already applied',
    'you have already applied',
)
RESPONSE_LIMIT_TEXTS = (
    'в течение 24 часов можно совершить',
    'не более 200 откликов',
    'исчерпали лимит откликов',
    'попробуйте отправить отклик позднее',
    'within 24 hours',
    'no more than 200 responses',
    'response limit',
    'you have reached the limit',
)
RESPONSE_READY_TEXTS = (
    'откликнуться',
    'все равно откликнуться',
    'всё равно откликнуться',
    'respond',
    'apply anyway',
)
RESPONSE_CHAT_TEXTS = (
    'чат',
    'chat',
)
RESPONSE_OTHER_RESUME_TEXTS = (
    'отклик другим резюме',
    'apply with another resume',
)
RESPONSE_MODAL_SELECTOR = (
    '[data-qa="vacancy-response-popup"], '
    '[data-qa*="relocation-warning"], '
    '[data-qa*="warning-popup"], '
    '[data-qa*="popup"], '
    '.bloko-modal, '
    '[role="dialog"], '
    '[class*="modal"], '
    '[class*="popup"]'
)
RESPONSE_SUBMIT_SELECTORS = (
    '[data-qa="vacancy-response-submit-popup"]',
    '[data-qa="vacancy-response-letter-submit"]',
    '[data-qa*="warning-confirm"]',
    '[data-qa*="relocation-warning-confirm"]',
    '[data-qa*="relocation"] button',
    '[data-qa*="warning"] button',
    'button[data-qa*="submit"]',
    'button[data-qa*="confirm"]',
    'button[type="submit"]',
    'button[class*="submit"]',
    '.bloko-modal button[class*="primary"]',
    '[class*="modal"] button[class*="primary"]',
    '[role="dialog"] button[class*="primary"]',
)
RESPONSE_BLOCKER_TEXTS = (
    # Только фраза самой проверки hh. Слова «captcha»/«капча» по всей странице давали ложную капчу:
    # 07.10 VillaCarte (веб-безопасность) упоминает CAPTCHA в описании вакансии, бот ждал по 3 мин.
    ('подтвердите, что вы не робот', 'Требуется капча'),
    ('ответьте на вопрос', 'Не заполнены вопросы работодателя'),
    ('ответьте на вопросы', 'Не заполнены вопросы работодателя'),
    ('поменяйте видимость резюме', 'hh временно скрыл резюме от откликов'),
    ('вакансия в архиве', 'Вакансия в архиве'),
    ('вакансия перемещена в архив', 'Вакансия в архиве'),
    ('попробуйте позднее', 'HH просит попробовать позднее'),
    ('попробуйте позже', 'HH просит попробовать позднее'),
    ('что-то пошло не так', 'Ошибка HH после клика'),
    ('произошла ошибка', 'Ошибка HH после клика'),
)
# Дежурные отписки ai_assistant: если ответ свёлся к ним, осмысленного ответа нет.
# Строки продублированы из ai_assistant._heuristic_text_answer — файл править нельзя.
# Ответ, который на деле ответом не является. Такой текст нельзя отправлять в
# анкету работодателя: он останется у него в заявке навсегда. Лучше пропустить
# вакансию, чем написать в её анкету «извините, я не могу ответить».
# Условия в описании вакансии, которые многие считают неприемлемыми. Список
# правится в настройках (ключ skip_keywords); пустой список отключает отсев.
DEFAULT_SKIP_KEYWORDS = (
    'полиграф',
    'проверка на полиграфе',
    'детектор лжи',
    'неоплачиваемая стажировка',
    'бесплатная стажировка',
    'тестовое задание до собеседования',
    'выполнение тестового задания является обязательным до',
)


GENERIC_ANSWER_MARKERS = (
    # дежурные отписки самого ai_assistant
    'готов подробно обсудить данный вопрос',
    'имею соответствующий практический опыт',
    # отказы языковой модели
    'не могу ответить',
    'не могу предоставить',
    'не располагаю информацией',
    'недостаточно информации',
    'затрудняюсь ответить',
    'уточните вопрос',
    'как языковая модель',
    'как ии-ассистент',
    'как ai-ассистент',
)
# Маркер пропуска вакансии из-за вопроса без осмысленного ответа (см. describe_unanswered_questions)
QUESTIONS_SKIP_MARKER = 'Нет осмысленного ответа на вопрос работодателя'
# Контейнеры вопросов работодателя: и в модалке отклика, и на отдельной странице-анкете.
QUESTION_CONTAINER_SELECTORS = (
    '[data-qa="task-body"]',
    '[data-qa="vacancy-response-popup-form-question"]',
    '.vacancy-response-popup-form__question',
)
# Широкий селектор применяем только внутри формы отклика: по всей странице
# он ловит посторонние блоки и вакансия пропускалась бы зря.
QUESTION_CONTAINER_SELECTORS_LOOSE = QUESTION_CONTAINER_SELECTORS + ('[class*="question"]',)
from config_manager import (  # один список для всех фильтров
    SECURITY_TITLE_KEYWORDS as STRICT_TITLE_INCLUDE_KEYWORDS,
    NON_IT_SAFETY_MARKERS, SECURITY_PROTECTION_CONTEXT, security_title_by_meaning,
    find_title_keyword, STRICT_TITLE_EXCLUDE_KEYWORDS, TECHNICAL_FALLBACK_INCLUDE_KEYWORDS,
    title_excludes, commercial_title_keyword,
)


def is_short_question(text) -> bool:
    """Короткий фактический вопрос («Ваш ТГ?», «Зарплатные ожидания?»).

    Только на такие отвечаем готовым ответом из настроек; на развёрнутые
    отвечает модель по профилю.
    """
    q = ' '.join(str(text or '').replace('Писать тут', ' ').split())
    return len(q) <= 90


VISIBLE_RESPONSE_STATE_SCRIPT = r"""
const viewportWidth = window.innerWidth;
const viewportHeight = window.innerHeight;

function controlInfo(element) {
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  const text = (element.innerText || element.textContent || '').trim();
  const visible = (
    style.display !== 'none'
    && style.visibility !== 'hidden'
    && style.opacity !== '0'
    && rect.width > 0
    && rect.height > 0
  );
  const inViewport = (
    visible
    && rect.bottom > 0
    && rect.right > 0
    && rect.top < viewportHeight
    && rect.left < viewportWidth
  );

  return {
    text,
    visible,
    inViewport,
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    width: rect.width,
    height: rect.height,
    area: rect.width * rect.height,
    centerX: rect.left + (rect.width / 2),
    centerY: rect.top + (rect.height / 2),
  };
}

const bodyText = document.body ? document.body.innerText : '';
const controls = Array.from(document.querySelectorAll('button, a, [role="button"]'))
  .map((element, index) => ({ index, ...controlInfo(element) }))
  .filter((control) => control.visible && control.inViewport && control.text);

return {
  hasModal: /Отклик на вакансию|другой стране|другом регионе|все равно откликнуться|всё равно откликнуться/i.test(bodyText),
  controls,
};
"""
VISIBLE_APPLY_TARGET_SCRIPT = r"""
const viewportWidth = window.innerWidth;
const viewportHeight = window.innerHeight;

function isVisible(element) {
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  return (
    style.display !== 'none'
    && style.visibility !== 'hidden'
    && style.opacity !== '0'
    && rect.width > 0
    && rect.height > 0
    && rect.bottom > 0
    && rect.right > 0
    && rect.top < viewportHeight
    && rect.left < viewportWidth
  );
}

function targetInfo(element, index) {
  const rect = element.getBoundingClientRect();
  const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true';
  const bodyText = document.body?.innerText || '';
  // Центр кнопки может быть закрыт липкой шапкой или оверлеем модалки: клик по
  // координатам уйдёт мимо, а Python получит «успех». Проверяем, что в точке
  // действительно кнопка.
  const cx = rect.left + (rect.width / 2);
  const cy = rect.top + (rect.height / 2);
  const hit = document.elementFromPoint(cx, cy);
  return {
    hitsTarget: !!hit && (hit === element || element.contains(hit) || hit.contains(element)),
    found: true,
    index,
    text: (element.innerText || element.textContent || '').trim(),
    disabled,
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    width: rect.width,
    height: rect.height,
    area: rect.width * rect.height,
    centerX: rect.left + (rect.width / 2),
    centerY: rect.top + (rect.height / 2),
    hasModal: /Отклик на вакансию|другой стране|другом регионе|все равно откликнуться|всё равно откликнуться/i.test(bodyText),
  };
}

const bodyText = document.body?.innerText || '';
const hasModal = /Отклик на вакансию|другой стране|другом регионе|все равно откликнуться|всё равно откликнуться/i.test(bodyText);
let candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
  .map((element, index) => ({ element, index }))
  .filter(({ element }) => isVisible(element))
  .map(({ element, index }) => ({ element, ...targetInfo(element, index) }))
  .filter((candidate) => {
    const t = candidate.text.toLowerCase();
    return (t === 'откликнуться' || t === 'все равно откликнуться' || t === 'всё равно откликнуться' || t === 'apply anyway') && !candidate.disabled;
  });

if (!candidates.length) {
  return { found: false, hasModal };
}

if (hasModal) {
  const modalCandidates = candidates.filter((candidate) => candidate.top > viewportHeight * 0.35);
  candidates = modalCandidates.length ? modalCandidates : candidates;
  candidates.sort((left, right) => right.area - left.area || right.top - left.top);
} else {
  const pageCandidates = candidates.filter((candidate) => candidate.top > 100 && candidate.top < viewportHeight - 10);
  candidates = pageCandidates.length ? pageCandidates : candidates;
  candidates.sort((left, right) => left.top - right.top || right.area - left.area);
}

const target = candidates[0];
target.element.scrollIntoView({ block: 'center', inline: 'center' });
return targetInfo(target.element, target.index);
"""

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def is_allowed_hh_url(url):
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname or ''
    return parsed_url.scheme == 'https' and (
        hostname == HH_ALLOWED_HOST_SUFFIX
        or hostname.endswith(f'.{HH_ALLOWED_HOST_SUFFIX}')
    )


def get_api_vacancy_url(vacancy):
    for field_name in ('alternate_url', 'apply_alternate_url'):
        url = vacancy.get(field_name)
        if isinstance(url, str) and is_allowed_hh_url(url):
            return url

    return None


def resolve_workspace_path(path):
    target = os.path.realpath(os.path.join(SCRIPT_DIR, path))
    if PROFILE_ID != 'default' and os.path.commonpath([target, os.path.realpath(SCRIPT_DIR)]) != os.path.realpath(SCRIPT_DIR):
        raise ValueError('Кеш вакансий должен находиться внутри текущего профиля')
    return target


def wait_before_browser_close():
    if not sys.stdin.isatty():
        return

    try:
        input("\nНажмите Enter для закрытия браузера...")
    except EOFError:
        logging.info("stdin закрыт, браузер будет закрыт автоматически")


def write_json_atomic(path, data):
    temp_path = f"{path}.tmp"
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)

# Настройка логирования
from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)
from ai_assistant import AIAssistant, clean_public_text, is_restricted_question, SALARY_ANSWER
from db_manager import DatabaseManager
from template_engine import render_template
from terminal_ui import (
    ColoredConsoleFormatter, colorize_text, explain_error,
    c_ok, c_err, c_warn, c_info, c_skip,
    c_priority, c_header, c_company, c_title, c_accent,
    CYAN, GREEN, YELLOW, RED, MAGENTA, BLUE, BOLD, RESET, WHITE, DIM
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredConsoleFormatter('%(levelname)s - %(message)s'))
file_handler = logging.FileHandler(os.path.join(SCRIPT_DIR, 'hh_selenium.log'), encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Уровень DEBUG у логгера, INFO у консоли, DEBUG у файла.
#
# Раньше логгер стоял на INFO, и logging.debug отсекался ЕЩЁ НА ЛОГГЕРЕ — то есть
# не доходил даже до файла журнала. Каждое «подробности записаны в журнал» было
# неправдой: технические детали исчезали бесследно, и разбирать сбои приходилось
# вслепую. Теперь пользователь по-прежнему видит только INFO и выше, а подробности
# реально ложатся в файл.
console_handler.setLevel(logging.INFO)
# Служебные строки библиотек (адреса запросов, коды ответов) — только в файл.
try:
    from terminal_ui import hide_technical_from_console
    hide_technical_from_console(console_handler)
except Exception:
    pass
file_handler.setLevel(logging.DEBUG)
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[file_handler, console_handler]
)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)
logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.ERROR)

class HHSeleniumBot:
    is_paused = False
    stop_requested = False

    def __init__(self, headless=False, debugger_address=None, pause_before_close=True):
        self.driver = None
        self.wait = None
        self.headless = headless
        self.debugger_address = debugger_address
        self.pause_before_close = pause_before_close
        
        # Файлы данных
        self.applied_file = os.path.join(SCRIPT_DIR, 'applied_vacancies_selenium.json')
        self.shared_applied_file = os.path.join(SCRIPT_DIR, 'applied_vacancies.json')
        self.config_file = os.path.join(SCRIPT_DIR, 'hh_selenium_config.json')
        self.api_cache_file = os.path.join(SCRIPT_DIR, DEFAULT_API_CACHE_FILE)
        
        # База данных SQLite для истории откликов, отказов и адаптивных исправлений
        self.db = DatabaseManager()
        self.last_application_meta = {}
        # Почему письмо не попало в отклик (для честного итога в логе)
        self.letter_skip_reason = ''
        # Вопросы работодателя, на которые не нашлось осмысленного ответа
        self.unanswered_questions = []

        # Загружаем конфиг
        self.config = self.load_config()
        self.ai_assistant = AIAssistant(self.config)
        
        # Загружаем список откликов
        self.applied_vacancies = self.load_applied()

        # Счетчики
        self.applied_today = self.count_sent_today()
        self.sent_this_run = 0
        self.skipped = 0
        self.errors = 0
        self.response_limit_reached = False
        self.last_search_page_count = 0 # сколько карточек было на последней странице поиска
        self.consecutive_apply_failures = 0 # серия неподтверждённых откликов подряд
        self.throttled_stop = False # выставляется, когда серия превысила лимит (троттлинг hh)
        
        # Интерактивное управление (пауза / стоп / статус)
        self.is_paused = False
        self.stop_requested = False
        for flag_name in ('pause.flag', 'stop.flag'):
            flag_path = os.path.join(SCRIPT_DIR, flag_name)
            if os.path.exists(flag_path):
                try:
                    os.remove(flag_path)
                except Exception:
                    pass
        
        # Настройки задержек (секунды) - БЫСТРЫЙ РЕЖИМ
        self.delay_between_actions = (0.5, 1.5) # Между действиями
        self.delay_between_vacancies = (1, 2) # Между вакансиями
        self.delay_after_apply = (0.5, 1) # После отклика

    def save_window_geometry(self):
        """Сохраняет текущие координаты и размер окна браузера."""
        if not self.driver or self.headless:
            return
        try:
            geom_file = os.path.join(SCRIPT_DIR, 'chrome_window_geometry.json')
            rect = self.driver.get_window_rect()
            if rect and rect.get('width', 0) > 200:
                with open(geom_file, 'w', encoding='utf-8') as f:
                    json.dump(rect, f)
        except Exception:
            pass

    def restore_window_geometry(self):
        """Восстанавливает предыдущие координаты и размер окна браузера."""
        if not self.driver or self.headless:
            return
        try:
            geom_file = os.path.join(SCRIPT_DIR, 'chrome_window_geometry.json')
            if os.path.exists(geom_file):
                with open(geom_file, 'r', encoding='utf-8') as f:
                    rect = json.load(f)
                if rect and 'x' in rect and 'y' in rect and 'width' in rect and 'height' in rect:
                    self.driver.set_window_rect(
                        x=rect['x'],
                        y=rect['y'],
                        width=rect['width'],
                        height=rect['height']
                    )
        except Exception:
            pass

    def close_driver(self):
        """Закрывает браузер без ручной паузы в пакетных режимах с сохранением геометрии окна."""
        if not self.driver:
            return

        try:
            if getattr(self, 'pause_before_close', False):
                wait_before_browser_close()
        finally:
            try:
                self.save_window_geometry()
            except Exception:
                pass
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass
        
    def load_config(self):
        """Загружает конфигурацию"""
        try:
            from config_manager import load_config as cm_load_config, get_active_preset
            cm_cfg = cm_load_config()
            preset = get_active_preset()
        except Exception:
            cm_cfg = {}
            preset = {}

        default_config = {
            'search_url': preset.get('search_url') or 'https://hh.ru/search/vacancy?text=python+developer&area=1',
            'api_cache_file': DEFAULT_API_CACHE_FILE,
            'cover_letter': 'Добрый день! Заинтересован в данной позиции. Готов обсудить детали.',
            'max_applications': 200,
            'skip_with_tests': True,
            'skip_applied': True,
            'allow_technical_fallback': True,
            'keywords_include': preset.get('keywords_include', []), # Вакансии должны содержать эти слова
            'keywords_exclude': preset.get('keywords_exclude', ['стажер', 'intern', 'junior']), # Исключить вакансии с этими словами
            # Регион для браузерного поиска (113 = Россия), совпадает с API-поиском test.py
            'search_area': '113',
            'max_search_pages': 50, # предохранитель глубины пагинации на один запрос
            'max_consecutive_failures': APPLY_FAILURE_STREAK_LIMIT, # стоп при серии провалов (троттлинг)
            # ЯРУС 2: добор ИБ-вакансий напрямую с сайта (строгий security-фильтр, без dev-fallback)
            'security_search_queries': [
                'информационная безопасность',
                'кибербезопасность',
                'специалист по защите информации',
                'пентест',
                'penetration tester',
                'application security',
                'devsecops',
                'security engineer',
                'аналитик soc',
                'soc',
                'siem',
                'red team',
                'анализ защищенности',
                'безопасность приложений',
            ],
            # ЯРУС 3: добор обычной разработкой/IT до дневного лимита.
            # Запросы намеренно IT-специфичные, чтобы не цеплять не-IT (технолог/повар и т.п.).
            'dev_topup_enabled': True,
            'dev_search_queries': [
                'python разработчик',
                'backend разработчик',
                'java разработчик',
                'golang разработчик',
                'c# разработчик',
                'frontend разработчик',
                'fullstack разработчик',
                'devops инженер',
                'инженер-программист',
                'программист 1с',
                'системный администратор',
            ],
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    default_config.update(config)
            elif cm_cfg:
                default_config.update(cm_cfg)
        except Exception as e:
            logging.warning(f"Ошибка загрузки конфига: {explain_error(e)}")
        
        # Сохраняем конфиг для редактирования
        self.save_config(default_config)
        return default_config
    
    def save_config(self, config):
        """Сохраняет конфигурацию"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка сохранения конфига: {explain_error(e)}")
    
    def load_applied(self):
        """Загружает список вакансий, на которые уже откликнулись"""
        try:
            if os.path.exists(self.applied_file):
                with open(self.applied_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"Ошибка загрузки списка откликов: {explain_error(e)}")
        return {}

    def count_sent_today(self):
        shared_history = {}
        shared_file = getattr(self, 'shared_applied_file', '')
        if shared_file and os.path.exists(shared_file):
            try:
                with open(shared_file, 'r', encoding='utf-8') as f:
                    shared_history = json.load(f)
                if not isinstance(shared_history, dict):
                    raise ValueError('Неверный формат общей истории откликов')
            except (OSError, ValueError) as e:
                logging.warning(f"Не удалось учесть общую историю откликов: {explain_error(e)}")
        return count_recent_applications(shared_history, self.applied_vacancies,
                                         APPLICATION_LIMIT_WINDOW_HOURS)

    def local_application_limit_reached(self):
        from config_manager import local_application_limit
        limit = local_application_limit(self.config)
        return limit is not None and self.applied_today >= limit
    
    def remove_from_api_cache(self, vacancy_id):
        """Удаляет обработанную вакансию из API-кеша."""
        try:
            if not os.path.exists(self.api_cache_file):
                return

            with open(self.api_cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            vacancies = cache_data.get('vacancies', [])
            if not isinstance(vacancies, list):
                return

            original_count = len(vacancies)
            cache_data['vacancies'] = [
                vacancy
                for vacancy in vacancies
                if str(vacancy.get('id')) != str(vacancy_id)
            ]

            if len(cache_data['vacancies']) == original_count:
                return

            cache_data['total_count'] = len(cache_data['vacancies'])
            write_json_atomic(self.api_cache_file, cache_data)
            logging.debug(f"Вакансия {vacancy_id} убрана из списка")
        except Exception as e:
            logging.error(f"Не удалось убрать вакансию из списка: {explain_error(e)}")

    def save_shared_applied(self, vacancy_id, timestamp, status):
        """Синхронизирует Selenium-историю с общей историей API-бота."""
        if status not in SHARED_APPLIED_STATUSES:
            return

        try:
            if os.path.exists(self.shared_applied_file):
                with open(self.shared_applied_file, 'r', encoding='utf-8') as f:
                    shared_applied = json.load(f)
            else:
                shared_applied = {}

            shared_applied[str(vacancy_id)] = timestamp

            write_json_atomic(self.shared_applied_file, shared_applied)
        except Exception as e:
            logging.error(f"Ошибка синхронизации общей истории откликов: {explain_error(e)}")

    def save_applied(self, vacancy_id, vacancy_name, status=STATUS_SENT, company='', url='', cover_letter='', questions_count=0, ats_score=0, detected_skills=None):
        """Сохраняет информацию об отклике в JSON-файлы и SQLite базу данных."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.applied_vacancies[str(vacancy_id)] = {
            'name': vacancy_name,
            'date': timestamp,
            'status': status
        }
        try:
            write_json_atomic(self.applied_file, self.applied_vacancies)
            self.save_shared_applied(vacancy_id, timestamp, status)
            if status in SHARED_APPLIED_STATUSES:
                self.applied_today = self.count_sent_today()
            self.remove_from_api_cache(vacancy_id)

            # Сохранение в базу данных SQLite
            if hasattr(self, 'db') and self.db:
                meta = getattr(self, 'last_application_meta', {})
                eff_company = company or meta.get('company', '')
                eff_url = url or meta.get('url', '')
                eff_cover_letter = cover_letter or meta.get('cover_letter', '')
                eff_questions = questions_count or meta.get('questions_count', 0)
                eff_ats_score = ats_score or meta.get('ats_score', 0)
                eff_skills = detected_skills or meta.get('skills', [])

                self.db.record_application(
                    vacancy_id=str(vacancy_id),
                    title=vacancy_name,
                    company=eff_company,
                    url=eff_url,
                    cover_letter=eff_cover_letter,
                    questions_count=eff_questions,
                    ats_score=eff_ats_score,
                    detected_skills=eff_skills,
                    status=status,
                    resume_id=os.environ.get('HH_RESUME_ID') or self.config.get('resume_id', '')
                )
        except Exception as e:
            logging.error(f"Ошибка сохранения отклика: {explain_error(e)}")

    def skip_cached_vacancy(self, vacancy_id):
        """Убирает из текущего API-кеша вакансию, которую точно не надо повторять."""
        self.remove_from_api_cache(vacancy_id)

    def get_visible_page_text(self):
        try:
            return self.driver.find_element(By.TAG_NAME, 'body').text.lower()
        except Exception:
            return self.driver.page_source.lower()

    def is_response_limit_reached(self):
        page_text = self.get_visible_page_text()
        return any(text in page_text for text in RESPONSE_LIMIT_TEXTS)

    def is_disabled_element(self, element):
        try:
            if not element.is_enabled():
                return True
        except Exception:
            pass

        for attribute_name in ('disabled', 'aria-disabled'):
            try:
                attribute_value = element.get_attribute(attribute_name)
            except Exception:
                continue
            if str(attribute_value).lower() in ('true', 'disabled'):
                return True

        return False

    def page_says_archived(self) -> bool:
        """Страница говорит, что в архиве именно ЭТА вакансия.

        Карточки других вакансий внизу страницы («похожие», «вы смотрели»)
        несут свою пометку «Вакансия в архиве». Поиск по тексту всей страницы
        принимал её на свой счёт и срывал настоящие отклики — поэтому текст
        внутри карточек `vacancy-serp__*` не учитываем.
        """
        try:
            # Строго True: любой другой ответ (None, заглушка) — не архив.
            return True is (self.driver.execute_script(r"""
                const re = /вакансия\s+(в\s+архиве|перемещена\s+в\s+архив)/i;
                for (const el of document.querySelectorAll('body *')) {
                    if (el.children.length) continue;
                    if (!re.test(el.textContent || '')) continue;
                    if (el.closest('[data-qa^="vacancy-serp"]')) continue;
                    if (el.offsetParent === null) continue;
                    return true;
                }
                return false;
            """))
        except Exception:
            return False

    def get_response_blocker_message(self):
        if self.is_response_limit_reached():
            return "Лимит откликов"

        page_text = self.get_visible_page_text()

        if 'обязательное поле' in page_text and 'сопровод' in page_text:
            return "Сопроводительное письмо не заполнено"

        for text, message in RESPONSE_BLOCKER_TEXTS:
            # Архив проверяется отдельно: по тексту всей страницы он ловил
            # пометку чужой карточки внизу страницы.
            if message == 'Вакансия в архиве':
                continue
            if text in page_text:
                if message == 'Не заполнены вопросы работодателя':
                    # Заголовок анкеты остаётся и после заполнения всех полей.
                    validation_error = self.get_modal_validation_error(self.driver)
                    if not validation_error or not any(
                            marker in validation_error.lower()
                            for marker, reason in RESPONSE_BLOCKER_TEXTS
                            if reason == message):
                        continue
                return message

        if self.page_says_archived():
            return 'Вакансия в архиве'

        return None

    def wait_for_network_back(self) -> bool:
        """Ждёт возвращения сети, слушая «пауза/стоп». True — можно повторять."""
        try:
            from terminal_ui import wait_for_network
        except Exception:
            return False
        return wait_for_network(
            NETWORK_WAIT_SECONDS, logging.getLogger(__name__),
            should_stop=lambda: self.check_interactive_controls() == 'stop' or self.stop_requested)

    def page_has_captcha(self):
        """На странице всё ещё висит проверка «вы не робот»."""
        page_text = self.get_visible_page_text()
        has_text = any(text in page_text
                   for text, message in RESPONSE_BLOCKER_TEXTS
                   if message == 'Требуется капча')
        if has_text:
            return True
        return any(el.is_displayed() for el in self.driver.find_elements(
            By.CSS_SELECTOR, 'input[data-qa="account-captcha-input"], img[data-qa="account-captcha-picture"], '
                             'iframe[src*="captcha"], [data-qa*="captcha"]'))

    def wait_for_human_captcha(self, timeout_seconds=180):
        """Ждёт, пока человек сам решит капчу в окне браузера.

        Капчу бот не решает и не пытается: её решает человек. Раньше вакансия
        с капчей просто уходила в ошибки, хотя достаточно было пары кликов.
        Возвращает True, если капча пропала со страницы, иначе False.
        """
        if self.try_vision_captcha():
            return True
        if self.headless and self.show_browser_for_captcha():
            pass  # окно открыто — ниже обычное ожидание, как в видимом режиме
        elif self.headless:
            # Окно открыть не вышло — решить капчу некому. Подсказку даём один раз за
            # прогон, а не на каждую вакансию, чтобы не засорять лог.
            if not getattr(self, '_captcha_headless_hint_shown', False):
                self._captcha_headless_hint_shown = True
                logging.info(" [~] hh просит подтвердить, что вы не робот. В фоновом режиме "
                             "решить это некому: запустите бота с видимым окном браузера, "
                             "и он дождётся, пока вы решите проверку.")
            return False

        minutes = max(1, round(timeout_seconds / 60))
        print(f"\n{YELLOW}{BOLD}{'=' * 60}{RESET}")
        print(f"{YELLOW}{BOLD} hh просит подтвердить, что вы не робот.{RESET}")
        print(f"{YELLOW}{BOLD} Решите проверку в окне браузера.{RESET}")
        print(f"{YELLOW}{BOLD} Бот подождёт до {minutes} мин. и продолжит сам.{RESET}")
        print(f"{YELLOW}{BOLD}{'=' * 60}{RESET}\n")
        logging.info(f" [~] Жду, пока вы решите капчу в окне браузера (до {minutes} мин.)")

        # Звук — чтобы человек заметил, даже если смотрит в другое окно.
        # winsound есть только на Windows, поэтому без него просто молчим.
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass
        # Поднимаем окно браузера наверх, чтобы капчу не пришлось искать.
        try:
            self.driver.switch_to.window(self.driver.current_window_handle)
        except Exception:
            pass
        try:
            self.driver.execute_cdp_cmd('Page.bringToFront', {})
        except Exception:
            pass

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.check_interactive_controls() == 'stop' or self.stop_requested:
                return False
            try:
                if not self.page_has_captcha():
                    return True
            except Exception:
                # Страница может перезагружаться прямо после решения капчи —
                # это не повод бросать ожидание.
                pass
            time.sleep(2)

        logging.info(f" [~] Капчу не решили за {minutes} мин. — иду дальше")
        return False

    def show_browser_for_captcha(self) -> bool:
        """Фоновый браузер не показать, поэтому перезапускаем его с окном на той же странице.

        Вход сохраняется в профиле Chrome. До конца прогона браузер остаётся видимым:
        если hh спросил капчу один раз, скорее всего спросит снова.
        """
        try:
            url = self.driver.current_url if self.driver else ''
        except Exception:
            url = ''
        logging.info(" [~] hh просит капчу — открываю окно браузера, чтобы вы её решили")
        pause = getattr(self, 'pause_before_close', False)
        self.pause_before_close = False
        try:
            self.close_driver()
        finally:
            self.pause_before_close = pause
        self.driver = None
        self.headless = False
        try:
            if not self.init_driver():
                return False
            if url and is_allowed_hh_url(url):
                self.driver.get(url)
                time.sleep(2)
            return True
        except Exception as e:
            logging.debug(f"Окно для капчи не открылось: {explain_error(e)}")
            return False

    def apply_delay(self):
        """Пауза между откликами: из настроек apply_delay_seconds [мин, макс], иначе прежняя.

        Длинная случайная пауза (например, 20–60 с) реже вызывает капчу hh.
        """
        raw = (getattr(self, 'config', {}) or {}).get('apply_delay_seconds')
        try:
            low, high = float(raw[0]), float(raw[1])
            if 0 <= low <= high:
                return low, high
        except (TypeError, ValueError, IndexError, KeyError):
            pass
        return self.delay_between_vacancies

    def try_vision_captcha(self):
        settings = (getattr(self, 'config', {}) or {}).get('captcha') or {}
        if not settings.get('enabled'):
            return False
        try:
            attempts = min(3, max(1, int(settings.get('max_attempts', 2))))
            for _ in range(attempts):
                if self.check_interactive_controls() == 'stop' or self.stop_requested:
                    return False
                if not is_allowed_hh_url(self.driver.current_url):
                    return False
                pictures = self.driver.find_elements(By.CSS_SELECTOR, 'img[data-qa="account-captcha-picture"]')
                inputs = self.driver.find_elements(By.CSS_SELECTOR, 'input[data-qa="account-captcha-input"]')
                picture = next((p for p in pictures if p.is_displayed()), None)
                field = next((f for f in inputs if f.is_displayed()), None)
                if picture is None or field is None:
                    return False
                answer = self.ai_assistant.recognize_captcha(picture.screenshot_as_png)
                if not answer:
                    return False
                field.clear()
                field.send_keys(answer)
                field.send_keys(Keys.ENTER)

                def cleared(driver):
                    fields = driver.find_elements(By.CSS_SELECTOR, 'input[data-qa="account-captcha-input"]')
                    return not any(f.is_displayed() for f in fields) and not self.page_has_captcha()

                try:
                    WebDriverWait(self.driver, 10).until(cleared)
                    logging.info('[OK] Текстовая капча решена')
                    return True
                except TimeoutException:
                    continue
        except Exception as exc:
            logging.debug('Vision captcha failed: %s', type(exc).__name__)
        return False

    def find_response_modal(self, wait_seconds=0):
        deadline = time.time() + max(0, wait_seconds)
        while True:
            try:
                # Общий CSS-список сортируется по DOM, а не по приоритету селекторов.
                for selector in ('[data-qa="vacancy-response-popup"]', RESPONSE_MODAL_SELECTOR):
                    for modal in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        try:
                            if modal.tag_name.lower() not in ('button', 'input', 'textarea', 'a') and modal.is_displayed():
                                return modal
                        except Exception:
                            continue
            except Exception as e:
                logging.debug(f"Не удалось найти модалку отклика: {e}")

            if time.time() >= deadline:
                break
            time.sleep(0.3)

        return None

    def application_ui_problem(self, code, detail):
        counts = getattr(self, '_application_ui_errors', {})
        counts[code] = counts.get(code, 0) + 1
        self._application_ui_errors = counts
        message = (f'[{code}] {detail}. Финальная отправка ботом остановлена; '
                   'успех не подтверждён, история отправок не изменена.')
        try:
            directory = os.path.join(SCRIPT_DIR, '.apply_diagnostics')
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + code)
            dom = self.driver.execute_script("""
                const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
                const modals = [...document.querySelectorAll(arguments[0])].filter(visible);
                const root = modals.find(el => el.getAttribute('data-qa') === 'vacancy-response-popup')
                    || modals[0] || document;
                const nodes = [...root.querySelectorAll(
                    '[data-qa], [role="dialog"], input, textarea, button, [contenteditable]')].filter(visible);
                return {host: location.hostname, ready_state: document.readyState,
                    page_kind: location.pathname.split('/')[1],
                    modals: modals.slice(0, 20).map(el => ({tag: el.tagName, qa: el.getAttribute('data-qa'),
                        role: el.getAttribute('role'), controls: el.querySelectorAll('input,textarea,button').length})),
                    loading: [...document.querySelectorAll('[role="progressbar"], [data-qa*="loading"], [data-qa*="spinner"]')].some(visible),
                    nodes: nodes.slice(0, 150).map(el => ({tag: el.tagName, qa: el.getAttribute('data-qa'),
                        role: el.getAttribute('role'), class: typeof el.className === 'string' ? el.className : '',
                        text_length: (el.innerText || '').trim().length, disabled: !!el.disabled}))};
            """, RESPONSE_MODAL_SELECTOR)
            screenshot = None
            # ponytail: три снимка на тип ошибки за запуск; структура сохраняется для каждого случая.
            if counts[code] <= 3:
                try:
                    if self.driver.save_screenshot(path + '.png'):
                        screenshot = os.path.basename(path + '.png')
                except Exception:
                    logging.warning('[%s] Снимок недоступен; сохраняю структурную диагностику.', code)
            meta = getattr(self, 'last_application_meta', {}) or {}
            vacancy_id = self.get_vacancy_id_from_url(meta.get('url', ''))
            if not str(vacancy_id or '').isdigit():
                vacancy_id = None
            with open(path + '.json', 'w', encoding='utf-8') as stream:
                json.dump({'code': code, 'vacancy_id': vacancy_id,
                           'dom': dom, 'screenshot': screenshot}, stream, ensure_ascii=False, indent=2)
            return message + f' Диагностика: {path}.json'
        except Exception:
            logging.warning('[%s] Не удалось сохранить диагностику; история не изменена.', code)
            return message + ' Диагностику не удалось сохранить; проверьте окно браузера.'

    def handle_warning_popups(self):
        """
        Обнаруживает и подтверждает предупреждающие всплывающие окна HH.ru:
        - «Вы откликаетесь на вакансию в другой стране»
        - «Вы откликаетесь на вакансию в другом регионе»
        - Предупреждения о несоответствии требованиям
        Кликает по кнопке «Все равно откликнуться» / «Apply anyway».
        """
        confirm_keywords = (
            'все равно откликнуться',
            'всё равно откликнуться',
            'apply anyway',
            'да, откликнуться',
            'продолжить',
        )

        warning_markers = (
            'в другой стране',
            'в другом регионе',
            'в другом городе',
            'не указали, что хотите переехать',
            'скорее всего, будет отказ',
            'все равно откликнуться',
            'всё равно откликнуться',
        )

        try:
            body_text = self.get_visible_page_text().lower()
            has_warning = any(m in body_text for m in warning_markers)
            if not has_warning:
                return False

            button_selectors = [
                '[data-qa*="relocation-warning-confirm"]',
                '[data-qa*="warning-confirm"]',
                '[data-qa*="confirm"]',
                '[data-qa*="relocation"] button',
                '[data-qa*="warning"] button',
                '[role="dialog"] button',
                '.bloko-modal button',
                '[class*="modal"] button',
                'button',
                'a[role="button"]',
                '[role="button"]',
            ]

            for sel in button_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        if not el.is_displayed() or self.is_disabled_element(el):
                            continue
                        text = el.text.strip().lower()
                        if any(kw in text for kw in confirm_keywords):
                            logging.info(f" [OK] Подтверждено предупреждение HH: «{el.text.strip()}»")
                            if self.click_element_with_mouse(el):
                                time.sleep(0.8)
                                return True
                except Exception:
                    continue

            # JavaScript fallback
            clicked = self.driver.execute_script("""
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                for (const b of buttons) {
                    const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
                    if (txt.includes('все равно откликнуться') || txt.includes('всё равно откликнуться') || txt.includes('apply anyway')) {
                        b.scrollIntoView({ block: 'center' });
                        b.click();
                        return true;
                    }
                }
                return false;
            """)
            if clicked:
                logging.info(" [OK] Подтверждено предупреждение HH через JS: «Все равно откликнуться»")
                time.sleep(0.8)
                return True

        except Exception as e:
            logging.debug(f"Ошибка проверки предупреждающих окон: {e}")

        return False

    def get_visible_response_state(self):
        try:
            state = self.driver.execute_script(VISIBLE_RESPONSE_STATE_SCRIPT)
            if isinstance(state, dict):
                return state
        except Exception as e:
            logging.debug(f"Не удалось прочитать видимые кнопки отклика: {e}")

        return {'hasModal': False, 'controls': []}

    def get_response_control_texts(self):
        selectors = [
            '[data-qa="vacancy-response-link-top"]',
            '[data-qa="vacancy-response-link-bottom"]',
            'a[data-qa*="vacancy-response"]',
            'button[data-qa*="vacancy-response"]',
        ]
        texts = []

        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for element in elements:
                try:
                    if element.is_displayed():
                        text = element.text.lower().strip()
                        if text:
                            texts.append(text)
                except Exception:
                    continue

        visible_state = self.get_visible_response_state()
        for control in visible_state.get('controls', []):
            text = str(control.get('text', '')).lower().strip()
            if text:
                texts.append(text)

        return texts

    def detect_response_state(self):
        if self.is_response_limit_reached():
            return 'limit'

        visible_state = self.get_visible_response_state()
        visible_control_texts = [
            str(control.get('text', '')).lower().strip()
            for control in visible_state.get('controls', [])
            if str(control.get('text', '')).strip()
        ]
        response_control_texts = [text.lower().strip() for text in self.get_response_control_texts()]
        all_control_texts = visible_control_texts + response_control_texts
        visible_control_text = ' '.join(visible_control_texts)
        response_control_text = ' '.join(response_control_texts)

        has_chat = any(any(marker in text for marker in RESPONSE_CHAT_TEXTS) for text in all_control_texts)
        has_other_resume = any(
            any(marker in text for marker in RESPONSE_OTHER_RESUME_TEXTS)
            for text in all_control_texts
        )
        has_apply = any(text == 'откликнуться' or text in RESPONSE_READY_TEXTS for text in visible_control_texts)

        if has_chat and has_other_resume:
            return 'success'
        if any(text in visible_control_text for text in RESPONSE_SUCCESS_TEXTS):
            return 'success'
        if any(text in visible_control_text for text in RESPONSE_DENIED_TEXTS):
            return 'denied'
        if any(text in visible_control_text for text in RESPONSE_ALREADY_TEXTS):
            return 'already'
        if has_apply:
            return 'ready'

        if any(text in response_control_text for text in RESPONSE_SUCCESS_TEXTS):
            return 'success'
        if any(text in response_control_text for text in RESPONSE_READY_TEXTS):
            return 'ready'
        if any(text in response_control_text for text in RESPONSE_DENIED_TEXTS):
            return 'denied'
        if any(text in response_control_text for text in RESPONSE_ALREADY_TEXTS):
            return 'already'

        return 'unknown'

    def wait_for_response_state(self):
        deadline = time.time() + RESPONSE_STATE_WAIT_SECONDS
        while time.time() < deadline:
            self.handle_warning_popups()
            state = self.detect_response_state()
            if state not in ('unknown', 'ready'):
                return state
            time.sleep(0.5)

        return self.detect_response_state()

    def find_visible_apply_text_buttons(self):
        buttons = []
        target_texts = (
            'откликнуться',
            'все равно откликнуться',
            'всё равно откликнуться',
            'apply anyway',
        )
        for element in self.driver.find_elements(By.CSS_SELECTOR, 'button, a'):
            try:
                text = element.text.lower().strip()
                if (
                    element.is_displayed()
                    and any(tt == text or tt in text for tt in target_texts)
                    and not self.is_disabled_element(element)
                ):
                    buttons.append(element)
            except Exception:
                continue

        return buttons

    def point_hits_element(self, element, x, y):
        """Лежит ли под этой точкой нужный элемент.

        CDP-клик стреляет по координатам и «успешно» отрабатывает, даже если по
        этим координатам оказалась липкая шапка, оверлей модалки или вообще
        пустое место за границей видимой области. Отсюда шли ложные успехи
        откликов. Проверяем реальную цель точки до выстрела.

        Попадание в РОДИТЕЛЯ тоже считается попаданием: у вёрстки hh переключатели
        спрятаны под своей подписью, и клик по подписи их честно переключает.
        """
        try:
            return bool(self.driver.execute_script(
                """
                const element = arguments[0];
                const hit = document.elementFromPoint(arguments[1], arguments[2]);
                return !!hit && (hit === element || element.contains(hit) || hit.contains(element));
                """,
                element,
                x,
                y,
            ))
        except Exception as e:
            logging.debug(f"Проверка точки клика не выполнилась: {e}")
            return False

    def element_click_point(self, element):
        """Координаты центра элемента в видимой области или None."""
        try:
            rect = self.driver.execute_script(
                """
                const rect = arguments[0].getBoundingClientRect();
                return {
                    centerX: rect.left + (rect.width / 2),
                    centerY: rect.top + (rect.height / 2),
                    width: rect.width,
                    height: rect.height,
                };
                """,
                element,
            )
        except Exception as e:
            logging.debug(f"Не удалось измерить элемент: {e}")
            return None
        if not rect or rect['width'] <= 0 or rect['height'] <= 0:
            return None
        return rect['centerX'], rect['centerY']

    def click_viewport_coordinates(self, x, y):
        try:
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseMoved',
                'x': x,
                'y': y,
            })
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed',
                'x': x,
                'y': y,
                'button': 'left',
                'clickCount': 1,
            })
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased',
                'x': x,
                'y': y,
                'button': 'left',
                'clickCount': 1,
            })
            return True
        except Exception as e:
            logging.debug(f"CDP-клик не сработал: {e}")
            return False

    def click_element_with_mouse(self, element):
        if self.is_disabled_element(element):
            logging.debug("Клик по disabled-элементу отклика пропущен")
            return False

        # Два захода: если точка занята чужим элементом, прокручиваем ещё раз и
        # перемеряем. Стрелять по координатам вслепую нельзя — промах раньше
        # возвращался как успешный клик.
        for attempt in range(2):
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                    element,
                )
            except Exception as e:
                logging.debug(f"Прокрутка к элементу не выполнилась: {e}")
                break
            time.sleep(0.2 if attempt == 0 else 0.4)

            point = self.element_click_point(element)
            if not point:
                break
            if not self.point_hits_element(element, point[0], point[1]):
                logging.debug(f"Точка клика занята другим элементом (попытка {attempt + 1})")
                continue
            if self.click_viewport_coordinates(point[0], point[1]):
                time.sleep(0.8)
                return True
            break

        # Последний заход — обычный клик Selenium. Он сам отказывается кликать
        # по перекрытому элементу, поэтому ложного успеха здесь не будет.
        try:
            element.click()
            time.sleep(0.8)
            return True
        except Exception as e:
            logging.debug(f"Обычный клик по элементу не сработал: {e}")
            return False

    def click_choice(self, option):
        """Отмечает radio/checkbox и проверяет, что отметка встала.

        Сам input у Magritte скрыт, поэтому кликаем его подпись (label); если
        input виден — сначала его. Успех — только когда input реально выбран.
        """
        targets = []
        identity = [option.get_attribute(key) for key in ('id', 'name', 'type', 'value')]

        def selected(_driver):
            try:
                return option.is_selected()
            except StaleElementReferenceException:
                # React мог заменить input. Не считаем исчезновение успехом и
                # не нажимаем повторно: читаем единственный тот же вариант.
                replacement = self.driver.execute_script("""
                    const [id, name, type, value] = arguments[0];
                    if (id) return document.getElementById(id);
                    const matches = Array.from(document.querySelectorAll('input'))
                      .filter(el => el.name === (name || '') && el.type === type && el.value === value);
                    return matches.length === 1 ? matches[0] : null;
                """, identity)
                return bool(replacement and replacement.is_selected())
        try:
            if option.is_displayed():
                targets.append(option)
            label = self.driver.execute_script(
                "const el = arguments[0];"
                "return el.closest('label') || (el.id && document.querySelector("
                "'label[for=\"' + CSS.escape(el.id) + '\"]')) || el.parentElement;", option)
            if label:
                targets.append(label)
        except Exception as e:
            logging.debug(f"Подпись варианта не найдена: {e}")
            targets.append(option)
        for target in targets:
            if not self.click_element_with_mouse(target):
                continue
            try:
                return bool(WebDriverWait(self.driver, 1.5, poll_frequency=0.1,
                                          ignored_exceptions=(StaleElementReferenceException,)).until(selected))
            except TimeoutException:
                return False
        return False

    def get_element_context_text(self, element):
        try:
            return self.driver.execute_script(
                """
                const element = arguments[0];
                const parent = element.closest('div, form, section') || element.parentElement;
                return [
                    element.getAttribute('data-qa') || '',
                    element.getAttribute('name') || '',
                    element.getAttribute('placeholder') || '',
                    element.getAttribute('aria-label') || '',
                    parent ? parent.innerText : '',
                ].join(' ').toLowerCase();
                """,
                element,
            )
        except Exception:
            return ''

    def is_cover_letter_required(self):
        page_text = self.get_visible_page_text()
        return 'обязательное поле' in page_text and 'сопровод' in page_text

    def get_text_input_value(self, element):
        try:
            value = element.get_attribute('value')
            if value:
                return value
        except Exception:
            pass

        try:
            return element.text or ''
        except Exception:
            return ''

    def focus_text_field(self, element) -> None:
        """Ставит курсор в поле. Промах не критичен: send_keys фокусирует и сам."""
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.15)
        except Exception:
            pass
        try:
            element.click()
            return
        except Exception:
            pass
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(self.driver).move_to_element(element).pause(0.1).click().perform()
            return
        except Exception:
            pass
        try:
            self.driver.execute_script("arguments[0].focus();", element)
        except Exception:
            pass

    def set_text_input_value(self, element, text):
        """Вводит текст в поле и подтверждает, что он там остался.

        Поля на вёрстке Magritte — управляемые компоненты React: значение хранится
        в состоянии компонента, а не в DOM. Отсюда два правила.

        Первое: печатать по-настоящему. Подстановка значения через JS меняет DOM и
        проходит нашу же проверку «текст на месте», но React о ней не знает — при
        отправке hh отвечает «не заполнены вопросы работодателя» или молча теряет
        сопроводительное письмо.

        Второе: фокус отделён от печати. Раньше `element.click()` и `send_keys`
        стояли в одном try: если клик не проходил (сверху оверлей модалки), печать
        не выполнялась вовсе, и всё сваливалось на ненадёжный JS-путь.

        «Дожать» JS-значение настоящей клавишей нельзя: React на первом же нажатии
        перерисует поле из своего пустого состояния и сотрёт подставленный текст.
        """
        # Пустой текст вводить бессмысленно, а старая проверка «текст входит в
        # значение» на пустой строке всегда давала True — поле оставалось
        # незаполненным, а бот считал вопрос отвеченным.
        if not str(text or '').strip():
            logging.debug("Пустой текст в поле не вводится")
            return False

        self.focus_text_field(element)

        try:
            element.send_keys(Keys.CONTROL, 'a')
            element.send_keys(Keys.DELETE)
            element.send_keys(text)
            time.sleep(0.3)
        except Exception as e:
            logging.debug(f"Ввод с клавиатуры не сработал: {e}")

        if self.text_value_matches(element, text):
            return True

        # Последняя попытка — JS. Для обычных, не управляемых React полей она
        # работает; для управляемых её результат ниже проверяется ещё раз.
        try:
            self.driver.execute_script(
                """
                const element = arguments[0];
                const value = arguments[1];
                const tag = (element.tagName || '').toUpperCase();

                // Способ выбирается по тегу. Раньше цепочка дескрипторов
                // заканчивалась на HTMLTextAreaElement.prototype, который есть
                // ВСЕГДА, поэтому для редактируемого div вызывался чужой сеттер
                // и падал с ошибкой, а ветка для contenteditable была недостижима.
                if (tag === 'TEXTAREA' || tag === 'INPUT') {
                    const prototype = tag === 'TEXTAREA'
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
                    if (descriptor && descriptor.set) {
                        descriptor.set.call(element, value);
                    } else {
                        element.value = value;
                    }
                } else {
                    element.textContent = value;
                }

                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                element,
                text,
            )
            time.sleep(0.4)
        except Exception as e:
            logging.debug(f"JS-подстановка текста не сработала: {e}")

        return self.text_value_matches(element, text)

    def text_value_matches(self, element, text) -> bool:
        """Совпадает ли значение поля с тем, что мы вводили, целиком.

        Проверка на вхождение подстроки пропускала обрезание: поле с ограничением
        длины сохраняло половину ответа, JS дописывал остальное мимо React, и
        hh отбивал отклик уже на отправке. Сравниваем полностью; пробелы и
        переносы приводим к одному виду, потому что редактируемый div отдаёт их
        схлопнутыми.
        """
        expected = ' '.join(str(text or '').split())
        if not expected:
            return False
        return ' '.join(self.get_text_input_value(element).split()) == expected

    def click_response_submit_button(self, modal):
        # Ссылка на форму могла протухнуть: hh перерисовывает её, пока бот
        # печатает письмо. На мёртвой ссылке find_elements падает, и кнопка
        # «недоступна», хотя она на экране. Берём форму заново.
        modal = self.find_response_modal() or modal
        for selector in RESPONSE_SUBMIT_SELECTORS:
            try:
                submit_buttons = modal.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for submit_button in submit_buttons:
                try:
                    if submit_button.is_displayed() and not self.is_disabled_element(submit_button):
                        if self.click_element_with_mouse(submit_button):
                            return True
                except Exception:
                    continue

        confirm_texts = (
            'все равно откликнуться',
            'всё равно откликнуться',
            'откликнуться',
            'отправить отклик',
            'apply anyway',
            'подтвердить',
            'продолжить',
        )
        try:
            buttons = modal.find_elements(By.CSS_SELECTOR, 'button, a, [role="button"]')
            for btn in buttons:
                try:
                    if btn.is_displayed() and not self.is_disabled_element(btn):
                        txt = btn.text.strip().lower()
                        if any(ct in txt for ct in confirm_texts):
                            if self.click_element_with_mouse(btn):
                                return True
                except Exception:
                    continue
        except Exception:
            pass

        return False

    def open_cover_letter_in_modal(self, modal=None):
        """Раскрывает поле ввода сопроводительного письма в модальном окне, если оно свернуто."""
        # 1. Если поле уже доступно и видимо, ничего не делаем.
        # Именно поэтому в логе бывает «Заполнено» без «Раскрыто»: раскрывать было нечего.
        if self.find_cover_letter_fields():
            logging.info(" Поле сопроводительного письма уже открыто")
            return True

        # 2. Селекторы кнопки "Добавить сопроводительное"
        toggle_selectors = [
            '[data-qa="vacancy-response-letter-toggle"]',
            '[data-qa*="letter-toggle"]',
            'button[data-qa*="letter"]',
            'button[class*="letter"]',
            'a[data-qa*="letter"]',
            '.bloko-modal button',
            'button',
            'a',
            '[role="button"]',
        ]

        # На странице-анкете модалки нет — ищем по всей странице.
        scope = modal if modal is not None else self.driver

        for selector in toggle_selectors:
            try:
                elements = scope.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    if not el.is_displayed():
                        continue
                    text = el.text.strip().lower()
                    if any(t in text for t in ['добавить сопроводительн', 'написать сопроводительн', 'сопроводительн', 'сопроводительное']):
                        self.click_element_with_mouse(el)
                        time.sleep(0.5)
                        if self.find_cover_letter_fields():
                            logging.info(" Раскрыто поле сопроводительного письма")
                            return True
            except Exception:
                continue

        # 3. JavaScript fallback для клика по кнопке добавления сопроводительного
        try:
            clicked = self.driver.execute_script("""
                const root = arguments[0] || document;
                const elements = root.querySelectorAll('button, a, [role="button"], span');
                for (const el of elements) {
                    const txt = (el.innerText || el.textContent || '').toLowerCase();
                    if (txt.includes('сопроводительн')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            """, modal)
            if clicked:
                time.sleep(0.5)
                if self.find_cover_letter_fields():
                    logging.info(" Раскрыто поле сопроводительного письма")
                    return True
        except Exception:
            pass

        return False

    def ensure_target_resume_selected(self, modal):
        """
        Гарантирует, что для отклика выбрано целевое резюме, заданное в конфигурации.
        Если активно нецелевое резюме, раскрывает список и переключает на целевое резюме.
        Если выбрать не удалось и активно заведомо запрещенное резюме — блокирует отправку отклика.
        """
        try:
            from config_manager import get_active_resume
            active_resume_id, active_resume_title = get_active_resume()
        except Exception as e:
            # Чужой ID резюме тут — прямой путь к откликам не от того аккаунта.
            # Пусто лучше, чем чужое: вызывающий код сам решит, что делать.
            logging.warning(f"Не удалось получить активное резюме из конфига: {explain_error(e)}")
            active_resume_id = ""
            active_resume_title = ""

        target_title_lower = active_resume_title.lower()
        if not active_resume_id and not active_resume_title:
            return False, 'Не задано целевое резюме — отправка отменена'

        DISALLOWED_KEYWORDS = ['фотограф', 'видеограф', 'photographer', 'videographer']
        if any(d in target_title_lower for d in DISALLOWED_KEYWORDS):
            DISALLOWED_KEYWORDS = []

        try:
            def matches_target(element):
                resume_id = element.get_attribute('data-resume-id')
                if isinstance(resume_id, str) and resume_id:
                    return resume_id == active_resume_id
                title = re.sub(r'\s*/\s*', '/', ' '.join((element.text or '').lower().split()))
                target = re.sub(r'\s*/\s*', '/', ' '.join(target_title_lower.split()))
                return bool(target and (title == target or title.startswith(target + '/')))

            def current_title(root):
                selectors = (
                    '[data-qa="resume-title"]', '[data-qa*="resume-title"]',
                    '[class*="resume-title"]',
                    '[data-qa*="resume"] [data-qa="cell-text-content"]',
                    '[data-qa="cell"]:has(img) [data-qa="cell-text-content"]',
                )
                for selector in selectors:
                    for element in root.find_elements(By.CSS_SELECTOR, selector):
                        if element.is_displayed() and element.text.strip():
                            return element
                return None

            def read_current_title(_driver):
                nonlocal modal
                try:
                    if modal is None or not modal.is_displayed():
                        modal = self.find_response_modal()
                    return current_title(modal) if modal is not None else False
                except StaleElementReferenceException:
                    modal = self.find_response_modal()
                    return False

            # 1. Считываем заголовок текущего выбранного резюме в модальном окне
            try:
                current_title_elem = WebDriverWait(
                    self.driver, 3, poll_frequency=0.1,
                    ignored_exceptions=(StaleElementReferenceException,),
                ).until(read_current_title)
            except TimeoutException:
                return False, self.application_ui_problem(
                    'resume_not_confirmed',
                    'За 3 с не удалось прочитать выбранное резюме в форме HH. '
                    'Это не означает, что резюме не выбрано: его состояние не подтверждено')

            current_text = current_title_elem.text.strip() if current_title_elem else ""
            current_lower = current_text.lower()

            is_disallowed = any(d in current_lower for d in DISALLOWED_KEYWORDS) if DISALLOWED_KEYWORDS else False
            is_target = current_title_elem is not None and matches_target(current_title_elem)

            # Если текущее резюме уже профильное и не фотограф — всё отлично
            if is_target and not is_disallowed:
                return True, None

            if not current_text:
                return False, 'Не удалось подтвердить выбранное резюме — отправка отменена'

            logging.warning(f" [!] В модалке активно другое резюме: «{current_text}». Переключаю на целевое: «{active_resume_title}»...")

            # 2. Кликаем по переключателю резюме в модалке
            click_target = current_title_elem
            if not click_target:
                try:
                    click_target = modal.find_element(By.CSS_SELECTOR, '[data-qa="cell"], [data-qa="resume-title"]')
                except Exception:
                    pass

            if not click_target or not self.click_element_with_mouse(click_target):
                return False, 'Список резюме не открылся — отправка отменена'
            time.sleep(0.3)

            # 3. Ищем список опций в появившемся bottom sheet / dropdown
            option_selectors = [
                '[data-qa="bottom-sheet-content"] label[data-qa="cell"]',
                '[data-qa="bottom-sheet-content"] [data-qa="resume-title"]',
                '[data-qa="bottom-sheet-content"] [data-qa="cell"]',
                'label[data-qa="cell"]',
                '[data-qa="resume-title"]',
                '[data-qa*="option"]'
            ]
            def find_target(_driver):
                for selector in option_selectors:
                    for option in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        if option.is_displayed() and matches_target(option):
                            return option
                return False

            try:
                target_option = WebDriverWait(self.driver, 2, poll_frequency=0.1,
                                             ignored_exceptions=(StaleElementReferenceException,)).until(find_target)
            except TimeoutException:
                target_option = None

            if target_option and self.click_element_with_mouse(target_option):
                def switched(_driver):
                    title = read_current_title(_driver)
                    return bool(title) and matches_target(title)

                WebDriverWait(self.driver, 3, poll_frequency=0.1,
                              ignored_exceptions=(StaleElementReferenceException,)).until(switched)
                logging.info(f" [OK] Резюме успешно переключено на: {active_resume_title}")
                return True, None

            # Если не удалось переключить и активно запрещенное резюме
            if is_disallowed:
                err = f"Отказ: активно нецелевое резюме «{current_text}», переключение не удалось"
                logging.error(f" [X] {err}")
                return False, err

            return False, f'Целевое резюме не выбрано: сейчас «{current_text}»'
        except Exception as e:
            logging.error(f" [!] Ошибка при проверке/переключении резюме: {explain_error(e)}")
            return False, 'Выбор целевого резюме не подтверждён — отправка отменена'

    def submit_open_response_modal(self, cover_letter, letter_sent):
        modal = self.find_response_modal(wait_seconds=2)
        if not modal:
            return self.apply_via_response_page(cover_letter, letter_sent)

        # hh может временно прятать резюме от откликов («поменяйте видимость
        # резюме»). Проверяем до печати письма: дальше всё равно не отправится.
        try:
            if HIDDEN_RESUME_TEXT in (modal.text or '').lower():
                return False, letter_sent, 0, HIDDEN_RESUME_MESSAGE
        except Exception:
            pass

        # Гарантируем выбор профильного резюме по ИБ
        resume_ok, resume_err = self.ensure_target_resume_selected(modal)
        if not resume_ok:
            return False, letter_sent, 0, resume_err

        modal = self.find_response_modal() or modal
        if not letter_sent:
            self.open_cover_letter_in_modal(modal)
            letter_sent = self.fill_cover_letter(cover_letter, wait_seconds=4)

        # Пока печаталось письмо, hh мог перерисовать форму — старая ссылка
        # тогда мертва, и вопросы в ней «не находятся». Берём форму заново.
        modal = self.find_response_modal() or modal

        questions_answered = self.answer_employer_questions(modal)
        if self.unanswered_questions:
            return False, letter_sent, questions_answered, self.describe_unanswered_questions()

        # В форме с анкетой поле письма перерисовывается, пока догружаются
        # вопросы: первая попытка ловила «stale element», и отклик уходил без
        # письма (СОГАЗ, Солар, 25.09). После ответов форма уже не меняется.
        if not letter_sent and cover_letter:
            modal = self.find_response_modal() or modal
            self.open_cover_letter_in_modal(modal)
            letter_sent = self.fill_cover_letter(cover_letter, wait_seconds=3)

        if self.is_cover_letter_required() and not letter_sent:
            return False, letter_sent, questions_answered, "Сопроводительное письмо не заполнено"

        blocker_message = self.get_response_blocker_message()

        # Заголовок не ошибка, но без найденных полей надо дождаться анкеты.
        question_hint = 'ответьте на вопрос' in (modal.text or '').lower()
        if ((question_hint or (blocker_message and 'вопрос' in blocker_message.lower()))
                and questions_answered == 0 and not self.unanswered_questions):
            logging.info(" В форме есть анкета — ищу вопросы повторно")
            time.sleep(1.5)
            modal = self.find_response_modal() or modal
            questions_answered = self.answer_employer_questions(modal)
            if self.unanswered_questions:
                return False, letter_sent, questions_answered, self.describe_unanswered_questions()

            if questions_answered:
                blocker_message = self.get_response_blocker_message()
            else:
                # Вопросов в модалке нет и не будет: у таких вакансий анкета живёт
                # на отдельной странице отклика. Уходим туда.
                return self.apply_via_response_page(cover_letter, letter_sent)

        # Все найденные вопросы отвечены, а на странице всё равно «ответьте на
        # вопрос». 24.09 так срывались анкеты, где бот заполнил все поля: фраза
        # стоит в форме и до отправки. Отправляем — если что-то не так, hh
        # покажет ошибку, и бот её прочтёт ниже. Где стояла фраза — в журнал.
        if (blocker_message == 'Не заполнены вопросы работодателя'
                and questions_answered > 0 and not self.unanswered_questions
                and not self.get_modal_validation_error(modal)):
            try:
                page = self.get_visible_page_text()
                i = page.find('ответьте на вопрос')
                logging.debug("До отправки на странице «ответьте на вопрос», хотя ответов "
                              f"{questions_answered}: …{page[max(0, i - 200):i + 120]}…")
            except Exception:
                pass
            blocker_message = None

        if blocker_message and blocker_message != "Сопроводительное письмо не заполнено":
            return False, letter_sent, questions_answered, blocker_message

        if self.click_response_submit_button(modal):
            val_error = self.get_modal_validation_error(modal)
            if val_error:
                return False, letter_sent, questions_answered, val_error
            return True, letter_sent, questions_answered, None

        if self.click_lowest_visible_apply_button():
            val_error = self.get_modal_validation_error(modal)
            if val_error:
                return False, letter_sent, questions_answered, val_error
            return True, letter_sent, questions_answered, None

        # Кнопка в окне отклика уехала за экран (длинное письмо или анкета растянули
        # окно, 06.10 ALVILS, SkillStaff: «y=1107 сверху: ничего (за экраном)»), и
        # клик по координатам не попадает. Крутим окно до низа и жмём Enter на самой
        # кнопке; успех — только если окно отклика закрылось.
        if self.submit_popup_by_keyboard():
            return True, letter_sent, questions_answered, None

        blocker_message = self.get_response_blocker_message()
        # Само сообщение ничего не объясняет. Складываем в журнал, что было на
        # экране: без этого повторяющийся отказ нечем разбирать.
        try:
            # Два кандидата в причины: протухшая ссылка на форму и что-то
            # поверх кнопки (всплывашка). Пишем в журнал оба признака.
            try:
                modal.is_displayed()
                modal_state = 'живая'
            except Exception as e:
                modal_state = f'мёртвая ({type(e).__name__})'
            covers = self.driver.execute_script(r"""
                return Array.from(document.querySelectorAll('button, a, [role="button"]'))
                  .filter(b => /^откликнуться$/i.test((b.innerText || '').trim()) && b.offsetParent)
                  .map(b => {
                    const r = b.getBoundingClientRect();
                    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                    const onTop = !top ? 'ничего (за экраном)'
                      : (top === b || b.contains(top)) ? 'сама кнопка'
                      : (top.tagName + ' ' + (top.getAttribute('data-qa') || '') + ' «'
                         + (top.innerText || '').trim().slice(0, 40) + '»');
                    return `${b.getAttribute('data-qa') || '-'} y=${Math.round(r.top)} сверху: ${onTop}`;
                  });
            """)
            logging.debug(f"Отправка не нажалась. Ссылка на форму: {modal_state}. "
                          f"Кнопки «Откликнуться»: {covers}")
        except Exception:
            pass
        return False, letter_sent, questions_answered, blocker_message or "Кнопка отправки отклика недоступна"

    SUBMIT_POPUP = '[data-qa="vacancy-response-submit-popup"]'

    def submit_popup_by_keyboard(self) -> bool:
        """Последняя попытка отправить отклик: прокрутка окна до низа и Enter на кнопке."""
        try:
            buttons = [b for b in self.driver.find_elements(By.CSS_SELECTOR, self.SUBMIT_POPUP)
                       if b.is_displayed()]
            if not buttons or self.is_disabled_element(buttons[0]):
                return False
            button = buttons[0]
            self.driver.execute_script(
                "let el = arguments[0];"
                "while (el) { if (el.scrollHeight > el.clientHeight + 1) el.scrollTop = el.scrollHeight;"
                " el = el.parentElement; }", button)
            time.sleep(0.4)
            button.send_keys(Keys.ENTER)
            deadline = time.time() + 6
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    if not any(b.is_displayed() for b in self.driver.find_elements(By.CSS_SELECTOR, self.SUBMIT_POPUP)):
                        logging.info(" Кнопка отправки нажата клавишей Enter; ожидаю подтверждение HH")
                        return True
                except StaleElementReferenceException:
                    continue
            return False
        except Exception as e:
            logging.debug(f"Отправка клавишей не удалась: {e}")
            return False

    def get_modal_validation_error(self, modal):
        """Проверяет наличие подсвеченных ошибок валидации полей в модальном окне."""
        error_selectors = [
            '[class*="error-message"]',
            '[class*="field-error"]',
            '[data-qa*="error"]',
            '.bloko-form-error'
        ]
        for sel in error_selectors:
            try:
                elems = modal.find_elements(By.CSS_SELECTOR, sel)
                for el in elems:
                    if el.is_displayed():
                        txt = el.text.strip()
                        if txt:
                            return f"Ошибка валидации: {txt}"
            except Exception:
                continue
        return None

    def current_vacancy_id(self):
        """id вакансии из текущего адреса, если он там есть."""
        m = re.search(r'/vacancy/(\d+)', self.driver.current_url or '')
        if m:
            return m.group(1)
        m = re.search(r'vacancyId=(\d+)', self.driver.current_url or '')
        return m.group(1) if m else None

    def apply_via_response_page(self, cover_letter, letter_sent):
        """Отправляет отклик через страницу анкеты и возвращает браузер назад.

        Раньше метод уводил браузер на страницу отклика и оставлял его там: при
        неудаче цикл подтверждения продолжал искать кнопки уже на другой
        странице и мог нажать «Откликнуться» у чужой вакансии.
        """
        origin_url = self.driver.current_url
        result = self.apply_on_response_page(cover_letter, letter_sent)

        if not result[0]:
            try:
                self.driver.get(origin_url)
                time.sleep(1.0)
            except Exception as e:
                logging.debug(f"Возврат на карточку вакансии не удался: {e}")
                return (False, result[1], result[2],
                        result[3] or 'Страница вакансии не открылась обратно — отклик не отправлен')
        return result

    def apply_on_response_page(self, cover_letter, letter_sent):
        """Отправляет отклик через полную страницу отклика, а не через модалку.

        У вакансий с анкетой вопросы работодателя живут ТОЛЬКО на странице
        /applicant/vacancy_response?vacancyId=... В модалке, которая открывается
        с карточки вакансии, их нет: hh просто пишет «не заполнены вопросы
        работодателя» и дальше не пускает. Замер 2026-09-21: на странице отклика
        5 блоков вопросов и 9 полей, в модалке той же вакансии — ноль.

        Возвращает (успех, letter_sent, отвечено_вопросов, сообщение_об_ошибке).
        """
        vacancy_id = self.current_vacancy_id()
        if not vacancy_id:
            # Пустая причина доходила до пользователя как «Статус отклика не
            # изменился» — сообщение, по которому ничего не понять.
            return (False, letter_sent, 0,
                    'Не удалось определить вакансию для страницы анкеты')

        url = f'https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}'
        logging.info(" Открываю полную страницу отклика: там анкета работодателя")
        try:
            self.driver.get(url)
        except Exception as e:
            logging.debug(f"Страница отклика не открылась: {e}")
            return (False, letter_sent, 0,
                    f'Страница анкеты не открылась: {explain_error(e)}')

        # Проверяем, куда нас в итоге привели. Вместо анкеты легко оказаться на
        # входе, на архивной вакансии или в общей выдаче — а дальше бот брал на
        # этой странице ЛЮБУЮ видимую кнопку «Откликнуться», то есть мог
        # откликнуться на чужую вакансию.
        time.sleep(1.0)
        landed_url = self.driver.current_url or ''
        if 'vacancy_response' not in landed_url or vacancy_id not in landed_url:
            logging.debug(f"Страница анкеты подменилась: {landed_url}")
            return (False, letter_sent, 0,
                    'Вместо анкеты открылась другая страница — отклик не отправлен')

        # Фиксированной паузы мало: на свежем браузере вопросы видны через секунду,
        # а на долго живущей сессии страница может отрисовываться заметно дольше.
        # Ждём появления блоков, а не отмеренных секунд.
        found_blocks = 0
        deadline = time.time() + 12.0
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                found_blocks = len(self.collect_question_blocks())
            except Exception:
                found_blocks = 0
            if found_blocks:
                break
        logging.info(f" Вопросов на странице отклика: {found_blocks}")

        if not found_blocks:
            # Сюда приходят только когда hh уже сказал, что анкета не заполнена.
            # Ноль вопросов означает, что мы её не распознали, а не что её нет.
            # Отправлять отклик в этом случае — отправлять пустую анкету.
            return (False, letter_sent, 0,
                    'Анкету работодателя не удалось прочитать — отклик не отправлен')

        questions_answered = self.answer_employer_questions()
        if self.unanswered_questions:
            return False, letter_sent, questions_answered, self.describe_unanswered_questions()

        if not letter_sent:
            self.open_cover_letter_in_modal()
            letter_sent = self.fill_cover_letter(cover_letter, wait_seconds=3)
            if not letter_sent and not self.letter_skip_reason:
                self.letter_skip_reason = 'на странице-анкете поля для письма нет'

        if not self.click_lowest_visible_apply_button():
            return False, letter_sent, questions_answered, 'Кнопка отклика на странице анкеты не нажалась'

        time.sleep(2.5)
        blocker = self.get_response_blocker_message()
        if blocker:
            return False, letter_sent, questions_answered, blocker

        state = self.detect_response_state()
        if state == 'already':
            # «Уже откликались» — не отправка. Возвращать это успехом значило
            # накручивать счётчик откликов на вакансиях, куда ничего не ушло.
            return False, letter_sent, questions_answered, 'Уже откликнулись'
        if state == 'success':
            return True, letter_sent, questions_answered, None

        page_text = self.get_visible_page_text()
        if any(t in page_text for t in RESPONSE_SUCCESS_TEXTS):
            return True, letter_sent, questions_answered, None

        return False, letter_sent, questions_answered, 'Отклик через страницу анкеты не подтвердился'

    ATTACH_LETTER_BUTTON = '[data-qa="responded-success-attach-cover-letter"]'
    ATTACH_LETTER_INPUT = '[data-qa="vacancy-response-popup-form-letter-input"]'
    ATTACH_LETTER_SUBMIT = '[data-qa="vacancy-response-letter-submit"]'

    def attach_letter_after_response(self, cover_letter, vacancy_url):
        """Дописывает письмо к уже отправленному отклику. True — письмо ушло.

        Кнопка «Приложить сопроводительное письмо» открывает окно с полем и
        кнопкой «Отправить». Успех — только когда окно закрылось после отправки.
        """
        def visible(selector):
            for el in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
            return None

        try:
            button = None
            for attempt in range(2):
                deadline = time.time() + 6
                while time.time() < deadline and not button:
                    button = visible(self.ATTACH_LETTER_BUTTON)
                    if not button:
                        time.sleep(0.5)
                if button or attempt:
                    break
                # Отклик ушёл со страницы анкеты или модалка закрылась иначе —
                # кнопка есть на странице вакансии.
                self.driver.get(vacancy_url)
            if not button:
                logging.debug("Кнопки «Приложить сопроводительное письмо» нет")
                return False
            if not self.click_element_with_mouse(button):
                return False
            field = None
            deadline = time.time() + 6
            while time.time() < deadline and not field:
                field = visible(self.ATTACH_LETTER_INPUT)
                if not field:
                    time.sleep(0.5)
            if not field or not self.set_text_input_value(field, cover_letter):
                logging.debug("Поле письма после отклика не заполнилось")
                return False
            submit = visible(self.ATTACH_LETTER_SUBMIT)
            if not submit or not self.click_element_with_mouse(submit):
                return False
            deadline = time.time() + 8
            while time.time() < deadline:
                if not visible(self.ATTACH_LETTER_INPUT):
                    logging.info(" Письмо дописано к отклику")
                    return True
                time.sleep(0.5)
            logging.debug("Окно письма после «Отправить» не закрылось")
            return False
        except Exception as e:
            logging.debug(f"Письмо после отклика не дописалось: {e}")
            return False

    CHAT_BUTTON = '[data-qa="vacancy-response-link-view-topic"]'
    CHAT_FRAME = 'iframe[src*="chatik"]'
    CHAT_INPUT = 'textarea[data-qa="text-input"]'
    CHAT_ADD_LETTER = '[data-qa="chatik-chat-message-applicant-action"]'

    def send_letter_to_chat(self, cover_letter, vacancy_url):
        """Отправляет письмо первым сообщением в чат по отклику. True — ушло.

        У вакансий с анкетой hh часто не даёт ни поля письма в форме, ни кнопки
        «Приложить сопроводительное» после отклика — только «Чат». 29.09 так
        могут уйти без письма отклики, а в чате останется «Без
        сопроводительного письма».

        Чат открывается с этой же страницы вакансии, поэтому это чат именно по
        этому отклику. Ничего не шлём, если письмо уже есть в переписке.
        """
        from selenium.webdriver.common.action_chains import ActionChains

        def first_visible(selector, root=None):
            for el in (root or self.driver).find_elements(By.CSS_SELECTOR, selector):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
            return None

        def wait_for(selector, seconds):
            deadline = time.time() + seconds
            while time.time() < deadline:
                el = first_visible(selector)
                if el:
                    return el
                time.sleep(0.5)
            return None

        in_frame = False
        try:
            # Страницу открываем заново всегда: после отклика с анкетой поверх неё
            # остаётся окно-оверлей, и кнопка «Чат» под ним не кликалась
            # («element click intercepted: modal-overlay», 06.10 Датаджайл, Syberry).
            if vacancy_url:
                self.driver.get(vacancy_url)
            button = wait_for(self.CHAT_BUTTON, 8)
            if not button or not self.click_element_with_mouse(button):
                logging.debug("Кнопки «Чат» у отклика нет")
                return False
            frame = wait_for(self.CHAT_FRAME, 10)
            if not frame:
                logging.debug("Окно чата не открылось")
                return False
            self.driver.switch_to.frame(frame)
            in_frame = True
            field = wait_for(self.CHAT_INPUT, 10)
            if not field:
                logging.debug("Поле сообщения в чате не найдено")
                return False

            def page_text():
                try:
                    return ' '.join((self.driver.find_element(By.TAG_NAME, 'body').text or '').split())
                except Exception:
                    return ''

            lines = [' '.join(line.split()) for line in str(cover_letter).splitlines()]
            lines = [line for line in lines if line]
            if not lines:
                return False
            probe = lines[0][:40]
            if probe and probe in page_text():
                logging.info(" Письмо уже есть в чате — повторно не отправляю")
                return True

            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
            # По полю мышью не кликаем: над ним кнопки-подсказки hh («Добрый день!»),
            # промах отправил бы работодателю эту фразу. Фокус — скриптом.
            self.driver.execute_script("arguments[0].focus();", field)
            time.sleep(0.2)
            # Enter в чате отправляет сообщение, поэтому строки — через Shift+Enter,
            # иначе письмо ушло бы по кускам.
            for i, line in enumerate(lines):
                if i:
                    ActionChains(self.driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
                field.send_keys(line)
            time.sleep(0.5)
            typed = ' '.join((field.get_attribute('value') or '').split())
            if probe not in typed:
                logging.debug("Письмо не набралось в поле чата — ничего не отправлено")
                field.clear()
                return False
            field.send_keys(Keys.ENTER)
            deadline = time.time() + 6
            while time.time() < deadline:
                if not (field.get_attribute('value') or '').strip() and probe in page_text():
                    logging.info(" Письмо отправлено в чат по отклику")
                    return True
                time.sleep(0.5)
            logging.debug("После Enter письмо осталось в поле чата")
            return False
        except Exception as e:
            logging.debug(f"Письмо в чат не отправилось: {e}")
            return False
        finally:
            if in_frame:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass

    def build_success_message(self, letter_sent, questions_answered):
        if letter_sent:
            message = "Отклик отправлен С сопроводительным письмом"
        else:
            reason = self.letter_skip_reason or 'причина не определена'
            message = f"Отклик отправлен БЕЗ письма: {reason}"
        if questions_answered:
            message += f"; отвечено на вопросы работодателя: {questions_answered}"
        return message

    def resolve_response_state(self, state, letter_sent, questions_answered):
        if state == 'denied':
            return False, "Вам отказали"
        if state == 'already':
            return False, "Уже откликнулись"
        if state == 'limit':
            return False, "Лимит откликов"
        if state == 'success':
            return True, self.build_success_message(letter_sent, questions_answered)

        return None

    def confirm_response_submission(self, cover_letter, letter_sent, questions_answered, modal_submitted=False):
        last_state = 'unknown'

        for attempt in range(1, APPLY_CONFIRM_ATTEMPTS + 1):
            if self.handle_warning_popups():
                time.sleep(0.8)

            state = self.wait_for_response_state()
            last_state = state

            resolved_state = self.resolve_response_state(state, letter_sent, questions_answered)
            if resolved_state is not None:
                return resolved_state

            blocker_message = self.get_response_blocker_message()
            if blocker_message:
                return False, blocker_message

            modal = self.find_response_modal()
            if modal and not modal_submitted:
                submitted, letter_sent, answered_now, blocker_message = self.submit_open_response_modal(
                    cover_letter,
                    letter_sent,
                )
                questions_answered += answered_now
                if blocker_message:
                    return False, blocker_message
                if submitted:
                    modal_submitted = True
                    time.sleep(1.5)
                    state_now = self.detect_response_state()
                    res = self.resolve_response_state(state_now, letter_sent, questions_answered)
                    if res is not None:
                        return res
                    continue

            # Если модалка была отправлена и закрылась
            if modal_submitted:
                time.sleep(1.0)
                state = self.wait_for_response_state()
                resolved_state = self.resolve_response_state(state, letter_sent, questions_answered)
                if resolved_state is not None:
                    return resolved_state

                # Проверяем текст страницы на подтверждение отправки
                pt = self.get_visible_page_text()
                # Проверка по тексту ВСЕЙ страницы ловила и «Отклик НЕ отправлен»,
                # и пункт бокового меню «Мои отклики» рядом со словом «отправлен».
                # Отсюда брались отклики, засчитанные без отправки.
                negative = ('отклик не отправлен', 'не удалось отправить',
                            'отклик не был отправлен')
                if not any(n in pt for n in negative):
                    if any(s in pt for s in RESPONSE_SUCCESS_TEXTS) or 'вы откликнулись' in pt:
                        return True, self.build_success_message(letter_sent, questions_answered)

            if state == 'ready' and not modal_submitted and self.click_lowest_visible_apply_button():
                continue

            if attempt < APPLY_CONFIRM_ATTEMPTS:
                time.sleep(1)

        # Внутренний код состояния (ready/unknown) пользователю ничего не
        # говорит — он уходит в журнал, а на экран идёт человеческая фраза.
        logging.debug(f"Состояние отклика после попыток: {last_state}")
        if modal_submitted:
            return False, (f"{APPLY_NOT_CONFIRMED_MARKER} ({last_state}). "
                           "Отправка не подтверждена; не повторяю автоматически. Проверьте отклики на HH.")
        return False, 'Кнопка отправки недоступна — отклик не отправлен'

    def collect_question_field_ids(self):
        """id всех полей, которые принадлежат вопросам работодателя.

        Нужен, чтобы сопроводительное письмо не уехало в анкету: на форме с
        вопросами textarea несколько, и письмо однажды попало в поле «На какую
        зарплату ты ориентируешься?» — работодатель увидел там текст письма.
        """
        ids = set()
        try:
            for block in self.collect_question_blocks():
                try:
                    inputs = block.find_elements(
                        By.CSS_SELECTOR, 'textarea, input, [contenteditable="true"]')
                except Exception:
                    continue
                for el in inputs:
                    try:
                        ids.add(el.id)
                    except Exception:
                        continue
        except Exception as e:
            logging.debug(f"Не удалось собрать поля вопросов: {e}")
        return ids

    def find_cover_letter_fields(self):
        """Поля сопроводительного письма — и только они.

        Слепой фолбэк «любая textarea, если на странице встречается слово
        сопроводительное» отсюда убран: на вакансиях с анкетой он выбирал поле
        вопроса. Сначала пробуем явные селекторы hh, затем поля с подходящей
        подписью рядом, и в самом конце — одиночную textarea, но никогда ту,
        что принадлежит вопросу работодателя.
        """
        explicit_selectors = [
            '[data-qa="vacancy-response-letter-text"]',
            '[data-qa="vacancy-response-popup-form-letter-input"]',
            'textarea[name="letter"]',
            'textarea[placeholder*="Сопровод"]',
            'textarea[placeholder*="сопровод"]',
            'textarea[placeholder*="письмо"]',
        ]

        question_ids = self.collect_question_field_ids()
        fields = []
        seen = set()

        def add(element):
            try:
                element_id = element.id
                if element_id in seen or element_id in question_ids:
                    return False
                if not element.is_displayed():
                    return False
                fields.append(element)
                seen.add(element_id)
                return True
            except Exception:
                return False

        # 1. Явные селекторы hh — им доверяем без дополнительных проверок.
        for selector in explicit_selectors:
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    add(element)
            except Exception:
                continue
        if fields:
            return fields

        # 2. Поля, рядом с которыми написано про сопроводительное письмо.
        for selector in ('textarea', '[contenteditable="true"]'):
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    context_text = self.get_element_context_text(element)
                except Exception:
                    continue
                if ('сопровод' in context_text
                        or 'письм' in context_text
                        or 'letter' in context_text):
                    add(element)
        if fields:
            return fields

        # 3. Последняя попытка: единственная свободная textarea на форме.
        # Если их несколько, угадывать нельзя — письмо может уехать в анкету.
        try:
            free = [el for el in self.driver.find_elements(By.CSS_SELECTOR, 'textarea')
                    if el.id not in question_ids and el.is_displayed()]
        except Exception:
            free = []
        if len(free) == 1 and 'сопровод' in self.get_visible_page_text():
            add(free[0])

        return fields

    def fill_cover_letter(self, cover_letter, wait_seconds=0):
        """Заполняет поле письма. Причину неудачи кладёт в self.letter_skip_reason."""
        cover_letter = clean_public_text(cover_letter, (getattr(self, 'config', None) or {}).get('candidate_profile') or {})
        if not cover_letter.strip():
            self.letter_skip_reason = (
                'текст письма пуст (ИИ не сгенерировал письмо и шаблон cover_letter в конфиге не задан)'
            )
            return False

        deadline = time.time() + wait_seconds
        field_seen = False
        reopened = 0
        while True:
            fields = self.find_cover_letter_fields()
            field_seen = field_seen or bool(fields)
            for field in fields:
                if self.set_text_input_value(field, cover_letter):
                    logging.info(" Сопроводительное письмо заполнено")
                    self.letter_skip_reason = ''
                    return True

            # Поле было и пропало: hh перерисовал форму (обычно сразу после
            # предупреждения «Все равно откликнуться»), и поле свернулось. Без
            # повторного раскрытия цикл до конца срока искал то, чего нет, и
            # отклик уходил без письма. Раскрытие жмёт только элементы с текстом
            # «сопроводительн» — кнопку отправки оно не заденет.
            if not fields and field_seen and reopened < 2:
                reopened += 1
                logging.debug("Поле письма пропало после перерисовки формы — раскрываю заново")
                self.open_cover_letter_in_modal(self.find_response_modal())
                continue

            if time.time() >= deadline:
                self.letter_skip_reason = (
                    'поле письма найдено, но hh не принял текст (ввод не сохранился)'
                    if field_seen else
                    'поле сопроводительного письма не найдено и не раскрылось в форме отклика'
                )
                return False
            time.sleep(0.5)

    def click_lowest_visible_apply_button(self):
        try:
            target = self.driver.execute_script(VISIBLE_APPLY_TARGET_SCRIPT)
        except Exception as e:
            logging.debug(f"Не удалось выбрать видимую кнопку отклика: {e}")
            target = {'found': False}

        # hitsTarget: под координатами кнопки действительно кнопка, а не шапка и
        # не оверлей. Без этой проверки промах возвращался как отправленный отклик.
        if target.get('found') and target.get('hitsTarget'):
            if self.click_viewport_coordinates(target['centerX'], target['centerY']):
                time.sleep(0.8)
                return True

        buttons = self.find_visible_apply_text_buttons()
        if not buttons:
            return False

        def vertical_position(element):
            try:
                return element.location.get('y', 0)
            except Exception:
                return 0

        button = sorted(buttons, key=vertical_position)[-1]
        return self.click_element_with_mouse(button)

    def is_dead_session_message(self, message):
        lower_message = str(message).lower()
        dead_markers = (
            'invalid session id',
            'no such window',
            'target window already closed',
            'web view not found',
            'disconnected: unable to connect',
            'disconnected: unable to send message',
            'chrome not reachable',
            'session deleted',
            'session not created',
            'tab crashed',
            'окно браузера закрыто',
        )
        return any(marker in lower_message for marker in dead_markers)
    
    def check_interactive_controls(self):
        """
        Проверяет интерактивные команды пользователя:
        - Клавиатурные нажатия на Windows:
            [P] / [p] / [З] / [з] -> Пауза / Возобновление работы
            [S] / [s] / [Ы] / [ы] / [Q] / [q] -> Плавная остановка
            [I] / [i] / [Ш] / [ш] -> Вывод текущей статистики
        - Сигнальные файлы (для управления из меню батника или другого терминала):
            pause.flag -> переключение паузы
            stop.flag -> сигнал плавной остановки
        """
        if not hasattr(self, 'is_paused'):
            self.is_paused = False
        if not hasattr(self, 'stop_requested'):
            self.stop_requested = False

        stop_flag = os.path.join(SCRIPT_DIR, 'stop.flag')
        pause_flag = os.path.join(SCRIPT_DIR, 'pause.flag')

        if os.path.exists(stop_flag):
            try:
                os.remove(stop_flag)
            except Exception:
                pass
            print("\n[СТОП] Получена команда остановки из меню!")
            self.stop_requested = True
            return 'stop'

        if os.path.exists(pause_flag):
            try:
                os.remove(pause_flag)
            except Exception:
                pass
            self.is_paused = not self.is_paused
            state = "приостановлен" if self.is_paused else "возобновлен"
            print(f"\n[ИНФО] Бот {state} по команде из меню.\n")

        if sys.platform == 'win32':
            try:
                import msvcrt
                while msvcrt.kbhit():
                    ch = msvcrt.getwch().lower() if hasattr(msvcrt, 'getwch') else msvcrt.getch().decode('utf-8', errors='ignore').lower()
                    if ch in ('p', 'з', 'п'):
                        self.is_paused = not self.is_paused
                        if self.is_paused:
                            print("\n" + "=" * 55)
                            print("[ПАУЗА] Бот приостановлен пользователем.")
                            print(" [P] - продолжить | [S] - остановить | [I] - статус")
                            print("=" * 55 + "\n")
                        else:
                            print("\n[ВОЗОБНОВЛЕНИЕ] Бот продолжает отправку откликов...\n")
                    elif ch in ('s', 'ы', 'q', 'й', 'с'):
                        print("\n[СТОП] Завершаю работу после текущей операции...\n")
                        self.stop_requested = True
                        return 'stop'
                    elif ch in ('i', 'ш', 'и', '?'):
                        self.print_interactive_status()
            except Exception:
                pass

        if self.is_paused and not self.stop_requested:
            while self.is_paused and not self.stop_requested:
                time.sleep(0.3)
                if os.path.exists(stop_flag):
                    try:
                        os.remove(stop_flag)
                    except Exception:
                        pass
                    print("\n[СТОП] Остановка из режима паузы по команде из меню!")
                    self.stop_requested = True
                    return 'stop'
                if os.path.exists(pause_flag):
                    try:
                        os.remove(pause_flag)
                    except Exception:
                        pass
                    self.is_paused = False
                    print("\n[ВОЗОБНОВЛЕНИЕ] Бот продолжает работу по команде из меню...\n")
                    break

                if sys.platform == 'win32':
                    try:
                        import msvcrt
                        if msvcrt.kbhit():
                            ch = msvcrt.getwch().lower() if hasattr(msvcrt, 'getwch') else msvcrt.getch().decode('utf-8', errors='ignore').lower()
                            if ch in ('p', 'з', 'п', 'r', 'к'):
                                self.is_paused = False
                                print("\n[ВОЗОБНОВЛЕНИЕ] Бот продолжает отправку откликов...\n")
                                break
                            elif ch in ('s', 'ы', 'q', 'й', 'с'):
                                self.stop_requested = True
                                print("\n[СТОП] Выход из режима паузы.\n")
                                return 'stop'
                            elif ch in ('i', 'ш', 'и', '?'):
                                self.print_interactive_status()
                    except Exception:
                        pass

        if self.stop_requested:
            return 'stop'
        return 'continue'

    def print_interactive_status(self):
        """Выводит оперативную сводку текущего состояния бота."""
        from config_manager import local_application_limit
        max_app = local_application_limit(self.config)
        print(f"\n{CYAN}{BOLD}{'-' * 50}{RESET}")
        print(f"{CYAN}{BOLD}СТАТУС В РЕАЛЬНОМ ВРЕМЕНИ:{RESET}")
        suffix = f" / {max_app} (локальный предел)" if max_app is not None else " (до сообщения о лимите HH)"
        print(f" Известных откликов за 24ч: {GREEN}{self.applied_today}{RESET}{suffix}")
        print(f" Пропущено: {YELLOW}{self.skipped}{RESET}")
        print(f" Ошибок: {RED}{self.errors}{RESET}")
        state_str = f"{YELLOW}{BOLD}[ПАУЗА]{RESET}" if self.is_paused else f"{GREEN}{BOLD}[РАБОТАЕТ]{RESET}"
        print(f" Состояние: {state_str}")
        print(f"{CYAN}{BOLD}{'-' * 50}{RESET}\n")

    def random_delay(self, delay_range):
        """Случайная задержка с неблокирующей проверкой интерактивных команд."""
        delay = random.uniform(*delay_range)
        deadline = time.time() + delay
        while time.time() < deadline:
            if self.check_interactive_controls() == 'stop':
                break
            time.sleep(min(0.2, max(0.05, deadline - time.time())))
    
    def _cleanup_profile_processes(self, profile_dir):
        """Завершает зависшие процессы Chrome, блокирующие chrome_profile."""
        from terminal_ui import kill_profile_chrome
        kill_profile_chrome(profile_dir)

    def init_driver(self):
        """Инициализация браузера"""
        options = Options()
        if os.environ.get('CHROME_BINARY'):
            options.binary_location = os.environ['CHROME_BINARY']

        if self.debugger_address:
            options.debugger_address = self.debugger_address
            try:
                self.driver = webdriver.Chrome(options=options)
                self.driver.set_page_load_timeout(90)
                self.wait = WebDriverWait(self.driver, 10)
                logging.info(f"[OK] Подключен к Chrome debugger: {self.debugger_address}")
                return True
            except Exception as e:
                logging.error(f"[X] Ошибка подключения к Chrome debugger {self.debugger_address}: {explain_error(e)}")
                return False
        

        # eager: не ждать load-событие сторонних скриптов hh. Без него driver.get() в фоне висел вечно (05.10),
        # а в окне страница выдачи грузилась ~35 с вместо ~7 с (06.10: 12 страниц = 6 минут «тишины»).
        options.page_load_strategy = 'eager'
        if self.headless:
            options.add_argument('--headless=new')
            # Без этого driver.get() в фоне ждёт load-событие вечно: 05.10 страница hh
            # застряла в readyState=interactive и разбор отказов молча завис.
            options.page_load_strategy = 'eager'
            # Без явного размера headless-Chrome стартует в 800x600, hh отдает узкий
            # мобильный лейаут, и селекторы с координатными кликами, отлаженные на
            # видимом режиме [2], начинают промахиваться. Блок геометрии ниже стоит
            # под `if not self.headless` и сюда не доходит.
            options.add_argument('--window-size=1920,1080')

        # Антидетект и подавление служебных логов браузера
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument('--disable-infobars')
        options.add_argument('--disable-extensions')
        options.add_argument('--log-level=3')
        options.add_argument('--silent')
        options.add_argument('--disable-logging')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-background-networking')
        options.add_argument('--disable-sync')
        options.add_argument('--disable-default-apps')
        
        # User-Agent
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        # Профиль для сохранения cookies
        profile_dir = os.path.join(SCRIPT_DIR, 'chrome_profile')
        options.add_argument(f'--user-data-dir={profile_dir}')

        if not self.headless:
            geom_file = os.path.join(SCRIPT_DIR, 'chrome_window_geometry.json')
            if os.path.exists(geom_file):
                try:
                    with open(geom_file, 'r', encoding='utf-8') as f:
                        rect = json.load(f)
                    if rect and 'x' in rect and 'y' in rect and 'width' in rect and 'height' in rect:
                        options.add_argument(f"--window-position={rect['x']},{rect['y']}")
                        options.add_argument(f"--window-size={rect['width']},{rect['height']}")
                except Exception:
                    pass
        
        # Служебный вывод браузера — в browser.log, а не на экран.
        service = None
        try:
            from terminal_ui import chrome_service
            service = chrome_service(SCRIPT_DIR)
        except Exception:
            pass

        try:
            if service:
                self.driver = webdriver.Chrome(service=service, options=options)
                self.driver.set_page_load_timeout(90)
            else:
                self.driver = webdriver.Chrome(options=options)
                self.driver.set_page_load_timeout(90)
        except Exception as e:
            err_s = str(e).lower()
            if any(k in err_s for k in ('instance exited', 'session not created', 'crashed', 'devtoolsactiveport')):
                self._cleanup_profile_processes(profile_dir)
                # Неудачный запуск закрывает browser.log внутри Service, и
                # повтор с тем же Service падал «I/O operation on closed file».
                service = chrome_service(SCRIPT_DIR) if service else None
                try:
                    if service:
                        self.driver = webdriver.Chrome(service=service, options=options)
                        self.driver.set_page_load_timeout(90)
                    else:
                        self.driver = webdriver.Chrome(options=options)
                        self.driver.set_page_load_timeout(90)
                except Exception as e2:
                    logging.error(f"[X] Ошибка запуска браузера после очистки: {e2}")
                    return False
            else:
                logging.error(f"[X] Ошибка запуска браузера: {explain_error(e)}")
                return False

        self.wait = WebDriverWait(self.driver, 10)
        
        # Скрываем webdriver
        try:
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        except Exception:
            pass
        
        self.restore_window_geometry()
        logging.info("[OK] Браузер запущен")
        return True
    
    def check_login(self):
        """Проверяет авторизацию на hh.ru"""
        try:
            self.driver.get('https://hh.ru')
            self.random_delay((2, 3))
            return self.is_logged_in_current_page()
                
        except Exception as e:
            logging.error(f"Ошибка проверки авторизации: {explain_error(e)}")
            return False

    def is_logged_in_current_page(self):
        """Проверяет текущую страницу без принудительной навигации."""
        from terminal_ui import is_hh_logged_in
        if not is_hh_logged_in(self.driver):
            return False

        logging.info("[OK] Авторизация активна")
        # Все селекторы бота — по русским надписям. На другом языке
        # письма не прикладываются, а часть откликов не уходит вовсе.
        try:
            from terminal_ui import ensure_russian_interface
            ensure_russian_interface(self.driver)
        except Exception:
            pass
        return True

    def wait_for_login(self):
        """Ждет ручной вход в браузере без чтения stdin."""
        from terminal_ui import wait_for_hh_login, ensure_russian_interface
        logged_in = wait_for_hh_login(
            self.driver,
            should_stop=lambda: self.stop_requested or self.check_interactive_controls() == 'stop',
        )
        if logged_in:
            ensure_russian_interface(self.driver)
        return logged_in
    
    def login(self):
        """Ручная авторизация"""
        if self.headless:
            print("\n" + "="*60)
            print("НЕТ СОХРАНЁННОЙ СЕССИИ HH.RU")
            print("="*60)
            print("\nФоновый режим не может показать окно входа.")
            print("Запустите пункт [2] (быстрые отклики с окном), войдите в аккаунт —")
            print("вход сохранится, и пункт [3] заработает.")
            print("="*60 + "\n")
            logging.error("Headless-режим: нет сохранённой сессии, авторизация невозможна")
            return False

        print("\n" + "="*60)
        print("ТРЕБУЕТСЯ АВТОРИЗАЦИЯ")
        print("="*60)
        print("\n1. Браузер откроет страницу входа hh.ru")
        print("2. Войдите в свой аккаунт вручную")
        print("3. После входа бот сам продолжит работу")
        print("\n" + "="*60)
        
        self.driver.get('https://hh.ru/account/login?role=applicant')
        return self.wait_for_login()
    
    def get_vacancy_id_from_url(self, url):
        """Извлекает ID вакансии из URL"""
        try:
            # URL вида: https://hh.ru/vacancy/12345678
            if '/vacancy/' in url:
                parts = url.split('/vacancy/')
                if len(parts) > 1:
                    vacancy_id = parts[1].split('?')[0].split('/')[0]
                    return vacancy_id
        except:
            pass
        return None

    def normalize_title(self, title):
        return str(title or '').lower().replace('ё', 'е')

    def find_keyword(self, text, keywords):
        return find_title_keyword(text, keywords)

    def validate_security_title(self, title):
        # Отсев по грейду — первым делом. Браузерный ярус имеет собственную копию
        # логики фильтрации и не получал проверку из test.py, поэтому всё ещё мог
        # откликнуться на позицию уровня правления при трёх годах опыта.
        try:
            from config_manager import check_title_grade
            grade_allowed, grade_reason = check_title_grade(title)
            if not grade_allowed:
                return False, grade_reason
        except Exception as e:
            logging.debug(f"Проверка грейда недоступна: {e}")

        try:
            from config_manager import get_active_preset
            preset = get_active_preset()
        except Exception:
            preset = {'id': 'security'}

        preset_id = preset.get('id', 'security')
        if preset_id == 'security':
            commercial_role = commercial_title_keyword(title)
            if commercial_role:
                return False, f"Продажи/пресейл не входят в направление ИБ: {commercial_role}"
        excluded_keyword = self.find_keyword(
            title,
            title_excludes(self.config),
        )
        if excluded_keyword:
            return False, f"Исключено по ключевому слову: {excluded_keyword}"

        if preset_id == 'security':
            strict_match = self.find_keyword(title, STRICT_TITLE_INCLUDE_KEYWORDS)
            fallback_match = None
            if self.config.get('allow_technical_fallback', True):
                fallback_match = self.find_keyword(title, TECHNICAL_FALLBACK_INCLUDE_KEYWORDS)

            if not strict_match and not fallback_match:
                # Правило по смыслу: ИБ-вакансии называют как угодно, и список
                # слов вечно отстаёт («Инженер защиты от сетевых атак»,
                # «...по сетевой безопасности», опечатка «иформационной»).
                if security_title_by_meaning(title):
                    return True, "OK (безопасность по смыслу названия)"
                return False, "Не security/appsec/pentest/devsecops/soc или technical fallback"

            # Второй проверки по keywords_include из настроек нет: там лежал
            # снимок того же списка, он отставал и отсеивал «Threat Intelligence».
            return True, "OK"

        include_keywords = tuple(self.config.get('keywords_include', [])) or tuple(preset.get('keywords_include', []))
        if include_keywords:
            matched = self.find_keyword(title, include_keywords)
            if matched:
                return True, f"OK ({matched})"
            return False, f"Не содержит ключевых слов пресета {preset.get('name')}"

        return True, "OK"

    def should_skip_known_vacancy(self, vacancy_id, vacancy_name):
        history_entry = self.applied_vacancies.get(str(vacancy_id))
        if not history_entry:
            return False

        if isinstance(history_entry, dict) and history_entry.get('status') == STATUS_SKIPPED_FILTER:
            suitable, _ = self.validate_security_title(vacancy_name)
            return not suitable

        return True

    def get_vacancy_contacts_text(self) -> str:
        """Видимый текст блока контактов на странице вакансии (если он есть)."""
        try:
            parts = [el.text for el in self.driver.find_elements(
                        By.CSS_SELECTOR, '[data-qa*="vacancy-contacts"]') if el.is_displayed()]
            return '\n'.join(p for p in parts if p)
        except Exception:
            return ''

    def maybe_email_employer(self, vacancy_id, vacancy_name):
        """После успешного отклика дублирует письмо на почту работодателя.

        Работает, только если включено в настройках (email_outreach) и задан
        пароль приложения. Сбой почты отклик не отменяет: он уже ушёл.
        """
        try:
            from email_outreach import maybe_send_application_email
        except Exception as e:
            logging.debug(f"Модуль писем на почту недоступен: {e}")
            return
        meta = getattr(self, 'last_application_meta', {}) or {}
        # Модуль сам пишет в журнал успехи и сбои; итог выводим один раз здесь,
        # поэтому его собственный вывод глушим.
        quiet = logging.getLogger('email_outreach.quiet')
        quiet.propagate = False
        if not quiet.handlers:
            quiet.addHandler(logging.NullHandler())
        try:
            status = maybe_send_application_email(
                self.config, vacancy_id, vacancy_name, meta.get('company', ''),
                meta.get('page_text', ''), meta.get('cover_letter', ''), quiet)
        except Exception as e:
            logging.debug(f"Письмо на почту не отправлено: {e}")
            return
        if status:
            logging.info(f" {status}")

    def get_vacancy_page_description(self):
        """Извлекает текст описания вакансии со страницы."""
        selectors = [
            '[data-qa="vacancy-description"]',
            '.vacancy-description',
            '.g-user-content',
            '[class*="vacancy-description"]'
        ]
        for sel in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for elem in elems:
                    if elem.is_displayed():
                        text = elem.text.strip()
                        if len(text) > 50:
                            return text
            except Exception:
                continue
        return ""

    def get_vacancy_page_company(self):
        """Извлекает название компании со страницы вакансии."""
        selectors = [
            '[data-qa="vacancy-company-name"]',
            '.vacancy-company-name',
            '[data-qa="vacancy-view-link-to-company"]',
            'a[href*="/employer/"]'
        ]
        for sel in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for elem in elems:
                    if elem.is_displayed():
                        text = elem.text.strip()
                        if text:
                            return text
            except Exception:
                continue
        return "вашей компании"

    def get_vacancy_page_skills(self):
        """Извлекает теги ключевых навыков со страницы вакансии."""
        skills = []
        selectors = [
            '[data-qa="skills-element"]',
            '[data-qa="bloko-tag__text"]',
            '.bloko-tag__text',
            '[class*="skills-element"]'
        ]
        for sel in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for elem in elems:
                    t = elem.text.strip()
                    if t and t not in skills:
                        skills.append(t)
            except Exception:
                continue
        return skills
    
    def collect_question_blocks(self, container=None):
        """Собирает видимые блоки вопросов работодателя (в модалке или на странице-анкете)."""
        scopes = []
        if container is not None:
            scopes.append(container)
        scopes.append(self.driver)

        for scope in scopes:
            blocks = []
            seen = set()
            selectors = (
                QUESTION_CONTAINER_SELECTORS if scope is self.driver
                else QUESTION_CONTAINER_SELECTORS_LOOSE
            )
            for selector in selectors:
                try:
                    found = scope.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for el in found:
                    try:
                        if el.id in seen or not el.is_displayed():
                            continue
                        # Блок без полей ввода вопросом не является
                        if not el.find_elements(By.CSS_SELECTOR, 'input, textarea, select'):
                            continue
                        seen.add(el.id)
                        blocks.append(el)
                    except Exception:
                        continue
            if blocks:
                return self.drop_wrapper_blocks(blocks)

        return []

    @staticmethod
    def drop_wrapper_blocks(blocks):
        """Убирает блоки-обёртки, внутри которых лежат другие найденные блоки.

        Широкий селектор [class*="question"] матчит и контейнер всей анкеты, и
        каждый вопрос внутри. Обёртка идёт первой в порядке документа, её текст —
        склейка всех вопросов, а её радиокнопки — все переключатели формы сразу.
        Ответив «на неё», бот помечал обработанными все поля анкеты, отправлял
        один вариант из десяти вопросов и молчал об этом.
        """
        if len(blocks) < 2:
            return blocks
        inner = set()
        for outer in blocks:
            try:
                children = outer.find_elements(By.CSS_SELECTOR, '*')
            except Exception:
                continue
            child_ids = {c.id for c in children}
            for other in blocks:
                if other.id != outer.id and other.id in child_ids:
                    inner.add(outer.id)
                    break
        if not inner:
            return blocks
        kept = [b for b in blocks if b.id not in inner]
        logging.debug(f"Отброшено блоков-обёрток: {len(blocks) - len(kept)}")
        return kept or blocks

    # Подпись варианта: поднимаемся от input, пока контейнер содержит только
    # этот вариант. Разметка у hh разная (bloko, Magritte: input в label, а текст
    # в соседнем узле), и прежний поиск по трём XPath на анкетах 25.09 не
    # находил текст — модели уходили id вариантов «395387587 | 395387588»
    # вместо «Москва | Другая локация», и анкета оставалась незаполненной.
    OPTION_LABEL_SCRIPT = """
        const el = arguments[0];
        if (el.id) {
            const byFor = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (byFor && byFor.textContent.trim()) return byFor.textContent;
        }
        const isOption = n => n.nodeType === 1 && (n.matches('input[type=radio],input[type=checkbox]')
            || n.querySelector('input[type=radio],input[type=checkbox]'));
        let box = el;
        for (let p = el.parentElement, k = 0; p && k < 6; p = p.parentElement, k++) {
            if (p.querySelectorAll('input[type=radio],input[type=checkbox]').length > 1) break;
            box = p;
            const text = (p.textContent || '').trim();
            if (text) return text;
        }
        // Текст рядом с контейнером варианта — до следующего варианта.
        let text = '';
        for (let n = box.nextSibling; n && !isOption(n); n = n.nextSibling) text += ' ' + (n.textContent || '');
        return text;
    """

    def get_option_label(self, option_element):
        """Читает подпись варианта ответа (label рядом с radio/checkbox)."""
        try:
            text = option_element.parent.execute_script(self.OPTION_LABEL_SCRIPT, option_element)
            text = ' '.join(str(text or '').split())
            if text:
                return text
        except Exception:
            pass
        for xpath in ('following-sibling::*', '../label', '../../label', '..'):
            try:
                label = option_element.find_element(By.XPATH, xpath)
                text = (label.text or '').strip()
                if text:
                    return text
            except Exception:
                continue
        try:
            return (option_element.get_attribute('value') or '').strip()
        except Exception:
            return ''

    # Отрицание отличает «Готов» от «Не готов» одним словом. Подстрочное
    # сравнение на таких парах даёт ИНВЕРСИЮ ответа: 'готов' входит в 'не готов',
    # и первый же вариант сверху оказывался выбранным. Работодателю уходил ответ,
    # противоположный задуманному.
    NEGATION_PREFIXES = ('не ', 'нет', 'без ', 'не-')

    @staticmethod
    def _is_negation_of(candidate: str, wanted: str) -> bool:
        """Отличается ли вариант от искомого ровно отрицанием."""
        c, w = (candidate or '').strip().lower(), (wanted or '').strip().lower()
        if not c or not w or c == w:
            return False
        for prefix in HHSeleniumBot.NEGATION_PREFIXES:
            if c.startswith(prefix) and c[len(prefix):].strip() == w:
                return True
            if w.startswith(prefix) and w[len(prefix):].strip() == c:
                return True
        return False

    @staticmethod
    def match_option_index(wanted, labels):
        """Индекс варианта, соответствующего искомому тексту, или None.

        Два прохода. Сначала точное совпадение по ВСЕМ вариантам — иначе более
        короткий отрицательный вариант, стоящий первым, перехватывает ответ.
        Подстрока разрешается только для достаточно длинных строк и по границам
        слов: иначе «Go» находится внутри «Google Cloud», а «Да» — внутри
        «Даю согласие на обработку персональных данных».
        """
        w = ' '.join(str(wanted or '').split()).lower().strip()
        if not w:
            return None
        lowered = [' '.join(str(l or '').split()).lower().strip() for l in labels]

        for idx, lbl in enumerate(lowered):
            if lbl and lbl == w:
                return idx

        if len(w) < 3:
            return None

        for idx, lbl in enumerate(lowered):
            if not lbl or HHSeleniumBot._is_negation_of(lbl, w):
                continue
            longer, shorter = (lbl, w) if len(lbl) >= len(w) else (w, lbl)
            if len(shorter) < 4:
                continue
            if re.search(r'(?<!\w)' + re.escape(shorter) + r'(?!\w)', longer):
                return idx
        return None

    def batch_answer_for(self, question_text):
        """Ответ модели на ЭТОТ вопрос из пакетного разбора анкеты.

        Только точное совпадение нормализованного текста. Приблизительный поиск
        подстрокой тянул чужой ответ: короткий блок «Владеете Python? Да Нет»
        является подстрокой блока «Владеете Python? Да Нет Затрудняюсь» — и
        второй вопрос получал ответ первого.
        """
        # Сначала ответ, привязанный к текущему блоку: два блока с одинаковым
        # текстом («Оцените уровень» дважды) схлопывались в общем словаре и
        # получали один ответ на двоих.
        by_block = getattr(self, '_batch_answers_by_block', None) or {}
        block_id = getattr(self, '_current_block_id', None)
        if block_id and block_id in by_block:
            return clean_public_text(by_block[block_id], self.config.get('candidate_profile') or {}) or None

        batch = getattr(self, '_batch_answers', None) or {}
        if not batch:
            return None
        answer = batch.get(' '.join((question_text or '').split()))
        return clean_public_text(answer, self.config.get('candidate_profile') or {}) or None

    def batch_choices_for(self, question_text):
        """Варианты, которые модель назвала для этого вопроса в пакетном разборе.

        Возвращает список в нижнем регистре: при «можно несколько» модель
        перечисляет их через « | ».
        """
        picked = self.batch_answer_for(question_text)
        if not picked:
            return []
        return [p.lower().strip() for p in str(picked).split('|') if p.strip()]

    def choose_option_index(self, question_text, labels):
        """Индекс варианта ответа или None, если уверенного выбора нет.

        Наугад (медиана / первый непустой) не выбираем: случайный ответ работодателю
        хуже отсутствия отклика.
        """
        q_lower = (question_text or '').lower()
        lowered = [(lbl or '').lower().strip() for lbl in labels]
        profile = self.config.get('candidate_profile') or {}
        if is_restricted_question(question_text):
            return None
        if any(clean_public_text(label, profile) != str(label or '').strip() for label in labels):
            return None

        # 1. Явно заданный ответ из конфига question_answers
        for keyword, answer in (self.config.get('question_answers') or {}).items():
            if keyword.lower() in q_lower:
                wanted = str(answer).lower().strip()
                if wanted:
                    for idx, lbl in enumerate(lowered):
                        if wanted in lbl:
                            return idx

        # 2. Ответ из пакетного разбора анкеты: модель вернула точный текст варианта.
        picked = self.batch_answer_for(question_text)
        if True:
            if picked:
                # При «можно несколько» модель перечисляет варианты через |.
                for part in str(picked).split('|'):
                    idx = self.match_option_index(part, labels)
                    if idx is not None:
                        return idx

        # 3. ИИ — только когда LLM реально доступна. Без неё ai_assistant отдаёт
        #    первый непустой вариант, то есть угадывает, а это нам не нужно.
        if getattr(self, 'ai_assistant', None) and getattr(self.ai_assistant, 'enabled', False):
            try:
                idx = self.ai_assistant.answer_question(
                    question_text=question_text,
                    question_type="radio",
                    options=list(labels),
                )
            except Exception as e:
                logging.debug(f"ИИ не выбрал вариант ответа: {e}")
                idx = None
            if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(labels):
                return idx

        # 3. Утвердительный вариант для вопросов «готовы / согласны / подтверждаете».
        #
        # Но не на любую тему: «Готовы работать без оформления по ТК?» — тоже
        # «готовы», и слепое «Да» здесь соглашается за пользователя на условия,
        # которые он не выбирал. На такие вопросы отвечаем только точным ответом
        # из настроек, иначе пропускаем вакансию.
        RISKY_TOPICS = ('без оформления', 'без трудового договора', 'без тк',
                        'серая зарплата', 'в конверте', 'испытательный срок без',
                        'неоплачиваемо', 'бесплатно', 'переработк',
                        'полиграф', 'детектор лжи', 'судимост', 'уголовн')
        if any(topic in q_lower for topic in RISKY_TOPICS):
            return None

        if any(k in q_lower for k in ('готов', 'соглас', 'подтвержд', 'можете', 'рассматрив')):
            for idx, lbl in enumerate(lowered):
                if not lbl or lbl.startswith('не ') or 'не готов' in lbl or 'не соглас' in lbl:
                    continue
                if lbl.startswith('да') or any(w in lbl for w in ('готов', 'соглас', 'подтвержд')):
                    return idx

        return None

    OWN_VARIANT_LABELS = ('свой вариант', 'другое', 'свой ответ', 'иное', 'other')

    def is_own_variant(self, label) -> bool:
        return (label or '').strip().lower().rstrip(':.') in self.OWN_VARIANT_LABELS

    def fill_own_variant(self, block, question_text, default_answers, handled) -> bool:
        """Заполняет поле, появившееся после «Свой вариант». True — заполнено или поля нет."""
        time.sleep(0.6)
        try:
            fields = [f for f in block.find_elements(By.CSS_SELECTOR, 'textarea, input[type="text"], input:not([type])')
                      if f.is_displayed() and not (f.get_attribute('value') or '').strip()]
        except Exception:
            fields = []
        if not fields:
            return True
        answer = self.get_answer_for_question(
            f"{question_text} (свой вариант — опишите коротко по профилю)", default_answers)
        if not answer or not self.set_text_input_value(fields[0], str(answer)):
            return False
        handled.add(fields[0].id)
        logging.info(f" Вопрос: «{' '.join(question_text.split())[:80]}» -> свой вариант: «{str(answer)[:120]}»")
        return True

    def answer_single_question(self, block, question_text, default_answers, handled):
        """Отвечает на один блок вопроса.

        Возвращает (количество_заполненных_полей, текст_нерешённого_вопроса или None).
        """
        answered = 0
        short_q = ' '.join(question_text.split())[:120]

        def fresh(elements, hidden_ok=False):
            out = []
            for el in elements:
                try:
                    if el.id in handled or (not hidden_ok and not el.is_displayed()):
                        continue
                except Exception:
                    continue
                out.append(el)
            return out

        try:
            # У Magritte сами radio/checkbox невидимы — видна их подпись. Фильтр
            # «только видимые» выбрасывал все варианты, блок считался пустым, и
            # анкета уходила с неотмеченными пунктами: hh отвечал «Не заполнены
            # вопросы работодателя» (СОГАЗ, Солар, 25.09).
            radios = fresh(block.find_elements(By.CSS_SELECTOR, 'input[type="radio"]'), hidden_ok=True)
            checkboxes = fresh(block.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]'), hidden_ok=True)
            selects = fresh(block.find_elements(By.CSS_SELECTOR, 'select'))
            text_fields = []
            for field in fresh(block.find_elements(By.CSS_SELECTOR, 'input, textarea')):
                try:
                    field_type = (field.get_attribute('type') or 'text').lower()
                except Exception:
                    continue
                if field_type in ('radio', 'checkbox', 'hidden', 'submit', 'button', 'file', 'image'):
                    continue
                text_fields.append(field)
        except Exception as e:
            logging.debug(f"Не удалось разобрать блок вопроса: {e}")
            return 0, None

        # 1. Выбор одного варианта (radio)
        if radios:
            labels = [self.get_option_label(r) for r in radios]
            idx = self.choose_option_index(question_text, labels)
            if idx is None:
                return answered, f"{short_q} [варианты: {', '.join(l for l in labels if l) or 'без подписей'}]"
            if self.click_choice(radios[idx]):
                answered += 1
                logging.info(f" Вопрос: «{short_q}» -> вариант «{labels[idx] or idx}»")
                # «Свой вариант» открывает поле, которое hh требует заполнить:
                # без него анкета отбивалась «Не заполнены вопросы» (25.09).
                if self.is_own_variant(labels[idx]) and not self.fill_own_variant(
                        block, question_text, default_answers, handled):
                    for r in radios:
                        handled.add(r.id)
                    return answered, f"{short_q} [«Свой вариант» выбран, а поле к нему не заполнилось]"
            else:
                # Вариант выбран, но клик не прошёл. Молчать нельзя: вопрос
                # останется пустым, а hh отобьёт отклик своей валидацией.
                for r in radios:
                    handled.add(r.id)
                return answered, f"{short_q} [вариант «{labels[idx] or idx}» не отметился]"
            for r in radios:
                handled.add(r.id)

        # 2. Выпадающий список
        for select in selects:
            try:
                options = select.find_elements(By.CSS_SELECTOR, 'option')
            except Exception:
                continue
            if len(options) < 2:
                handled.add(select.id)
                continue
            opt_texts = [(o.text or '').strip() for o in options]
            idx = self.choose_option_index(question_text, opt_texts)
            if idx is None:
                return answered, f"{short_q} [список: {', '.join(t for t in opt_texts if t) or 'без подписей'}]"
            try:
                options[idx].click()
                answered += 1
                logging.info(f" Вопрос: «{short_q}» -> «{opt_texts[idx]}»")
            except Exception as e:
                logging.debug(f"Не удалось выбрать опцию селекта: {e}")
                handled.add(select.id)
                return answered, f"{short_q} [вариант «{opt_texts[idx]}» не выбрался]"
            handled.add(select.id)

        # 3. Чекбоксы. Сначала — то, что назвала модель в пакетном разборе анкеты:
        # вопросы вида «отметьте всё, чем владеете» требуют нескольких галочек,
        # а согласиями их не закрыть.
        if checkboxes:
            wanted = self.batch_choices_for(question_text)
            labels_all = [self.get_option_label(cb) for cb in checkboxes]
            chosen = {idx for w in wanted
                      if (idx := self.match_option_index(w, labels_all)) is not None}
            if not chosen:
                idx = self.choose_option_index(question_text, labels_all)
                if idx is not None:
                    chosen.add(idx)
                    wanted = [labels_all[idx]]
            if chosen:
                checked_any = False
                for pos, cb in enumerate(checkboxes):
                    label = labels_all[pos].lower().strip()
                    if not label:
                        continue
                    if pos in chosen:
                        try:
                            # Уже отмеченный флажок — не наш ответ: он мог стоять
                            # по умолчанию. Считаем только то, что отметили сами.
                            if cb.is_selected():
                                checked_any = True
                            elif self.click_choice(cb):
                                answered += 1
                                checked_any = True
                                logging.info(f" Вопрос: «{short_q}» -> отмечено «{labels_all[pos]}»")
                        except Exception as e:
                            logging.debug(f"Не удалось отметить чекбокс: {e}")
                    handled.add(cb.id)
                # Раньше здесь стоял return: блок вида «Отметьте технологии ☑
                # + Другое (укажите) ___» закрывался на галочках, и текстовое
                # поле оставалось пустым — молча, без пометки «без ответа».
                if not checked_any:
                    labels = [self.get_option_label(c) for c in checkboxes]
                    return answered, f"{short_q} [ни один флажок не отметился: {', '.join(l for l in labels if l)}]"
                if any(self.is_own_variant(labels_all[i]) for i in chosen):
                    if not self.fill_own_variant(block, question_text, default_answers, handled):
                        return answered, f"{short_q} [«Свой вариант» отмечен, а поле к нему не заполнилось]"
                    answered += 1
                if not text_fields:
                    return answered, None
                # Есть и текстовое поле — заполняется ниже, в шаге 4. Раньше здесь
                # возвращалось «ни один флажок не отметился», хотя флажки стояли.

            # «да» ищем ОТДЕЛЬНЫМ СЛОВОМ. Как подстрока оно входит в «данные»,
            # «дата», «задача» — и бот сам ставил галочку «Передать мои данные
            # партнёрам». Согласие на обработку персональных данных бот за
            # пользователя не даёт.
            consent_words = ('соглас', 'готов', 'подтвержд', 'ознакомлен', 'agree', 'consent')
            personal_data_markers = ('персональн', 'третьим лицам', 'партнёрам', 'партнерам',
                                     'рассылк', 'рекламн')

            def looks_like_consent(text):
                if any(marker in text for marker in personal_data_markers):
                    return False
                if any(w in text for w in consent_words):
                    return True
                return bool(re.search(r'(?<!\w)да(?!\w)', text))

            checked_any = bool(chosen)
            for cb in (() if chosen else checkboxes):
                label = self.get_option_label(cb).lower()
                if looks_like_consent(label) or (len(checkboxes) == 1 and not label):
                    try:
                        if not cb.is_selected() and self.click_choice(cb):
                            answered += 1
                            checked_any = True
                    except Exception as e:
                        logging.debug(f"Не удалось отметить чекбокс: {e}")
                    handled.add(cb.id)
                elif cb.is_selected():
                    checked_any = True
                    handled.add(cb.id)
            if not checked_any:
                labels = [self.get_option_label(c) for c in checkboxes]
                return answered, f"{short_q} [флажки: {', '.join(l for l in labels if l) or 'без подписей'}]"

        # 4. Свободный текст
        for field in text_fields:
            try:
                if (field.get_attribute('value') or '').strip():
                    handled.add(field.id)
                    continue
            except Exception:
                pass
            answer = self.get_answer_for_question(question_text, default_answers)
            if not answer:
                return answered, short_q
            if self.set_text_input_value(field, str(answer)):
                answered += 1
                logging.info(f" Вопрос: «{short_q}» -> «{str(answer)[:160]}»")
            else:
                # Раньше провал ввода не попадал никуда: вопрос не считался
                # отвеченным, но и в список неотвеченных не уходил. Отклик
                # отправлялся, а hh отбивал его своей валидацией.
                return answered, f"{short_q} [ответ не удалось ввести в поле]"
            handled.add(field.id)

        return answered, None

    def describe_unanswered_questions(self):
        """Понятная причина пропуска вакансии из-за вопросов работодателя."""
        if not self.unanswered_questions:
            return QUESTIONS_SKIP_MARKER
        extra = f" (и ещё {len(self.unanswered_questions) - 1})" if len(self.unanswered_questions) > 1 else ''
        return f"{QUESTIONS_SKIP_MARKER}: «{self.unanswered_questions[0]}»{extra}"

    def answer_employer_questions(self, container=None):
        """Отвечает на вопросы работодателя (тест/анкета при отклике).

        Вопросы, на которые не нашлось осмысленного ответа, складываются в
        self.unanswered_questions — вызывающий код по ним пропускает вакансию.
        """
        self.unanswered_questions = []
        questions_answered = 0
        handled = set()
        default_answers = self.config.get('question_answers') or {}

        # Модалка отклика дорисовывает вопросы асинхронно: один прогон успевал их
        # увидеть, следующий на той же вакансии — уже нет, и отклик отбивался hh
        # с «не заполнены вопросы работодателя». Поэтому пустой результат
        # перепроверяем, а не принимаем за «вопросов нет».
        blocks = self.collect_question_blocks(container)
        if not blocks:
            # Одна короткая перепроверка вместо трёх по секунде. Сюда доходим,
            # когда письмо уже написано и напечатано, форма открыта 5-6 с —
            # вопросы, если они есть, уже отрисованы. На поздний случай есть
            # страховка: hh при отправке пишет «ответьте на вопросы», и бот ищет
            # их повторно. Раньше здесь уходило 3 с на каждом отклике без анкеты.
            time.sleep(0.5)
            blocks = self.collect_question_blocks(container)
            if blocks:
                logging.debug('Вопросы появились в форме не сразу')

        # Анкеты бывают на 10-50 вопросов. Спрашивать модель по одному — значит
        # выжечь суточную квоту на первой же такой вакансии, после чего все
        # оставшиеся вопросы получат один дежурный ответ. Поэтому сначала
        # спрашиваем обо всём сразу, одним запросом.
        self.prefetch_batch_answers(blocks, default_answers)

        # Блок, который не прочитался или упал при обработке, раньше выпадал и из
        # отвеченных, и из неотвеченных: отклик уходил с пустым обязательным
        # вопросом, а hh отбивал его молча. Теперь каждый такой блок виден.
        for number, block in enumerate(blocks, 1):
            try:
                question_text = (block.text or '').strip()
                self._current_block_id = getattr(block, 'id', None)
            except Exception as e:
                logging.debug(f"Блок вопроса не прочитался: {e}")
                self.unanswered_questions.append(f'вопрос {number} [не удалось прочитать]')
                continue
            if not question_text:
                self.unanswered_questions.append(f'вопрос {number} [без текста вопроса]')
                continue
            try:
                answered, unresolved = self.answer_single_question(
                    block, question_text, default_answers, handled
                )
            except Exception as e:
                logging.debug(f"Ошибка обработки вопроса: {e}")
                self.unanswered_questions.append(
                    ' '.join(question_text.split())[:120] + ' [обработать не удалось]')
                continue
            finally:
                self._current_block_id = None
            questions_answered += answered
            if unresolved:
                self.unanswered_questions.append(unresolved)

        # Отдельные поля ответа вне распознанных блоков
        try:
            inputs = self.driver.find_elements(
                By.CSS_SELECTOR,
                'input[placeholder*="ответ"], textarea[placeholder*="ответ"], '
                'input[data-qa*="question"], textarea[data-qa*="question"]'
            )
            for inp in inputs:
                if inp.id in handled or not inp.is_displayed():
                    continue
                if (inp.get_attribute('value') or '').strip():
                    continue
                placeholder = (inp.get_attribute('placeholder') or '').strip()
                handled.add(inp.id)
                if not placeholder:
                    # Поле без подписи опознать нельзя. Если оно обязательное,
                    # hh покажет ошибку валидации и отклик отвалится по ней.
                    continue
                answer = self.get_answer_for_question(placeholder, default_answers)
                if not answer:
                    self.unanswered_questions.append(' '.join(placeholder.split())[:120])
                    continue
                if self.set_text_input_value(inp, str(answer)):
                    questions_answered += 1
                else:
                    # Тот же молчаливый провал, что уже закрыт в answer_single_question:
                    # ответ не встал в поле, но вопрос нигде не отмечался — отклик
                    # уходил с пустым обязательным полем, а hh отбивал его.
                    self.unanswered_questions.append(
                        ' '.join(placeholder.split())[:120] + ' [ответ не удалось ввести]')
        except Exception as e:
            logging.debug(f"Ошибка при ответе на вопросы: {e}")

        if self.unanswered_questions:
            logging.warning(f" [?] {self.describe_unanswered_questions()}")

        return questions_answered

    def prefetch_batch_answers(self, blocks, default_answers):
        """Заранее спрашивает модель обо всех вопросах анкеты одним запросом.

        В кеш кладутся только те вопросы, на которые нет точного ответа в конфиге:
        факты (телеграм, зарплата, опыт) модели доверять незачем.
        """
        self._batch_answers = {}
        self._batch_answers_by_block = {}
        self._current_block_id = None
        ai = getattr(self, 'ai_assistant', None)
        if not ai or not getattr(ai, 'enabled', False):
            return
        if not hasattr(ai, 'answer_questions_batch'):
            return

        pending = []
        # Ключ, отправленный модели -> (исходный текст вопроса, id блока).
        # Нужен, чтобы вернуть ответ ИМЕННО тому блоку, у которого спросили.
        origin_by_key = {}
        seen_texts = {}
        for block in blocks:
            try:
                text = ' '.join((block.text or '').split())
                block_id = getattr(block, 'id', None)
            except Exception:
                continue
            if not text:
                continue
            # Варианты ответа отдаём модели вместе с вопросом: иначе на
            # «отметьте нужное» она вернёт свободный текст, который некуда деть.
            options, multi = [], False
            try:
                radios = block.find_elements(By.CSS_SELECTOR, 'input[type="radio"]')
                boxes = block.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]')
                selects = block.find_elements(By.CSS_SELECTOR, 'select')
                if radios:
                    options = [self.get_option_label(r) for r in radios]
                elif boxes:
                    options = [self.get_option_label(c) for c in boxes]
                    multi = len(boxes) > 1
                elif selects:
                    options = [(o.text or '').strip()
                               for o in selects[0].find_elements(By.CSS_SELECTOR, 'option')]
                options = [o for o in options if o]
            except Exception:
                options, multi = [], False

            # Ответ из конфига подходит только свободному полю: для переключателей
            # и списков он почти никогда не совпадает с подписью варианта, и
            # вопрос, выброшенный из пакета, оставался вообще без ответа.
            q_lower = text.lower()
            if (not options and is_short_question(text) and not self.yes_policy_question(text)
                    and any(k.lower() in q_lower for k in (default_answers or {}))):
                continue

            # Одинаковый текст у двух блоков модель видит как один вопрос, а её
            # ответы приходят словарём — второй затирал первого. Помечаем повтор
            # номером, чтобы у каждого блока был свой ответ.
            repeat = seen_texts.get(text, 0)
            seen_texts[text] = repeat + 1
            key = text if not repeat else f'{text} (вопрос {repeat + 1})'
            origin_by_key[key] = (text, block_id)
            pending.append({'text': key, 'options': options, 'multi': multi})

        # Один свободный вопрос ИИ разберёт и без пакета. Вопрос с вариантами —
        # нет: для флажков иного пути к ИИ нет, и «Какой формат работы вы
        # рассматриваете? ☐Офис ☐Гибрид ☐Удалённо» оставался без ответа (вариант с несколькими флажками).
        if not pending or (len(pending) == 1 and not pending[0]['options']):
            return

        logging.info(f" Вопросов в анкете: {len(blocks)}; спрашиваю ИИ обо всех одним запросом")
        try:
            answers = ai.answer_questions_batch(pending) or {}
        except Exception as e:
            logging.debug(f"Пакетный разбор анкеты не удался: {e}")
            return

        for key, answer in answers.items():
            text, block_id = origin_by_key.get(key, (key, None))
            self._batch_answers.setdefault(text, answer)
            if block_id:
                self._batch_answers_by_block[block_id] = answer

    YES_QUESTION = re.compile(r'(подходит|устраивает|готовы|готов ли|рассматриваете ли|согласны)', re.I)

    def yes_policy_question(self, text) -> bool:
        """Вопрос «подходит ли / готовы ли» при включённой политике «да».

        Готовый ответ «Рассматриваю удалённую работу» на «Наш формат 4/1 в
        пользу офиса. Подходит?» звучал как отказ (25.09). Такие вопросы отдаём ИИ.
        """
        policy = (self.config or {}).get('answer_policy') or {}
        return bool(policy.get('yes_to_conditions', True) and self.YES_QUESTION.search(text or ''))

    def get_answer_for_question(self, question_text, custom_answers=None):
        """Подбирает ответ на вопрос. None — осмысленного ответа нет, вакансию лучше пропустить."""
        custom_answers = custom_answers or self.config.get('question_answers') or {}
        q_clean = (question_text or '').strip()
        q_lower = q_clean.lower()

        if not q_clean:
            return None

        profile = self.config.get('candidate_profile') or {}
        if is_restricted_question(q_clean):
            return SALARY_ANSWER

        # 1. Готовые ответы из конфига — только на короткие вопросы. Ключ ищется
        # подстрокой, и на длинный вопрос «Опишите случай подключения источника
        # логов к SIEM…» бот отвечал «Рассматриваю удалённую работу» (25.09).
        if is_short_question(q_clean) and not self.yes_policy_question(q_clean):
            for keyword, answer in custom_answers.items():
                if keyword.lower() in q_lower:
                    return clean_public_text(answer, profile) or None

        # 2. Ответ из пакетного разбора анкеты (один запрос на всю форму)
        cached = self.batch_answer_for(q_clean)
        if cached and not any(m in cached.lower() for m in GENERIC_ANSWER_MARKERS):
            return clean_public_text(cached, profile) or None

        # 3. ИИ-ассистент на основе профиля кандидата
        if hasattr(self, 'ai_assistant') and self.ai_assistant:
            try:
                ans = self.ai_assistant.answer_question(q_clean, question_type="text")
            except Exception as e:
                logging.debug(f"ИИ не ответил на вопрос: {e}")
                ans = None
            ans = str(ans).strip() if ans else ''
            # Дежурная отписка ai_assistant = ответа нет
            if ans and not any(m in ans.lower() for m in GENERIC_ANSWER_MARKERS):
                return clean_public_text(ans, profile) or None

        # 3. Нейтральный ответ, если он задан в конфиге.
        #
        # Решение пользователя 2026-09-21: анкеты встречаются примерно у 13%
        # вакансий по ИБ, и пропускать их все — терять заметную часть откликов.
        # Нейтральная формулировка честна: она не утверждает ничего, чего мы не
        # знаем, и переносит вопрос на собеседование.
        #
        # Чтобы вернуть прежнее поведение (пропускать вакансию), достаточно убрать
        # neutral_answer из hh_selenium_config.json.
        neutral = str(self.config.get('neutral_answer') or '').strip()
        if neutral:
            return clean_public_text(neutral, profile) or None

        # 4. Ответа нет — вакансия пропускается. Выдумывать работодателю нельзя.
        return None


    def is_vacancy_suitable(self, vacancy_element):
        """Проверяет подходит ли вакансия по критериям"""
        try:
            # Получаем название вакансии
            title_elem = vacancy_element.find_element(By.CSS_SELECTOR, '[data-qa="serp-item__title"]')
            return self.validate_security_title(title_elem.text)
            
        except Exception as e:
            return False, f"Ошибка проверки: {e}"

    def is_api_vacancy_suitable(self, vacancy):
        """Проверяет API-вакансию по строгому title-фильтру."""
        return self.validate_security_title(vacancy.get('name', ''))

    def maybe_bump_resume(self):
        """Поднимает резюме, если по расписанию пора. Иначе — ничего не делает."""
        try:
            from resume_updater import HHResumeUpdater, bump_is_due
        except Exception as e:
            logging.debug(f"Поднятие недоступно: {e}")
            return
        if not bump_is_due():
            return
        resume_id = str(self.config.get('resume_id') or '').strip()
        if not resume_id:
            return
        logging.info(" Подошло время бесплатного поднятия резюме — поднимаю")
        try:
            ok, msg = HHResumeUpdater(resume_id=resume_id, driver=self.driver).bump_resume()
            if not ok:
                logging.info(f" Резюме не поднято: {msg}")
        except Exception as e:
            logging.debug(f"Поднятие не удалось: {e}")

    RESUME_SEARCH_MAX_PAGES = 20          # у hh выдача режется на 2000 = 20 страниц по 100
    RESUME_SEARCH_REFRESH_SECONDS = 6 * 3600

    def collect_resume_recommendations(self):
        """Вакансии со страницы «подходящие вакансии для резюме» (веб-поиск hh).

        API отдаёт не всё. Карточки «Вы откликнулись» (vacancy_responded)
        отбрасываются сразу — страницу вакансии ради этого открывать незачем.
        Возвращает вакансии в том же виде, что и список из API.
        """
        resume_id = str(self.config.get('resume_id') or '').strip()
        if not resume_id:
            return []
        found, seen, responded = [], set(), 0
        for page in range(self.RESUME_SEARCH_MAX_PAGES):
            if self.check_interactive_controls() == 'stop' or self.stop_requested:
                break
            url = (f"https://hh.ru/search/vacancy?resume={resume_id}&from=resumelist"
                   f"&items_on_page=100&page={page}")
            try:
                self.driver.get(url)
                cards = []
                for _ in range(20):
                    time.sleep(0.5)
                    cards = self.driver.execute_script(r"""
                        return Array.from(document.querySelectorAll('[data-qa="vacancy-serp__vacancy"]')).map(c => {
                          const t = c.querySelector('[data-qa="serp-item__title"]');
                          const e = c.querySelector('[data-qa="vacancy-serp__vacancy-employer"]');
                          const id = ((t && t.getAttribute('href')) || '').match(/vacancy\/(\d+)/);
                          const eid = ((e && e.getAttribute('href')) || '').match(/employer\/(\d+)/);
                          return {id: id && id[1], name: t ? t.innerText.trim() : '',
                                  employer: e ? e.innerText.trim() : '', employer_id: eid && eid[1],
                                  responded: !!c.querySelector('[data-qa="vacancy-serp__vacancy_responded"]')};
                        });
                    """) or []
                    if cards:
                        break
                has_next = bool(self.driver.find_elements(By.CSS_SELECTOR, '[data-qa="pager-next"]'))
            except Exception as e:
                logging.debug(f"Страница {page + 1} вакансий для резюме не открылась: {e}")
                break
            if not cards:
                break
            new_here = 0
            for c in cards:
                if not c.get('id') or c['id'] in seen:
                    continue
                seen.add(c['id'])
                if c.get('responded'):
                    responded += 1
                    continue
                found.append({
                    'id': c['id'],
                    'name': c.get('name') or 'Без названия',
                    'employer': {'id': c.get('employer_id'), 'name': c.get('employer') or ''},
                    'alternate_url': f"https://hh.ru/vacancy/{c['id']}",
                    'has_test': False,
                    'source': 'resume_search',
                })
                new_here += 1
            logging.info(f" Вакансии для резюме: страница {page + 1}, неоткликнутых {new_here}")
            if not has_next:
                break
        logging.info(f"Вакансии для резюме: всего {len(seen)}, из них вы уже откликались на "
                     f"{responded}, остальные {len(found)}")
        return found

    def merge_resume_recommendations(self):
        """Кладёт новые вакансии для резюме в начало сохранённого списка.

        В тот же файл, что и список из API: тогда пропуск, отметка «обработана»
        и удаление из очереди работают как для остальных. Сбор — не чаще раза в
        6 часов: 16-20 страниц по 100 карточек занимают пару минут.
        """
        path = self.api_cache_file
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            cache = {'vacancies': []}
        if time.time() - float(cache.get('resume_search_at', 0) or 0) < self.RESUME_SEARCH_REFRESH_SECONDS:
            return
        logging.info("Собираю подходящие вакансии для резюме в браузере (API отдаёт не все)...")
        recs = self.collect_resume_recommendations()
        if self.stop_requested:
            return
        known = {str(v.get('id')) for v in cache.get('vacancies', []) if isinstance(v, dict)}
        # Отсеянные фильтром не исключаем: фильтр меняется (23.09 он не знал
        # «ИБ»), а такие вакансии задуманы перепроверяемыми.
        known |= {str(k) for k, v in (self.applied_vacancies or {}).items()
                  if not (isinstance(v, dict) and v.get('status') == STATUS_SKIPPED_FILTER)}
        fresh = [v for v in recs if v['id'] not in known]
        cache['vacancies'] = fresh + list(cache.get('vacancies', []))
        cache['total_count'] = len(cache['vacancies'])
        cache['resume_search_at'] = time.time()
        write_json_atomic(path, cache)
        logging.info(f"Новых вакансий для резюме в очереди: {len(fresh)} — они идут первыми")

    def load_api_vacancies(self):
        """Загружает вакансии, найденные API-ботом."""
        cache_file = resolve_workspace_path(self.config.get('api_cache_file', DEFAULT_API_CACHE_FILE))

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
        except FileNotFoundError:
            logging.error("Сохранённый список вакансий не найден")
            return []
        except json.JSONDecodeError as e:
            logging.error(f"Сохранённый список вакансий повреждён: {explain_error(e)}")
            return []

        vacancies = cache_data.get('vacancies', [])
        if not isinstance(vacancies, list):
            logging.error("В сохранённом списке нет вакансий")
            return []

        logging.info(f"Вакансий в списке: {len(vacancies)}")
        # Один раз за прогон: чьи вакансии не открываем вообще.
        try:
            self.blocked_employers = self.load_blocked_employers()
        except Exception as e:
            logging.debug(f'Список нежелательных не загружен: {e}')
            self.blocked_employers = (set(), set())
        # Один раз называем, чем будут писаться письма.
        try:
            if getattr(self, "ai_assistant", None):
                report = self.ai_assistant.probe_report()
                if report:
                    logging.info(report)
                logging.info(f"Письма пишет: {self.ai_assistant.active_model_label()}")
        except Exception:
            pass
        return vacancies

    def load_blocked_employers(self) -> tuple:
        """Работодатели, которым не отправляем отклики.

        Список свой, из настроек (`blocked_employers`): номера работодателей
        и/или куски названия. Своего списка скрытых hh наружу не отдаёт, а
        угадывать его страницу — значит отсеивать по случайным номерам.

        Возвращает (номера, куски названия в нижнем регистре).
        """
        raw = self.config.get('blocked_employers') or []
        ids, names = set(), set()
        for item in raw:
            text = str(item).strip()
            if not text:
                continue
            if text.isdigit():
                ids.add(text)
            else:
                names.add(text.lower())
        if ids or names:
            logging.info(
                f"Не откликаемся на работодателей из вашего списка: "
                f"{len(ids) + len(names)}")
        return ids, names

    def employer_is_blocked(self, employer_id: str, employer_name: str) -> bool:
        """Работодатель в вашем списке — по номеру или по названию."""
        ids, names = getattr(self, 'blocked_employers', (set(), set()))
        if employer_id and str(employer_id) in ids:
            return True
        low = (employer_name or '').lower()
        return bool(low) and any(part in low for part in names)

    def process_api_vacancies(self, vacancies):
        """Откликается через браузер на вакансии из API-кеша."""
        vacancies_processed = 0
        for index, vacancy in enumerate(vacancies, 1):
            if self.check_interactive_controls() == 'stop' or self.stop_requested:
                logging.info("[СТОП] Остановка по запросу пользователя")
                break

            if self.local_application_limit_reached():
                logging.info("[СТОП] Достигнут пользовательский локальный предел откликов")
                break

            vacancy_id = str(vacancy.get('id') or '')
            vacancy_name = vacancy.get('name', 'Без названия')
            employer = vacancy.get('employer', {}).get('name', 'Неизвестно')
            vacancy_url = get_api_vacancy_url(vacancy)

            # Пустая строка печатается отдельно: перевод строки внутри
            # сообщения оставлял пустой «INFO -», а название вакансии
            # уезжало следующей строкой без префикса.
            print()
            logging.info(f"[{index}/{len(vacancies)}] {vacancy_name}")
            logging.info(f" {employer}")

            if not vacancy_id:
                logging.info(" [ПРОПУСК] Пропуск: нет ID вакансии")
                self.skipped += 1
                continue

            employer_id = str(vacancy.get('employer', {}).get('id') or '')
            if self.employer_is_blocked(employer_id, employer):
                logging.info(" [ПРОПУСК] Этот работодатель в вашем списке нежелательных")
                self.skipped += 1
                self.skip_cached_vacancy(vacancy_id)
                self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_FILTER)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(
                        vacancy_id, vacancy_name, employer, vacancy_url,
                        reason='excluded_filter',
                        details='работодатель в списке нежелательных')
                continue

            known = self.applied_vacancies.get(str(vacancy_id))
            pending = isinstance(known, dict) and known.get('status') == STATUS_PENDING_CONFIRMATION
            if (self.config.get('skip_applied', True) or pending) and self.should_skip_known_vacancy(vacancy_id, vacancy_name):
                # Отсеянная фильтром — не «уже откликались»: так лог врал про
                # «Младшего специалиста», на которого отклика не было.
                if pending:
                    logging.info(' [ПРОПУСК] Прошлая отправка не подтверждена; проверьте отклики на HH. Не повторяю.')
                elif isinstance(known, dict) and known.get('status') == STATUS_SKIPPED_FILTER:
                    _, why = self.validate_security_title(vacancy_name)
                    logging.info(f" [ПРОПУСК] Отсеяна фильтром по названию: {why}")
                else:
                    logging.info(" [ПРОПУСК] Уже откликались")
                self.skipped += 1
                self.skip_cached_vacancy(vacancy_id)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, vacancy_url, reason='already_applied')
                continue

            if not vacancy_url:
                logging.info(" [ПРОПУСК] Пропуск: нет безопасной HH-ссылки")
                self.skipped += 1
                self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_NO_SAFE_URL)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, '', reason='skipped_no_safe_url')
                continue

            if self.config.get('skip_with_tests', True) and vacancy.get('has_test', False):
                logging.info(" [ПРОПУСК] Пропуск: есть тест")
                self.skipped += 1
                self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_TEST)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, vacancy_url, reason='skipped_test', details='has_test=True')
                continue

            suitable, reason = self.is_api_vacancy_suitable(vacancy)
            if not suitable:
                logging.info(f" [ПРОПУСК] Пропуск: {reason}")
                self.skipped += 1
                self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_FILTER)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, vacancy_url, reason='excluded_filter', details=str(reason))
                continue

            success, message = self.apply_to_vacancy(vacancy_url, vacancy_name)
            # Капчу решает человек; после решения повторяем отклик ровно один
            # раз, и его итог идёт по той же цепочке ниже.
            if not success and message == NETWORK_MESSAGE and self.wait_for_network_back():
                logging.info(" Повторяю отклик на эту вакансию")
                success, message = self.apply_to_vacancy(vacancy_url, vacancy_name)
            if not success and message == "Требуется капча" and self.wait_for_human_captcha():
                logging.info(" [OK] Капча решена — повторяю отклик")
                success, message = self.apply_to_vacancy(vacancy_url, vacancy_name)

            if success:
                logging.info(f" [OK] {message}")
                self.applied_today += 1
                vacancies_processed += 1
                self.note_apply_success()
                self.save_applied(vacancy_id, vacancy_name, STATUS_SENT)
                self.maybe_email_employer(vacancy_id, vacancy_name)
            elif message == NETWORK_MESSAGE:
                # Не сохраняем: вакансия вернётся в очередь.
                logging.info(" [ПРОПУСК] Нет связи — вакансия останется в очереди")
                self.skipped += 1
            elif message == "Требуется капча":
                # Не сохраняем: вакансия вернётся в очередь в следующий прогон.
                logging.info(" [ПРОПУСК] Капча не решена — вакансия останется в очереди")
                self.skipped += 1
            elif message == HIDDEN_RESUME_MESSAGE:
                # Не помечаем вакансию: она вернётся в очередь. Серия означает,
                # что hh прячет резюме на несколько минут — пережидаем, а не
                # прожигаем список по 45 секунд на вакансию.
                logging.info(" [~] hh временно скрыл резюме от откликов — вакансия останется в очереди")
                self.skipped += 1
                self.hidden_resume_streak = getattr(self, "hidden_resume_streak", 0) + 1
                if self.hidden_resume_streak >= 2:
                    self.wait_out_hidden_resume()
            elif message == "Вакансия в архиве":
                logging.info(" [ПРОПУСК] Вакансия в архиве")
                self.skipped += 1
                if vacancy_id:
                    self.save_applied(vacancy_id, vacancy_name, STATUS_ARCHIVED)
            elif str(message).startswith(("Токсичный маркер", "AI-фильтр:")):
                # Уже помечено и выведено как [ПРОПУСК] внутри apply_to_vacancy.
                pass
            elif message == "Уже откликнулись":
                logging.info(" [ПРОПУСК] Уже откликнулись")
                self.skipped += 1
                self.save_applied(vacancy_id, vacancy_name, STATUS_ALREADY_APPLIED)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, vacancy_url, reason='already_applied')
            elif message == "Вам отказали":
                logging.info(" [ПРОПУСК] Вам отказали")
                self.skipped += 1
                self.save_applied(vacancy_id, vacancy_name, STATUS_DENIED)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, vacancy_url, reason='denied')
            elif message == "Лимит откликов":
                logging.info(" [СТОП] HH показал лимит откликов. "
                             f"В локальной истории за 24 часа: {self.applied_today}. "
                             "Счётчик HH может учитывать отклики вне бота; новые отправки прекращены.")
                self.response_limit_reached = True
                break
            elif str(message).startswith(QUESTIONS_SKIP_MARKER):
                # Это пропуск, а не ошибка: серию неудач не наращиваем
                logging.info(f" [ПРОПУСК] {message}")
                self.skipped += 1
                self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_TEST)
                if hasattr(self, 'db') and self.db:
                    self.db.record_skipped_vacancy(vacancy_id, vacancy_name, employer, vacancy_url, reason='skipped_questions', details=str(message))
            else:
                if self.is_dead_session_message(message) or message == "Окно браузера закрыто пользователем":
                    logging.info(" [!] Окно браузера закрыто пользователем. Работа завершена.")
                    self.stop_requested = True
                    break
                logging.info(f" [X] {message}")
                self.errors += 1
                if APPLY_NOT_CONFIRMED_MARKER in str(message):
                    self.save_applied(vacancy_id, vacancy_name, STATUS_PENDING_CONFIRMATION)
                if self.register_apply_failure(message):
                    break

            self.random_delay(self.apply_delay())

        return vacancies_processed

    def apply_to_vacancy(self, vacancy_url, vacancy_name):
        """Откликается на конкретную вакансию с сопроводительным письмом"""
        if self.check_interactive_controls() == 'stop' or self.stop_requested:
            return False, "Остановлено пользователем"
        self.letter_skip_reason = ''
        self.unanswered_questions = []
        # Прогон идёт часами, а бесплатное поднятие доступно каждые 4 часа —
        # поднимаем, как только подошло время, а не раз за запуск.
        self.maybe_bump_resume()
        try:
            self.driver.get(vacancy_url)
            self.random_delay((0.8, 1.2))

            initial_state = self.detect_response_state()
            if initial_state == 'denied':
                return False, "Вам отказали"
            if initial_state == 'success':
                return False, "Уже откликнулись"
            if initial_state == 'already':
                return False, "Уже откликнулись"
            if initial_state == 'limit':
                return False, "Лимит откликов"
            if self.page_has_captcha():
                return False, 'Требуется капча'
            
            # Прокрутка страницы вакансии до низа: считается, что hh засчитывает
            # просмотр целиком. Раньше здесь была анимированная прокрутка
            # (behavior: 'smooth') с паузами по 0.3 с — но паузы анимацию не
            # дожидались, до низа страница доехать не успевала, и за половину
            # эффекта платили 0.6 с на каждой вакансии.
            try:
                self.driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.15)
            except Exception:
                pass

            # Архив видно уже на странице. Раньше это выяснялось только в форме
            # отклика — после письма, на которое уходил запрос к ИИ из суточной
            # квоты. Строка та же, что у позднего обнаружения: дальше вакансия
            # обрабатывается как раньше.
            if self.page_says_archived():
                return False, "Вакансия в архиве"

            # Получаем контекст со страницы
            company_name = self.get_vacancy_page_company()
            vacancy_desc = self.get_vacancy_page_description()
            skills_list = self.get_vacancy_page_skills()

            # Слова в описании, из-за которых вакансия пропускается. Раньше список
            # был зашит в код — пользователь не мог ни посмотреть его, ни поменять,
            # а условия вроде полиграфа или неоплачиваемой стажировки каждый решает
            # для себя сам. Пустой список в настройках отключает отсев полностью.
            toxic_keywords = self.config.get('skip_keywords')
            if toxic_keywords is None:
                toxic_keywords = DEFAULT_SKIP_KEYWORDS
            toxic_keywords = tuple(str(k).lower().strip() for k in toxic_keywords if str(k).strip())
            desc_lower = (vacancy_desc or '').lower()
            toxic_found = next((kw for kw in toxic_keywords if kw in desc_lower), None)
            if toxic_found:
                logging.info(f" [ПРОПУСК] В описании есть «{toxic_found}» — вакансия пропущена по вашему списку отсева")
                self.skipped += 1
                v_id = self.get_vacancy_id_from_url(vacancy_url)
                if v_id:
                    self.save_applied(v_id, vacancy_name, STATUS_SKIPPED_FILTER)
                    if hasattr(self, 'db') and self.db:
                        self.db.record_skipped_vacancy(v_id, vacancy_name, company_name, vacancy_url, reason='toxic_filter', details=f"маркер: {toxic_found}")
                return False, f"Токсичный маркер в описании: {toxic_found}"

            # ИИ-генерация персонализированного сопроводительного письма
            if (self.config.get('ai_filter') or {}).get('mode', 'off') != 'off':
                suitable, reason = self.ai_assistant.filter_vacancy(vacancy_name, vacancy_desc, skills_list)
                if suitable is not True:
                    self.skipped += 1
                    vacancy_id = self.get_vacancy_id_from_url(vacancy_url)
                    deferred = suitable is None
                    if vacancy_id and getattr(self, 'db', None):
                        self.db.record_skipped_vacancy(vacancy_id, vacancy_name, company_name, vacancy_url,
                            reason='ai_unavailable' if deferred else 'ai_filter', details=reason)
                    if vacancy_id and not deferred:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_FILTER)
                    logging.info('[ПРОПУСК] AI-фильтр: %s', reason)
                    return False, 'AI-фильтр: ' + reason

            letter_job = None
            ai_enabled = self.config.get('ai_config', {}).get('enabled', True)
            if hasattr(self, 'ai_assistant') and self.ai_assistant and ai_enabled:
                # Модель называется один раз при старте и при каждой смене —
                # повторять её в каждой строке незачем, она не меняется от
                # вакансии к вакансии.
                logging.info(f" [ИИ] Составляю письмо для {company_name}...")
                # ИИ пишет в фоне, пока браузер жмёт «Откликнуться» и ждёт форму:
                # раньше браузер 2-4 с стоял без дела. Письмо забираем перед
                # вводом. Браузер трогает только основной поток.
                from concurrent.futures import ThreadPoolExecutor
                if getattr(self, '_letter_pool', None) is None:
                    self._letter_pool = ThreadPoolExecutor(max_workers=1)
                letter_job = self._letter_pool.submit(
                    self.ai_assistant.generate_cover_letter,
                    vacancy_title=vacancy_name,
                    company_name=company_name,
                    vacancy_description=vacancy_desc,
                    skills_list=skills_list,
                )
                cover_letter = ''
            else:
                custom_template = str(self.config.get('cover_letter', '')).strip()
                if custom_template:
                    cover_letter = render_template(custom_template, {
                        'vacancy_name': vacancy_name,
                        'employer_name': company_name,
                        'company_name': company_name,
                        'vacancy_title': vacancy_name,
                        'first_name': self.config.get('first_name', ''),
                        'last_name': self.config.get('last_name', '')
                    })
                else:
                    cover_letter = ""

            self.last_application_meta = {
                'title': vacancy_name,
                'company': company_name,
                'url': vacancy_url,
                'cover_letter': cover_letter,
                # Для письма на почту работодателя: адрес ищется в описании
                # вакансии и в блоке контактов, если hh его показывает.
                'page_text': f"{vacancy_desc or ''}\n{self.get_vacancy_contacts_text()}",
                'skills': skills_list,
                'questions_count': 0,
                'ats_score': 0
            }

            # Ищем кнопку "Откликнуться" (быстрый поиск без лишних задержек)
            apply_selectors = [
                '[data-qa="vacancy-response-link-top"]',
                '[data-qa="vacancy-response-link-bottom"]',
                'a[data-qa*="vacancy-response"]',
                'button[data-qa*="vacancy-response"]',
                '[data-qa*="vacancy-response"]'
            ]
            
            apply_btn = None
            for selector in apply_selectors:
                try:
                    for el in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        if el.is_displayed() and not self.is_disabled_element(el):
                            apply_btn = el
                            break
                    if apply_btn:
                        break
                except Exception:
                    continue

            if not apply_btn:
                try:
                    apply_btn = WebDriverWait(self.driver, 1.5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, ', '.join(apply_selectors)))
                    )
                except Exception:
                    pass

            if not apply_btn:
                return False, self.application_ui_problem(
                    'apply_button_missing',
                    'Не найдена видимая доступная кнопка отклика после ожидания 1.5 с')
            
            # Проверяем текст кнопки
            btn_text = apply_btn.text.lower()
            if 'отклик отправлен' in btn_text or 'уже откликнулись' in btn_text or 'вы откликнулись' in btn_text:
                return False, "Уже откликнулись"
            if 'вам отказали' in btn_text:
                return False, "Вам отказали"
            
            # Кликаем на кнопку отклика
            if not self.click_element_with_mouse(apply_btn):
                return False, "Не удалось нажать кнопку отклика"
            # Ждём появления формы, а не отмеренные 0.8 с: на быстрой странице
            # продолжаем сразу, на медленной даём до 3 с вместо преждевременного
            # продолжения вслепую.
            if not self.find_response_modal(wait_seconds=3):
                time.sleep(0.4)

            if self.is_response_limit_reached():
                return False, "Лимит откликов"

            # Проверяем и подтверждаем предупреждающие окна (другая страна / релокация)
            if self.handle_warning_popups():
                time.sleep(0.8)

            # Забираем письмо из фона: форма открыта, дальше ввод.
            if letter_job is not None:
                # С пределом: 24.09 бот ждал письмо без ограничения, запрос к ИИ
                # завис на обрыве сети — и прогон замер навсегда. Не дождались —
                # письмо по шаблону, отклик не теряем.
                try:
                    cover_letter = letter_job.result(timeout=LETTER_WAIT_SECONDS) or ''
                except Exception as e:
                    logging.info(f" ИИ не успел написать письмо за {LETTER_WAIT_SECONDS} с — письмо по шаблону")
                    logging.debug(f"Письмо из фона не получено: {e}")
                    try:
                        cover_letter = self.ai_assistant._heuristic_cover_letter(
                            vacancy_name, company_name, vacancy_desc, skills_list)
                    except Exception:
                        cover_letter = ''
                self.last_application_meta['cover_letter'] = cover_letter

            letter_sent = False
            questions_answered = 0
            submitted, letter_sent, answered_now, blocker_message = self.submit_open_response_modal(
                cover_letter,
                letter_sent,
            )
            questions_answered += answered_now
            self.last_application_meta['questions_count'] = questions_answered

            if blocker_message:
                return False, blocker_message

            # Повторная проверка предупреждений после отправки формы
            if self.handle_warning_popups():
                time.sleep(0.8)

            if submitted:
                time.sleep(0.5)

            ok, message = self.confirm_response_submission(
                cover_letter, letter_sent, questions_answered, modal_submitted=submitted)
            # В форме с анкетой у hh нет поля письма. После отклика на странице
            # вакансии появляется «Приложить сопроводительное письмо» — дописываем
            # туда: разбор отказов раз за разом называл причиной «откликнулся без
            # письма» (25.09). Выключается в меню «Поведение бота».
            if (ok and 'БЕЗ письма' in message and cover_letter.strip()
                    and self.config.get('letter_after_response', True)):
                tail = message.split(';', 1)[1] if ';' in message else ''
                tail = ';' + tail if tail else ''
                if self.attach_letter_after_response(cover_letter, vacancy_url):
                    message = 'Отклик отправлен С сопроводительным письмом (дописано после отклика)' + tail
                elif self.send_letter_to_chat(cover_letter, vacancy_url):
                    message = 'Отклик отправлен С сопроводительным письмом (отправлено в чат)' + tail
            return ok, message
            
        except ElementClickInterceptedException:
            return False, "Элемент перекрыт"
        except Exception as e:
            msg = str(e)
            if self.is_dead_session_message(msg):
                file_handler.emit(logging.LogRecord(
                    name='hh_selenium', level=logging.DEBUG,
                    pathname=__file__, lineno=2315,
                    msg=f"Сессия браузера закрыта: {e}",
                    args=(), exc_info=None
                ))
                return False, "Окно браузера закрыто пользователем"
            # Обрыв сети — не ошибка вакансии: вызывающий цикл дождётся связи
            # и повторит эту же вакансию.
            try:
                from terminal_ui import is_network_error
                if is_network_error(e):
                    logging.debug(f"Сетевой сбой при отклике: {e}")
                    return False, NETWORK_MESSAGE
            except Exception:
                pass
            logging.error(f"Ошибка отклика: {explain_error(e)}")
            return False, explain_error(e)

    def process_search_page(self):
        """Обрабатывает текущую загруженную страницу поиска.

        Сначала собирает ссылки/названия всех карточек (пока DOM-элементы не
        устарели), затем откликается по прямым URL. Это позволяет обрабатывать
        ВСЕ вакансии страницы: после навигации на вакансию исходные элементы
        списка становятся stale, поэтому опираться на них в цикле нельзя.
        """
        vacancies_processed = 0
        self.last_search_page_count = 0

        try:
            self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-qa="vacancy-serp__results"]'))
            )
            vacancy_items = self.driver.find_elements(By.CSS_SELECTOR, '[data-qa="vacancy-serp__vacancy"]')

            # 1) Сбор данных карточек до любой навигации
            collected = []
            for item in vacancy_items:
                try:
                    title_elem = item.find_element(By.CSS_SELECTOR, '[data-qa="serp-item__title"]')
                    url = title_elem.get_attribute('href')
                    name = title_elem.text
                    has_test = False
                    if self.config.get('skip_with_tests', True):
                        try:
                            item.find_element(By.CSS_SELECTOR, '[data-qa="vacancy-serp__vacancy-test"]')
                            has_test = True
                        except NoSuchElementException:
                            has_test = False
                    if url:
                        collected.append((url, name, has_test))
                except (StaleElementReferenceException, Exception):
                    continue

            self.last_search_page_count = len(collected)
            logging.info(f"Найдено вакансий на странице: {len(collected)}")

            # 2) Отклики по собранным ссылкам
            for i, (vacancy_url, vacancy_name, has_test) in enumerate(collected):
                if self.check_interactive_controls() == 'stop' or self.stop_requested:
                    logging.info("[СТОП] Остановка обработки страницы поиска пользователем")
                    return vacancies_processed

                if self.local_application_limit_reached():
                    logging.info("[СТОП] Достигнут пользовательский локальный предел откликов")
                    return vacancies_processed

                vacancy_id = self.get_vacancy_id_from_url(vacancy_url)
                logging.info(f"\n[{i+1}/{len(collected)}] {vacancy_name}")

                if vacancy_id and self.should_skip_known_vacancy(vacancy_id, vacancy_name):
                    known = self.applied_vacancies.get(str(vacancy_id))
                    if isinstance(known, dict) and known.get('status') == STATUS_PENDING_CONFIRMATION:
                        logging.info(' [ПРОПУСК] Прошлая отправка не подтверждена; проверьте отклики на HH. Не повторяю.')
                    else:
                        logging.info(" [ПРОПУСК] Уже откликались")
                    self.skipped += 1
                    if hasattr(self, 'db') and self.db:
                        self.db.record_skipped_vacancy(vacancy_id, vacancy_name, '', vacancy_url, reason='already_applied')
                    continue

                # Title-фильтр; строгость зависит от allow_technical_fallback текущего яруса
                suitable, reason = self.validate_security_title(vacancy_name)
                if not suitable:
                    logging.info(f" [ПРОПУСК] Пропуск: {reason}")
                    self.skipped += 1
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_FILTER)
                        if hasattr(self, 'db') and self.db:
                            self.db.record_skipped_vacancy(vacancy_id, vacancy_name, '', vacancy_url, reason='excluded_filter', details=str(reason))
                    continue

                if has_test:
                    logging.info(" [ПРОПУСК] Пропуск: есть тест")
                    self.skipped += 1
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_TEST)
                        if hasattr(self, 'db') and self.db:
                            self.db.record_skipped_vacancy(vacancy_id, vacancy_name, '', vacancy_url, reason='skipped_test', details='has_test=True')
                    continue

                success, message = self.apply_to_vacancy(vacancy_url, vacancy_name)
                # Капчу решает человек; после решения повторяем отклик ровно один
                # раз, и его итог идёт по той же цепочке ниже.
                if not success and message == NETWORK_MESSAGE and self.wait_for_network_back():
                    logging.info(" Повторяю отклик на эту вакансию")
                    success, message = self.apply_to_vacancy(vacancy_url, vacancy_name)
                if not success and message == "Требуется капча" and self.wait_for_human_captcha():
                    logging.info(" [OK] Капча решена — повторяю отклик")
                    success, message = self.apply_to_vacancy(vacancy_url, vacancy_name)

                if success:
                    logging.info(f" [OK] {message}")
                    self.applied_today += 1
                    vacancies_processed += 1
                    self.note_apply_success()
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_SENT)
                        self.maybe_email_employer(vacancy_id, vacancy_name)
                elif message == NETWORK_MESSAGE:
                    # Не сохраняем: вакансия вернётся в очередь.
                    logging.info(" [ПРОПУСК] Нет связи — вакансия останется в очереди")
                    self.skipped += 1
                elif message == "Требуется капча":
                    # Не сохраняем: вакансия вернётся в очередь в следующий прогон.
                    logging.info(" [ПРОПУСК] Капча не решена — вакансия останется в очереди")
                    self.skipped += 1
                elif message == "Лимит откликов":
                    logging.info(" [СТОП] HH показал лимит откликов. "
                                 f"В локальной истории за 24 часа: {self.applied_today}. "
                                 "Счётчик HH может учитывать отклики вне бота; новые отправки прекращены.")
                    self.response_limit_reached = True
                    return vacancies_processed
                elif message == HIDDEN_RESUME_MESSAGE:
                    # Не помечаем вакансию: она вернётся в очередь. Серия означает,
                    # что hh прячет резюме на несколько минут — пережидаем, а не
                    # прожигаем список по 45 секунд на вакансию.
                    logging.info(" [~] hh временно скрыл резюме от откликов — вакансия останется в очереди")
                    self.skipped += 1
                    self.hidden_resume_streak = getattr(self, "hidden_resume_streak", 0) + 1
                    if self.hidden_resume_streak >= 2:
                        self.wait_out_hidden_resume()
                elif message == "Вакансия в архиве":
                    logging.info(" [ПРОПУСК] Вакансия в архиве")
                    self.skipped += 1
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_ARCHIVED)
                elif str(message).startswith(("Токсичный маркер", "AI-фильтр:")):
                    # Уже помечено и выведено как [ПРОПУСК] внутри apply_to_vacancy.
                    pass
                elif message == "Уже откликнулись":
                    logging.info(" [ПРОПУСК] Уже откликнулись")
                    self.skipped += 1
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_ALREADY_APPLIED)
                elif message == "Вам отказали":
                    logging.info(" [ПРОПУСК] Вам отказали")
                    self.skipped += 1
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_DENIED)
                elif str(message).startswith(QUESTIONS_SKIP_MARKER):
                    # Это пропуск, а не ошибка: серию неудач не наращиваем
                    logging.info(f" [ПРОПУСК] {message}")
                    self.skipped += 1
                    if vacancy_id:
                        self.save_applied(vacancy_id, vacancy_name, STATUS_SKIPPED_TEST)
                        if hasattr(self, 'db') and self.db:
                            self.db.record_skipped_vacancy(vacancy_id, vacancy_name, '', vacancy_url, reason='skipped_questions', details=str(message))
                else:
                    if self.is_dead_session_message(message) or message == "Окно браузера закрыто пользователем":
                        logging.info(" [!] Окно браузера закрыто пользователем. Работа завершена.")
                        self.stop_requested = True
                        return vacancies_processed
                    logging.info(f" [X] {message}")
                    self.errors += 1
                    if vacancy_id and APPLY_NOT_CONFIRMED_MARKER in str(message):
                        self.save_applied(vacancy_id, vacancy_name, STATUS_PENDING_CONFIRMATION)
                    if self.register_apply_failure(message):
                        return vacancies_processed

                self.random_delay(self.apply_delay())

        except Exception as e:
            logging.error(f"Ошибка обработки страницы: {explain_error(e)}")

        return vacancies_processed
    
    def go_to_next_page(self):
        """Переходит на следующую страницу"""
        try:
            next_btn = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="pager-next"]')
            next_btn.click()
            self.random_delay(self.delay_between_actions)
            return True
        except NoSuchElementException:
            logging.info("Больше страниц нет")
            return False
        except Exception as e:
            logging.error(f"Ошибка перехода на следующую страницу: {explain_error(e)}")
            return False
    
    def select_primary_resume_on_startup(self):
        """Переходит на страницу целевого резюме при старте сессии,
        чтобы HeadHunter зафиксировал его как активное резюме по умолчанию."""
        try:
            from config_manager import get_active_resume
            target_resume_id, target_resume_title = get_active_resume()
        except Exception as e:
            logging.warning(f"Не удалось получить целевое резюме из конфига: {explain_error(e)}")
            target_resume_id = ""
            target_resume_title = ""

        try:
            self.driver.get(f"https://hh.ru/resume/{target_resume_id}")
            time.sleep(1.2)
            logging.info(f"Целевое резюме активировано в текущей сессии: {target_resume_title}")
        except Exception as e:
            logging.warning(f"Не удалось активировать целевое резюме на старте: {explain_error(e)}")

    def run(self):
        """Основной цикл работы"""
        print(f"\n{CYAN}{BOLD}{'='*60}{RESET}")
        print(f"{RED}{BOLD}HH.RU: АВТООТКЛИКИ ЧЕРЕЗ БРАУЗЕР{RESET}")
        print(f"{CYAN}{BOLD}{'='*60}{RESET}")
        
        # Инициализация
        if not self.init_driver():
            return
        
        try:
            # Проверяем авторизацию
            if not self.check_login():
                if not self.login():
                    return

            # Активируем целевое резюме по ИБ как активное по умолчанию
            self.select_primary_resume_on_startup()
            
            print(f"\n{YELLOW}{BOLD}{'='*60}{RESET}")
            print(f"{BLUE}{BOLD}УПРАВЛЕНИЕ: [P] Пауза/Продолжить | [S] Стоп | [I] Статус{RESET}")
            print(f"{YELLOW}Паузу, продолжение и остановку можно включать из меню{RESET}")
            print(f"{YELLOW}{BOLD}{'='*60}{RESET}\n")

            # Обрабатываем страницы поиска из конфига (search_url)
            self.process_search_url(self.config.get('search_url'), label='search_url')

            self.print_run_summary()
            
        except KeyboardInterrupt:
            print("\n\n[СТОП] Остановлено пользователем")
        except Exception as e:
            if self.is_dead_session_message(str(e)):
                logging.info("[!] Окно браузера закрыто пользователем. Работа завершена.")
            else:
                logging.error(f"Критическая ошибка: {explain_error(e)}")
        finally:
            self.close_driver()

    def print_run_summary(self):
        self.applied_today = self.count_sent_today()
        print("\n" + "="*60)
        print("ИТОГИ")
        print("="*60)
        print(f"[OK] Новых откликов за этот запуск: {getattr(self, 'sent_this_run', 0)}")
        print(f"[OK] Известных откликов за последние 24 часа: {self.applied_today}")
        print(f"[ПРОПУСК] Пропущено: {self.skipped}")
        print(f"[X] Ошибок: {self.errors}")
        for code, count in getattr(self, '_application_ui_errors', {}).items():
            label = {'resume_not_confirmed': 'Выбранное резюме не прочитано',
                     'apply_button_missing': 'Кнопка отклика недоступна'}.get(code, code)
            print(f"  [{code}] {label}: {count}")
        if getattr(self, '_application_ui_errors', {}):
            print(f"  Диагностика ошибок интерфейса: {os.path.join(SCRIPT_DIR, '.apply_diagnostics')}")
        print("="*60)

    def wait_out_hidden_resume(self):
        """Пережидает, пока hh снова разрешит откликаться этим резюме.

        Сбой временный: в прогоне 23.09 серии длились 7-10 минут и проходили
        сами. Ждём паузу, продолжая слушать команды «пауза/стоп».
        """
        minutes = HIDDEN_RESUME_PAUSE_SECONDS // 60
        logging.warning(
            f"hh уже {self.hidden_resume_streak} раза подряд скрывает резюме от откликов. "
            f"Обычно это на несколько минут — жду {minutes} мин и продолжаю.")
        deadline = time.time() + HIDDEN_RESUME_PAUSE_SECONDS
        while time.time() < deadline:
            if self.check_interactive_controls() == 'stop' or self.stop_requested:
                return
            time.sleep(2)
        self.hidden_resume_streak = 0

    def note_apply_success(self):
        """Сбрасывает серию провалов после удачного отклика."""
        self.sent_this_run = getattr(self, 'sent_this_run', 0) + 1
        self.consecutive_apply_failures = 0
        self.hidden_resume_streak = 0

    def register_apply_failure(self, message):
        """Учитывает провал подтверждения отклика (признак троттлинга hh).

        Возвращает True, если серия достигла предела и прогон надо остановить.
        Настоящий признак троттлинга — форма отправлена, но кнопка «Откликнуться»
        осталась: состояние (ready). Состояние (unknown) — это, как правило,
        нестандартная/архивная вакансия, а не троттлинг, поэтому к остановке оно
        не копится (иначе серия «странных» вакансий зря оборвёт прогон до 200).
        """
        text = message or ''
        if APPLY_NOT_CONFIRMED_MARKER not in text or '(ready)' not in text:
            return False

        self.consecutive_apply_failures += 1
        limit = self.config.get('max_consecutive_failures', APPLY_FAILURE_STREAK_LIMIT)
        if self.consecutive_apply_failures >= limit:
            self.throttled_stop = True
            logging.error(
                f"[СТОП] {self.consecutive_apply_failures} провалов подтверждения подряд — "
                f"похоже на троттлинг hh, останавливаю прогон."
            )
            print(
                f"\n[СТОП] Остановка: {self.consecutive_apply_failures} неудачных подтверждений подряд "
                f"(вероятно, hh ограничивает темп откликов). Попробуйте позже или увеличьте задержки."
            )
            return True
        return False

    def build_site_search_url(self, query):
        """URL поиска hh.ru по названию вакансии (свежие сверху). Без area - поиск по всему миру."""
        area = str(self.config.get('search_area', '') or '').strip()
        area_part = f'&area={quote_plus(area)}' if area and area.lower() not in ('all', 'world', '0', 'none') else ''
        return (
            'https://hh.ru/search/vacancy'
            f'?text={quote_plus(query)}'
            '&search_field=name'
            f'{area_part}'
            '&order_by=publication_time'
        )

    def process_search_url(self, search_url, label=''):
        """Листает все страницы одного запроса через &page=N и откликается до лимита."""
        max_pages = self.config.get('max_search_pages', 50)
        logging.info(f"\nПоиск [{label}]: {search_url}")

        page = 0
        while (not self.local_application_limit_reached()
               and not self.response_limit_reached
               and not self.throttled_stop
               and not self.stop_requested):
            page_url = f"{search_url}&page={page}"
            logging.info(f"\nСтраница {page + 1} [{label}]")
            self.driver.get(page_url)
            self.random_delay(self.delay_between_actions)

            self.process_search_page()

            if self.stop_requested:
                logging.info("[СТОП] Остановка поиска по запросу пользователя")
                return
            if self.response_limit_reached:
                logging.info("[СТОП] Остановка: HH сообщил лимит откликов")
                return
            if self.throttled_stop:
                return
            if self.last_search_page_count == 0:
                logging.info(f" Конец выдачи по запросу [{label}]")
                return
            page += 1
            if page >= max_pages:
                logging.info(f" Достигнут предел страниц ({max_pages}) [{label}]")
                return

    def run_site_search(self, queries, allow_technical_fallback, label):
        """Каскадный браузерный поиск по списку запросов до дневного лимита.

        allow_technical_fallback=False → только строгие ИБ-заголовки (ярус 2);
        True → разрешены dev/IT-заголовки через technical fallback (ярус 3).
        """
        queries = [q for q in (queries or []) if q]
        if not queries:
            return
        if (self.local_application_limit_reached()
                or self.response_limit_reached
                or self.throttled_stop
                or self.stop_requested):
            return

        print("\n" + "=" * 60)
        print(f"{label}")
        print(f" Запросов: {len(queries)} | Известных откликов за 24ч: {self.applied_today}")
        print("=" * 60)

        previous_fallback = self.config.get('allow_technical_fallback', True)
        self.config['allow_technical_fallback'] = allow_technical_fallback
        try:
            for i, query in enumerate(queries, 1):
                if (self.local_application_limit_reached()
                        or self.response_limit_reached
                        or self.throttled_stop
                        or self.stop_requested):
                    break
                self.process_search_url(
                    self.build_site_search_url(query),
                    label=f"{i}/{len(queries)}: {query}",
                )
        finally:
            self.config['allow_technical_fallback'] = previous_fallback

    def run_api_cache(self):
        """Откликается через браузер на вакансии, найденные API-ботом."""
        print(f"\n{CYAN}{BOLD}{'='*60}{RESET}")
        print(f"{RED}{BOLD}HH.RU: ОТКЛИКИ ПО СОХРАНЁННОМУ СПИСКУ ВАКАНСИЙ{RESET}")
        print(f"{CYAN}{BOLD}{'='*60}{RESET}")

        self.run_failed = False
        if not self.init_driver():
            self.run_failed = True
            return

        try:
            if not self.check_login():
                if not self.login():
                    self.run_failed = True
                    return

            # Активируем целевое резюме по ИБ как активное по умолчанию
            self.select_primary_resume_on_startup()

            print(f"\n{YELLOW}{BOLD}{'='*60}{RESET}")
            print(f"{BLUE}{BOLD}УПРАВЛЕНИЕ: [P] Пауза/Продолжить | [S] Стоп | [I] Статус{RESET}")
            print(f"{YELLOW}Паузу, продолжение и остановку можно включать из меню{RESET}")
            print(f"{YELLOW}{BOLD}{'='*60}{RESET}\n")

            # Поднятие — до сбора вакансий: сбор идёт пару минут, и 28.09 бот
            # поднял бы резюме только на первой вакансии, позже, чем пользователь вручную.
            self.maybe_bump_resume()

            # ЯРУС 0: подходящие вакансии для резюме из веб-поиска — API отдаёт не все.
            try:
                self.merge_resume_recommendations()
            except Exception as e:
                logging.warning(f"Вакансии для резюме не собраны: {explain_error(e)}")
            if self.stop_requested:
                logging.info("[СТОП] Остановка по запросу пользователя")
                return

            # ЯРУС 1: ИБ-вакансии из API-кеша (уже отфильтрованы test.py до приоритетов 1..5)
            vacancies = self.load_api_vacancies()
            if vacancies:
                self.process_api_vacancies(vacancies)
            else:
                print("\n[ИНФО] В сохранённом списке нет подходящих вакансий — ищу на сайте")

            if self.stop_requested:
                logging.info("[СТОП] Остановка по запросу пользователя")
                return

            # Направление: для ИБ — ИБ-запросы и добор разработкой, для других —
            # запросы своего направления и без добора. Раньше ИБ-запросы шли для
            # любого направления, и поиск впустую перебирал чужие вакансии.
            try:
                from config_manager import get_active_preset
                preset = get_active_preset()
            except Exception:
                preset = {'id': 'security'}
            is_security = preset.get('id', 'security') == 'security'

            # ЯРУС 2: добор напрямую с сайта hh.ru (строгий фильтр по названию)
            self.run_site_search(
                self.config.get('security_search_queries', []) if is_security
                else [q.replace('"', '') for q in (preset.get('queries')
                      or ([preset['custom_query']] if preset.get('custom_query') else []))],
                allow_technical_fallback=False,
                label=('ЯРУС 2: ИБ-вакансии напрямую с сайта hh.ru' if is_security
                       else f"ЯРУС 2: вакансии напрямую с сайта hh.ru ({preset.get('name', '')})"),
            )

            if self.stop_requested:
                logging.info("[СТОП] Остановка по запросу пользователя")
                return

            # ЯРУС 3: если вся ИБ исчерпана, а до лимита не дотянули — добор разработкой/IT
            if is_security and self.config.get('dev_topup_enabled', True):
                self.run_site_search(
                    self.config.get('dev_search_queries', []),
                    allow_technical_fallback=True,
                    label='ЯРУС 3: добор разработкой/IT до дневного лимита',
                )

            self.print_run_summary()

        except KeyboardInterrupt:
            print("\n\n[СТОП] Остановлено пользователем")
        except Exception as e:
            self.run_failed = True
            if self.is_dead_session_message(str(e)):
                logging.info("[!] Окно браузера закрыто пользователем. Работа завершена.")
            else:
                logging.error(f"Критическая ошибка API-cache режима: {explain_error(e)}")
        finally:
            self.close_driver()


def main():
    """Точка входа"""
    print("\n" + "="*60)
    print(f"{RED}{BOLD}HH.RU: АВТООТКЛИКИ ЧЕРЕЗ БРАУЗЕР{RESET}")
    print("="*60)
    print("\nНастройки бота хранятся в папке программы.")
    print("История ваших откликов сохраняется там же.")
    print("\n" + "="*60)

    args = sys.argv[1:]
    if '--help' in args:
        print("\nКоманды:")
        print(" python hh_selenium.py --api-cache --limit 200            - отклики по сохранённому списку, до 200 за раз")
        print(" python hh_selenium.py --api-cache --headless --limit 200 - то же, но в фоне, без окна браузера")
        print(" python hh_selenium.py --api-cache --debugger 127.0.0.1:9122 --limit 200  - подключиться к уже открытому браузеру")
        print(" python hh_selenium.py --api-cache --keep-browser-open --limit 200        - не закрывать браузер после работы")
        print(" python hh_selenium.py --analyze-rejections [--limit 20]  - разобрать отказы работодателей")
        print(" python hh_selenium.py                                    - обычный запуск с выбором режима")
        return

    if '--analyze-rejections' in args:
        from rejection_analyzer import run_rejection_analysis_cli
        run_rejection_analysis_cli(args)
        return

    if '--api-cache' in args:
        headless = '--headless' in args
        debugger_address = None
        if '--debugger' in args:
            debugger_index = args.index('--debugger') + 1
            if debugger_index >= len(args):
                print("[X] После --debugger нужен адрес, например 127.0.0.1:9122")
                return
            debugger_address = args[debugger_index]

        bot = HHSeleniumBot(
            headless=headless,
            debugger_address=debugger_address,
            pause_before_close='--keep-browser-open' in args,
        )

        if '--limit' in args:
            limit_index = args.index('--limit') + 1
            if limit_index >= len(args):
                print("[X] После --limit нужно число")
                return
            try:
                bot.config['max_applications'] = max(1, int(args[limit_index]))
                bot.config['stop_at_local_limit'] = True
            except ValueError:
                print("[X] Значение --limit должно быть числом")
                return

        if '--until-hh-limit' in args:
            bot.config['stop_at_local_limit'] = False

        bot.run_api_cache()
        return
    
    # Спрашиваем режим
    print("\nВыберите режим:")
    print("1. Обычный (с браузером)")
    print("2. В фоне, без окна браузера")
    print("3. Только создать файл настроек")
    print("4. Откликаться по сохранённому списку вакансий")
    print("5. Разбор отказов работодателей (ИИ)")
    
    choice = input("\nВаш выбор (1/2/3/4/5): ").strip()
    
    if choice == '3':
        bot = HHSeleniumBot()
        print(f"\n[OK] Файл настроек создан: {bot.config_file}")
        print("Отредактируйте его и запустите снова")
        return

    if choice == '5':
        from rejection_analyzer import run_rejection_analysis_cli
        run_rejection_analysis_cli([])
        return
    
    headless = choice == '2'
    
    bot = HHSeleniumBot(headless=headless, pause_before_close=choice != '4')
    if choice == '4':
        bot.run_api_cache()
    else:
        bot.run()


if __name__ == '__main__':
    main()
