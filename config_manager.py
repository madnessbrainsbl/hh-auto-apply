"""
Модуль динамического управления конфигурацией, резюме и поисковыми фильтрами HeadHunter.
Позволяет любому пользователю легко переключать целевое резюме из своего аккаунта hh.ru
и менять специализацию поиска (Python, DevOps, Системное администрирование, Custom) через меню.
"""

import os
import sys
import json
import time
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

from terminal_ui import (
    ColoredConsoleFormatter, colorize_text, c_ok, c_err, c_warn, c_info, explain_error,
    c_priority, c_accent, c_header, GREEN, YELLOW, RED, CYAN, MAGENTA, BLUE, BOLD, RESET, WHITE, DIM, GRAY
)

CONFIG_FILE = os.path.join(SCRIPT_DIR, 'hh_selenium_config.json')
EXAMPLE_CONFIG_FILE = os.path.join(CODE_DIR, 'hh_selenium_config.example.json')
VACANCIES_CACHE_FILE = os.path.join(SCRIPT_DIR, 'vacancies_cache.json')

logger = logging.getLogger('config_manager')
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredConsoleFormatter('%(levelname)s - %(message)s'))
file_handler = logging.FileHandler(os.path.join(SCRIPT_DIR, 'config_manager.log'), encoding='utf-8')
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


def log_detail(message: str) -> None:
    """Технические подробности — только в файл журнала, мимо консоли.

    Консольный обработчик стоит на уровне INFO, поэтому logger.error виден
    пользователю. Сырой текст исключения и полные пути ему не нужны.
    """
    file_handler.emit(logging.LogRecord(
        name='config_manager', level=logging.DEBUG, pathname=__file__, lineno=0,
        msg=message, args=(), exc_info=None,
    ))


STRICT_TITLE_EXCLUDE_KEYWORDS = (
    # Уровень должности (младший, начальник, руководитель...) не исключаем:
    # подаёмся массово, отказ по уровню тоже данные для разбора. Отсеиваем
    # только продажи, HR и учебные работы.
    'менеджер по продажам',
    'менеджер по работе с клиентами',
    'менеджер по развитию',
    'продаж',
    'sales manager',
    'head of sales',
    'account manager',
    'business development',
    'product manager',
    'студенческ',
    'диплом',
    'реферат',
    'hr',
    'рекрутер',
    'bitrix',
    'битрикс',
    'полиграф',
    'хакатон',
    'конкурс',
)
_EXCLUDES_CACHE = {}


def title_excludes(config=None):
    """Слова-исключения для названий вакансий.

    Главный — список из настроек (его правят в меню «Поведение бота»); пустой
    или отсутствующий — встроенный STRICT_TITLE_EXCLUDE_KEYWORDS. Без config
    читает файл настроек, запоминая его по времени изменения: фильтр зовут
    на каждую из тысяч вакансий.
    """
    if config is None:
        try:
            stamp = os.path.getmtime(CONFIG_FILE)
        except OSError:
            stamp = 0
        if _EXCLUDES_CACHE.get('stamp') != stamp:
            try:
                _EXCLUDES_CACHE['words'] = tuple(load_config().get('keywords_exclude') or ())
            except Exception:
                _EXCLUDES_CACHE['words'] = ()
            _EXCLUDES_CACHE['stamp'] = stamp
        words = _EXCLUDES_CACHE['words']
    else:
        words = tuple(config.get('keywords_exclude') or ())
    return words or STRICT_TITLE_EXCLUDE_KEYWORDS


def find_title_keyword(text, keywords):
    """Первое слово из списка, найденное в названии, или None.

    Короткие слова — только целым словом: «qa» сидит внутри «aqua»,
    «ит» — внутри «кредит», «hr» — внутри «chrome».
    """
    text = str(text or '').lower().replace('ё', 'е')
    for keyword in keywords:
        word = str(keyword or '').lower().replace('ё', 'е')
        if not word:
            continue
        if len(word) <= 4:
            if re.search(r'(?<![a-zа-яё0-9])' + re.escape(word) + r'(?![a-zа-яё0-9])', text):
                return keyword
        elif word in text:
            return keyword
    return None


# Пресеты специализаций поиска
SEARCH_PRESETS = {
    'python': {
        'id': 'python',
        'name': 'Python Developer / Backend',
        'search_url': 'https://hh.ru/search/vacancy?text=python+developer+OR+python+разработчик+OR+fastapi+OR+django&area=113&salary=&ored_clusters=true',
        'queries': [
            '"python разработчик"', '"python developer"', '"backend python"',
            '"разработчик python"', '"fastapi developer"', '"django разработчик"',
            '"python backend engineer"', '"middle python"'
        ],
        'keywords_include': [
            'python', 'django', 'fastapi', 'flask', 'asyncio', 'backend',
            'разработчик python', 'python developer', 'sqlalchemy', 'postgresql', 'docker'
        ],
        'keywords_exclude': [
            'стажер', 'стажёр', 'intern', 'менеджер', 'директор', 'руководитель',
            'продаж', 'преподаватель', 'hr', 'рекрутер'
        ]
    },
    'devops': {
        'id': 'devops',
        'name': 'DevOps / SRE / Kubernetes / Cloud',
        'search_url': 'https://hh.ru/search/vacancy?text=devops+OR+sre+OR+kubernetes+OR+ci%2Fcd&area=113&salary=&ored_clusters=true',
        'queries': [
            '"devops инженер"', '"devops engineer"', '"sre"', '"инженер kubernetes"',
            '"cloud devops engineer"', '"ci/cd engineer"'
        ],
        'keywords_include': [
            'devops', 'sre', 'kubernetes', 'k8s', 'docker', 'ci/cd', 'ansible',
            'terraform', 'gitlab ci', 'linux', 'helm', 'prometheus', 'grafana'
        ],
        'keywords_exclude': [
            'стажер', 'стажёр', 'intern', 'менеджер', 'директор', 'руководитель',
            'продаж', 'hr', 'рекрутер'
        ]
    },
    'sysadmin': {
        'id': 'sysadmin',
        'name': 'Системный администратор / Сети',
        'search_url': 'https://hh.ru/search/vacancy?text=системный+администратор+OR+сетевой+инженер+OR+linux+administrator&area=113&salary=&ored_clusters=true',
        'queries': [
            '"системный администратор"', '"system administrator"',
            '"сетевой инженер"', '"администратор linux"', '"системный инженер"'
        ],
        'keywords_include': [
            'системный администратор', 'linux', 'windows server', 'active directory',
            'сетевой инженер', 'tcp/ip', 'cisco', 'mikrotik', 'virtualization', 'vmware'
        ],
        'keywords_exclude': [
            'стажер', 'стажёр', 'intern', 'менеджер', 'директор', 'руководитель',
            'продаж', 'hr'
        ]
    },
    'custom': {
        'id': 'custom',
        'name': 'Пользовательский поиск (свои запросы и URL)',
        'search_url': '',
        'queries': [],
        'keywords_include': [],
        'keywords_exclude': ['стажер', 'intern', 'менеджер', 'продаж']
    }
}

# Пусто по умолчанию: резюме выбирается пунктом меню [R] под каждого пользователя.
# Зашитый сюда чужой ID означал бы отклики не от того аккаунта.
DEFAULT_RESUME_ID = ""
DEFAULT_RESUME_TITLE = "Резюме не выбрано"


