import requests
import json
import time
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs, urlparse
import logging
import re
import random
import os
import sys
import subprocess
from typing import Optional

from db_manager import human_status

# Получаем путь к директории скрипта
from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR

BASE_URL = 'https://api.hh.ru'
OAUTH_URL = 'https://hh.ru/oauth'
TOKEN_URL = 'https://api.hh.ru/token'
APP_USER_AGENT = 'AutoJobApplyBot/1.0'
CONTACT_EMAIL_ENV = 'HH_CONTACT_EMAIL'
REQUEST_TIMEOUT_SECONDS = 15
MAX_REQUEST_RETRIES = 3
DEFAULT_MANUAL_APPLY_LIMIT = 10
DEFAULT_SELENIUM_APPLY_LIMIT = 200
APPLICATION_LIMIT_WINDOW_HOURS = 24
# Понятные подписи счётчиков активности, которые hh.ru отдаёт машинными ключами
COUNTER_LABELS = {
    'new_resume_views': 'Новых просмотров резюме',
    'resumes_count': 'Резюме в аккаунте',
    'unread_negotiations': 'Непрочитанных сообщений от работодателей',
}
SEARCH_RESULT_LIMIT = 1500
# HH API отдаёт максимум 2000 результатов на запрос (page*per_page<=2000),
# т.е. 20 страниц по 100 — берём весь доступный объём, а не 15 страниц.
SEARCH_PAGE_LIMIT = 20
MAX_SEARCH_NETWORK_ERRORS_WITHOUT_RESULTS = 3
SHARED_APPLIED_FILE = 'applied_vacancies.json'
SELENIUM_APPLIED_FILE = 'applied_vacancies_selenium.json'
STATUS_SKIPPED_FILTER = 'skipped_filter'
AUTHORIZATION_CODE_GRANT = 'authorization_code'
CLIENT_CREDENTIALS_GRANT = 'client_credentials'
REFRESH_TOKEN_GRANT = 'refresh_token'
APP_TOKEN_REFRESH_TOO_EARLY = 'app token refresh too early'
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
BLACKLISTED_CONTACT_DOMAINS = {'example.com', 'hh.ru'}
HH_ALLOWED_APPLY_HOST_SUFFIX = 'hh.ru'
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)
from terminal_ui import (
    ColoredConsoleFormatter, colorize_text, c_ok, c_err, c_warn, c_info, c_skip,
    c_priority, c_header, c_company, c_title, c_accent, explain_error,
    CYAN, GREEN, YELLOW, RED, MAGENTA, BLUE, BOLD, RESET, WHITE, DIM
)
from config_manager import (
    SECURITY_TITLE_KEYWORDS as STRICT_TITLE_INCLUDE_KEYWORDS, search_direction_chosen,
    security_title_by_meaning,
    find_title_keyword,
    STRICT_TITLE_EXCLUDE_KEYWORDS, TECHNICAL_FALLBACK_INCLUDE_KEYWORDS, title_excludes,
    get_active_resume,
    get_active_preset,
    interactive_resume_picker,
    interactive_search_picker,
    interactive_cover_letter_editor,
    prompt_gemini_key,
    prompt_groq_key,
    load_config,
    save_config,
    check_title_grade
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredConsoleFormatter('%(levelname)s - %(message)s'))
file_handler = logging.FileHandler(os.path.join(SCRIPT_DIR, 'hh_auto_apply.log'), encoding='utf-8')
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


def log_detail(message: str) -> None:
    """Технические подробности — только в файл журнала, мимо консоли.

    Корневой логгер стоит на уровне INFO, поэтому logging.debug не доходит даже
    до файла. Пишем прямо в файловый обработчик: консоль его не видит.
    """
    file_handler.emit(logging.LogRecord(
        name='hh', level=logging.DEBUG, pathname=__file__, lineno=0,
        msg=message, args=(), exc_info=None,
    ))


def log_problem(message: str, error, level: int = logging.ERROR) -> None:
    """Пользователю — человеческая причина, техническая — только в журнал.

    Консольный обработчик висит на корневом логгере с уровнем INFO, поэтому любой
    logging.error виден в терминале. Сырой текст исключения ('invalid session id',
    куски ответа сервера, коды) пользователю ничего не объясняет.
    """
    logging.log(level, f"{message}: {explain_error(error)}")
    log_detail(f"{message} [подробности]: {error!r}")


def build_user_agent(contact_email: Optional[str]) -> str:
    """Возвращает User-Agent в формате, который принимает HH API."""
    if contact_email is None or not contact_email.strip():
        return APP_USER_AGENT

    normalized_email = contact_email.strip().lower()
    if not EMAIL_PATTERN.fullmatch(normalized_email):
        raise ValueError(f'{CONTACT_EMAIL_ENV} должен быть email-адресом')

    email_domain = normalized_email.rsplit('@', 1)[1]
    if email_domain in BLACKLISTED_CONTACT_DOMAINS:
        raise ValueError(f'{CONTACT_EMAIL_ENV} должен быть реальным контактным email, не {email_domain}')

    return f'{APP_USER_AGENT} ({normalized_email})'


def build_hh_headers(user_agent: str, access_token: Optional[str] = None) -> dict[str, str]:
    headers = {
        'User-Agent': user_agent,
        'HH-User-Agent': user_agent,
        'Accept': 'application/json',
    }

    if access_token:
        headers['Authorization'] = f'Bearer {access_token}'

    return headers


def build_client_credentials_payload(client_id: str, client_secret: str) -> dict[str, str]:
    return {
        'grant_type': CLIENT_CREDENTIALS_GRANT,
        'client_id': client_id,
        'client_secret': client_secret,
    }


def is_allowed_hh_url(url: str) -> bool:
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname or ''
    return parsed_url.scheme == 'https' and (
        hostname == HH_ALLOWED_APPLY_HOST_SUFFIX
        or hostname.endswith(f'.{HH_ALLOWED_APPLY_HOST_SUFFIX}')
    )


def get_vacancy_apply_url(vacancy: dict) -> Optional[str]:
    for field_name in ('apply_alternate_url', 'alternate_url'):
        url = vacancy.get(field_name)
        if isinstance(url, str) and is_allowed_hh_url(url):
            return url

    return None


def normalize_title(title: object) -> str:
    return str(title or '').lower().replace('ё', 'е')


def find_keyword(text: object, keywords: tuple[str, ...]) -> Optional[str]:
    return find_title_keyword(text, keywords)


_TITLE_FILTER_WARNED = False


def _warn_title_filter_disabled(preset: dict) -> None:
    """Один раз за запуск сообщает, что фильтр заголовков отключён пустым keywords_include."""
    global _TITLE_FILTER_WARNED
    if _TITLE_FILTER_WARNED:
        return
    _TITLE_FILTER_WARNED = True
    message = (
        f"Фильтр заголовков ОТКЛЮЧЁН: у направления '{preset.get('name', preset.get('id'))}' "
        f"пустой список keywords_include, поэтому принимается ЛЮБАЯ вакансия из выдачи. "
        f"Задайте ключевые слова через меню [S] (пункт 5 - свой запрос), "
        f"либо убедитесь, что отбор уже задан фильтрами в самой ссылке hh.ru."
    )
    logging.warning(message)
    print(f"\n{YELLOW}[!] {message}{RESET}\n")


_GRADE_SKIP_LOGGED = set()


def _log_grade_skip(title: object, reason: str) -> None:
    """Пишет в лог человеческую причину пропуска по грейду, один раз на заголовок."""
    key = normalize_title(title)
    if key in _GRADE_SKIP_LOGGED:
        return
    _GRADE_SKIP_LOGGED.add(key)
    logging.info(f"Пропускаю вакансию «{title}»: {reason}")


def validate_apply_title(title: object, allow_technical_fallback: bool = True) -> tuple[bool, str]:
    try:
        preset = get_active_preset()
    except Exception:
        preset = {'id': 'security'}

    # Грейд проверяем ДО ключевых слов: списки keywords_exclude знают только
    # 'директор'/'начальник'/'руководитель' и молча пропускали правление банка,
    # а опыт кандидата не учитывали вообще.
    grade_allowed, grade_reason = check_title_grade(title)
    if not grade_allowed:
        _log_grade_skip(title, grade_reason)
        return False, grade_reason

    preset_id = preset.get('id', 'security')
    # Список из настроек (правится в меню «Поведение бота»), как и в hh_selenium.
    custom_excludes = title_excludes()

    excluded_keyword = find_keyword(title, custom_excludes)
    if excluded_keyword:
        return False, f"Исключено по ключевому слову: {excluded_keyword}"

    if preset_id == 'security':
        if find_keyword(title, STRICT_TITLE_INCLUDE_KEYWORDS) or security_title_by_meaning(title):
            return True, "strict_security"

        if allow_technical_fallback and find_keyword(title, TECHNICAL_FALLBACK_INCLUDE_KEYWORDS):
            return True, "technical_fallback"

        return False, "Не security/appsec/pentest/devsecops/soc или технический fallback"

    # Любой другой пресет (python, devops, sysadmin, custom)
    include_keywords = tuple(preset.get('keywords_include', []))
    if include_keywords:
        matched = find_keyword(title, include_keywords)
        if matched:
            return True, f"preset_{preset_id}:{matched}"
        return False, f"Не соответствует ключевым словам пресета {preset.get('name')}"

    # keywords_include пуст (обычно пресет 'custom', заданный готовой ссылкой hh.ru):
    # фильтр заголовков пропускает ВСЁ. Молча это делать нельзя — пользователь
    # считает, что отбор работает, а бот откликается на любую вакансию из выдачи.
    _warn_title_filter_disabled(preset)
    return True, "preset_allowed"


def parse_saved_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None

    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def count_recent_timestamps(timestamps: list[object], window_hours: int) -> int:
    cutoff = datetime.now() - timedelta(hours=window_hours)
    count = 0

    for timestamp in timestamps:
        parsed_timestamp = parse_saved_timestamp(timestamp)
        if parsed_timestamp is None:
            continue
        if parsed_timestamp >= cutoff:
            count += 1

    return count


def next_slot_free_at(timestamps: list[object], window_hours: int):
    """Когда из суточного окна выпадет самый старый отклик.

    Окно скользящее: hh не обнуляет счётчик в полночь, отклик перестаёт
    учитываться ровно через window_hours после отправки. Поэтому «сброс» — это
    момент, когда освободится ближайшее место, а не начало суток.

    None — если в окне ничего нет.
    """
    cutoff = datetime.now() - timedelta(hours=window_hours)
    inside = []
    for timestamp in timestamps:
        parsed = parse_saved_timestamp(timestamp)
        if parsed is not None and parsed >= cutoff:
            inside.append(parsed)
    if not inside:
        return None
    return min(inside) + timedelta(hours=window_hours)