def _report_save(ok: bool, message: str) -> bool:
    """Печатает результат save_config: успех зелёным, провал — красным.

    save_config возвращает False при ошибке записи (нет прав, диск занят).
    Раньше все редакторы печатали зелёное [OK] не глядя на результат,
    и пользователь уходил из меню уверенный, что настройка сохранена.
    """
    if ok:
        print(f"\n{GREEN}{BOLD}[OK] {message}{RESET}\n")
    else:
        print(f"\n{RED}{BOLD}[X] НЕ СОХРАНЕНО: {message}{RESET}")
        print(f"{RED}    Настройки не записались. Проверьте, что папка программы "
              f"не защищена от записи и на диске есть место; подробности сохранены в журнал работы.{RESET}\n")
    return ok


def edit_ai_filter_and_captcha():
    cfg = load_config()
    filtering = cfg.setdefault('ai_filter', {})
    captcha = cfg.setdefault('captcha', {})
    print('\nAI-фильтр: off — выключен, light — название и навыки, heavy — полный контекст, custom — свои критерии')
    mode = input(f"Режим [{filtering.get('mode', 'off')}]: ").strip().lower()
    if mode:
        if mode not in ('off', 'light', 'heavy', 'custom'):
            print('Неизвестный режим; настройки не изменены')
            return
        filtering['mode'] = mode
    if filtering.get('mode') == 'custom':
        prompt = input('Критерии отбора (Enter — оставить прежние): ').strip()
        if prompt:
            filtering['prompt'] = prompt
        if not filtering.get('prompt'):
            print('Для custom нужны критерии; настройки не изменены')
            return
    print('Vision отправляет только изображение текстовой капчи выбранному AI-провайдеру.')
    enabled = input(f"Распознавание капчи [{'on' if captcha.get('enabled') else 'off'}], on/off: ").strip().lower()
    if enabled:
        if enabled not in ('on', 'off'):
            print('Введите on или off; настройки не изменены')
            return
        captcha['enabled'] = enabled == 'on'
    if captcha.get('enabled'):
        model = input('Модель с поддержкой изображений (Enter — модель основного ИИ): ').strip()
        if model:
            captcha['model'] = model
    _report_save(save_config(cfg), 'AI-фильтр и капча настроены')


def choose_account_profile():
    import subprocess
    from app_paths import profile_directory
    root = os.environ.get('HH_DATA_DIR') or CODE_DIR
    folder = os.path.join(root, 'profiles')
    names = ['default'] + (sorted(n for n in os.listdir(folder)
                                 if os.path.isdir(os.path.join(folder, n))) if os.path.isdir(folder) else [])
    print(f'Текущий аккаунт: {PROFILE_ID}. Профили: {", ".join(names)}')
    name = input('Имя профиля для открытия/создания (Enter или 0 — назад): ').strip()
    # «0» везде в меню значит «назад»; здесь он создавал аккаунт с именем «0».
    if not name or name == '0' or name == PROFILE_ID:
        return
    if name not in names:
        answer = input(f'Аккаунта «{name}» нет. Создать новый? [y/N]: ').strip().lower()
        if answer not in ('y', 'д', 'да', 'yes'):
            return
    try:
        profile_directory(name)
    except ValueError as exc:
        print(str(exc))
        return
    subprocess.run([sys.executable, os.path.join(CODE_DIR, 'hh.py'), '--profile-id', name, 'menu'])
    print(f'Возврат в аккаунт {PROFILE_ID}')


def load_config() -> Dict[str, Any]:
    """Загружает текущую конфигурацию или создает её с дефолтными значениями."""
    if not os.path.exists(CONFIG_FILE) and (PROFILE_ID != 'default' or os.environ.get('HH_DATA_DIR')):
        config = {'resume_id': '', 'resume_title': '', 'candidate_profile': {},
                  'ai_config': {'enabled': False}, 'ai_filter': {'mode': 'off'},
                  'captcha': {'enabled': False},
                  'max_applications': 200, 'skip_with_tests': True,
                  'api_cache_file': 'vacancies_cache.json'}
        if not save_config(config):
            raise OSError('Не удалось создать настройки нового профиля')
        return config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError(f"ожидался объект, получен {type(config).__name__}")
            return config
        except Exception as e:
            # Раньше отсюда молча возвращался example-конфиг, и первое же сохранение
            # из меню затирало боевой файл: пропадали ключ Gemini, настроенные
            # поисковые запросы и dev_search_queries (добор до 200 откликов в день).
            # Уводим битый файл в сторону — тогда перезаписывать уже нечего,
            # и пользователь может достать из него свои настройки.
            broken = f"{CONFIG_FILE}.broken-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(CONFIG_FILE, broken)
                logger.error(f"Файл настроек повреждён ({explain_error(e)}). Старый сохранён как "
                             f"{os.path.basename(broken)}, дальше используются значения по умолчанию.")
                log_detail(f"Повреждённый файл настроек: {e!r}")
            except Exception as move_err:
                logger.error(f"Файл настроек повреждён ({explain_error(e)}), отложить его в сторону "
                             f"не удалось: {explain_error(move_err)}")
                log_detail(f"Повреждённый файл настроек: {e!r}; перенос: {move_err!r}")

    # Попробуем загрузить пример
    if os.path.exists(EXAMPLE_CONFIG_FILE):
        try:
            with open(EXAMPLE_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # Базовая конфигурация по умолчанию
    default_config = {
        "resume_id": DEFAULT_RESUME_ID,
        "resume_title": DEFAULT_RESUME_TITLE,
        "search_preset": "custom",
        "search_url": SEARCH_PRESETS['custom']['search_url'],
        "max_applications": 200,
        "skip_with_tests": True,
        "skip_applied": True,
        "keywords_include": SEARCH_PRESETS['custom']['keywords_include'],
        "keywords_exclude": SEARCH_PRESETS['custom']['keywords_exclude'],
        "search_queries": SEARCH_PRESETS['custom']['queries'],
    }
    save_config(default_config)
    return default_config


def save_config(config: Dict[str, Any]) -> bool:
    """Атомарно сохраняет конфигурацию в файл."""
    try:
        tmp_file = CONFIG_FILE + '.tmp'
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        if os.path.exists(CONFIG_FILE):
            os.replace(tmp_file, CONFIG_FILE)
        else:
            os.rename(tmp_file, CONFIG_FILE)
        return True
    except Exception as e:
        # Полный путь пользователю не нужен — только имя файла.
        logger.error(f"Не удалось сохранить настройки ({os.path.basename(CONFIG_FILE)}): {explain_error(e)}")
        log_detail(f"Сохранение {CONFIG_FILE}: {e!r}")
        return False


def get_active_resume() -> Tuple[str, str]:
    """Возвращает текущие (resume_id, resume_title)."""
    cfg = load_config()
    rid = os.environ.get('HH_RESUME_ID') or cfg.get('resume_id') or DEFAULT_RESUME_ID
    title = cfg.get('resume_title') or DEFAULT_RESUME_TITLE
    return rid, title


def set_active_resume(resume_id: str, resume_title: str) -> bool:
    """Устанавливает и сохраняет активное резюме."""
    cfg = load_config()
    cfg['resume_id'] = resume_id.strip()
    cfg['resume_title'] = resume_title.strip()
    return save_config(cfg)


def search_direction_chosen(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Выбрал ли пользователь, что искать. Без выбора бот искал бы по чужому
    направлению, и новый пользователь откликался бы не на свои вакансии."""
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get('search_preset') or cfg.get('search_url') or cfg.get('search_queries')
                or cfg.get('custom_search_query'))


def get_active_preset() -> Dict[str, Any]:
    """Возвращает текущий пресет поиска."""
    cfg = load_config()
    preset_id = cfg.get('search_preset', 'custom')
    preset = SEARCH_PRESETS.get(preset_id, SEARCH_PRESETS['custom']).copy()
    
    # Если в конфиге переопределен search_url или queries
    if cfg.get('search_url'):
        preset['search_url'] = cfg['search_url']
    if cfg.get('search_queries'):
        preset['queries'] = cfg['search_queries']
    if cfg.get('custom_search_query') and preset_id == 'custom':
        preset['custom_query'] = cfg['custom_search_query']
        preset['name'] = f"Пользовательский: {cfg['custom_search_query']}"

    # Ключевые слова тоже берем из конфига, а не только из хардкода SEARCH_PRESETS.
    # set_active_preset их честно вычисляет и сохраняет, но читателя у них не было,
    # и для пресета 'custom' (keywords_include = []) фильтр заголовков в
    # validate_apply_title выключался целиком — бот откликался на что угодно.
    if cfg.get('keywords_include'):
        preset['keywords_include'] = cfg['keywords_include']
    if cfg.get('keywords_exclude'):
        preset['keywords_exclude'] = cfg['keywords_exclude']

    return preset


# --- Отсев вакансий, грейд которых заведомо выше профиля кандидата ---------
# Повод: бот откликнулся на «Заместитель Председателя Правления по IT».
# В keywords_exclude есть 'директор', 'начальник', 'руководитель',
# 'chief', 'ciso' — но правление банка называется иначе, и не совпало НИ ОДНО слово.
# Плюс сам список слов ничего не знает про опыт кандидата: он одинаков и для
# джуна, и для человека с 15 годами стажа.
#
# Формат уровня: (сколько лет опыта обычно ждут за такой строкой, как назвать
# уровень человеку, маркеры в заголовке). Кандидату с меньшим опытом вакансия
# не отдаётся; с большим — уровень перестаёт отсекаться.
# ponytail: сравнение подстрокой, как и в остальном фильтре заголовков. Поэтому
# в маркерах нет коротких латинских аббревиатур (в 'director' сидит 'cto',
# в 'decision' — почти 'ciso'); длинные однозначные формы безопаснее регулярок.
TITLE_GRADE_LEVELS = (
    (10, 'должность уровня правления (C-level)', (
        'председател',            # председатель правления, заместитель председателя
        'член правления', 'члена правления', 'членом правления',
        'вице-президент', 'вице президент', 'vice president', 'vice-president',
        'генеральный директор', 'генерального директора',
        'управляющий директор', 'управляющего директора',
        'исполнительный директор', 'исполнительного директора',
        'chief', 'ciso', 'c-level', 'head of',
        'топ-менеджер', 'топ менеджер',
    )),
    (6, 'руководство департаментом или управлением', (
        'директор департамента', 'директора департамента',
        'директор по ', 'директора по ',
        'технический директор', 'технического директора',
        'ит-директор', 'it-директор',
        'начальник департамента', 'начальника департамента',
        'начальник управления', 'начальника управления',
        'руководитель департамента', 'руководителя департамента',
        'руководитель управления', 'руководителя управления',
        'руководитель службы', 'начальник службы',
        'заместитель генерального', 'первый заместитель',
        'заместитель директора', 'заместителя директора',
        'заместитель начальника', 'заместитель руководителя',
    )),
)


def get_candidate_experience_years(cfg: Optional[Dict[str, Any]] = None) -> int:
    """Опыт кандидата в годах из candidate_profile.

    Поля нет или в нём мусор — возвращаем 0, то есть ведём себя консервативно:
    все топ-позиции отсекаются.
    """
    try:
        if cfg is None:
            cfg = load_config()
        profile = cfg.get('candidate_profile') or {}
        return max(0, int(float(profile.get('experience_years'))))
    except Exception:
        return 0


def check_title_grade(title: object, experience_years: Optional[int] = None) -> Tuple[bool, str]:
    """Проверяет, не выше ли грейд вакансии, чем профиль кандидата.

    Возвращает (можно_откликаться, причина_понятная_человеку).
    """
    # По умолчанию ВЫКЛЮЧЕН. Отклик на позицию выше грейда ничего не стоит, а отказ
    # по нему — данные для разбора: анализатор сам скажет, что не так. Молча резать
    # вакансии вредно ещё и потому, что цифра стажа в настройках легко устаревает
    # (и фильтр рубил доступное).
    # Включается флагом grade_filter в настройках.
    try:
        if not (load_config().get('grade_filter') or False):
            return True, ''
    except Exception:
        return True, ''

    normalized = str(title or '').lower().replace('ё', 'е')
    if experience_years is None:
        experience_years = get_candidate_experience_years()

    for min_years, level_name, markers in TITLE_GRADE_LEVELS:
        if experience_years >= min_years:
            continue
        for marker in markers:
            if marker in normalized:
                return False, (
                    f"это {level_name}: обычно требуется от {min_years} лет опыта, "
                    f"а в профиле указано {experience_years}. "
                    f"В названии вакансии: «{marker.strip()}»"
                )

    return True, ''


def set_active_preset(preset_id: str, custom_query: Optional[str] = None, custom_url: Optional[str] = None) -> bool:
    """Устанавливает пресет поиска и обновляет поисковые фильтры."""
    if preset_id not in SEARCH_PRESETS:
        preset_id = 'custom'

    cfg = load_config()
    current_preset = cfg.get('search_preset')
    cfg['search_preset'] = preset_id
    preset = SEARCH_PRESETS[preset_id]

    if preset_id == 'custom':
        if custom_query:
            cfg['custom_search_query'] = custom_query.strip()
            # Формируем поисковый URL
            encoded_query = custom_query.strip().replace(' ', '+')
            cfg['search_url'] = f"https://hh.ru/search/vacancy?text={encoded_query}&area=113&salary=&ored_clusters=true"
            cfg['search_queries'] = [f'"{custom_query.strip()}"']
            cfg['keywords_include'] = [w.lower() for w in custom_query.strip().split() if len(w) > 2]
        elif custom_url:
            cfg['search_url'] = custom_url.strip()
            cfg['search_queries'] = []
            cfg['keywords_include'] = []
    else:
        cfg['search_url'] = preset['search_url']
        cfg['search_queries'] = preset['queries']
        cfg['keywords_include'] = preset['keywords_include']
        cfg['keywords_exclude'] = preset['keywords_exclude']

    # Очищаем кеш вакансий только при реальной смене направления, чтобы не смешивать вакансии
    if current_preset != preset_id and os.path.exists(VACANCIES_CACHE_FILE):
        try:
            os.remove(VACANCIES_CACHE_FILE)
            logger.info("Кеш вакансий очищен в связи со сменой направления поиска")
        except Exception as e:
            # Молчаливый провал делал неверной именно ту операцию, ради которой
            # пользователь сюда зашел: бот продолжал откликаться по старому направлению.
            logger.warning(f"Не удалось очистить список вакансий при смене направления: {explain_error(e)}")
            log_detail(f"Очистка списка вакансий: {e!r}")
            print(f"{YELLOW}[!] Не удалось очистить список найденных вакансий ({explain_error(e)}). "
                  f"Очистите его через меню, иначе поиск смешает направления.{RESET}")

    return save_config(cfg)


def fetch_account_resumes(headless: bool = True) -> List[Dict[str, Any]]:
    """Собирает все резюме пользователя из личного кабинета hh.ru через Selenium."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    profile_dir = os.path.join(SCRIPT_DIR, 'chrome_profile')
    options = Options()
    if os.environ.get('CHROME_BINARY'):
        options.binary_location = os.environ['CHROME_BINARY']
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    # Тут браузер запускался без единой настройки тишины, и служебные строки
    # Chrome сыпались прямо в экран пользователя.
    options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
    options.add_argument('--log-level=3')
    if os.path.exists(profile_dir):
        options.add_argument(f'--user-data-dir={profile_dir}')

    driver = None
    resumes = []
    try:
        service = None
        try:
            from terminal_ui import chrome_service
            service = chrome_service(SCRIPT_DIR)
        except Exception:
            pass
        driver = (webdriver.Chrome(service=service, options=options) if service
                  else webdriver.Chrome(options=options))
        driver.get('https://hh.ru/applicant/resumes')
        time.sleep(2.5)

        if 'login' in driver.current_url:
            logger.warning("Требуется авторизация в аккаунте hh.ru")
            return []

        # Ищем все карточки резюме
        cards = driver.find_elements(By.CSS_SELECTOR, '[data-qa="resume-card"], [data-qa="resume"], div[data-qa*="resume"]')
        if not cards:
            cards = driver.find_elements(By.CSS_SELECTOR, 'div[class*="resume"]')

        seen = set()
        for c in cards:
            try:
                link = c.find_element(By.CSS_SELECTOR, 'a[href*="/resume/"]')
                href = link.get_attribute('href') or ''
                rid = href.split('/resume/')[1].split('?')[0].split('/')[0]
                if len(rid) >= 30 and rid not in seen:
                    seen.add(rid)
                    
                    # Ищем целевую должность (position)
                    title = ""
                    for sel in ['[data-qa*="title"]', 'h3', 'a[data-qa*="title"]', 'strong']:
                        try:
                            t_el = c.find_element(By.CSS_SELECTOR, sel)
                            t_text = t_el.text.strip()
                            if t_text and not any(m in t_text.lower() for m in ['постоянная работа', 'подработка']):
                                title = t_text
                                break
                        except Exception:
                            pass

                    status = "Опубликовано"
                    for sel in ['[data-qa*="status"]', '[class*="status"]']:
                        try:
                            s_el = c.find_element(By.CSS_SELECTOR, sel)
                            if s_el.text.strip():
                                status = s_el.text.strip()
                                break
                        except Exception:
                            pass

                    resumes.append({
                        'id': rid,
                        'title': title or "Резюме соискателя",
                        'status': status,
                        'url': f"https://hh.ru/resume/{rid}"
                    })
            except Exception:
                continue

        # Уточняем точные должности с каждой страницы резюме
        for r in resumes:
            if r['title'] == "Резюме соискателя" or not r['title']:
                try:
                    driver.get(r['url'])
                    time.sleep(1.2)
                    pos_el = driver.find_element(By.CSS_SELECTOR, '[data-qa="resume-block-title-position"]')
                    if pos_el.text.strip():
                        r['title'] = pos_el.text.strip()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Не удалось получить резюме из аккаунта: {explain_error(e)}")
        log_detail(f"Получение резюме из аккаунта: {e!r}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return resumes


def _offer_profile_import():
    """После выбора резюме — профиль из него. Иначе письма и ответы работодателям
    писались бы по профилю прежнего резюме (или по пустому у нового пользователя)."""
    try:
        answer = input(f"{BOLD}Заполнить профиль (опыт, навыки, «О себе») из этого резюме? [Y/n]: {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer in ('', 'y', 'д', 'да', 'yes'):
        import subprocess
        subprocess.run([sys.executable, os.path.join(CODE_DIR, 'resume_updater.py'), '--import-profile'],
                       cwd=SCRIPT_DIR)


def interactive_resume_picker():
    """Интерактивное консольное меню выбора резюме."""
    current_id, current_title = get_active_resume()

    print(f"\n{CYAN}{BOLD}{'='*60}{RESET}")
    print(f"{RED}{BOLD}              ВЫБОР ЦЕЛЕВОГО РЕЗЮМЕ HH.RU{RESET}")
    print(f"{CYAN}{BOLD}{'='*60}{RESET}")
    print(f"Текущее резюме: {GREEN}{BOLD}{current_title}{RESET}")
    print(f"ID:             {GRAY}{current_id}{RESET}")
    print(f"{CYAN}{'-'*60}{RESET}")
    print(f"Поиск доступных резюме в вашем аккаунте hh.ru...")

    resumes = fetch_account_resumes(headless=True)

    if not resumes:
        print(f"{YELLOW}[!] Не удалось загрузить список резюме из браузера.{RESET}")
        print(f"  Чаще всего это значит, что вход на hh.ru не выполнен: [N] → [L].")
        print(f"  Или введите ID резюме вручную — он в адресе страницы резюме после /resume/.")
    else:
        print(f"\nНайдено резюме в вашем аккаунте ({len(resumes)}):\n")
        for idx, r in enumerate(resumes, 1):
            is_cur = " (активно)" if r['id'] == current_id else ""
            mark = f"{GREEN}{BOLD}[{idx}]{RESET}"
            print(f"  {mark} {BOLD}{r['title']}{RESET}{GREEN}{is_cur}{RESET}")
            print(f"      ID:     {GRAY}{r['id']}{RESET}")
            print(f"      Статус: {CYAN}{r['status']}{RESET}")
            print()

    print(f"  {WHITE}[M]{RESET} Ввести ID резюме вручную")
    print(f"  {RED}[0]{RESET} Назад в главное меню")
    print(f"{CYAN}{BOLD}{'='*60}{RESET}")

    try:
        numbers = f"1-{len(resumes)}, " if resumes else ""
        choice = input(f"{BOLD}Выберите номер резюме [{numbers}M, 0]: {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if choice in ('0', 'q', 'exit', 'quit'):
        return

    if choice.isdigit() and 1 <= int(choice) <= len(resumes):
        selected = resumes[int(choice) - 1]
        if _report_save(set_active_resume(selected['id'], selected['title']),
                        f"Резюме переключено на: {selected['title']}"):
            print(f"ID: {selected['id']}\n")
            _offer_profile_import()
        time.sleep(1.5)
        return

    if choice in ('m', 'ь'):
        try:
            custom_id = input(f"{BOLD}Вставьте ID резюме (из URL hh.ru/resume/XXXX): {RESET}").strip()
            if not custom_id:
                print(f"{RED}[X] ID не может быть пустым.{RESET}")
                return
            custom_title = input(f"{BOLD}Введите название/должность для этого резюме: {RESET}").strip() or "Пользовательское резюме"
            if _report_save(set_active_resume(custom_id, custom_title),
                            f"Резюме сохранено: {custom_title} ({custom_id})"):
                _offer_profile_import()
            time.sleep(1.5)
        except (EOFError, KeyboardInterrupt):
            return


def interactive_search_picker():
    """Интерактивное меню настройки направления поиска и фильтров."""
    active_preset = get_active_preset()

    print(f"\n{CYAN}{BOLD}{'='*60}{RESET}")
    print(f"{CYAN}{BOLD}       НАСТРОЙКА НАПРАВЛЕНИЯ ПОИСКА И ФИЛЬТРОВ{RESET}")
    print(f"{CYAN}{BOLD}{'='*60}{RESET}")
    print(f"Текущее направление: {GREEN}{BOLD}{active_preset['name']}{RESET}")
    print(f"{CYAN}{'-'*60}{RESET}")
    print(f"  {YELLOW}[1]{RESET} Python Developer / Backend (Django, FastAPI, Asyncio)")
    print(f"  {CYAN}[2]{RESET} DevOps / SRE / Kubernetes / Cloud")
    print(f"  {BLUE}[3]{RESET} Системный администратор / Сетевой инженер")
    print(f"  {MAGENTA}[4]{RESET} Свой поисковый запрос (ввести должность/навыки)")
    print(f"  {WHITE}[5]{RESET} Вставить готовую ссылку с фильтрами с сайта hh.ru")
    print(f"  {RED}[0]{RESET} Назад в главное меню")
    print(f"{CYAN}{BOLD}{'='*60}{RESET}")

    try:
        choice = input(f"{BOLD}Выберите вариант [1-5, 0]: {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if choice == '1':
        _report_save(set_active_preset('python'), "Выбрано направление: Python Developer / Backend")
    elif choice == '2':
        _report_save(set_active_preset('devops'), "Выбрано направление: DevOps / SRE / Cloud")
    elif choice == '3':
        _report_save(set_active_preset('sysadmin'), "Выбрано направление: Системный администратор / Сети")
    elif choice == '4':
        try:
            q = input(f"\n{BOLD}Введите поисковый запрос (например, 'frontend react' или 'data engineer'): {RESET}").strip()
            if q:
                _report_save(set_active_preset('custom', custom_query=q),
                             f"Направление поиска установлено на: {q}")
        except (EOFError, KeyboardInterrupt):
            return
    elif choice == '5':
        try:
            url = input(f"\n{BOLD}Вставьте полную ссылку поиска с фильтрами с hh.ru: {RESET}").strip()
            if url.startswith('http'):
                if _report_save(set_active_preset('custom', custom_url=url),
                                "Пользовательский URL поиска сохранён"):
                    print(f"{YELLOW}[!] Фильтр по заголовку вакансии при этом отключён: "
                          f"отбор целиком на стороне ссылки hh.ru.{RESET}\n")
            else:
                print(f"{RED}[X] Некорректная ссылка.{RESET}")
        except (EOFError, KeyboardInterrupt):
            return

    time.sleep(1.2)


def default_cover_letter(profile: dict, telegram: str = '') -> str:
    """Запасной шаблон письма из профиля — для любой профессии."""
    profile = profile or {}
    spec = str(profile.get('specialization') or '').split(':')[0].strip()
    skills = [str(x) for x in (profile.get('skills') or [])][:4]
    years = profile.get('experience_years')
    parts = ["Добрый день! Заинтересован в данной позиции."]
    if spec:
        parts.append(f"Моя специализация — {spec}" + (f", опыт около {round(float(years))} лет." if years else "."))
    if skills:
        parts.append(f"Основные инструменты и навыки: {', '.join(skills)}.")
    parts.append("Буду рад обсудить задачи компании.")
    text = ' '.join(parts)
    return text + (f"\n\nДля оперативной связи: Telegram {telegram}" if telegram else '')


def interactive_cover_letter_editor():
    """Интерактивное меню настройки текста сопроводительного письма, контактов и Gemini API."""
    cfg = load_config()
    current_letter = cfg.get('cover_letter', '')
    # Все три секции могут прийти из конфига как null, поэтому `or {}`, а не .get(k, {}).
    profile = cfg.get('candidate_profile') or {}
    contacts = profile.get('contacts') or {}
    # Пусто по умолчанию: зашитый сюда чужой Telegram уходил бы в письма нового пользователя.
    current_tg = contacts.get('telegram') or ''
    ai_cfg = cfg.get('ai_config') or {}
    gemini_key = ai_cfg.get('api_key') or os.environ.get('GEMINI_API_KEY', '')
    use_custom = cfg.get('use_custom_template', False)

    while True:
        print(f"\n{CYAN}{BOLD}{'='*65}{RESET}")
        print(f"{CYAN}{BOLD}       НАСТРОЙКА СОПРОВОДИТЕЛЬНОГО ПИСЬМА И КОНТАКТОВ{RESET}")
        print(f"{CYAN}{BOLD}{'='*65}{RESET}")
        tg_status = f"{GREEN}{BOLD}{current_tg}{RESET}" if current_tg else f"{YELLOW}[НЕ ЗАДАН - пункт 2]{RESET}"
        print(f"  Telegram для связи:  {tg_status}")
        api_status = f"{GREEN}[АКТИВЕН]{RESET}" if gemini_key else f"{YELLOW}[НЕ ЗАДАН - работает адаптивный генератор]{RESET}"
        print(f"  Ключ Google Gemini:  {api_status}")
        mode_str = ("Пользовательский фиксированный шаблон" if use_custom
                    else "Умный генератор: ИИ пишет под каждую вакансию по вашему профилю")
        print(f"  Текущий режим:       {CYAN}{BOLD}{mode_str}{RESET}")
        print(f"{CYAN}{'-'*65}{RESET}")
        print(f"{BOLD}Текущий каркас письма:{RESET}")
        print(f"{GRAY}{'-'*65}{RESET}")
        for line in current_letter.split('\n'):
            print(f"  {line}")
        print(f"{GRAY}{'-'*65}{RESET}")
        print(f"  {GREEN}[1]{RESET} Ввести новый каркас / шаблон письма")
        print(f"  {YELLOW}[2]{RESET} Изменить Telegram для оперативной связи")
        print(f"  {MAGENTA}[3]{RESET} Ввести / изменить ключ Google Gemini для умных писем")
        print(f"  {CYAN}[4]{RESET} Переключить режим: Умный генератор <-> Фиксированный шаблон")
        print(f"  {WHITE}[5]{RESET} Собрать шаблон заново из вашего профиля")
        print(f"  {RED}[0]{RESET} Сохранить и вернуться в главное меню")
        print(f"{CYAN}{BOLD}{'='*65}{RESET}")

        try:
            choice = input(f"{BOLD}Выберите действие [1-5, 0]: {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice in ('0', 'q', 'exit', 'quit'):
            break

        if choice == '1':
            print(f"\n{BOLD}Введите новый текст сопроводительного письма.{RESET}")
            print(f"{GRAY}(Для сохранения нажмите Enter на пустой строке){RESET}")
            lines = []
            try:
                first_line = input(f"{BOLD}Строка 1: {RESET}")
                if first_line.strip():
                    lines.append(first_line)
                    while True:
                        l = input()
                        if not l.strip():
                            break
                        lines.append(l)
                    new_text = "\n".join(lines).strip()
                    if new_text:
                        current_letter = new_text
                        cfg['cover_letter'] = current_letter
                        _report_save(save_config(cfg), "Шаблон письма обновлён")
            except (EOFError, KeyboardInterrupt):
                continue

        elif choice == '2':
            try:
                new_tg = input(f"\n{BOLD}Введите ваш Telegram (например, @username): {RESET}").strip()
                if new_tg:
                    if not new_tg.startswith('@') and not new_tg.startswith('http'):
                        new_tg = f"@{new_tg}"
                    current_tg = new_tg
                    if not isinstance(cfg.get('candidate_profile'), dict):
                        cfg['candidate_profile'] = {}
                    if not isinstance(cfg['candidate_profile'].get('contacts'), dict):
                        cfg['candidate_profile']['contacts'] = {}
                    cfg['candidate_profile']['contacts']['telegram'] = current_tg
                    if 'Telegram' in current_letter:
                        current_letter = re.sub(r'Telegram:?\s*@[A-Za-z0-9_]+', f'Telegram {current_tg}', current_letter)
                        cfg['cover_letter'] = current_letter
                    _report_save(save_config(cfg), f"Telegram обновлён: {current_tg}")
            except (EOFError, KeyboardInterrupt):
                continue

        elif choice == '3':
            try:
                print(f"\n{BOLD}Получить бесплатный ключ Gemini можно на:{RESET} {CYAN}https://aistudio.google.com/app/apikey{RESET}")
                new_key = input(f"{BOLD}Вставьте ключ Gemini API (или оставьте пустым для отмены): {RESET}").strip()
                if new_key:
                    if not isinstance(cfg.get('ai_config'), dict):
                        cfg['ai_config'] = {}
                    cfg['ai_config']['api_key'] = new_key
                    cfg['ai_config']['enabled'] = True
                    gemini_key = new_key
                    if not _report_save(save_config(cfg), "Ключ Gemini API сохранён"):
                        gemini_key = ''
                    else:
                        print(f"Теперь бот будет генерировать уникальные письма под каждую вакансию.\n")
            except (EOFError, KeyboardInterrupt):
                continue

        elif choice == '4':
            use_custom = not use_custom
            cfg['use_custom_template'] = use_custom
            new_mode = "Пользовательский фиксированный шаблон" if use_custom else "Умный адаптивный генератор"
            if not _report_save(save_config(cfg), f"Режим переключён на: {new_mode}"):
                use_custom = not use_custom  # на диске режим не изменился — не врём в шапке меню

        elif choice == '5':
            current_letter = default_cover_letter(profile, current_tg)
            cfg['cover_letter'] = current_letter
            _report_save(save_config(cfg), "Шаблон собран из профиля")

        time.sleep(1.2)


def prompt_gemini_key():
    """Быстрый ввод ключа Google Gemini API с авто-валидацией и подбором модели."""
    cfg = load_config()
    if not isinstance(cfg.get('ai_config'), dict):
        cfg['ai_config'] = {}
    current_key = cfg['ai_config'].get('api_key') or os.environ.get('GEMINI_API_KEY', '')
    current_model = cfg['ai_config'].get('model', 'gemini-flash-latest')

    print(f"\n{CYAN}{BOLD}{'='*65}{RESET}")
    print(f"{CYAN}{BOLD}        КЛЮЧ GOOGLE GEMINI ДЛЯ УМНЫХ ПИСЕМ{RESET}")
    print(f"{CYAN}{BOLD}{'='*65}{RESET}")
    if current_key:
        masked = current_key[:6] + "..." + current_key[-4:] if len(current_key) > 10 else "***"
        print(f"  Текущий ключ:    {GREEN}{masked}{RESET}")
        # Идентификатор модели пользователю ничего не говорит — в журнал.
        print(f"  Умные письма:    {GREEN}включены{RESET}")
        log_detail(f"Модель Gemini: {current_model}")
    else:
        print(f"  Текущий ключ:    {YELLOW}Не задан — письма собираются по шаблону{RESET}")
    print(f"  Бесплатный ключ можно получить на: {CYAN}https://aistudio.google.com/app/apikey{RESET}")
    print(f"{CYAN}{'-'*65}{RESET}")

    try:
        new_key = input(f"{BOLD}Вставьте Gemini API ключ (или Enter для отмены): {RESET}").strip()
        if new_key:
            print(f"\n[*] Проверяю ключ и подбираю подходящий режим...")
            chosen_model = 'gemini-flash-latest'
            try:
                import google.generativeai as genai
                genai.configure(api_key=new_key)
                supported = []
                for m in genai.list_models():
                    if 'generateContent' in m.supported_generation_methods:
                        supported.append(m.name.replace('models/', ''))

                preferred = ['gemini-flash-latest', 'gemini-2.5-flash-lite', 'gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash']
                for p in preferred:
                    if p in supported:
                        chosen_model = p
                        break
                else:
                    flashes = [s for s in supported if 'flash' in s and 'preview' not in s and 'image' not in s and 'tts' not in s]
                    chosen_model = flashes[0] if flashes else (supported[0] if supported else 'gemini-flash-latest')

                # Проверяем тестовый запрос к выбранной модели
                test_m = genai.GenerativeModel(chosen_model)
                test_m.generate_content('test')
                print(f"{GREEN}[OK] Ключ успешно проверен и активен!{RESET}")
                print(f"     Подходящий режим подобран автоматически.")
                log_detail(f"Выбрана модель Gemini: {chosen_model}")
            except Exception as e:
                err_msg = str(e)
                if 'API_KEY_INVALID' in err_msg or 'API key not valid' in err_msg:
                    print(f"{RED}[X] Ключ Google Gemini не подошёл.{RESET}")
                    print(f"    Проверьте, что скопировали ключ полностью без лишних пробелов.")
                    time.sleep(2)
                    return
                else:
                    print(f"{YELLOW}[!] Не удалось проверить ключ через интернет: {explain_error(e)}{RESET}")
                    print(f"    Будет использован режим по умолчанию.")
                    log_detail(f"Проверка ключа Gemini: {e!r}; модель по умолчанию {chosen_model}")

            cfg['ai_config']['api_key'] = new_key
            cfg['ai_config']['model'] = chosen_model
            cfg['ai_config']['enabled'] = True
            if _report_save(save_config(cfg), "Конфигурация Gemini сохранена"):
                print(f"Теперь бот будет генерировать уникальные письма под каждую вакансию.\n")
        else:
            print(f"\n{GRAY}Изменения не внесены.{RESET}\n")
    except (EOFError, KeyboardInterrupt):
        return
    time.sleep(1.5)


def prompt_groq_key():
    """Ввод ключа Groq — запасного ИИ, когда Gemini упирается в суточную квоту.

    У бесплатного Gemini 20 запросов в сутки на модель, у Groq — 1000.
    Ключ хранится отдельно (`ai_config.groq_api_key`), основной провайдер не меняется:
    бот сам уходит на запасной, когда основной отвечает отказом по лимиту.
    """
    cfg = load_config()
    if not isinstance(cfg.get('ai_config'), dict):
        cfg['ai_config'] = {}
    current = cfg['ai_config'].get('groq_api_key') or os.environ.get('GROQ_API_KEY', '')
    try:
        from ai_assistant import GROQ_PREFERRED_MODELS
    except Exception:
        GROQ_PREFERRED_MODELS = ('openai/gpt-oss-120b',)
    model = cfg['ai_config'].get('groq_model') or GROQ_PREFERRED_MODELS[0]

    print(f"\n{CYAN}{BOLD}{'='*65}{RESET}")
    print(f"{CYAN}{BOLD}        ЗАПАСНОЙ ИИ (GROQ) ДЛЯ УМНЫХ ПИСЕМ{RESET}")
    print(f"{CYAN}{BOLD}{'='*65}{RESET}")
    if current:
        masked = current[:6] + "..." + current[-4:] if len(current) > 10 else "***"
        print(f"  Текущий ключ:    {GREEN}{masked}{RESET}")
        print(f"  Умные письма:    {GREEN}включены{RESET}")
        log_detail(f"Модель Groq: {model}")
    else:
        print(f"  Текущий ключ:    {YELLOW}Не задан{RESET}")
    print(f"  Зачем нужен: у основного ИИ бесплатно всего 20 запросов в сутки.")
    print(f"  Когда он закончится, бот сам перейдёт на запасной и продолжит работу.")
    print(f"  Бесплатный ключ можно получить на: {CYAN}https://console.groq.com/keys{RESET}")
    print(f"{CYAN}{'-'*65}{RESET}")

    try:
        new_key = input(f"{BOLD}Вставьте ключ Groq (или Enter для отмены): {RESET}").strip()
        if not new_key:
            print(f"\n{GRAY}Изменения не внесены.{RESET}\n")
            time.sleep(1.2)
            return

        print(f"\n[*] Проверяю ключ и подбираю подходящий режим...")
        ok = False
        try:
            from openai import OpenAI
            client = OpenAI(api_key=new_key,
                            base_url='https://api.groq.com/openai/v1',
                            timeout=20)
            # Набор моделей отличается между аккаунтами, поэтому спрашиваем у Groq,
            # что доступно именно этому ключу, и берём лучшую из доступных.
            try:
                available = {m.id for m in client.models.list().data}
                for candidate in GROQ_PREFERRED_MODELS:
                    if candidate in available:
                        model = candidate
                        break
                else:
                    chat_models = sorted(
                        m for m in available
                        if not any(x in m for x in ('whisper', 'orpheus', 'guard', 'tts'))
                    )
                    if chat_models:
                        model = chat_models[0]
            except Exception as e:
                logger.debug(f"Не удалось получить список моделей Groq: {e}")
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ответь одним словом: ок"}],
            )
            ok = bool(resp and resp.choices)
        except Exception as e:
            err = str(e)
            if 'invalid_api_key' in err or '401' in err:
                print(f"{RED}[X] Ключ Groq не подошёл.{RESET}")
                print(f"    Проверьте, что скопировали его полностью без лишних пробелов.")
                time.sleep(2)
                return
            print(f"{YELLOW}[!] Не удалось проверить ключ через интернет: {explain_error(e)}{RESET}")
            log_detail(f"Проверка ключа Groq: {e!r}")
            print(f"    Ключ всё равно будет сохранён.")

        if ok:
            print(f"{GREEN}[OK] Ключ проверен и работает!{RESET}")

        cfg['ai_config']['groq_api_key'] = new_key
        cfg['ai_config']['groq_model'] = model
        if _report_save(save_config(cfg), "Запасной ИИ настроен"):
            print(f"Теперь письма не прервутся, даже когда основной ИИ исчерпает лимит.\n")
    except (EOFError, KeyboardInterrupt):
        return
    time.sleep(1.5)

if __name__ == '__main__':
    args = sys.argv[1:]
    if '--pick-resume' in args:
        interactive_resume_picker()
    elif '--pick-search' in args:
        interactive_search_picker()
    elif '--edit-letter' in args or '--edit-template' in args:
        interactive_cover_letter_editor()
    elif '--set-gemini-key' in args:
        prompt_gemini_key()
    elif '--selfcheck-grade' in args:
        # Проверка фильтра грейда: ничего не отправляет, только считает.
        assert not check_title_grade('Заместитель Председателя Правления по IT', 3)[0]
        assert not check_title_grade('Директор департамента разработки', 3)[0]
        assert check_title_grade('Заместитель Председателя Правления по IT', 12)[0], 'опыт 12 лет — пропускаем'
        for ok_title in ('Ведущий специалист по разработке',
                         'Главный специалист по данным',
                         'Senior Backend Engineer', 'Lead DevOps Engineer',
                         'Эксперт по аналитике',
                         'Специалист по тестированию'):
            assert check_title_grade(ok_title, 3)[0], f'ложное срабатывание: {ok_title}'
        assert get_candidate_experience_years({}) == 0, 'нет профиля — считаем опыт нулевым'
        print(c_ok('Фильтр грейда: все проверки пройдены'))
    else:
        rid, rtitle = get_active_resume()
        preset = get_active_preset()
        print(f"Активное резюме: {rtitle} ({rid})")
        print(f"Направление поиска: {preset['name']}")

# Вопросы, которые встречаются в анкетах и чатах чаще всего. Показываются первыми,
# чтобы человек сразу увидел, что именно уйдёт работодателю от его имени.
COMMON_ANSWER_KEYS = (
    ('зарплат', 'Зарплатные ожидания'),
    ('в каком городе', 'Город проживания'),
    ('переезд', 'Готовность к переезду'),
    ('сменный график', 'Сменный график и ночные смены'),
    ('удалённ', 'Формат работы'),
    ('стаж', 'Стаж работы'),
    ('воинск', 'Воинский учёт'),
    ('гражданство', 'Гражданство'),
    ('образование', 'Образование'),
    ('английск', 'Английский язык'),
    ('телеграм', 'Телеграм для связи'),
    ('когда готовы приступить', 'Когда готов приступить'),
)


def _toggle_label(value) -> str:
    return f"{GREEN}вкл{RESET}" if value else f"{YELLOW}выкл{RESET}"


def _edit_word_list(cfg, key, title, hint):
    """Правка списка строк в настройках: добавить, убрать, показать."""
    words = cfg.get(key)
    if not isinstance(words, list):
        words = []
    while True:
        print(f"\n{BOLD}{title}{RESET}  ({len(words)})")
        print(f"{DIM}  {hint}{RESET}")
        for i in range(0, len(words), 4):
            print('  ' + ' · '.join(words[i:i + 4]))
        try:
            raw = input("Добавить: +слово   убрать: -слово   Enter — назад: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw:
            return
        sign, word = raw[0], raw[1:].strip()
        if sign not in '+-' or not word:
            print(f"{YELLOW}[!] Начните с + или -{RESET}")
            continue
        if sign == '+' and word.lower() not in (w.lower() for w in words):
            words.append(word)
        elif sign == '-':
            words = [w for w in words if w.lower() != word.lower()]
        cfg[key] = words
        _report_save(save_config(cfg), f"{title}: сохранено")


def edit_bot_behavior():
    """Меню [M]: всё, как бот ведёт себя от имени пользователя, — в одном месте."""
    while True:
        cfg = load_config()
        policy = cfg.get('answer_policy') or {}
        chat = cfg.get('chat_autoreply') or {}
        email = cfg.get('email_outreach') or {}
        items = [
            ('limit', f"Откликов в сутки: {BOLD}{cfg.get('max_applications', 200)}{RESET}"),
            ('yes', f"«Да» на вопросы о готовности и условиях (офис, переезд, ИП, график): "
                    f"{_toggle_label(policy.get('yes_to_conditions', True))}"),
            ('detailed', f"Развёрнутые ответы ИИ на вопросы про опыт: "
                         f"{_toggle_label(policy.get('detailed_answers', True))}"),
            ('letter_chat', f"Дописывать письмо в чат, если в форме отклика нет поля: "
                            f"{_toggle_label(cfg.get('letter_after_response', True))}"),
            ('chat', f"Отвечать ботам-ассистентам работодателя в чате: "
                     f"{_toggle_label(chat.get('enabled', True))}"
                     f", не больше {chat.get('max_per_run', 10)} за прогон"),
            ('resume', f"Самому вносить правки в резюме по разбору отказов: "
                       f"{_toggle_label(cfg.get('auto_apply_resume', False))}"),
            ('blocked', f"Нежелательные работодатели: {len(cfg.get('blocked_employers') or [])}"),
            ('exclude', f"Слова-исключения в названиях вакансий: "
                        f"{len(cfg.get('keywords_exclude') or []) or 'встроенный список'}"),
            ('email', f"Письма работодателям на почту: {_toggle_label(email.get('enabled', False))}"),
        ]
        items.append(
            ('topup', f"Добирать по дополнительным запросам, когда основные вакансии кончились: "
                      f"{_toggle_label(cfg.get('topup_enabled', True))}"))
        print(f"\n{CYAN}{BOLD}{'=' * 62}{RESET}")
        print(f"{CYAN}{BOLD}   ПОВЕДЕНИЕ БОТА{RESET}")
        print(f"{CYAN}{BOLD}{'=' * 62}{RESET}")
        for i, (_, label) in enumerate(items, 1):
            print(f"  {CYAN}[{i}]{RESET} {label}")
        print(f"  {RED}[0]{RESET} Назад")
        try:
            choice = input(f"{BOLD}Что изменить? {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ('', '0'):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(items):
            print(f"{YELLOW}[!] Нет такого пункта{RESET}")
            continue
        key = items[int(choice) - 1][0]
        if key == 'limit':
            try:
                n = int(input("Сколько откликов в сутки (1-500): ").strip())
            except (ValueError, EOFError, KeyboardInterrupt):
                print(f"{YELLOW}[!] Нужно число{RESET}")
                continue
            cfg['max_applications'] = max(1, min(500, n))
        elif key in ('yes', 'detailed'):
            field = 'yes_to_conditions' if key == 'yes' else 'detailed_answers'
            policy[field] = not policy.get(field, True)
            cfg['answer_policy'] = policy
        elif key == 'letter_chat':
            cfg['letter_after_response'] = not cfg.get('letter_after_response', True)
        elif key == 'chat':
            chat['enabled'] = not chat.get('enabled', True)
            if chat['enabled']:
                try:
                    raw = input(f"Сколько ответов за прогон (сейчас {chat.get('max_per_run', 10)}, Enter — оставить): ").strip()
                    if raw:
                        chat['max_per_run'] = max(1, min(50, int(raw)))
                except (ValueError, EOFError, KeyboardInterrupt):
                    pass
            cfg['chat_autoreply'] = chat
        elif key == 'resume':
            cfg['auto_apply_resume'] = not cfg.get('auto_apply_resume', False)
        elif key == 'blocked':
            _edit_word_list(cfg, 'blocked_employers', 'Нежелательные работодатели',
                            'Название компании или его часть: «Web3 Tech». На них бот не откликается.')
            continue
        elif key == 'exclude':
            if not cfg.get('keywords_exclude'):
                cfg['keywords_exclude'] = list(STRICT_TITLE_EXCLUDE_KEYWORDS)
            _edit_word_list(cfg, 'keywords_exclude', 'Слова-исключения',
                            'Вакансия со словом в названии пропускается. Короткие слова (hr, ит) — целым словом.')
            continue
        elif key == 'email':
            email['enabled'] = not email.get('enabled', False)
            if email['enabled'] and not email.get('app_password'):
                print(f"{YELLOW}[!] Для отправки нужен пароль приложения почты (email_outreach.app_password).{RESET}")
            cfg['email_outreach'] = email
        elif key == 'topup':
            cfg['topup_enabled'] = not cfg.get('topup_enabled', True)
        _report_save(save_config(cfg), 'Сохранено')


def edit_question_answers():
    """Показывает и правит ответы, которые бот даёт работодателям."""
    cfg = load_config()
    answers = cfg.setdefault('question_answers', {})

    while True:
        print()
        print(f"{RED}{BOLD}{'=' * 62}{RESET}")
        print(f"{RED}{BOLD}   ОТВЕТЫ НА ВОПРОСЫ РАБОТОДАТЕЛЕЙ{RESET}")
        print(f"{RED}{BOLD}{'=' * 62}{RESET}")
        print(f"{DIM}  Эти ответы бот подставляет в анкеты и пишет в чат от вашего имени.{RESET}")
        print()

        shown = []
        for key, title in COMMON_ANSWER_KEYS:
            value = answers.get(key)
            shown.append(key)
            num = len(shown)
            if value:
                print(f"  {CYAN}[{num}]{RESET} {BOLD}{title}{RESET}")
                print(f"      {value[:110]}")
            else:
                print(f"  {CYAN}[{num}]{RESET} {BOLD}{title}{RESET}  {YELLOW}(не задан){RESET}")
        print()
        extra = len(answers) - sum(1 for k in shown if k in answers)
        if extra > 0:
            print(f"{DIM}  Ещё {extra} ответов на похожие формулировки правятся вместе с основным.{RESET}")
        neutral = cfg.get('neutral_answer') or ''
        print(f"  {CYAN}[N]{RESET} {BOLD}Ответ на незнакомый вопрос{RESET}")
        print(f"      {neutral or 'не задан — такие вакансии пропускаются'}")
        print()
        print(f"  {CYAN}[+]{RESET} Добавить свой вопрос и ответ")
        print(f"  {CYAN}[-]{RESET} Удалить ответ по ключевому слову")
        print(f"  {CYAN}[A]{RESET} Показать все ответы списком")
        print(f"  {RED}[0]{RESET} Назад")
        print(f"{RED}{BOLD}{'=' * 62}{RESET}")

        try:
            choice = input(f"{BOLD}Что изменить? {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if choice in ('0', '', 'q', 'й'):
            return

        if choice == 'a':
            print()
            for k in sorted(answers):
                print(f"  {k:32} {str(answers[k])[:70]}")
            continue

        if choice == '+':
            print(f"{DIM}  Ключевое слово ищется в тексте вопроса: «воинск» подойдёт к «Отношение")
            print(f"  к воинской обязанности». Ответ уходит только на короткие вопросы;")
            print(f"  на развёрнутые («опишите опыт…») отвечает ИИ по профилю.{RESET}")
            try:
                key = input("Ключевое слово: ").strip().lower()
                value = input("Ответ: ").strip() if key else ''
            except (EOFError, KeyboardInterrupt):
                continue
            if key and value:
                answers[key] = value
                _report_save(save_config(cfg), f'Ответ на «{key}» сохранён')
            continue

        if choice == '-':
            try:
                key = input("Ключевое слово ответа, который убрать: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                continue
            if key in answers:
                answers.pop(key)
                _report_save(save_config(cfg), f'Ответ на «{key}» убран')
            elif key:
                print(f"{YELLOW}[!] Такого ключа нет — список: [A]{RESET}")
            continue

        if choice == 'n':
            print(f"{DIM}  Пусто — вакансии с незнакомым вопросом будут пропускаться.{RESET}")
            try:
                new = input("Новый ответ: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            cfg['neutral_answer'] = new
            _report_save(save_config(cfg), 'Ответ на незнакомый вопрос сохранён')
            continue

        if not choice.isdigit() or not (1 <= int(choice) <= len(shown)):
            print(f"{YELLOW}[!] Нет такого пункта{RESET}")
            continue

        key = shown[int(choice) - 1]
        title = dict(COMMON_ANSWER_KEYS)[key]
        print()
        print(f"{BOLD}{title}{RESET}")
        print(f"  сейчас: {answers.get(key) or '(не задан)'}")
        print(f"{DIM}  Пустая строка — оставить как есть. Слово «убрать» — стереть ответ.{RESET}")
        try:
            new = input("Новый ответ: ").strip()
        except (EOFError, KeyboardInterrupt):
            continue
        if not new:
            continue

        # Одну формулировку правим во ВСЕХ похожих ключах: иначе «зарплатные
        # ожидания» и «заработная плата» разъедутся, и часть вопросов получит
        # старый ответ.
        family = [k for k in list(answers) if _same_answer_family(k, key)]
        if key not in family:
            family.append(key)

        if new.lower() in ('убрать', 'удалить', '-'):
            for k in family:
                answers.pop(k, None)
            _report_save(save_config(cfg), f'Ответ «{title}» убран')
            continue

        for k in family:
            answers[k] = new
        answers[key] = new
        _report_save(save_config(cfg), f'Ответ «{title}» сохранён ({len(family)} формулировок)')


# Группы ключей, которые описывают один и тот же вопрос разными словами.
ANSWER_FAMILIES = (
    ('зарплат', 'оклад', 'вилку', 'доход', 'заработн', 'сколько хотите получать',
     'на какой уровень', 'уровень оплаты', 'по деньгам', 'финансовые ожидания', 'зп '),
    ('город', 'где вы живете', 'где проживаете', 'локация', 'место жительства',
     'откуда вы', 'формат работы', 'какой формат', 'удаленн', 'удалённ'),
    ('переезд', 'релокац', 'сменить город'),
    ('сменный график', 'сменном график', 'ночные смены', 'ночным сменам',
     'графике 2/2', 'график 2/2', 'круглосуточн', 'вахт'),
    ('опыт работы', 'стаж', 'сколько лет'),
    ('образование', 'высшее образование'),
    ('английск', 'english'),
    ('телеграм', 'telegram', 'тг-аккаунт'),
    ('когда готовы приступить', 'когда сможете приступить'),
)


def _same_answer_family(key_a: str, key_b: str) -> bool:
    """Относятся ли два ключа к одному и тому же вопросу."""
    a, b = key_a.lower(), key_b.lower()
    for family in ANSWER_FAMILIES:
        if any(m in a for m in family) and any(m in b for m in family):
            return True
    return a == b