def describe_time_left(moment) -> str:
    """«через 3 ч 12 мин (в 02:41)» — и сколько ждать, и во сколько."""
    if moment is None:
        return ''
    left = moment - datetime.now()
    total_minutes = max(0, int(left.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        human = f'через {hours} ч {minutes} мин'
    else:
        human = f'через {minutes} мин'
    return f'{human} (в {moment.strftime("%H:%M")})'


def active_direction() -> tuple:
    """(это ИБ?, название направления) — для надписей в выводе."""
    try:
        preset = get_active_preset()
    except Exception:
        preset = {'id': 'security', 'name': 'Информационная безопасность'}
    return preset.get('id', 'security') == 'security', preset.get('name', '') or 'вакансии'


def write_json_atomic(path: str, data: object) -> None:
    temp_path = f'{path}.tmp'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


class HHAutoApplicant:
    def __init__(self, client_id, client_secret, redirect_uri, resume_id):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.base_url = BASE_URL
        self.oauth_url = OAUTH_URL
        self.access_token = None
        self.refresh_token = None
        self.app_access_token = None
        self.api_negotiations_available = None
        self.session = requests.Session()
        self.user_agent = build_user_agent(os.environ.get(CONTACT_EMAIL_ENV))
        
        self.resume_id = resume_id
        
        self.max_applications_per_day = 200
        self.selenium_apply_limit = DEFAULT_SELENIUM_APPLY_LIMIT
        self.min_delay_between_requests = 2
        self.delay_after_429 = 60
        self.applied_today = 0
        
        # Все файлы сохраняются в директорию скрипта
        self.applied_vacancies_file = os.path.join(SCRIPT_DIR, SHARED_APPLIED_FILE)
        self.selenium_applied_vacancies_file = os.path.join(SCRIPT_DIR, SELENIUM_APPLIED_FILE)
        self.vacancies_cache_file = os.path.join(SCRIPT_DIR, 'vacancies_cache.json')
        self.token_file = os.path.join(SCRIPT_DIR, 'hh_token.json')
        self.app_token_file = os.path.join(SCRIPT_DIR, 'hh_app_token.json')
        self.cache_lifetime_hours = 12 # Кеш актуален 12 часов
        self.min_vacancies_in_cache = 1 # Используем кеш, если есть хотя бы 1 необработанная вакансия
        self.allow_technical_fallback = True
        # Максимальный приоритет, на который реально откликаемся.
        # 1 Пентест/RedTeam, 2 ИБ, 3 Спец-ИБ, 4 Защита данных, 5 Разработка с ИБ.
        # 6 (чистая разработка) и 7 (другое IT) исключаются из откликов.
        self.max_apply_priority = 5
        
        self.load_applied_vacancies()
        
        # Всегда исключаем вакансии с тестами
        self.skip_vacancies_with_tests = True

        # ПРИОРИТЕТ НА КИБЕРБЕЗОПАСНОСТЬ
        self.security_priority = True
        self._user_closed = False
        
        self.ensure_token()

    def load_applied_vacancies(self):
        try:
            with open(self.applied_vacancies_file, 'r', encoding='utf-8') as f:
                self.applied_vacancies = json.load(f)
        except FileNotFoundError:
            self.applied_vacancies = {}

        self.processed_vacancy_ids = set(str(vacancy_id) for vacancy_id in self.applied_vacancies)
        selenium_processed_ids = self.load_selenium_processed_vacancy_ids()
        self.processed_vacancy_ids.update(selenium_processed_ids)
        
        self.applied_today = count_recent_timestamps(
            list(self.applied_vacancies.values()),
            APPLICATION_LIMIT_WINDOW_HOURS,
        )
        
        print(f"Загружено {len(self.applied_vacancies)} отправленных вакансий из общей истории")
        if selenium_processed_ids:
            print(f"Загружено {len(selenium_processed_ids)} вакансий из истории браузера")
        print(f"[-] Всего исключается из поиска: {len(self.processed_vacancy_ids)} вакансий")
        print(f"Откликов за последние 24 часа: {self.applied_today}")

    def load_selenium_processed_vacancy_ids(self):
        """Возвращает ID вакансий, которые Selenium уже проходил любым конечным статусом."""
        try:
            with open(self.selenium_applied_vacancies_file, 'r', encoding='utf-8') as f:
                selenium_history = json.load(f)
        except FileNotFoundError:
            return set()
        except json.JSONDecodeError as e:
            log_problem("Не удалось прочитать историю откликов из браузера", e)
            return set()

        if not isinstance(selenium_history, dict):
            logging.error("История откликов из браузера повреждена — она будет собрана заново")
            return set()

        processed_ids = set()
        for vacancy_id, entry in selenium_history.items():
            if vacancy_id is None or not str(vacancy_id).strip():
                continue

            if isinstance(entry, dict) and entry.get('status') == STATUS_SKIPPED_FILTER:
                title_is_allowed, _ = validate_apply_title(
                    entry.get('name', ''),
                    allow_technical_fallback=getattr(self, 'allow_technical_fallback', True),
                )
                if title_is_allowed:
                    continue

            processed_ids.add(str(vacancy_id))

        return processed_ids

    def save_applied_vacancy(self, vacancy_id, count_as_new=True):
        """Запоминает вакансию как обработанную.

        count_as_new=False — для вакансий, на которые откликались РАНЬШЕ.
        Раньше они тоже увеличивали счётчик откликов за сутки, и дневная норма
        в 200 упиралась заметно раньше реальной.
        """
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.applied_vacancies[str(vacancy_id)] = timestamp
        self.processed_vacancy_ids.add(str(vacancy_id))
        if count_as_new:
            self.applied_today += 1
        
        try:
            write_json_atomic(self.applied_vacancies_file, self.applied_vacancies)
            
            # Сразу удаляем из кеша
            self.remove_from_cache(vacancy_id)
        except Exception as e:
            log_problem("Не удалось сохранить список отправленных откликов", e)

    def remove_from_cache(self, vacancy_id):
        """Удаляет вакансию из кеша"""
        try:
            if not os.path.exists(self.vacancies_cache_file):
                return
                
            with open(self.vacancies_cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # Фильтруем вакансии
            original_count = len(cache_data['vacancies'])
            cache_data['vacancies'] = [v for v in cache_data['vacancies'] 
                                      if str(v.get('id')) != str(vacancy_id)]
            
            if len(cache_data['vacancies']) < original_count:
                cache_data['total_count'] = len(cache_data['vacancies'])
                write_json_atomic(self.vacancies_cache_file, cache_data)
                logging.info(f"Вакансия {vacancy_id} удалена из кеша")
                
        except Exception as e:
            logging.debug(f"Ошибка удаления из кеша: {e}")

    def sync_cache_with_applied(self):
        """Синхронизирует кеш с файлом обработанных вакансий"""
        try:
            if not os.path.exists(self.vacancies_cache_file):
                return
                
            with open(self.vacancies_cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            original_count = len(cache_data['vacancies'])
            
            filtered_vacancies = []
            removed_count = 0
            
            for v in cache_data['vacancies']:
                v_id = str(v.get('id'))
                if v_id not in self.processed_vacancy_ids:
                    filtered_vacancies.append(v)
                else:
                    removed_count += 1
            
            if removed_count > 0:
                cache_data['vacancies'] = filtered_vacancies
                cache_data['total_count'] = len(cache_data['vacancies'])
                
                write_json_atomic(self.vacancies_cache_file, cache_data)
                
                print(f"Обновляю список вакансий: убрано {removed_count} уже обработанных")
                print(f" Осталось в списке: {len(filtered_vacancies)} вакансий")
            
        except Exception as e:
            log_problem("Не удалось обновить список вакансий", e)

    def save_vacancies_cache(self, vacancies, silent: bool = False):
        """Сохраняет найденные вакансии в кеш"""
        filtered_vacancies = []
        for v in vacancies:
            v_id = str(v.get('id'))
            if v_id not in self.processed_vacancy_ids:
                filtered_vacancies.append(v)

        # Оставляем для откликов только ИБ + разработку с ИБ (приоритеты 1..max_apply_priority)
        before_priority = len(filtered_vacancies)
        filtered_vacancies = [v for v in filtered_vacancies if self._is_apply_priority(v)]
        removed_by_priority = before_priority - len(filtered_vacancies)

        cache_data = {
            'timestamp': datetime.now().isoformat(),
            'vacancies': filtered_vacancies,
            'total_count': len(filtered_vacancies)
        }
        
        try:
            write_json_atomic(self.vacancies_cache_file, cache_data)
            if not silent:
                print(f"Сохранено {len(filtered_vacancies)} вакансий в список"
                      + (" (ИБ + разработка с ИБ)" if active_direction()[0] else ""))
                if removed_by_priority > 0:
                    print(f" [-] Исключено не-ИБ (чистая разработка / другое IT): {removed_by_priority}")
                already_processed = len(vacancies) - removed_by_priority - len(filtered_vacancies)
                if already_processed > 0:
                    print(f" (исключено обработанных: {already_processed})")
        except Exception as e:
            log_problem("Не удалось сохранить список вакансий", e)

    def load_vacancies_cache(self):
        """Загружает вакансии из кеша если он актуален И содержит достаточно вакансий"""
        try:
            with open(self.vacancies_cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            cache_time = datetime.fromisoformat(cache_data['timestamp'])
            age_hours = (datetime.now() - cache_time).total_seconds() / 3600
            
            if age_hours > self.cache_lifetime_hours:
                print(f"Список вакансий устарел: ему {age_hours:.1f} ч., обновляем каждые {self.cache_lifetime_hours} ч.")
                return None
            
            vacancies = cache_data['vacancies']
            
            filtered_vacancies = []
            excluded_count = 0
            for v in vacancies:
                v_id = str(v.get('id'))
                if v_id not in self.processed_vacancy_ids:
                    filtered_vacancies.append(v)
                else:
                    excluded_count += 1
            
            print(f"В сохранённом списке: {len(filtered_vacancies)} вакансий, где вы ещё не откликались")
            if excluded_count > 0:
                print(f" [ПРОПУСК] Исключено: {excluded_count} уже обработанных")
            print(f"Список обновлялся {age_hours:.1f} ч. назад")

            # Оставляем только ИБ + разработку с ИБ (приоритеты 1..max_apply_priority).
            # Если в кеше лежат не-ИБ вакансии — чистим файл на месте, сохраняя timestamp,
            # чтобы Selenium читал из vacancies_cache.json только релевантные вакансии.
            before_priority = len(filtered_vacancies)
            filtered_vacancies = [v for v in filtered_vacancies if self._is_apply_priority(v)]
            removed_by_priority = before_priority - len(filtered_vacancies)
            if removed_by_priority > 0:
                print(f" [-] Отфильтровано не-ИБ (чистая разработка / другое IT): {removed_by_priority}")
                try:
                    write_json_atomic(self.vacancies_cache_file, {
                        'timestamp': cache_data['timestamp'],
                        'vacancies': filtered_vacancies,
                        'total_count': len(filtered_vacancies),
                    })
                    print(f" Список очищен от не-ИБ вакансий ({len(filtered_vacancies)} осталось)")
                except Exception as e:
                    log_problem("Не удалось убрать из списка лишние вакансии", e)

            if len(filtered_vacancies) < self.min_vacancies_in_cache:
                print("[!] В сохранённом списке нет вакансий, где вы ещё не откликались")
                print("Требуется обновление поиска...")
                return None
            
            print(f"[OK] Список вакансий свежий, можно откликаться ({len(filtered_vacancies)} вакансий)")
            return filtered_vacancies
            
        except FileNotFoundError:
            print("Сохранённого списка вакансий нет — ищем заново")
            return None
        except Exception as e:
            log_problem("Не удалось прочитать сохранённый список вакансий", e)
            return None

    def clear_cache(self):
        """Очищает кеш вакансий"""
        try:
            if os.path.exists(self.vacancies_cache_file):
                os.remove(self.vacancies_cache_file)
                print("Список найденных вакансий очищен")
        except Exception as e:
            log_problem("Не удалось удалить список найденных вакансий", e)

    def load_token(self):
        try:
            with open(self.token_file, 'r', encoding='utf-8') as f:
                token_info = json.load(f)
                self.access_token = token_info.get('access_token')
                self.refresh_token = token_info.get('refresh_token')
                logging.info("Вход на hh.ru восстановлен")
                return True
        except FileNotFoundError:
            logging.info("Сохранённого входа на hh.ru нет — потребуется войти заново")
            return False

    def save_token(self, token_info):
        try:
            with open(self.token_file, 'w', encoding='utf-8') as f:
                json.dump(token_info, f, ensure_ascii=False, indent=2)
            logging.info("Вход на hh.ru сохранён")
        except Exception as e:
            log_problem("Не удалось сохранить вход на hh.ru", e)

    def load_app_token(self):
        try:
            with open(self.app_token_file, 'r', encoding='utf-8') as f:
                token_info = json.load(f)

            if token_info.get('client_id') != self.client_id:
                logging.info("Сохранённый доступ относится к другой настройке бота")
                return False

            self.app_access_token = token_info.get('access_token')
            if not self.app_access_token:
                logging.warning("Сохранённый доступ к hh.ru неполный — получаем заново")
                return False

            logging.info("Доступ к hh.ru взят из сохранённого")
            return True

        except FileNotFoundError:
            logging.info("Сохранённого доступа к hh.ru нет — получаем новый")
            return False
        except (json.JSONDecodeError, OSError) as e:
            log_problem("Не удалось прочитать сохранённый доступ к hh.ru", e)
            return False

    def save_app_token(self, token_info):
        app_token_info = dict(token_info)
        app_token_info['client_id'] = self.client_id
        app_token_info['created_at'] = datetime.now().isoformat()

        try:
            with open(self.app_token_file, 'w', encoding='utf-8') as f:
                json.dump(app_token_info, f, ensure_ascii=False, indent=2)
            logging.info("Доступ к hh.ru сохранён")
        except Exception as e:
            log_problem("Не удалось сохранить доступ к hh.ru", e)

    def clear_token(self):
        """Удаляет сохраненный токен"""
        try:
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
                logging.info("Сохранённый вход на hh.ru удалён")
        except Exception as e:
            log_problem("Не удалось удалить сохранённый вход на hh.ru", e)

    def get_authorization_url(self):
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'scope': 'resume vacancy_response'
        }
        
        auth_url = f"{self.oauth_url}/authorize?" + urlencode(params)
        return auth_url

    def authorize(self):
        print("\n[!] Требуется новая авторизация...")
        print("Прежний доступ к hh.ru будет сброшен.")
        
        self.clear_token()
        
        auth_url = self.get_authorization_url()
        print(f"\nОткройте эту ссылку в браузере:\n{auth_url}")
        webbrowser.open(auth_url)
        
        print("\n[!] ВАЖНО: после входа браузер откроет страницу с ошибкой — это нормально")
        print("Скопируйте ПОЛНЫЙ адрес из адресной строки браузера")
        callback_url = input("\nВставьте полный URL после авторизации: ").strip()
        
        parsed_url = urlparse(callback_url)
        query_params = parse_qs(parsed_url.query)
        
        if 'code' not in query_params:
            raise Exception("Код авторизации не найден в URL")
        
        auth_code = query_params['code'][0]
        return self.get_access_token(auth_code)

    def get_access_token(self, auth_code):
        token_data = {
            'grant_type': AUTHORIZATION_CODE_GRANT,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
            'code': auth_code
        }
        
        try:
            response = requests.post(
                f"{self.oauth_url}/token",
                data=token_data,
                headers=build_hh_headers(self.user_agent),
                timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            
            token_info = response.json()
            self.access_token = token_info['access_token']
            self.refresh_token = token_info.get('refresh_token')
            
            self.save_token(token_info)
            print("[OK] Авторизация успешна!")
            return True
            
        except requests.exceptions.RequestException as e:
            log_problem("Не удалось завершить вход на hh.ru", e)
            if hasattr(e, 'response') and e.response is not None:
                # Тело ответа сервера пользователю ничего не объясняет — только в журнал.
                log_detail(f"Ответ сервера при входе: {e.response.text}")
            return False

    def refresh_access_token(self):
        if not self.refresh_token:
            logging.warning("Продлить вход на hh.ru нечем — потребуется войти заново")
            return False
        
        token_data = {
            'grant_type': REFRESH_TOKEN_GRANT,
            'refresh_token': self.refresh_token
        }
        
        try:
            response = requests.post(
                f"{self.oauth_url}/token",
                data=token_data,
                headers=build_hh_headers(self.user_agent),
                timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            
            token_info = response.json()
            self.access_token = token_info['access_token']
            if 'refresh_token' in token_info:
                self.refresh_token = token_info['refresh_token']
            
            self.save_token(token_info)
            logging.info("Вход на hh.ru продлён")
            return True
            
        except requests.exceptions.RequestException as e:
            log_problem("Не удалось продлить вход на hh.ru", e)
            return False

    def get_application_access_token(self):
        if self.load_app_token():
            return True

        token_data = build_client_credentials_payload(self.client_id, self.client_secret)

        try:
            response = requests.post(
                TOKEN_URL,
                data=token_data,
                headers=build_hh_headers(self.user_agent),
                timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()

            token_info = response.json()
            self.app_access_token = token_info['access_token']
            self.save_app_token(token_info)
            logging.info("Доступ к hh.ru получен")
            return True

        except requests.exceptions.RequestException as e:
            log_problem("Не удалось получить доступ к hh.ru", e)
            if hasattr(e, 'response') and e.response is not None:
                log_detail(f"Ответ сервера при получении доступа: {e.response.text}")
                if APP_TOKEN_REFRESH_TOO_EARLY in e.response.text and self.load_app_token():
                    return True
            return False

    def ensure_token(self):
        self.api_available = True
        if not self.load_token():
            try:
                if not self.authorize():
                    logging.warning("Не удалось войти на hh.ru через браузер.")
                    print(f"\n{YELLOW}[!] Не удалось авторизоваться напрямую. Бот продолжит через браузер.{RESET}")
                    self.api_available = False
            except Exception as e:
                log_problem("Не удалось войти на hh.ru", e, logging.WARNING)
                print(f"\n{YELLOW}[!] Не удалось войти на hh.ru: {explain_error(e)}. Бот продолжит через браузер.{RESET}")
                self.api_available = False
        else:
            try:
                token_is_valid = self.test_token()
            except requests.exceptions.RequestException as e:
                self.api_available = False
                log_problem("Сайт hh.ru сейчас не отвечает, сохранённый вход оставлен без изменений", e, logging.WARNING)
                print(f"\n{YELLOW}[!] Сайт hh.ru сейчас не отвечает.{RESET}")
                print(f"{YELLOW}[!] Сохранённый вход оставлен без изменений. Бот продолжит через браузер.{RESET}\n")
                return

            if not token_is_valid:
                print("\n[!] Сохранённый вход на hh.ru больше не действует")
                try:
                    if not self.authorize():
                        logging.warning("Не удалось обновить доступ через браузер. Бот продолжит через браузер.")
                        print(f"{YELLOW}[!] Не удалось обновить доступ. Бот продолжит через браузер.{RESET}")
                        self.api_available = False
                except Exception as e:
                    log_problem("Не удалось обновить доступ к hh.ru", e, logging.WARNING)
                    self.api_available = False

    def test_token(self):
        """Проверяет валидность токена"""
        try:
            url = f"{self.base_url}/me"
            response = self.make_authenticated_request('GET', url)
            user_info = response.json()
            first_name = user_info.get('first_name', '')
            last_name = user_info.get('last_name', '')
            full_name = f"{first_name} {last_name}".strip()
            print(f"{GREEN}[OK]{RESET} Авторизован как: {BOLD}{full_name or 'Пользователь'}{RESET}")
            return True
        except requests.exceptions.HTTPError as e:
            log_problem("Сохранённый вход на hh.ru больше не действует", e)
            return False
        except requests.exceptions.RequestException as e:
            log_problem("Не удалось проверить вход на hh.ru", e, logging.WARNING)
            raise

    def make_request(self, method, url, authenticated=True, **kwargs):
        """Универсальный метод для запросов (с авторизацией или без)"""
        headers = build_hh_headers(
            self.user_agent,
            self.access_token if authenticated and self.access_token else None
        )
        headers.update(kwargs.get('headers', {}))
        kwargs['headers'] = headers
        
        retry_count = 0
        
        while retry_count < MAX_REQUEST_RETRIES:
            try:
                response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
                
                if response.status_code == 401:
                    logging.warning("Вход на hh.ru истёк — пробуем продлить")
                    if self.refresh_access_token():
                        headers['Authorization'] = f'Bearer {self.access_token}'
                        response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
                    else:
                        print("\n[!] Вход на hh.ru истёк, нужно войти заново")
                        if self.authorize():
                            headers['Authorization'] = f'Bearer {self.access_token}'
                            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
                        else:
                            raise Exception("Не удалось продлить вход на hh.ru")
                
                if response.status_code == 403:
                    file_handler.emit(logging.LogRecord(
                        name='hh_api', level=logging.DEBUG,
                        pathname=__file__, lineno=818,
                        msg=f"403 Forbidden для {url}. Ответ сервера: {response.text}",
                        args=(), exc_info=None
                    ))
                    error_msg = f"403 Forbidden: доступ к {url} запрещен"
                    http_error = requests.exceptions.HTTPError(error_msg)
                    http_error.response = response
                    raise http_error
                
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', self.delay_after_429))
                    print(f"Превышен лимит запросов. Ждем {retry_after} секунд...")
                    time.sleep(retry_after)
                    retry_count += 1
                    continue
                
                response.raise_for_status()
                return response
                
            except requests.exceptions.HTTPError as e:
                if e.response.status_code != 429:
                    raise
                retry_count += 1
                
            except requests.exceptions.RequestException as e:
                log_problem("Запрос к hh.ru не выполнен", e)
                log_detail(f"Адрес запроса: {method} {url}")
                raise
        
        raise Exception(f"Превышено количество попыток после 429 ошибки")

    def make_authenticated_request(self, method, url, **kwargs):
        """Запрос с авторизацией (для совместимости)"""
        return self.make_request(method, url, authenticated=True, **kwargs)

    def make_application_request(self, method, url, **kwargs):
        """Запрос с токеном приложения для методов, не требующих пользователя."""
        if not self.app_access_token and not self.get_application_access_token():
            raise RuntimeError("Не удалось получить доступ к hh.ru")

        headers = build_hh_headers(self.user_agent, self.app_access_token)
        headers.update(kwargs.pop('headers', {}))
        kwargs['headers'] = headers

        response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        if response.status_code == 401:
            self.app_access_token = None
            if not self.get_application_access_token():
                raise RuntimeError("Не удалось обновить доступ к hh.ru")
            kwargs['headers'] = build_hh_headers(self.user_agent, self.app_access_token)
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)

        if response.status_code == 403:
            file_handler.emit(logging.LogRecord(
                name='hh_api', level=logging.DEBUG,
                pathname=__file__, lineno=868,
                msg=f"403 Forbidden для app-token {url}. Ответ сервера: {response.text}",
                args=(), exc_info=None
            ))

        response.raise_for_status()
        return response

    def make_public_request(self, method, url, **kwargs):
        """Запрос без авторизации (публичный API)"""
        headers = build_hh_headers(self.user_agent)
        headers.update(kwargs.pop('headers', {}))
        
        try:
            # Используем прямой запрос без сессии (чтобы избежать cookies от авторизации)
            response = requests.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                log_detail(f"Ответ сервера: {e.response.text[:300]}")
            log_problem("Запрос к hh.ru не выполнен", e)
            log_detail(f"Адрес запроса: {method} {url}")
            raise

    def get_my_resumes(self):
        """Получает список резюме пользователя"""
        url = f"{self.base_url}/resumes/mine"
        
        try:
            response = self.make_authenticated_request('GET', url)
            data = response.json()
            resumes = data.get('items', [])
            
            print(f"\nВаши резюме:")
            for resume in resumes:
                resume_id = resume.get('id')
                title = resume.get('title', 'Без названия')
                status = resume.get('status', {}).get('name', 'Неизвестен')
                print(f" ID резюме: {resume_id}")
                print(f" Название: {title}")
                print(f" Статус: {status}")
                print(f" ---")
            
            return resumes
            
        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 403:
                print(f"\n{YELLOW}[!] hh.ru не отдаёт список резюме сторонним программам:{RESET}")
                print(f"    Это ограничение самого hh.ru. Бот продолжит работу с выбранным резюме.")
            else:
                log_problem("Не удалось получить список резюме", e)
            return None # None = не удалось проверить, но можно продолжить
        except Exception as e:
            log_problem("Не удалось получить список резюме", e)
            return None

    def whoami(self):
        """Выводит подробную информацию о текущем пользователе, резюме и откликах (по аналогии с s3rgeym/hh-applicant-tool)."""
        url = f"{self.base_url}/me"
        try:
            resp = self.make_authenticated_request('GET', url)
            data = resp.json()
            user_id = data.get('id', 'N/A')
            # hh отдает отсутствующее отчество как null, а не как пропущенный ключ,
            # поэтому .get(..., '') возвращает None и в ФИО печаталось «Иван Иванов None».
            first_name = data.get('first_name') or ''
            middle_name = data.get('middle_name') or ''
            last_name = data.get('last_name') or ''
            full_name = ' '.join(p for p in (last_name, first_name, middle_name) if p)
            email = data.get('email', 'N/A')
            phone = data.get('phone', 'N/A')
            counters = data.get('counters', {})

            print(f"\n{CYAN}{BOLD}{'='*60}{RESET}")
            print(f"{RED}{BOLD}                  ВАШ ПРОФИЛЬ НА HH.RU{RESET}")
            print(f"{CYAN}{BOLD}{'='*60}{RESET}")
            print(f"  Ваш номер на hh.ru: {YELLOW}{user_id}{RESET}")
            print(f"  ФИО:             {GREEN}{full_name or 'Не указано'}{RESET}")
            print(f"  Email:           {CYAN}{email}{RESET}")
            if phone and phone != 'N/A':
                print(f"  Телефон:         {CYAN}{phone}{RESET}")

            if counters:
                print(f"\n  {BOLD}Счётчики активности:{RESET}")
                for k, v in counters.items():
                    print(f"    • {COUNTER_LABELS.get(k, k)}: {v}")

            self.get_my_resumes()
            print(f"{CYAN}{BOLD}{'='*60}{RESET}\n")
        except Exception as e:
            print(f"\n{RED}[X] Не удалось загрузить ваш профиль с hh.ru.{RESET}")
            active_id, active_title = get_active_resume()
            print(f"  Целевое резюме: {CYAN}{active_title}{RESET} ({active_id})\n")

    def check_resume_status(self):
        """Проверяет статус резюме"""
        resumes = self.get_my_resumes()
        
        # Если API не дает доступ к резюме, продолжаем с указанным resume_id
        if resumes is None:
            _, active_title = get_active_resume()
            print(f"{GREEN}[OK] Бот использует целевое резюме:{RESET} {CYAN}{active_title}{RESET}")
            return True
        
        if not resumes:
            print("[X] Резюме не найдены")
            return False
        
        resume_found = False
        for resume in resumes:
            if resume.get('id') == self.resume_id:
                resume_found = True
                status = resume.get('status', {})
                status_id = status.get('id', 'unknown')
                status_name = status.get('name', 'Неизвестен')
                
                print(f"\n[OK] Резюме найдено!")
                print(f" Статус: {status_name}")
                
                if status_id != 'published':
                    print(f" [!] ВНИМАНИЕ: Резюме не опубликовано!")
                    return False
                
        if not resume_found:
            _, active_title = get_active_resume()
            print(f"\n[!] Резюме '{active_title}' не найдено в списке, но продолжаем...")

        # Автоматическая проверка и оптимизация опубликованных навыков через Selenium
        try:
            from resume_updater import HHResumeUpdater
            updater = HHResumeUpdater(resume_id=self.resume_id, headless=True)
            try:
                info = updater.get_current_resume_status()
                skills_count = info.get('skills_count', 0)
                if skills_count == 0:
                    print("[!] В резюме не активированы ключевые навыки! Автоматически оптимизирую...")
                    updater.activate_and_save_all_skills()
                else:
                    print(f"Ключевые навыки в резюме активны: {skills_count} навыков")
            finally:
                if getattr(updater, '_user_closed', False):
                    self._user_closed = True
                updater.close()
        except Exception as e:
            logging.debug(f"Пропуск авто-оптимизации резюме: {e}")

        if getattr(self, '_user_closed', False):
            return False

        return True # Продолжаем работу

    def get_my_applications(self):
        url = f"{self.base_url}/negotiations"
        
        try:
            response = self.make_authenticated_request('GET', url, params={'per_page': 5})
            data = response.json()
            
            applications = data.get('items', [])
            total = data.get('found', 0)
            self.api_negotiations_available = True
            
            print(f"\nВсего откликов: {total}")
            print(f"Последние отклики:")
            
            if not applications:
                print(" Отклики не найдены")
            else:
                for i, app in enumerate(applications[:5], 1):
                    vacancy = app.get('vacancy', {})
                    created_at = app.get('created_at', '')
                    
                    if created_at:
                        try:
                            date_obj = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            created_at = date_obj.strftime('%Y-%m-%d %H:%M')
                        except:
                            created_at = created_at[:16]
                    
                    print(f" {i}. {vacancy.get('name', 'Без названия')} - {created_at}")
            
            return total

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                self.api_negotiations_available = False
                print(f"{YELLOW}[!] hh.ru не разрешает сторонним программам отправлять отклики напрямую:{RESET}")
                print(f"    Это ограничение самого hh.ru, а не ошибка бота.")
                print(f"{GREEN}[OK] Бот переключается на отправку через браузер:{RESET}")
                print(f"    Отклики будут отправлены через ваш браузер, в котором вы вошли на hh.ru.")
                return None

            log_problem("Не удалось получить список откликов", e, logging.WARNING)
            return None
        except requests.exceptions.RequestException as e:
            # Флаг обязателен: без него проверка `api_negotiations_available is False`
            # не срабатывает, и бот уходит в API-цикл откликов, который только что
            # доказал свою недоступность — каждая вакансия падает в network_error,
            # а сообщение выше обещает Selenium, которого не будет.
            self.api_negotiations_available = False
            log_problem("Не удалось проверить отклики на hh.ru", e, logging.WARNING)
            print(f"{YELLOW}[!] Сайт hh.ru временно не отвечает. Отклики будут отправлены через браузер.{RESET}")
            return None
        except Exception as e:
            self.api_negotiations_available = False
            log_problem("Не удалось прочитать отклики", e, logging.WARNING)
            return None

    def get_vacancy_priority(self, vacancy):
        """Определяет приоритет вакансии (чем меньше число, тем выше приоритет)"""
        name = vacancy.get('name', '').lower()
        snippet = vacancy.get('snippet', {})
        requirement = (snippet.get('requirement') or '').lower()
        responsibility = (snippet.get('responsibility') or '').lower()
        full_text = f"{name} {requirement} {responsibility}"

        # ПРИОРИТЕТ 1 - Чистая кибербезопасность и пентестинг
        priority_1_keywords = [
            'пентест', 'pentest', 'penetration test',
            'ethical hacker', 'этичный хакер', 'white hat',
            'bug bounty', 'vulnerability researcher',
            'red team', 'offensive security',
            'security researcher', 'исследователь безопасности',
            'exploit', 'zero day', '0day',
            'кибербезопасность', 'cybersecurity', 'cyber security'
        ]

        # ПРИОРИТЕТ 2 - Информационная безопасность
        priority_2_keywords = [
            'информационная безопасность', 'информационной безопасности',
            'information security', 'infosec', 'it security',
            'security analyst', 'security engineer', 'security architect',
            'безопасность приложений', 'application security', 'appsec',
            'soc analyst', 'soc engineer', 'security operations',
            'incident response', 'threat intelligence', 'threat hunting',
            'malware analyst', 'reverse engineer', 'forensics'
        ]

        # ПРИОРИТЕТ 3 - Специализированная ИБ
        priority_3_keywords = [
            'siem', 'dlp', 'waf', 'ids', 'ips', 'edr', 'xdr',
            'devsecops', 'secops', 'security automation',
            'cloud security', 'network security', 'web security',
            'mobile security', 'iot security',
            'blue team', 'purple team',
            'security audit', 'security compliance', 'grc',
            'iso 27001', 'pci dss', 'gdpr'
        ]

        # ПРИОРИТЕТ 4 - Защита данных и крипто
        priority_4_keywords = [
            'защита информации', 'защита данных',
            'криптограф', 'шифрован', 'crypto',
            'blockchain security', 'smart contract audit',
            'фстэк', 'скзи', 'pki',
            'data protection', 'privacy engineer'
        ]

        # Проверяем приоритеты
        for keyword in priority_1_keywords:
            if keyword in name or keyword in full_text:
                return 1

        for keyword in priority_2_keywords:
            if keyword in name or keyword in full_text:
                return 2

        for keyword in priority_3_keywords:
            if keyword in name or keyword in full_text:
                return 3

        for keyword in priority_4_keywords:
            if keyword in name or keyword in full_text:
                return 4

        # ПРИОРИТЕТ 5 - Разработка с безопасностью
        if any(kw in full_text for kw in ['secure', 'security', 'безопасн']) and \
           any(kw in full_text for kw in ['developer', 'разработчик', 'python', 'javascript']):
            return 5

        # ПРИОРИТЕТ 6 - Чистая разработка
        if any(kw in full_text for kw in ['developer', 'разработчик', 'программист', 'python', 'javascript']):
            return 6

        # ПРИОРИТЕТ 7 - Остальное IT
        return 7

    def is_vacancy_suitable(self, vacancy):
        """Проверяет, стоит ли отдавать вакансию Selenium-отклику."""
        name = vacancy.get('name', '').lower()
        snippet = vacancy.get('snippet', {})
        requirement = (snippet.get('requirement') or '').lower()
        responsibility = (snippet.get('responsibility') or '').lower()
        full_text = f"{name} {requirement} {responsibility}"

        hard_exclusions = (
            'техника безопасности', 'охрана труда', 'от и тб',
            'промышленная безопасность', 'пожарная безопасность',
            'радиационная безопасность', 'экологическая безопасность',
            'транспортная безопасность', 'физическая охрана',
            'охранник', 'вахтер', 'сторож', 'контролер кпп',
            'инженер-конструктор', 'инженер кипиа', 'асутп',
            'инженер-механик', 'инженер-электрик',
            'инженер-строитель', 'инженер-технолог',
            'главный инженер карьера', 'главный инженер завода',
            'инженер по ремонту', 'инженер по наладке',
            'инженер технического надзора',
            'менеджер по продажам', 'торговый представитель',
            'кассир', 'продавец', 'водитель', 'курьер',
            'повар', 'официант', 'бармен', 'уборщица'
        )

        for exclusion in hard_exclusions:
            if exclusion in full_text:
                return False

        title_is_allowed, _ = validate_apply_title(
            name,
            allow_technical_fallback=getattr(self, 'allow_technical_fallback', True),
        )
        return title_is_allowed

    def _is_apply_priority(self, vacancy):
        """True, если вакансия входит в целевые приоритеты."""
        try:
            preset = get_active_preset()
            if preset.get('id', 'security') != 'security':
                return True
        except Exception:
            pass
        max_priority = getattr(self, 'max_apply_priority', 5)
        return self.get_vacancy_priority(vacancy) <= max_priority

    def get_vacancies(self, search_params):
        """Поиск с приоритетом на кибербезопасность"""
        
        # Синхронизация и проверка кеша
        self.sync_cache_with_applied()
        
        cached_vacancies = self.load_vacancies_cache()
        
        # Используем кеш, если в нем есть вакансии
        if cached_vacancies:
            print("Беру вакансии из сохранённого списка")
            print(f"[OK] Подходящих вакансий в списке: {len(cached_vacancies)}")
            # Сортируем кешированные вакансии по приоритету
            cached_vacancies.sort(key=lambda v: self.get_vacancy_priority(v))
            return cached_vacancies
        else:
            print("Выполняется новый поиск вакансий...")
        
        all_vacancies = []
        processed_ids = set()
        excluded_with_tests = 0
        excluded_already_applied = 0
        excluded_not_suitable = 0
        network_errors_without_results = 0
        stop_search_reason = None
        
        try:
            preset = get_active_preset()
        except Exception:
            preset = {'id': 'security', 'name': 'Информационная безопасность / Пентест'}

        preset_id = preset.get('id', 'security')

        if preset_id == 'security':
            # ПРИОРИТЕТНЫЕ запросы по кибербезопасности
            search_queries = [
                # === ТОПОВЫЕ ЗАПРОСЫ ПО КИБЕРБЕЗОПАСНОСТИ ===
                '"пентестер"',
                '"pentester"',
                '"penetration tester"',
                '"ethical hacker"',
                '"security researcher"',
                '"bug bounty"',
                '"red team"',
                '"offensive security"',
                '"vulnerability researcher"',
                '"exploit developer"',

                '"кибербезопасность"',
                '"cybersecurity"',
                '"cyber security"',
                '"информационная безопасность"',
                '"information security"',
                '"security analyst"',
                '"security engineer"',
                '"security architect"',
                '"security specialist"',

                '"SOC analyst"',
                '"SOC engineer"',
                '"SIEM administrator"',
                '"incident response"',
                '"threat intelligence"',
                '"threat hunting"',
                '"malware analyst"',
                '"reverse engineer"',
                '"forensics analyst"',

                '"application security"',
                '"appsec engineer"',
                '"devsecops"',
                '"security operations"',
                '"blue team"',
                '"purple team"',

                # === РАСШИРЕННЫЕ ЗАПРОСЫ ПО ИБ ===
                'пентест',
                'pentest',
                'penetration testing',
                'ethical hacking',
                'vulnerability assessment',
                'security testing',
                'security audit',

                'кибербезопасность',
                'cybersecurity',
                'информационная безопасность',
                'information security',
                'IT security',
                'security operations center',

                'SIEM SOAR',
                'XDR EDR MDR',
                'DLP WAF IDS IPS',
                'incident management',
                'security monitoring',

                'cloud security',
                'network security',
                'web application security',
                'mobile security',
                'endpoint security',

                'защита информации',
                'защита данных',
                'безопасность приложений',
                'безопасность инфраструктуры',

                'криптография',
                'СКЗИ ФСТЭК',
                'compliance security',
                'GRC analyst',
                'ISO 27001',
                'PCI DSS',

                # === РАЗРАБОТКА С БЕЗОПАСНОСТЬЮ ===
                'security developer',
                'secure coding',
                'security engineer developer',
                'python security',
                'security automation',

                # === СПЕЦИФИЧНЫЕ РОЛИ ===
                'DevSecOps engineer',
                'AppSec engineer',
                'Cloud Security Architect',
                'Zero Trust Architect',
                'Blockchain Security',
                'IoT Security',
                'OT Security',
                'ICS Security',
                'SCADA Security'
            ]
        else:
            search_queries = preset.get('queries', [])
            if not search_queries and preset.get('custom_query'):
                search_queries = [f'"{preset["custom_query"]}"']
            if not search_queries:
                search_queries = ['"разработчик"']

        url = f"{self.base_url}/vacancies"
        total_queries = len(search_queries)
        
        cfg = load_config()
        search_area = str(cfg.get('search_area', '') or '').strip()
        if not search_area or search_area.lower() in ('all', 'world', '0', 'none'):
            area_param = None
            area_label = "Весь мир (без ограничений)"
        elif search_area == '113':
            area_param = '113'
            area_label = "Россия"
        else:
            area_param = search_area
            area_label = f"Регион ID: {search_area}"

        print(f"\nПараметры поиска:")
        print(f" ПРИОРИТЕТ: {preset.get('name', 'Пользовательский')}")
        print(f" • Регион: {area_label}")
        print(f" • Запросов: {total_queries}")
        
        last_saved_count = 0
        try:
            for query_idx, query in enumerate(search_queries, 1):
                if stop_search_reason:
                    break

                if len(all_vacancies) >= SEARCH_RESULT_LIMIT:
                    break
                
                clean_query = query.replace('"', '')
                print(f"[{query_idx}/{total_queries}] {clean_query[:40]}... | Найдено"
                      f"{' ИБ' if preset_id == 'security' else ''}: {len(all_vacancies)}")
                
                for page in range(SEARCH_PAGE_LIMIT):
                    params = {
                        'text': query,
                        'per_page': 100,
                        'page': page,
                        # Сортировка по дате публикации: свежие вакансии идут первыми и
                        # гарантированно попадают в первые страницы (иначе при сортировке
                        # по релевантности новые ИБ тонут за пределами лимита страниц).
                        'order_by': 'publication_time',
                    }
                    if area_param is not None:
                        params['area'] = area_param
                    
                    try:
                        response = self.make_application_request('GET', url, params=params)
                        data = response.json()
                        network_errors_without_results = 0
                        vacancies = data.get('items', [])
                        
                        if not vacancies:
                            break
                        
                        added_count = 0
                        
                        for v in vacancies:
                            v_id = str(v.get('id'))
                            
                            if not v_id or v_id in processed_ids:
                                continue
                            
                            processed_ids.add(v_id)
                            
                            if v_id in self.processed_vacancy_ids:
                                excluded_already_applied += 1
                                continue
                            
                            # Проверка соответствия (с приоритетом на ИБ)
                            if not self.is_vacancy_suitable(v):
                                excluded_not_suitable += 1
                                continue
                            
                            if v.get('has_test', False):
                                excluded_with_tests += 1
                                continue
                            
                            all_vacancies.append(v)
                            added_count += 1
                        
                        if added_count > 0:
                            print(f" Страница {page + 1}: +{added_count} "
                                  f"{'ИБ вакансий' if preset_id == 'security' else 'подходящих'}")
                        
                        pages_total = data.get('pages', 0)
                        if page + 1 >= pages_total:
                            break
                        
                        time.sleep(0.2) # Маленькая задержка
                        
                    except requests.exceptions.HTTPError as e:
                        if e.response is not None and e.response.status_code == 403:
                            if all_vacancies:
                                stop_search_reason = "hh.ru прекратил выдачу вакансий, использую уже найденные"
                                print(f"\n[!] {stop_search_reason}: {len(all_vacancies)}")
                                log_detail(f"Поиск запрещён после частичной выдачи: {e.response.text[:300]}")
                                break

                            print("\n[X] hh.ru закрыл поиск вакансий для сторонних программ")
                            print(" Это ограничение самого сайта, вашей сети или интернет-провайдера.")
                            print(" Без поиска бот не сможет найти вакансии и отправить отклики.")
                            log_detail(f"Поиск вакансий запрещён сервером: {e.response.text[:300]}")
                            return []
                        log_problem(f"Не удалось выполнить поиск по запросу «{query}»", e)
                        break
                    except requests.exceptions.RequestException as e:
                        file_handler.emit(logging.LogRecord(
                            name='hh_api', level=logging.DEBUG,
                            pathname=__file__, lineno=1400,
                            msg=f"Сетевая ошибка поиска {query}: {e}",
                            args=(), exc_info=None
                        ))
                        if all_vacancies:
                            stop_search_reason = (
                                f"Сайт hh.ru взял паузу, использую найденные {len(all_vacancies)} вакансий"
                            )
                            print(f" [!] {stop_search_reason}")
                            break

                        network_errors_without_results += 1
                        if network_errors_without_results >= MAX_SEARCH_NETWORK_ERRORS_WITHOUT_RESULTS:
                            stop_search_reason = (
                                "Сайт hh.ru временно не отвечает, попробуйте позже"
                            )
                            print(f" [!] {stop_search_reason}")
                            break

                        break
                    except Exception as e:
                        log_problem(f"Не удалось выполнить поиск по запросу «{query}»", e)
                        break

                # Инкрементальное автосохранение кеша после каждого запроса
                if len(all_vacancies) > last_saved_count:
                    self.save_vacancies_cache(all_vacancies, silent=True)
                    last_saved_count = len(all_vacancies)

        except KeyboardInterrupt:
            print(f"\n[!] Поиск остановлен пользователем (Ctrl+C). Сохранено найденных вакансий: {len(all_vacancies)}")
            stop_search_reason = "Поиск остановлен пользователем (Ctrl+C)"
        
        # СОРТИРОВКА ПО ПРИОРИТЕТУ (кибербезопасность первая)
        all_vacancies.sort(key=lambda v: self.get_vacancy_priority(v))
        
        print(f"\nИТОГИ ПОИСКА ({preset.get('name', 'ВАКАНСИИ')}):")
        if stop_search_reason:
            print(f" [!] Поиск остановлен досрочно: {stop_search_reason}")
        print(f" Найдено подходящих вакансий: {len(all_vacancies)}")
        print(f" [ПРОПУСК] С тестами: {excluded_with_tests}")
        print(f" Уже обработано: {excluded_already_applied}")
        print(f" [-] Не подходят: {excluded_not_suitable}")
        print(f" Всего проверено: {len(processed_ids)}")
        
        if all_vacancies and preset_id == 'security':
            # Подсчет по приоритетам
            priority_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0}
            for v in all_vacancies:
                priority = self.get_vacancy_priority(v)
                priority_counts[priority] = priority_counts.get(priority, 0) + 1

            print(f"\nРаспределение по приоритетам:")
            priority_names = {
                1: "[P1] Пентестинг и Red Team",
                2: "[P2] Информационная безопасность",
                3: "[P3] Специализированная ИБ",
                4: "[P4] Защита данных и крипто",
                5: "[P5] Разработка с безопасностью",
                6: "[P6] Чистая разработка",
                7: "[P7] Другое IT"
            }

            for priority in sorted(priority_counts.keys()):
                if priority_counts[priority] > 0:
                    print(f" {priority_names.get(priority, f'Приоритет {priority}')}: {priority_counts[priority]}")

            # Для откликов оставляем только ИБ + разработку с ИБ (приоритеты 1..max_apply_priority)
            target_vacancies = [v for v in all_vacancies if self._is_apply_priority(v)]
            dropped = len(all_vacancies) - len(target_vacancies)
            print(f"\n[OK] К отклику" + (" (ИБ + разработка с ИБ)" if preset_id == 'security' else "")
                  + f": {len(target_vacancies)}")
            if dropped > 0:
                print(f" [-] Исключено не-ИБ (чистая разработка / другое IT): {dropped}")
            all_vacancies = target_vacancies

            self.save_vacancies_cache(all_vacancies)
        else:
            print(f"\n[!] Не найдено подходящих вакансий по направлению «{preset.get('name', '')}»")

        return all_vacancies

    def generate_cover_letter(self, vacancy_details):
        """Сопроводительное письмо с акцентом на кибербезопасность"""
        position_name = vacancy_details.get('name', 'данную позицию')
        company_name = vacancy_details.get('employer', {}).get('name', 'вашей компании')

        # Определяем тип вакансии
        name_lower = position_name.lower()

        # Для пентестинга
        if any(kw in name_lower for kw in ['пентест', 'pentest', 'ethical hack', 'red team']):
            templates = [
                f"""Здравствуйте!

Заинтересовала позиция "{position_name}" в {company_name}.

Имею опыт в проведении тестирования на проникновение и поиске уязвимостей.
Готов применить свои навыки для повышения уровня защищенности инфраструктуры компании.

С уважением!""",

                f"""Добрый день!

Позиция "{position_name}" полностью соответствует моей специализации.

Готов проводить комплексное тестирование безопасности и помогать в устранении выявленных уязвимостей.

Буду рад обсудить детали!"""
            ]
        # Для кибербезопасности
        elif any(kw in name_lower for kw in ['безопасност', 'security', 'soc', 'siem']):
            templates = [
                f"""Здравствуйте!

С интересом рассмотрел вакансию "{position_name}" в {company_name}.

Специализируюсь на информационной безопасности и готов внести вклад в защиту цифровых активов компании.

С уважением!""",

                f"""Добрый день!

Позиция "{position_name}" соответствует моему опыту в области кибербезопасности.

Готов применить свои знания для обеспечения надежной защиты информационной инфраструктуры {company_name}.

Благодарю за рассмотрение!"""
            ]
        # Для разработки
        elif any(kw in name_lower for kw in ['developer', 'разработчик', 'программист']):
            templates = [
                f"""Здравствуйте!

Заинтересовала позиция "{position_name}" в {company_name}.

Имею опыт разработки с акцентом на безопасность кода и защищенность приложений.

С уважением!""",

                f"""Добрый день!

Рассматриваю вакансию "{position_name}" как возможность применить навыки безопасной разработки.

Готов создавать качественные и защищенные решения для {company_name}.

Буду рад сотрудничеству!"""
            ]
        else:
            templates = [
                f"""Здравствуйте!

Заинтересовала позиция "{position_name}" в {company_name}.

Мой опыт в IT и информационной безопасности позволит эффективно решать поставленные задачи.

С уважением!"""
            ]

        return random.choice(templates)

    def apply_to_vacancy(self, vacancy_id, cover_letter=None):
        """Отправка отклика на вакансию"""
        if (load_config().get('ai_filter') or {}).get('mode', 'off') != 'off':
            # All automated filtering runs in the shared browser apply path.
            return False, 'browser_required', None
        url = f"{self.base_url}/negotiations"
        
        # Данные для отклика
        form_data = {
            'vacancy_id': str(vacancy_id),
            'resume_id': self.resume_id
        }
        
        if cover_letter:
            form_data['message'] = cover_letter
        
        try:
            # Не передаём дополнительные headers - make_authenticated_request сам добавит нужные
            response = self.make_authenticated_request('POST', url, data=form_data)
            
            # Принимаем и 200, и 201 как успешные
            if response.status_code in [200, 201]:
                self.save_applied_vacancy(vacancy_id)
                return True, "success", "ok"
            else:
                # Код и тело ответа — только в журнал. Пользователю уходит код
                # 'server_error': его перевод есть в db_manager.STATUS_LABELS,
                # а unexpected_code_502 попадал в статистику как есть.
                log_detail(f"Неожиданный код ответа {response.status_code}: {response.text[:300]}")
                return False, "server_error", None
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                try:
                    error_data = e.response.json()
                    errors = error_data.get('errors', [])
                    if errors and errors[0].get('value') == 'test_required':
                        return False, "test_required", None
                    elif errors and errors[0].get('value') == 'already_applied':
                        # Отклик был раньше — запоминаем, но сегодняшним не считаем.
                        self.save_applied_vacancy(vacancy_id, count_as_new=False)
                        return False, "already_applied", None
                    elif errors and errors[0].get('value') == 'application_denied':
                        return False, "application_denied", None
                except (ValueError, KeyError) as parse_error:
                    log_detail(f"Не удалось разобрать ответ 403 при отклике: {parse_error!r}")
                return False, "forbidden", None
                
            elif e.response.status_code == 400:
                try:
                    error_data = e.response.json()
                    description = error_data.get('description', '')
                    if 'Daily negotiations limit is exceeded' in description:
                        return False, "daily_limit_exceeded", None
                except (ValueError, KeyError) as parse_error:
                    log_detail(f"Не удалось разобрать ответ 400 при отклике: {parse_error!r}")
                return False, "bad_request", None
                
            else:
                # Тот же единый код: http_error_403 в статистике был нечитаем.
                log_detail(f"Отклик отклонён кодом {e.response.status_code}")
                return False, "server_error", None
                
        except Exception as e:
            log_problem(f"Не удалось отправить отклик на вакансию {vacancy_id}", e)
            return False, "network_error", None

    def run_manual_applications(self, search_params, limit=DEFAULT_MANUAL_APPLY_LIMIT):
        print(f"\n{'='*70}")
        print("РУЧНЫЕ ОТКЛИКИ ЧЕРЕЗ БРАУЗЕР")
        print(f"{'='*70}")

        vacancies = self.get_vacancies(search_params)
        if not vacancies:
            print("\n[X] Нет вакансий для ручного отклика")
            return

        opened_count = 0
        for vacancy in vacancies[:limit]:
            vacancy_id = vacancy.get('id')
            vacancy_name = vacancy.get('name', 'Без названия')
            employer = vacancy.get('employer', {}).get('name', 'Неизвестно')
            apply_url = get_vacancy_apply_url(vacancy)

            if not vacancy_id or not apply_url:
                print(f"\n[!] Пропуск: нет ссылки для отклика: {vacancy_name}")
                continue

            cover_letter = self.generate_cover_letter(vacancy)
            opened_count += 1

            print(f"\n[{opened_count}/{min(limit, len(vacancies))}] {vacancy_name}")
            print(f"{employer}")
            print(f"{apply_url}")
            print("\nПисьмо:")
            print("-" * 50)
            print(cover_letter)
            print("-" * 50)

            webbrowser.open(apply_url)

            answer = input("Отклик отправлен? [y/N/q]: ").strip().lower()
            if answer == 'q':
                print("[СТОП] Остановлено")
                break
            if answer == 'y':
                self.save_applied_vacancy(vacancy_id)
                print("[OK] Отмечено как обработанное")
            else:
                print("[ПРОПУСК] Не отмечено обработанным")

        print(f"\nОткрыто вакансий: {opened_count}")

    def run_selenium_api_cache(self, limit=DEFAULT_SELENIUM_APPLY_LIMIT):
        selenium_script = os.path.join(CODE_DIR, 'hh_selenium.py')
        if not os.path.exists(selenium_script):
            raise FileNotFoundError(f"Не найден модуль отправки через браузер: {selenium_script}")

        command = [
            sys.executable,
            selenium_script,
            '--api-cache',
            '--limit',
            str(limit),
        ]
        if getattr(self, 'selenium_headless', False):
            command.append('--headless')

        # Показываем не голый лимит, а остаток и когда освободится место:
        # иначе непонятно, сколько ещё можно отправить и чего ждать.
        sent_24h = count_recent_timestamps(
            list(self.applied_vacancies.values()), APPLICATION_LIMIT_WINDOW_HOURS)
        left = max(0, limit - sent_24h)
        free_at = next_slot_free_at(
            list(self.applied_vacancies.values()), APPLICATION_LIMIT_WINDOW_HOURS)

        print(f"\n{CYAN}{'='*70}{RESET}")
        print(f"{CYAN}{BOLD}Прямая отправка закрыта. Отправляю отклики через браузер.{RESET}")
        print(f"   За последние сутки отправлено: {GREEN}{sent_24h}{RESET} из {GREEN}{limit}{RESET}"
              f" — осталось {GREEN}{left}{RESET}")
        if free_at is not None:
            when = describe_time_left(free_at)
            if left:
                print(f"   {DIM}Счётчик скользящий: первое место освободится {when}{RESET}")
            else:
                print(f"   {YELLOW}Лимит исчерпан. Первое место освободится {when}{RESET}")
        print(f"{CYAN}{'='*70}{RESET}\n")

        completed_process = subprocess.run(command, cwd=SCRIPT_DIR)
        if completed_process.returncode != 0:
            raise RuntimeError(f"Отправка через браузер завершилась с кодом {completed_process.returncode}")

    def run_auto_applications(self, search_params):
        print(f"\n{CYAN}{BOLD}{'='*70}{RESET}")
        print(f"{MAGENTA}{BOLD}ПОЛНЫЙ ЦИКЛ: РАЗБОР ОТКАЗОВ -> ПРАВКА РЕЗЮМЕ -> ПОДНЯТИЕ -> ПОИСК -> ОТКЛИКИ{RESET}")
        print(f"{CYAN}{BOLD}{'='*70}{RESET}")

        # ЭТАП 1: Глубокий разбор чатов с отказами и адаптация резюме/ответов
        if not any(arg in sys.argv for arg in ['--no-chat-audit', '--no-chat', '--skip-chat']):
            print(f"\n{CYAN}{BOLD}[1/5] РАЗБОР ПЕРЕПИСКИ С ОТКАЗАМИ И ПРАВКА РЕЗЮМЕ...{RESET}")
            print(f"  {DIM}Проверяются только отказы. Приглашения и собеседования не затрагиваются.{RESET}")
            try:
                from rejection_analyzer import RejectionAnalyzer, auto_apply_resume_enabled, analysis_headless_enabled
                analyzer = RejectionAnalyzer()
                analyzer.headless = analysis_headless_enabled(analyzer.config, sys.argv)
                # Флаг или настройка auto_apply_resume (по умолчанию включена): правка
                # резюме без вопроса, но только навыками, которые есть в профиле.
                auto_apply_skills = auto_apply_resume_enabled(analyzer.config, sys.argv)
                analyzer.auto_apply_skills = auto_apply_skills
                chat_limit = 0
                for arg in sys.argv:
                    if arg.startswith('--chat-limit='):
                        try:
                            chat_limit = int(arg.split('=')[1])
                        except Exception:
                            pass
                if '--all-chats' in sys.argv or '--all-rejections' in sys.argv:
                    chat_limit = 0
                try:
                    res = analyzer.run_chat_analysis(limit=chat_limit, use_mock_if_empty=False, fetch_live=True, auto_apply=auto_apply_skills)
                    if (isinstance(res, dict) and res.get('status') == 'user_closed') or getattr(analyzer, '_user_closed', False):
                        self._user_closed = True
                finally:
                    if getattr(analyzer, '_user_closed', False):
                        self._user_closed = True
                    analyzer.close()
                    time.sleep(1.0)

                if getattr(self, '_user_closed', False) or getattr(analyzer, '_user_closed', False):
                    print(f"\n{RED}[СТОП] Пользователь закрыл браузер. Выполнение прервано.{RESET}\n")
                    return
            except Exception as e:
                log_problem("Этап разбора переписки пропущен", e, logging.WARNING)

        if getattr(self, '_user_closed', False):
            print(f"\n{RED}[СТОП] Пользователь закрыл браузер. Выполнение прервано.{RESET}\n")
            return

        # ЭТАП 2: Синхронизация кеша при запуске
        print(f"\n{CYAN}{BOLD}[2/5] ОБНОВЛЕНИЕ СПИСКА ВАКАНСИЙ И ПРОВЕРКА РЕЗЮМЕ...{RESET}")
        self.sync_cache_with_applied()
        
        print(f"\n{CYAN}Проверка резюме...{RESET}")
        if not self.check_resume_status():
            if getattr(self, '_user_closed', False):
                print(f"\n{RED}[СТОП] Пользователь закрыл браузер. Выполнение прервано.{RESET}\n")
            else:
                print(f"{RED}[X] Проблема с резюме!{RESET}")
            return

        if getattr(self, '_user_closed', False):
            print(f"\n{RED}[СТОП] Пользователь закрыл браузер. Выполнение прервано.{RESET}\n")
            return
        
        print(f"\n{CYAN}Проверка существующих откликов...{RESET}")
        total_applications = self.get_my_applications()
        
        # ЭТАП 3: Поднятие резюме в поиске.
        # Стоит после адаптации и проверки резюме: дата публикации должна
        # обновиться уже с исправленным текстом и активными навыками. И до
        # поиска — пока идут отклики, резюме уже висит наверху выдачи.
        if not any(arg in sys.argv for arg in ['--no-bump', '--skip-bump']):
            print(f"\n{CYAN}{BOLD}[3/5] ПОДНЯТИЕ РЕЗЮМЕ В ПОИСКЕ...{RESET}")
            print(f"  {DIM}Бесплатное поднятие доступно раз в 4 часа. Кулдаун — норма, цикл продолжится.{RESET}")
            try:
                from resume_updater import HHResumeUpdater
                from rejection_analyzer import analysis_headless_enabled
                from config_manager import load_config
                # Профиль Chrome один на всех, два драйвера на нём дерутся,
                # поэтому свой драйвер закрываем здесь же, до следующего этапа.
                updater = HHResumeUpdater(resume_id=self.resume_id,
                                          headless=analysis_headless_enabled(load_config(), sys.argv))
                try:
                    promo = updater.promote_resume()
                    if promo.get('bumped'):
                        print(f"{GREEN}[OK]{RESET} Резюме поднято в поиске. {promo.get('bump_message', '')}")
                    else:
                        print(f"{YELLOW}[~]{RESET} Резюме не поднято: {promo.get('bump_message') or 'причина не указана'}")
                        if promo.get('next_bump_at'):
                            print(f"  {DIM}Следующее поднятие будет доступно в {promo['next_bump_at']}{RESET}")
                    if promo.get('visible') is False:
                        print(f"{YELLOW}[!]{RESET} Резюме скрыто от работодателей — поднятие не поможет, откройте видимость на hh.ru")
                    if promo.get('gaps'):
                        print(f"{YELLOW}[!]{RESET} Пустые блоки резюме: {', '.join(promo['gaps'])}")
                finally:
                    if getattr(updater, '_user_closed', False):
                        self._user_closed = True
                    updater.close()
                    time.sleep(1.0)
            except Exception as e:
                # Кулдаун, недоступная кнопка, упавший драйвер — не повод ронять цикл.
                log_problem("Этап поднятия резюме пропущен", e, logging.WARNING)
                print(f"{YELLOW}[~]{RESET} Этап поднятия пропущен: {explain_error(e)}")

        if getattr(self, '_user_closed', False):
            print(f"\n{RED}[СТОП] Пользователь закрыл браузер. Выполнение прервано.{RESET}\n")
            return

        # ЭТАП 4: Поиск вакансий
        print(f"\n{CYAN}{BOLD}[4/5] ПОИСК И АКТУАЛИЗАЦИЯ ВАКАНСИЙ: {active_direction()[1]}...{RESET}")
        
        vacancies = self.get_vacancies(search_params)
        
        if not vacancies:
            print(f"\n{RED}[X] Подходящие вакансии не найдены ({active_direction()[1]}){RESET}")
            print("\nРекомендации:")
            print(" 1. Подождите несколько часов - появятся новые вакансии")
            # Пользователь ходит через меню, а не через параметры командной строки.
            print(" 2. Очистите список найденных вакансий в меню (пункт «c») и запустите поиск заново")
            return
        
        print(f"\n{GREEN}[OK]{RESET} К обработке: {BOLD}{len(vacancies)}{RESET} вакансий (отсортированы по приоритету)")
        print(f"Откликов за последние 24 часа: {GREEN}{self.applied_today}{RESET}/{self.max_applications_per_day}")
        
        print(f"\n{CYAN}{BOLD}ТОП вакансии (первые будут обработаны):{RESET}")
        for i, vacancy in enumerate(vacancies[:15], 1):
            employer = vacancy.get('employer', {}).get('name', 'Неизвестно')
            vacancy_name = vacancy.get('name', 'Без названия')
            priority = self.get_vacancy_priority(vacancy)
            p_badge = c_priority(priority)
            print(f" {i:2d}. {p_badge} {c_title(vacancy_name)}")
            print(f"      {c_company(employer)}")
        
        if len(vacancies) > 15:
            print(f"   ... и еще {len(vacancies) - 15} вакансий")

        # Режим браузера, выбранный перед циклом, действует и на отклики.
        if '--headless' in sys.argv:
            self.selenium_headless = True

        # Интерактивное подтверждение перед отправкой откликов
        auto_flags = {'--auto', '--yes', '-y'}
        if not getattr(self, 'auto_confirm', False) and not any(arg in sys.argv for arg in auto_flags):
            print(f"\n{CYAN}{BOLD}{'='*70}{RESET}")
            print(f"{CYAN}{BOLD}ПОДТВЕРЖДЕНИЕ ЗАПУСКА ОТКЛИКОВ{RESET}")
            print(f"{CYAN}{BOLD}{'='*70}{RESET}")
            print(f"   {GREEN}[Enter] / [1]{RESET} - Запустить браузер и начать отклики")
            print(f"   {CYAN}[2]{RESET}           - Запустить в фоне, без окна браузера")
            print(f"   {YELLOW}[P]{RESET}           - Запустить сразу в режиме Паузы")
            print(f"   {RED}[Q] / [0]{RESET}     - Отмена / Выход")
            print(f"{CYAN}{BOLD}{'-'*70}{RESET}")
            try:
                action = input(f"{BOLD}Выберите действие [Enter - Старт, Q - Выход]: {RESET}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                action = 'q'

            if action in ('q', '0', 'exit', 'quit', 'й'):
                print(f"\n{RED}[СТОП] Отправка откликов отменена пользователем.{RESET}\n")
                return
            elif action in ('p', 'з'):
                pause_file = os.path.join(SCRIPT_DIR, 'pause.flag')
                with open(pause_file, 'w') as f:
                    f.write('1')
                print(f"\n{YELLOW}[ПАУЗА] Бот запустится на паузе. Нажмите [P] для начала откликов.{RESET}\n")
            elif action == '2':
                self.selenium_headless = True

        if (self.api_negotiations_available is False
                or (load_config().get('ai_filter') or {}).get('mode', 'off') != 'off'
                or (load_config().get('captcha') or {}).get('enabled')):
            self.run_selenium_api_cache(self.selenium_apply_limit)
            return
        
        print(f"\nНачинаем отклики" + (" (приоритет на кибербезопасность)" if active_direction()[0] else "") + "...")
        print(f"Задержка между откликами: 2-5 сек")
        
        successful_applications = 0
        processed = 0
        error_stats = {}
        daily_limit_reached = False
        priority_stats = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0}
        
        for vacancy in vacancies:
            if daily_limit_reached or self.applied_today >= self.max_applications_per_day:
                print(f"\n[ПАУЗА] Достигнут лимит за 24 часа ({self.max_applications_per_day})")
                break
            
            vacancy_id = vacancy.get('id')
            vacancy_name = vacancy.get('name', 'Без названия')
            employer = vacancy.get('employer', {}).get('name', 'Неизвестно')
            priority = self.get_vacancy_priority(vacancy)
            processed += 1
            
            if processed % 10 == 0:
                print(f"\nПрогресс: {processed}/{len(vacancies)} | [OK] Успешно: {successful_applications}"
                      + (f" | ИБ: {priority_stats.get(1, 0) + priority_stats.get(2, 0) + priority_stats.get(3, 0)}"
                         if active_direction()[0] else ''))
            
            cover_letter = self.generate_cover_letter(vacancy)
            
            # Эмодзи по приоритету
            priority_emoji = {
                1: "[P1]", # Пентестинг
                2: "[P2]", # ИБ
                3: "[P3]", # Спец ИБ
                4: "[P4]", # Защита данных
                5: "[P5]", # Dev+Security
                6: "[P6]", # Dev
                7: "[P7]" # Другое
            }.get(priority, "[P7]")
            
            print(f"\n{priority_emoji} [{processed}/{len(vacancies)}] {vacancy_name}")
            print(f"{employer}")
            
            success, error_type, _ = self.apply_to_vacancy(vacancy_id, cover_letter)
            
            if success:
                successful_applications += 1
                priority_stats[priority] = priority_stats.get(priority, 0) + 1
                print(f"[OK] Успешно отправлен отклик!")
            else:
                error_stats[error_type] = error_stats.get(error_type, 0) + 1
                
                # Перевод кодов ошибок — единый словарь в db_manager.STATUS_LABELS
                print(f"[-] {human_status(error_type)}")
                
                if error_type in ['daily_limit_exceeded']:
                    daily_limit_reached = True
                    break
            
            delay = random.randint(2, 5)
            time.sleep(delay)
        
        print(f"\n{'='*70}")
        print(f"ИТОГИ РАБОТЫ:")
        print(f" Обработано: {processed}")
        print(f" [OK] Успешно: {successful_applications}")
        print(f" Всего за последние 24 часа: {self.applied_today}")
        
        if successful_applications > 0:
            print(f"\nУспешные отклики по приоритетам:")
            priority_names = {
                1: "[P1] Пентестинг и Red Team",
                2: "[P2] Информационная безопасность",
                3: "[P3] Специализированная ИБ",
                4: "[P4] Защита данных",
                5: "[P5] Разработка с безопасностью",
                6: "[P6] Чистая разработка",
                7: "[P7] Другое IT"
            }
            
            for priority in sorted(priority_stats.keys()):
                if priority_stats[priority] > 0:
                    print(f" {priority_names.get(priority)}: {priority_stats[priority]}")
        
        if processed > 0:
            success_rate = (successful_applications / processed) * 100
            print(f"\n Успешность: {success_rate:.1f}%")
        
        if error_stats:
            print(f"\nСтатистика ошибок:")
            for error_type, count in sorted(error_stats.items(), key=lambda x: x[1], reverse=True):
                print(f" • {human_status(error_type)}: {count}")

        # Подсчет откликов на ИБ
        security_applications = sum(priority_stats.get(i, 0) for i in [1, 2, 3, 4])
        if security_applications > 0 and active_direction()[0]:
            print(f"\nОТКЛИКОВ НА КИБЕРБЕЗОПАСНОСТЬ: {security_applications} из {successful_applications} ({security_applications/max(successful_applications, 1)*100:.0f}%)")

def choose_ai_provider() -> None:
    """Меню [I]: «Авто» или конкретный ИИ (включая каждую модель Antigravity) первым."""
    from ai_assistant import describe_ai_chain
    cfg = load_config()
    ai = cfg.setdefault('ai_config', {})
    rows = [r for r in describe_ai_chain(cfg) if r['key'] != 'groq']
    current = ai.get('primary_ai') or 'auto'
    print(f"\n{CYAN}{BOLD}КАКОЙ ИИ ПИШЕТ ПИСЬМА{RESET}")
    print("Сейчас по очереди:")
    for n, row in enumerate(describe_ai_chain(cfg), 1):
        mark = f"{GREEN}готов{RESET}" if row['ready'] else f"{DIM}{row['note']}{RESET}"
        print(f"  {n}. {row['name']} — {mark}")
    print(f"\nРежим сейчас: {'Авто' if current == 'auto' else current}")
    print("  [1] Авто: быстрый и надёжный первым, письма проверяются на качество")
    for n, row in enumerate(rows, 2):
        suffix = ' (и следом Groq)' if row['key'] == 'gemini' else ''
        print(f"  [{n}] Первым: {row['name']}{suffix}")
    ans = input("Номер (Enter — оставить как есть): ").strip()
    if ans == '1':
        ai['primary_ai'] = 'auto'
        print(f"{GREEN}Режим: Авто{RESET}")
    elif ans.isdigit() and 2 <= int(ans) <= len(rows) + 1:
        row = rows[int(ans) - 2]
        ai['primary_ai'] = row['key']
        print(f"{GREEN}Первым будет: {row['name']}. Если он не ответит, пишут следующие.{RESET}")
    else:
        return
    # Выбор ИИ включает помощника: у нового профиля он выключен, и выбранный
    # ИИ иначе так и не писал бы писем.
    ai['enabled'] = True
    save_config(cfg)


def apply_blockers(cfg: dict) -> list:
    """Что мешает запустить отклики. Пусто — можно."""
    lines = []
    resume_id = str(os.environ.get('HH_RESUME_ID') or cfg.get('resume_id') or '')
    if not resume_id or resume_id.startswith('YOUR_'):
        lines.append(f"{YELLOW}[!] Не выбрано резюме: [P] → [R]{RESET}")
    if not search_direction_chosen(cfg):
        lines.append(f"{YELLOW}[!] Не выбрано, какие вакансии искать: [N] → [S]{RESET}")
    return lines


def check_first_run_setup() -> list[str]:
    """Проверяет, что пользователь настроил бота под себя.

    Ничего не блокирует: печатает список незаполненного и пункты меню,
    которыми это чинится. Возвращает список проблем (пустой — всё настроено).
    """
    try:
        cfg = load_config()
    except Exception as e:
        print(f"{RED}[X] Не удалось прочитать настройки: {explain_error(e)}{RESET}")
        return ['config']

    # Любая секция может лежать в конфиге как null, поэтому `or {}`, а не .get(k, {}).
    profile = cfg.get('candidate_profile') or {}
    contacts = profile.get('contacts') or {}
    ai_cfg = cfg.get('ai_config') or {}

    problems = []
    resume_id = str(os.environ.get('HH_RESUME_ID') or cfg.get('resume_id') or '')
    if not resume_id or resume_id.startswith('YOUR_'):
        problems.append(f"  {WHITE}[R]{RESET} Резюме не выбрано — отклики отправлять нечем. "
                        f"Выберите резюме из своего аккаунта hh.ru.")
    if not (profile.get('experience_highlights') or profile.get('about')):
        problems.append(f"  {CYAN}[H]{RESET} Профиль пуст — ИИ не знает вашего опыта и пишет общими словами. "
                        f"Заполните его из резюме: [P] → [H].")
    if not contacts or contacts.get('telegram') == '@username':
        problems.append(f"  {MAGENTA}[T]{RESET} Контакты пусты — работодателю некуда вам ответить. "
                        f"Укажите Telegram и текст сопроводительного письма.")
    if not (ai_cfg.get('api_key') or os.environ.get('GEMINI_API_KEY')):
        problems.append(f"  {CYAN}[G]{RESET} Ключ Google Gemini не задан — письма будет писать "
                        f"локальный адаптивный генератор вместо ИИ (бесплатный ключ: "
                        f"https://aistudio.google.com/app/apikey).")
    if not search_direction_chosen(cfg):
        problems.append(f"  {WHITE}[S]{RESET} Направление поиска не настроено — "
                        f"выберите специализацию или вставьте свою ссылку с hh.ru.")

    if problems:
        print(f"\n{YELLOW}{BOLD}{'='*60}{RESET}")
        print(f"{YELLOW}{BOLD}  ПЕРВИЧНАЯ НАСТРОЙКА: заполнено не всё{RESET}")
        print(f"{YELLOW}{BOLD}{'='*60}{RESET}")
        for p in problems:
            print(p)
        print(f"{YELLOW}Отклики запустятся, когда выбраны резюме [R] и направление поиска [S]; "
              f"остальное бот делает и без них, но хуже.{RESET}")
        print(f"{YELLOW}{BOLD}{'='*60}{RESET}")

    return problems


# Главное меню показывает 5 строк вместо 14: пункты сгруппированы, а ключи
# внутри групп остались теми же, что и в старом плоском меню, поэтому любой
# старый ключ продолжает работать и как скрытый шорткат с главного экрана.
# Группы намеренно висят на буквах O/D/P/N: все старые ключи (цифры 1-5,
# A B W R S T G C, 0/q/exit/quit и кириллические дубли ф и ц к ы е п с й)
# остаются за своими прежними действиями, а o/d/p/n и их кириллические
# позиционные двойники щ/в/з/т в коде не заняты ничем.
# Формат: группа -> (заголовок, ((ключи, цвет, подпись), ...)); первый ключ —
# канонический, остальные — кириллические дубли той же клавиши.
MENU_GROUPS = {
    'o': ("ЗАПУСТИТЬ ОТКЛИКИ", (
        (('1',), GREEN, "Полный цикл: разбор отказов, поднятие резюме, поиск, отклики"),
        (('2',), YELLOW, "Быстрые отклики с видимым окном браузера"),
        (('3',), CYAN, "Фоновые отклики, без окна браузера"),
    )),
    'd': ("ОТКАЗЫ И АНАЛИТИКА", (
        (('a', 'ф'), CYAN, "Разбор переписки с работодателями и правка резюме"),
        (('4',), MAGENTA, "Почему отказали: разбор причин и проверка резюме"),
        (('5',), BLUE, "Статистика откликов и конверсия"),
    )),
    'p': ("РЕЗЮМЕ И ПРОФИЛЬ", (
        (('b', 'и'), GREEN, "Поднять резюме в поиске (бесплатно, раз в 4 часа)"),
        (('w', 'ц'), CYAN, "Мой профиль и статус аккаунта"),
        (('r', 'к'), WHITE, "Выбрать целевое резюме из аккаунта hh.ru"),
        (('h', 'р'), CYAN, "Заполнить профиль из резюме hh (опыт, навыки, «О себе», контакты)"),
    )),
    'n': ("НАСТРОЙКИ", (
        (('f',), CYAN, "AI-фильтр вакансий и распознавание капчи"),
        (('u',), WHITE, "Аккаунты: открыть или создать отдельный профиль"),
        (('l',), WHITE, "Войти в HH для текущего профиля"),
        (('s', 'ы'), WHITE, "Сменить направление поиска / фильтры"),
        (('t', 'е'), MAGENTA, "Настроить сопроводительное письмо и Telegram"),
        (('i', 'ш'), CYAN, "Какой ИИ пишет письма: выбор и очередь"),
        (('g', 'п'), CYAN, "Ключ ИИ-генератора писем (Google Gemini)"),
        (('k', 'л'), GREEN, "Запасной ИИ, если основной исчерпает лимит (Groq)"),
        (('q', 'й'), YELLOW, "Ответы на вопросы работодателей (зарплата, город, график)"),
        (('m', 'ь'), GREEN, "Поведение бота: лимит, анкеты, письма, чат, фильтры"),
        (('c', 'с'), WHITE, "Очистить список найденных вакансий"),
    )),
}


# Кириллическая раскладка: те же физические клавиши, что и буквы групп.
MENU_GROUP_ALIASES = {'щ': 'o', 'в': 'd', 'з': 'p', 'т': 'n'}


def resolve_menu_choice(choice: str):
    """Разворачивает пункт главного меню в ключ действия старого меню.

    Ключ группы (o/d/p/n и кириллические щ/в/з/т) — показывает подменю и
    возвращает выбранный в нём ключ. Любой другой ввод (старый шорткат, цифра,
    пустая строка, '0') возвращается как есть, поэтому попадает ровно в ту же
    ветку диспетчера, что и до группировки меню.
    None означает «ничего не выбрано, вернуться в главное меню».
    """
    group = MENU_GROUPS.get(MENU_GROUP_ALIASES.get(choice, choice))
    if group is None:
        return choice

    title, items = group
    print(f"\n{CYAN}{BOLD}{'-'*60}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{'-'*60}{RESET}")
    for keys, color, label in items:
        print(f"  {color}[{keys[0].upper()}]{RESET} {label}")
    print(f"  {RED}[0]{RESET} Назад")
    print(f"{CYAN}{'-'*60}{RESET}")
    try:
        sub = input(f"{BOLD}Выберите действие [Enter - назад]: {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if sub in ('', '0', 'q', 'й'):
        return None
    for keys, _color, _label in items:
        if sub in keys:
            return keys[0]
    print(f"\n{YELLOW}[!] Неизвестный пункт: {sub}{RESET}")
    return None


def main():
    CLIENT_ID = os.environ.get("HH_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("HH_CLIENT_SECRET", "")
    REDIRECT_URI = "https://localhost/callback"
    active_resume_id, active_resume_title = get_active_resume()
    RESUME_ID = active_resume_id

    check_first_run_setup()

    search_params = {}
    manual_apply_limit = DEFAULT_MANUAL_APPLY_LIMIT
    selenium_apply_limit = DEFAULT_SELENIUM_APPLY_LIMIT
    manual_apply_mode = False
    
    # Интерактивное меню запуска, если скрипт запущен без ключей автоматизации
    auto_flags = {'--auto', '--yes', '-y'}
    if (len(sys.argv) == 1 or '--menu' in sys.argv) and not any(arg in sys.argv for arg in auto_flags):
        # Проверка ИИ при запуске: очередь «Авто» строится по живым ответам,
        # а не по давней статистике.
        try:
            from ai_assistant import AIAssistant
            # Одна серая строка, как было. Итог проверки виден в строке «ИИ» меню;
            # длинный список моделей над шапкой пользователю не нужен.
            print(f"{DIM}Проверяю, какие ИИ сейчас отвечают...{RESET}")
            report = AIAssistant(load_config()).probe_report()
            for line in (report or '').splitlines():
                if not line.startswith('Проверка ИИ:'):
                    print(f"{YELLOW}{line}{RESET}")   # «Antigravity не запущен…»
        except Exception as e:
            logging.debug(f"Проверка ИИ не удалась: {e}")
        while True:
            active_resume_id, active_resume_title = get_active_resume()
            preset = get_active_preset()
            RESUME_ID = active_resume_id

            cfg_now = load_config()
            # ai_config может лежать в конфиге как null — тогда .get(..., {}) вернёт None
            # и .get('api_key') уронит меню на старте.
            ai_key_now = (cfg_now.get('ai_config') or {}).get('api_key') or os.environ.get('GEMINI_API_KEY', '')
            ai_status = f"{GREEN}Активен{RESET}" if ai_key_now else f"{YELLOW}Не задан (адаптивный генератор){RESET}"

            print(f"\n{CYAN}{BOLD}{'='*60}{RESET}")
            print(f"{RED}{BOLD}         HH.RU BOT: АВТООТКЛИКИ И АНАЛИТИКА{RESET}")
            print(f"{CYAN}{BOLD}{'='*60}{RESET}")
            direction_ok = search_direction_chosen(cfg_now)
            print(f"  Направление:    " + (f"{GREEN}{BOLD}{preset['name']}{RESET}" if direction_ok
                                          else f"{YELLOW}не выбрано — [N] → [S]{RESET}"))
            print(f"  Аккаунт:        {PROFILE_ID}")
            print(f"  Целевое резюме: {CYAN}{BOLD}{active_resume_title}{RESET}")
            # Очередь ИИ вместо «Активен»: пишут по очереди несколько ИИ, и
            # по одному слову было не понять, кто сейчас первый и кого не хватает.
            try:
                from ai_assistant import ai_menu_lines
                head, queue = ai_menu_lines(cfg_now)
                print(f"  ИИ:             {GREEN}{head}{RESET}")
                print(f"                  {DIM}{queue}{RESET}")
            except Exception:
                print(f"  ИИ генератор:   {ai_status}")
            print(f"{CYAN}{'-'*60}{RESET}")
            print(f"  {GREEN}[O]{RESET} Запустить отклики")
            print(f"  {MAGENTA}[D]{RESET} Отказы и аналитика")
            print(f"  {CYAN}[P]{RESET} Резюме и профиль")
            print(f"  {WHITE}[N]{RESET} Настройки")
            print(f"  {RED}[0]{RESET} Выход")
            print(f"{CYAN}{BOLD}{'='*60}{RESET}")
            print(f"{DIM}  Enter — полный цикл. Прямые клавиши: 1-5, A B W R S T G C{RESET}")
            # Полный цикл без вопроса — только по явному флагу. Раньше его
            # запускал и «не терминал» на входе: меню из IDE или с перенаправленным
            # вводом молча уходило в разбор чатов и отклики (25.09). Для
            # автозапуска есть `hh.py run` и `hh.py schedule`.
            if any(arg in sys.argv for arg in ('--auto', '--yes', '-y', '--full-cycle')):
                choice = '1'
            else:
                try:
                    choice = input(f"{BOLD}Выберите действие [Enter - Полный цикл, 0 - Выход]: {RESET}").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = '0'
                choice = resolve_menu_choice(choice)
                if choice is None:
                    continue

            if choice in ('0', 'q', 'exit', 'quit', 'й'):
                print(f"\n{RED}[СТОП] Завершение работы.{RESET}\n")
                return
            elif choice in ('1', '', '2', '3') and apply_blockers(cfg_now):
                # Без резюме и направления отклики уходили бы с чужими настройками
                # по умолчанию (направление ИБ) — сначала настройка.
                for line in apply_blockers(cfg_now):
                    print(line)
                continue
            elif choice in ('1', ''):
                if not any(arg in sys.argv for arg in ('--auto', '--yes', '-y', '--full-cycle')):
                    from rejection_analyzer import ask_browser_mode
                    mode_flag = ask_browser_mode(load_config(), sys.argv)
                    if mode_flag:
                        sys.argv.append(mode_flag)
                break
            elif choice == '2':
                selenium_script = os.path.join(CODE_DIR, 'hh_selenium.py')
                subprocess.run([sys.executable, selenium_script, '--api-cache', '--limit', str(selenium_apply_limit)], cwd=SCRIPT_DIR)
                continue
            elif choice == '3':
                selenium_script = os.path.join(CODE_DIR, 'hh_selenium.py')
                subprocess.run([sys.executable, selenium_script, '--api-cache', '--headless', '--limit', str(selenium_apply_limit)], cwd=SCRIPT_DIR)
                continue
            elif choice == '4':
                subprocess.run([sys.executable, os.path.join(CODE_DIR, 'rejection_analyzer.py'), '--limit', '100'], cwd=SCRIPT_DIR)
                continue
            elif choice == '5':
                subprocess.run([sys.executable, os.path.join(CODE_DIR, 'db_manager.py')], cwd=SCRIPT_DIR)
                continue
            elif choice in ('a', 'ф'):
                subprocess.run([sys.executable, os.path.join(CODE_DIR, 'rejection_analyzer.py'), '--chats', '--limit', '0'], cwd=SCRIPT_DIR)
                continue
            elif choice in ('h', 'р'):
                subprocess.run([sys.executable, os.path.join(CODE_DIR, 'resume_updater.py'), '--import-profile'],
                               cwd=SCRIPT_DIR)
                continue
            elif choice in ('b', 'и'):
                subprocess.run([sys.executable, os.path.join(CODE_DIR, 'resume_updater.py'), '--bump'], cwd=SCRIPT_DIR)
                continue
            elif choice in ('w', 'ц'):
                try:
                    applicant = HHAutoApplicant(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, RESUME_ID)
                    applicant.whoami()
                except Exception as e:
                    print(f"\n{RED}[X] Не удалось показать профиль: {explain_error(e)}{RESET}")
                input(f"\n{BOLD}Нажмите Enter для возврата в меню...{RESET}")
                continue
            elif choice == 'f':
                from config_manager import edit_ai_filter_and_captcha
                edit_ai_filter_and_captcha()
                continue
            elif choice == 'u':
                from config_manager import choose_account_profile
                choose_account_profile()
                continue
            elif choice == 'l':
                from hh_selenium import HHSeleniumBot
                login_bot = HHSeleniumBot(pause_before_close=False)
                try:
                    if login_bot.init_driver():
                        if not login_bot.check_login():
                            login_bot.login()
                finally:
                    login_bot.close_driver()
                continue
            elif choice in ('r', 'к'):
                interactive_resume_picker()
                continue
            elif choice in ('s', 'ы'):
                interactive_search_picker()
                continue
            elif choice in ('t', 'е'):
                interactive_cover_letter_editor()
                continue
            elif choice in ('i', 'ш'):
                choose_ai_provider()
                continue
            elif choice in ('g', 'п'):
                prompt_gemini_key()
                continue
            elif choice in ('k', 'л'):
                prompt_groq_key()
                continue
            elif choice in ('q', 'й'):
                # Ответы на вопросы работодателей правятся из меню, а не в файле
                # настроек: у каждого свой город, зарплата и готовность к сменам.
                from config_manager import edit_question_answers
                edit_question_answers()
                continue
            elif choice in ('m', 'ь'):
                from config_manager import edit_bot_behavior
                edit_bot_behavior()
                continue
            elif choice in ('c', 'с'):
                cache_file = os.path.join(SCRIPT_DIR, 'vacancies_cache.json')
                if not os.path.exists(cache_file):
                    print(f"\n{YELLOW}[!] Список найденных вакансий и так пуст.{RESET}\n")
                else:
                    try:
                        os.remove(cache_file)
                        print(f"\n{GREEN}[OK] Список найденных вакансий очищен!{RESET}\n")
                    except OSError as e:
                        # На Windows файл держит параллельно запущенный бот — раньше
                        # это роняло всё меню вместо возврата к списку пунктов.
                        print(f"\n{RED}[X] Не удалось очистить список вакансий: {explain_error(e)}{RESET}")
                        print(f"{YELLOW}    Закройте другие запущенные копии бота и повторите, "
                              f"либо удалите файл вручную.{RESET}\n")
                continue

    # Обработка аргументов командной строки
    if len(sys.argv) > 1:
        if sys.argv[1] in ('--whoami', '-w'):
            applicant = HHAutoApplicant(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, RESUME_ID)
            applicant.whoami()
            return
        elif sys.argv[1] == '--clear-cache':
            cache_file = os.path.join(SCRIPT_DIR, 'vacancies_cache.json')
            if not os.path.exists(cache_file):
                print(f"{YELLOW}[!] Список найденных вакансий и так пуст.{RESET}")
            else:
                try:
                    os.remove(cache_file)
                    print(f"{GREEN}[OK] Список найденных вакансий очищен!{RESET}")
                    print("Запустите бота снова, чтобы начать новый поиск")
                except OSError as e:
                    print(f"{RED}[X] Не удалось очистить список вакансий: {explain_error(e)}{RESET}")
                    print(f"{YELLOW}    Закройте другие запущенные копии бота и повторите.{RESET}")
            # Раньше при отсутствии файла управление проваливалось дальше
            # и --clear-cache запускал полный цикл откликов.
            return
        elif sys.argv[1] == '--manual-apply':
            manual_apply_mode = True
            if len(sys.argv) > 2:
                try:
                    manual_apply_limit = max(1, int(sys.argv[2]))
                except ValueError:
                    print(f"{RED}[X] Лимит для --manual-apply должен быть числом{RESET}")
                    return
        elif sys.argv[1] == '--selenium-limit':
            if len(sys.argv) <= 2:
                print(f"{RED}[X] После --selenium-limit нужно число{RESET}")
                return
            try:
                selenium_apply_limit = max(1, int(sys.argv[2]))
            except ValueError:
                print(f"{RED}[X] Лимит для --selenium-limit должен быть числом{RESET}")
                return
        elif sys.argv[1] == '--help':
            print(f"\n{CYAN}{BOLD}Опции:{RESET}")
            print(" --clear-cache - Очистить список найденных вакансий и искать заново")
            print(" --manual-apply [N] - Открыть N найденных вакансий для ручного отклика")
            print(" --selenium-limit N - Лимит откликов за запуск, по умолчанию 200")
            print(" --no-bump - Пропустить этап поднятия резюме в поиске")
            print(" --help - Показать справку")
            print("\nЕсли hh.ru закрывает прямую отправку, бот сам переключается на браузер.")
            print(f"\n{CYAN}{BOLD}Фокус на:{RESET}")
            print(f" • {GREEN}Пентестинг и Red Team{RESET}")
            print(f" • {GREEN}Информационная безопасность{RESET}")
            print(f" • {GREEN}SOC, SIEM, Incident Response{RESET}")
            print(f" • {GREEN}Application Security{RESET}")
            print(f" • {GREEN}Cloud Security{RESET}")
            return
    
    try:
        applicant = HHAutoApplicant(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, RESUME_ID)
        if manual_apply_mode:
            applicant.run_manual_applications(search_params, manual_apply_limit)
        else:
            applicant.selenium_apply_limit = selenium_apply_limit
            applicant.run_auto_applications(search_params)
        
    except KeyboardInterrupt:
        print("\n[СТОП] Остановлено пользователем")
    except Exception as e:
        # Раньше exc_info=True печатал стек прямо в консоль (обработчик консоли
        # висит на корневом логгере), а текст ниже обещал показать его только с
        # --debug. Теперь стек уходит только в файл журнала.
        logging.error(f"Непредвиденная ошибка: {explain_error(e)}")
        file_handler.emit(logging.LogRecord(
            name='hh', level=logging.DEBUG, pathname=__file__, lineno=0,
            msg="Непредвиденная ошибка в основном цикле", args=(), exc_info=sys.exc_info(),
        ))
        print(f"\n{RED}[X] Ошибка:{RESET} {explain_error(e)}")
        print(f"{DIM}Подробности сохранены в журнал работы.{RESET}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Остановка по Ctrl+C — обычный сценарий, а не сбой. Раньше сюда
        # долетал traceback на семь кадров, и остановка выглядела поломкой.
        print(f"\n{YELLOW}Остановлено. Отправленные отклики сохранены.{RESET}")
        sys.exit(0)
