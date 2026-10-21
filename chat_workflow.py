"""Traversal and durable follow-ups for the HH messenger."""

import hashlib
import json
import random
import logging
import os
import re
import tempfile
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from markdown_it import MarkdownIt


logger = logging.getLogger('rejection_analyzer')
_INLINE_MARKDOWN = MarkdownIt('commonmark', {'html': False})
CARD_SELECTOR = '[data-qa*="chatik-open-chat"], [class*="chat-cell"], a[href*="/chat/"]'
MESSAGE_SELECTOR = '[data-qa*="chatik-chat-message"], [class*="chat-bubble"]'


def peak_parallelism(entries):
    active = maximum = 0
    events = [(row['start'], 1) for row in entries] + [(row['end'], -1) for row in entries]
    for _, delta in sorted(events):
        active += delta
        maximum = max(maximum, active)
    return maximum


def check_parallel_chat_preparation(config):
    """Real provider requests through the chat preparation queue, with isolated synthetic state."""
    import threading
    from ai_assistant import AIAssistant, _AI_BUDGET
    from rejection_analyzer import RejectionAnalyzer

    ai = AIAssistant(config)
    ai.candidate_profile = {'skills': ['Python', 'REST API', 'CI/CD'], 'contacts': {}}
    bot = RejectionAnalyzer.__new__(RejectionAnalyzer)
    bot.ai_assistant = ai
    bot.config = {'chat_autoreply': {'enabled': True}}
    rows, intervals = [], []
    guard = threading.Lock()
    origin = time.perf_counter()
    call_step = ai._call_step

    def measured_step(step, *args):
        start = time.perf_counter() - origin
        text = call_step(step, *args)
        end = time.perf_counter() - origin
        if text:
            with guard:
                intervals.append({'provider': step, 'start': start, 'end': end})
        return text

    ai._call_step = measured_step

    def compose(question, *args, **kwargs):
        start = time.perf_counter() - origin
        token = _AI_BUDGET.set(90)
        text = None
        try:
            text = RejectionAnalyzer.compose_chat_reply(bot, question, *args, **kwargs)
            return text
        finally:
            _AI_BUDGET.reset(token)
            with guard:
                rows.append({'task': question, 'start': start, 'end': time.perf_counter() - origin,
                             'worker': threading.current_thread().name, 'ok': bool(text)})

    bot.compose_chat_reply = compose
    questions = ('Какие две проверки выполняете для REST API?',
                 'Как проверяете права доступа в приложении?',
                 'Какие проверки добавляете в CI/CD?')
    with tempfile.TemporaryDirectory(prefix='hh_chat_control_') as directory:
        bot._chat_state_path = lambda: os.path.join(directory, 'chat_actions.json')
        bot._chat_reply_futures, bot._chat_reply_queue = {}, []
        bot._collect_chat_replies = True
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='hh-chat-control') as pool:
            bot._chat_reply_pool = pool
            for index, question in enumerate(questions):
                chat = {'identity': f'control:{index}', 'chat_url': f'https://hh.ru/chat/{index}',
                        'messages': [{'text': question, 'isOut': False}]}
                bot._prepared_chat_reply(chat, question, 'Контрольная задача', 'Контроль', continuing=True)
            for future in bot._chat_reply_futures.values():
                future.result(timeout=100)

    report = {'ok': len(rows) == 3 and all(row['ok'] for row in rows) and peak_parallelism(intervals) > 1,
              'seconds': round(time.perf_counter() - origin, 2),
              'peak_workers': peak_parallelism(rows), 'peak_provider_requests': peak_parallelism(intervals),
              'tasks': sorted(rows, key=lambda row: row['start']), 'requests': intervals,
              'hh_messages_sent': 0}
    from rejection_analyzer import SCRIPT_DIR
    directory = os.path.join(SCRIPT_DIR, '.live_runs')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-parallel-ai.json')
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"[ИИ-контроль] Готово {sum(row['ok'] for row in rows)}/3 за {report['seconds']:.1f} с; "
          f"пик задач {report['peak_workers']}, запросов ИИ {report['peak_provider_requests']}. Отправок в HH: 0.")
    print(f'[ИИ-контроль] Отчёт: {path}')
    return report


REJECTION_MARKERS = (
    'вам отказали', 'не готовы пригласить', 'не готов пригласить',
    'не можем пригласить', 'не сможем пригласить', 'не готовы предложить',
    'не можем предложить', 'ваша кандидатура не', 'выбрали другого кандидата',
    'не прошли отбор', 'не подходите', 'ваш профиль не подходит',
    # 07.10 КСБ-СОФТ: ответ на наш вопрос о причине отказа
    'с более релевантным опытом', 'выбор сделан в пользу', 'решение в пользу другого',
    # 14.10: «на данном этапе мы не готовы двигаться дальше» — бот ответил на отказ как на вопрос
    'не готовы двигаться дальше', 'не готовы продолжить', 'не можем двигаться дальше',
    'не будем продолжать', 'решили не продолжать',
)
# Работодатель сам предлагает продолжить общение, но вопроса в сообщении нет:
# «хотим связаться», «давайте созвонимся», «оставьте контакт». Раньше такие
# сообщения бот не читал как требующие ответа (отвечал только на вопросы).
CONTACT_REQUEST_MARKERS = (
    'хотим связаться', 'хотели бы связаться', 'хотел бы связаться',
    'хотим с вами связаться', 'хотели бы с вами связаться',
    'хотим обсудить', 'хотели бы обсудить', 'давайте обсудим', 'давайте созвон',
    'предлагаем обсудить', 'предлагаю обсудить', 'давайте пообщаемся',
    'давайте познакомимся', 'приглашаем вас', 'приглашаю вас',
    # 15.10: «хотелось бы установить контакт» принято за отказ — и бот спросил причину
    'хотелось бы установить контакт', 'хотим установить контакт',
    'предлагаем наладить контакт', 'предлагаем выйти на связь',
)
CONTACT_DETAILS_REQUEST = re.compile(
    r'(?:ваш(?:и|его)?|свой)\s+(?:контактный\s+|рабочий\s+)?'
    r'(?:контакт\w*|телефон\w*|номер\w*|telegram\b|телеграм\w*|ник\b)'
    r'|(?:оставьте|напишите|укажите|пришлите|сообщите|дайте|поделитесь)\W+'
    r'(?:(?:мне|нам|пожалуйста)\W+){0,2}(?:контакт\w*|telegram\b|телеграм\w*|тг\b|ник\b)'
    r'|(?:как|где|куда)\b[^.!?\n]{0,35}(?:связаться с вами|с вами связаться|вам написать)', re.I)
CONTACT_COORDINATION = re.compile(
    r'(?:можем|можно|можете|могли бы|готовы|готов|удобно|удобнее|когда|во сколько)'
    r'[^.!?\n]{0,40}(?:созвон|пообщаться|поговорить|связаться|встретиться)', re.I)
SELECTION_UPDATE_MARKERS = (
    'продолжаем рассматривать кандидат', 'продолжаем рассматривать вашу кандидатуру',
    'кандидатура остается', 'кандидатура остаётся', 'кандидатура на рассмотрении',
    'решение пока не принято', 'решение еще не принято', 'решение ещё не принято',
    'анализируем результаты собеседований', 'анализировать результаты собеседований',
)
# Типовое «мы рассмотрим и, если подойдёт, свяжемся» ответа не требует.
CONTACT_CANNED_MARKERS = ('если навыки', 'если ваш опыт', 'если вы подойд', 'если ваша кандидатура',
                          'рассмотрим ваше резюме', 'рассмотрим ваш отклик')


# Служебные «приняли в работу»: передали резюме, рассмотрим, свяжемся. Ответа не требуют —
# 15.10 на такое бот писал абзац с опытом .
ACK_MARKERS = CONTACT_CANNED_MARKERS + (
    'передала ваше резюме', 'передал ваше резюме', 'передали ваше резюме', 'резюме передано',
    'передаю информацию работодателю', 'всё зафиксировал', 'все зафиксировал',
    'получили ваш отклик', 'получили ваше резюме', 'ознакомимся с вашим', 'изучим ваше резюме',
    'свяжемся с вами', 'вернёмся к вам', 'вернемся к вам',
    # 15.10: «рассмотрит Ваше резюме и позже сообщит о своём решении»
    'рассмотрит ваше резюме', 'рассмотрит ваш отклик', 'сообщит вам о своём решении',
    'сообщит вам о своем решении', 'сообщит о своём решении', 'сообщит о своем решении',
)
_ACTION_MARKERS = ('созвон', 'позвонить', 'собеседован', 'интервью', 'пригласить', 'приглаша',
                   'telegram', 'телеграм', 'whatsapp', 'ваш телефон', 'ваш контакт', 'оставьте')
BOT_DONE = re.compile(r'ваши ответы (?:отправлены|переданы) работодателю|передаю информацию работодателю|'
                      r'(?:всё|все) зафиксировал', re.I)


def strip_politeness_tells(text):
    """Убирает «пожалуйста»: 15.10 пользователь заметил, что так бот палится. Сообщение остаётся связным."""
    out = str(text or '')
    out = re.sub(r',\s*пожалуйста\s*,', ',', out, flags=re.I)
    out = re.sub(r'(^|[.!?]\s+)пожалуйста[!,.]?\s*(\w)',
                 lambda m: m.group(1) + m.group(2).upper(), out, flags=re.I)
    out = re.sub(r'\s*,?\s*пожалуйста,?', '', out, flags=re.I)
    return out.strip()


THANKS_ONLY = re.compile(r'^\W*(?:спасибо|благодарю|благодарим|хорошо|ок|окей|ясно|понятно|отлично|'
                         r'договорились|принято|добро)\W*(?:большое\W*)?$', re.I)

# Не дозвонились / просят позвонить им: уводим в Telegram, как делает сам пользователь («в тг удобно?»).
CALL_FAILED_MARKERS = ('не дозвон', 'не смогла дозвон', 'не смог дозвон', 'недозвон', 'автоответчик',
                       'не берете трубку', 'не берёте трубку', 'не могу дозвониться', 'абонент недоступен',
                       'перезвоните', 'не отвечает телефон')
_PHONE = re.compile(r'\+?\d[\d\s()\-]{9,}\d')
_ASK_THEM_TO_CONTACT = re.compile(r'позвони|наберит|свяжитесь|напишите|whatsapp|ватсап|вацап|вотсап|viber|вайбер',
                                  re.I)
TG_REDIRECTS = (
    'в тг удобно? {link}',
    'Давайте лучше в тг, так мне проще: {link}',
    'Мне удобнее в тг, напишите сюда: {link}',
    'Звонки сейчас неудобны. В тг получится? {link}',
)
TG_REDIRECTS_AFTER_MISSED = (
    'Не услышал звонок. В тг удобно? {link}',
    'Видимо, был не на связи. Давайте в тг: {link}',
    'в тг удобно? {link}',
)


def telegram_link(profile):
    handle = str(((profile or {}).get('contacts') or {}).get('telegram') or '').strip()
    handle = re.sub(r'^(?:https?://)?(?:t\.me/|telegram\.me/)?@?', '', handle)
    return f'https://t.me/{handle}' if handle else ''


TG_REFUSED = re.compile(r'(?:(?:тг|телеграм\w*|telegram)\W{0,12}(?:к сожалению\W{0,3})?(?:не\s?удобн|не получится|не подход|не пользу)'
                        r'|не\s?удобн\w*\W{0,6}(?:в\s+)?(?:тг|телеграм\w*|telegram)'
                        r'|нет\s+(?:рабочего\s+)?(?:тг|телеграм\w*|telegram))', re.I)

PHONE_ASK = re.compile(r'(?:ваш|свой|напишите|оставьте|продиктуйте|пришлите|скиньте)\W+(?:\w+\W+){0,2}?(?:номер|телефон)', re.I)


def strip_phone_numbers(text):
    """16.10: бот написал в чат номер пользователя. Номера телефонов в ответах не нужны никогда:
    предложения с номером убираются."""
    parts = [p for p in re.split(r'(?<=[.!?])\s+|\n+', str(text or '')) if p.strip()]
    kept = [p for p in parts if not _PHONE.search(p)]
    return '\n\n'.join(kept) if len(kept) != len(parts) else str(text or '')


def telegram_redirect(profile, incoming):
    """Ответ-перенаправление в Telegram или None, если звонить/писать на номер нас не просят."""
    link = telegram_link(profile)
    low = normalized(incoming)
    if not link or not low:
        return None
    if any(m in low for m in CALL_FAILED_MARKERS):
        return random.choice(TG_REDIRECTS_AFTER_MISSED).format(link=link)
    if _PHONE.search(incoming) and _ASK_THEM_TO_CONTACT.search(incoming):
        return random.choice(TG_REDIRECTS).format(link=link)
    if PHONE_ASK.search(low):
        return random.choice(TG_REDIRECTS).format(link=link)
    return None


def is_acknowledgement(text):
    """Сообщение «рассмотрим / передали резюме»: вопроса и просьбы нет, отвечать не нужно."""
    low = normalized(text)
    if not low or '?' in low:
        return False
    if THANKS_ONLY.match(low):
        return True
    if any(m in low for m in REJECTION_MARKERS) or any(m in low for m in _ACTION_MARKERS):
        return False
    if any(w in low for w in _ASKING_WORDS):
        return False
    return any(m in low for m in ACK_MARKERS)


def wants_contact(text):
    """Работодатель предлагает связаться/созвониться/обсудить детали (не вопрос и не отказ)."""
    low = normalized(text)
    if not low or any(m in low for m in REJECTION_MARKERS):
        return False
    # Past interviews and conditional future contact are not invitations now.
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', low):
        if any(m in sentence for m in CONTACT_CANNED_MARKERS):
            continue
        if re.search(r'(?:если|в случае|при положительном|при необходимости)[^.!?]*'
                     r'(?:решени|подойд|кандидатур|заинтерес|потребност)', sentence):
            continue
        if (any(m in sentence for m in CONTACT_REQUEST_MARKERS)
                or CONTACT_COORDINATION.search(sentence) or CONTACT_DETAILS_REQUEST.search(sentence)):
            return True
    return False


def contact_details_requested(text):
    low = normalized(text)
    return wants_contact(low) and bool(CONTACT_DETAILS_REQUEST.search(low) or re.search(
        r'(?:в|через)\s+(?:telegram\b|телеграм\w*|тг\b)', low))


def is_selection_update(text):
    low = normalized(text)
    return ('?' not in low and not wants_contact(low)
            and not any(word in low for word in _ASKING_WORDS)
            and any(marker in low for marker in SELECTION_UPDATE_MARKERS))


def strip_unsolicited_contacts(text, profile):
    handle = telegram_link(profile).rsplit('/', 1)[-1]
    parts = re.split(r'(?<=[.!?])\s+|\n+', str(text or ''))
    promotion = re.compile(r'(?:связаться|связи|пишите|напишите|обсудить|обсудим|удобнее|удобно)'
                           r'[^.!?\n]{0,55}(?:telegram\b|телеграм\w*|тг\b)'
                           r'|(?:https?://)?(?:t\.me|telegram\.me)/\S+', re.I)
    kept = [part for part in parts if not promotion.search(part)
            and not (handle and '@' + handle.lower() in part.lower())]
    return '\n\n'.join(kept).strip() if len(kept) != len(parts) else str(text or '')


EXTERNAL_CONTACT_REQUEST = re.compile(
    r'(?:напишите|пишите|свяжитесь|перейдите)\b[^.!?\n]{0,160}'
    r'(?:(?:в|через)\s+(?:telegram\b|телеграм\w*|тг\b)|https?://(?:t\.me|telegram\.me)/)', re.I)
EXTERNAL_ACTION_CLAIM = re.compile(
    r'\b(?:напишу|написал|пишу|свяжусь|связался|отправлю|отправил|позвоню|позвонил)\b'
    r'[^.!?\n]{0,70}(?:(?:в|через|на)\s+(?:telegram\b|телеграм\w*|тг\b|whatsapp\b|почт\w*)'
    r'|https?://(?:t\.me|telegram\.me)/)', re.I)
CHAT_ONLY_REPLY = 'Спасибо за приглашение! Готов обсудить детали здесь, в чате hh.'


def strip_external_action_claims(text):
    """The HH chat sender cannot promise or confirm communication in another service."""
    parts = re.split(r'(?<=[.!?])\s+|\n+', str(text or ''))
    kept = [part for part in parts if not EXTERNAL_ACTION_CLAIM.search(part)]
    if len(kept) == len(parts):
        return str(text or '')
    reply = '\n\n'.join(kept).strip()
    return reply if len(reply) >= 20 else CHAT_ONLY_REPLY


def contact_reply(profile, incoming=''):
    """Короткий ответ на предложение связаться: согласие и контакт из профиля."""
    contacts = (profile or {}).get('contacts') or {}
    telegram = str(contacts.get('telegram') or '').strip()
    if telegram and not telegram.startswith(('@', 'http')):
        telegram = '@' + telegram
    if telegram and contact_details_requested(incoming):
        return random.choice((
            'Здравствуйте! Благодарю за обратную связь. Да, готов обсудить детали. '
            f'Удобнее всего в Telegram: {telegram}. Если удобен другой формат, напишите здесь, подстроюсь.',
            f'Добрый день! Да, давайте обсудим. Мне проще всего в Telegram — {telegram}, '
            'но если удобнее здесь, пишите, подстроюсь.',
        ))
    return 'Да, готов обсудить. Какое время и формат встречи вам удобны?'


EXPERIENCE_YES_REPLY = 'Да, есть опыт. Готов рассказать подробнее.'
EXPERIENCE_YES_VARIANTS = (
    EXPERIENCE_YES_REPLY,
    'Да, с этим работал, могу рассказать подробнее.',
    'Да, сталкивался на практике, с радостью расскажу детали.',
    'Да, такой опыт есть. Если интересно, расскажу подробнее.',
)
_ASKING_WORDS = ('подскажите', 'укажите', 'напишите', 'назовите', 'озвучьте', 'сообщите', 'какие', 'какой',
                 'каков', 'сколько', 'интересует', 'уточните')
_EXPERIENCE_YES_NO = re.compile(
    r'есть ли у вас (?:опыт|практик)|работали ли|имеете ли (?:вы )?опыт|владеете ли|знакомы ли|'
    r'использовали ли|приходилось ли|имеется ли опыт|опыт работы с .{0,80}\?', re.I)
_DETAIL_WORDS = ('расскажите', 'опишите', 'подробн', 'как именно', 'какие именно', 'приведите пример',
                 'поделитесь', 'в чём заключал', 'в чем заключал')


TENURE_QUESTION = re.compile(
    r'сколько\s+(?:лет|времени)|как(?:ой|ов)\s+(?:у\s+вас\s+)?(?:общий\s+)?(?:срок|стаж)|стаж\w*\s+(?:работы|в\s)|'
    r'срок\w*\s+(?:практическ|работы)|как\s+долго|сколько\s+опыта|years\s+of\s+experience|how\s+(?:many|long)', re.I)


def tenure_line(cfg=None):
    """Конкретный стаж, если пользователь задал его (chat_autoreply.tenure_years), иначе None."""
    years = (cfg or {}).get('tenure_years')
    try:
        n = int(years)
    except (TypeError, ValueError):
        return None
    word = 'лет' if 11 <= n % 100 <= 14 else {1: 'год', 2: 'года', 3: 'года', 4: 'года'}.get(n % 10, 'лет')
    return f'Общий опыт работы — {n} {word}.'


def tenure_asked_again(chat, incoming):
    """Про стаж спрашивают повторно: в текущей реплике и в реплике до нашего последнего ответа.

    Первый раз отвечаем обобщённо («Да, такой опыт есть»), на повторный — цифрой (07.10, решение пользователя).
    """
    if not TENURE_QUESTION.search(incoming or ''):
        return False
    messages = chat.get('messages') or []
    last_out = max((i for i, m in enumerate(messages) if m.get('isOut')), default=-1)
    return any(not m.get('isOut') and TENURE_QUESTION.search(m.get('text') or '')
               for m in messages[:last_out])


def allowed_tail_lines(cfg=None):
    """Только явно настроенный стаж; старые суммы не обходят запрет на зарплату."""
    return [line for line in (tenure_line(cfg),) if line]


def allowed_salary_lines():
    return list(SALARY_CHAT_VARIANTS)


def salary_line(cfg=None, concrete=False):
    """Без суммы даже при повторном вопросе и наличии старой настройки."""
    from ai_assistant import SALARY_ANSWER
    return SALARY_ANSWER


# 15.10: одна и та же фраза «Готов обсудить условия на следующем этапе.» в сотнях чатов читается
# как шаблон. Смысл тот же (суммы нет), слова разные. Первая — прежняя SALARY_ANSWER.
SALARY_CHAT_VARIANTS = (
    'Готов обсудить условия на следующем этапе.',
    'По деньгам давайте на следующем этапе, когда станет понятнее объём задач.',
    'Цифру пока называть не буду: сначала хочется разобраться в задачах, дальше договоримся.',
    'Условия обсудим ближе к делу, многое зависит от задач и формата работы.',
    'Без деталей по задачам гадать не хочу, давайте вернёмся к этому на следующем этапе.',
)


def chat_salary_line(cfg=None, concrete=False):
    """Нейтральный ответ о деньгах без суммы."""
    from ai_assistant import SALARY_ANSWER
    line = salary_line(cfg, concrete)
    return line if line != SALARY_ANSWER else random.choice(SALARY_CHAT_VARIANTS)


# 15.10: на «если вы рассматриваете 500 тыс, то нет смысла» пользователь отвечает встречным
# вопросом «а вы сколько предлагаете?)». Так же отвечает бот, если мы уже давали ответ про деньги.
SALARY_PUSHBACK_REPLIES = (
    'а вы сколько предлагаете?)',
    'а у вас сколько по деньгам?)',
    'а вы что по деньгам предлагаете?)',
)
_MONEY_AMOUNT = re.compile(r'\d[\d\s.,]*\s*(?:тыс|т\.\s?р|к\b|k\b|руб|₽|000)', re.I)
_PUSHBACK_WORDS = ('нет смысла', 'дорого', 'не вписыва', 'выше нашего', 'завышен', 'не потянем', 'не подойд')
_OFFER_WORDS = ('предлагаем', 'предложим', 'можем предложить', 'готовы платить', 'готовы предложить',
                'мы платим', 'наш бюджет', 'наша вилка', 'зарплата составля')


def salary_pushback(chat, cfg, incoming):
    """Работодатель возражает по деньгам (называет сумму или «нет смысла»), а мы уже отвечали про зарплату."""
    low = normalized(incoming)
    if not low or '?' in low or any(w in low for w in _OFFER_WORDS):
        return False
    if not (_MONEY_AMOUNT.search(low) or any(w in low for w in _PUSHBACK_WORDS)):
        return False
    from ai_assistant import SALARY_ANSWER
    ours = {normalized(v) for v in SALARY_CHAT_VARIANTS + (SALARY_ANSWER,)}
    custom = normalized(str((cfg or {}).get('salary_answer') or ''))
    if custom:
        ours.add(custom)
    return any(m.get('isOut') and any(v and v in normalized(m.get('text', '')) for v in ours)
               for m in chat.get('messages') or [])


def salary_asked_again(chat, incoming):
    """О зарплате спрашивают повторно: в текущей реплике и в реплике до нашего последнего ответа."""
    if not split_salary(incoming)[1]:
        return False
    messages = chat.get('messages') or []
    last_out = max((i for i, m in enumerate(messages) if m.get('isOut')), default=-1)
    return any(not m.get('isOut') and split_salary(m.get('text') or '')[1] for m in messages[:last_out])


def split_salary(text):
    """(остаток без вопросов о зарплате, был ли прямой вопрос о зарплате).

    Прямой — вопрос о деньгах в форме вопроса или просьбы («какие ожидания?», «укажите доход»).
    Простое упоминание зарплаты в тексте вакансии вопросом не считается.
    """
    from ai_assistant import is_salary_question
    sentences = re.split(r'(?<=[.!?])\s+|\n+', str(text or ''))
    rest, asked = [], False
    for sentence in sentences:
        low = normalized(sentence)
        if low and is_salary_question(low) and ('?' in low or any(w in low for w in _ASKING_WORDS)):
            asked = True
        elif sentence.strip():
            rest.append(sentence.strip())
    return ' '.join(rest), asked


def experience_yes_no(text):
    """Один вопрос «есть ли опыт с X?» — без просьбы рассказать подробнее."""
    low = normalized(text)
    if not low or low.count('?') > 1 or any(w in low for w in _DETAIL_WORDS):
        return False
    return bool(_EXPERIENCE_YES_NO.search(low))


# Открытый вопрос: ответ пишется текстом, вариантов-кнопок у него нет.
OPEN_QUESTION = re.compile(r'почему|расскажите|опишите|какие|каким|с какими|каков|как вы|что вы|поделитесь|приведите', re.I)


REASON_REQUEST = (
    'Здравствуйте! Спасибо за обратную связь. Подскажите '
    'основную причину отказа: каких навыков или опыта не хватило для этой позиции? '
    'Это поможет мне учесть ваши требования и улучшить кандидатуру. Спасибо!'
)


def normalized(text):
    return ' '.join(str(text or '').split()).casefold()


def _rendered_reply(reply):
    # Parse inline formatting only: list numbers and all factual text must survive.
    pieces = []
    for block in _INLINE_MARKDOWN.parseInline(str(reply or '')):
        for token in block.children or []:
            if token.type in ('text', 'code_inline'):
                pieces.append(token.content)
            elif token.type in ('softbreak', 'hardbreak'):
                pieces.append('\n')
            elif token.type not in ('strong_open', 'strong_close', 'em_open', 'em_close'):
                # Links and images carry information beyond visible text.
                return ''
    return normalized(''.join(pieces))


def confirms_reply(message, reply):
    expected = normalized(reply)
    if not expected or not message.get('isOut') or message.get('pending') or message.get('failed'):
        return False
    actual = normalized(message.get('raw_text', message.get('text')))
    if expected in actual:
        return True
    rendered = _rendered_reply(reply)
    return bool(rendered and rendered in actual)


def confirms_turn_reply(chat, reply, incoming):
    """Match the answer to its incoming turn, not the same text in an older exchange."""
    turn = []
    for message in chat.get('messages') or []:
        if message.get('isOut'):
            if normalized('\n\n'.join(turn)) == normalized(incoming) and confirms_reply(message, reply):
                return True
            turn = []
        elif message.get('text'):
            turn.append(message['text'])
    return False


def visible_turn_reply_count(chat, reply, incoming):
    turn, active, count = [], False, 0
    for message in chat.get('messages') or []:
        if message.get('isOut'):
            if turn:
                active = normalized('\n\n'.join(turn)) == normalized(incoming)
                if active:
                    count = 0
                turn = []
            if active and confirms_reply(message, reply):
                count += 1
        elif message.get('text'):
            active = False
            turn.append(message['text'])
    return count


_CARD_METADATA = re.compile(
    r'\d+|\d{1,2}:\d{2}|сегодня|вчера|позавчера|today|yesterday|'
    r'\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|'
    r'\d{1,2}\s+(?:янв\w*|фев\w*|мар\w*|апр\w*|ма[йя]|ию[нл]\w*|авг\w*|сен\w*|окт\w*|ноя\w*|дек\w*)'
    r'(?:\s+\d{4})?|'
    r'понедельник|вторник|среда|четверг|пятница|суббота|воскресенье|'
    r'(?:пн|вт|ср|чт|пт|сб|вс|mon|tue|wed|thu|fri|sat|sun)\.?(?:,?\s+\d{1,2}:\d{2})?|'
    r'отказ|собеседование|приглашение|отклик на вакансию|вакансия в архиве|в архиве', re.IGNORECASE)


def chat_card_names(card):
    lines = [line.strip() for line in str(card.get('text') or '').split('\n')
             if line.strip() and not _CARD_METADATA.fullmatch(normalized(line))]
    return {
        'vacancy_title': card.get('vacancy_title') or (lines[0] if lines else ''),
        'company_name': card.get('company_name') or (lines[1] if len(lines) > 1 else ''),
    }


def chat_name_matches(expected, actual):
    expected, actual = normalized(expected), normalized(actual)
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    for shortened, full in ((expected, actual), (actual, expected)):
        if shortened.endswith(('...', '…')):
            prefix = shortened.rstrip('.…')
            if len(prefix) >= 6 and full.startswith(prefix):
                return True
    return False


def fingerprint(text):
    return hashlib.sha256(normalized(text).encode('utf-8')).hexdigest()


def employer_turn(chat):
    """Only the unanswered incoming turn, not an older question above our reply."""
    turn = []
    for message in chat.get('messages') or []:
        if message.get('isOut'):
            turn = []
        elif message.get('text'):
            turn.append(message['text'])
    return '\n\n'.join(turn)


def external_interview_links(text):
    if not re.search(r'интервью|гига.?рекрутер|первичн\w*\s+отбор', text, re.I):
        return []
    links = []
    for raw in re.findall(r'https?://[^\s<>"\]]+', text):
        url = raw.rstrip('.,;!?)')
        try:
            parts = urlsplit(url)
            if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
                # A non-default port must never inherit the host allowlist.
                if parts.port in (None, 443) and url not in links:
                    links.append(url)
        except ValueError:
            continue
    return links


def supported_interview_link(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.port in (None, 443)
                and not parts.username and not parts.password
                and parts.hostname in ('t.me', 'telegram.me', 'max.ru')
                and parts.path.rstrip('/').casefold() == '/giga_recruiter_bot')
    except ValueError:
        return False


class ChatWorkflowMixin:
    @staticmethod
    def _can_reopen_chat_reply(chat):
        try:
            url = urlsplit(chat.get('chat_url', ''))
            return bool(url.scheme == 'https' and not url.username and not url.password
                        and url.port in (None, 443)
                        and (url.hostname == 'hh.ru' or (url.hostname or '').endswith('.hh.ru'))
                        and re.fullmatch(r'/chat/\d+/?', url.path))
        except ValueError:
            return False

    def _prepared_chat_reply(self, chat, question, title, company, **kwargs):
        key = (chat.get('identity'), question, title, company,
               kwargs.get('with_contacts', False), kwargs.get('continuing', False))
        prepared = getattr(self, '_chat_reply_futures', {})
        if key in prepared:
            try:
                return prepared[key].result(timeout=155)
            except Exception as exc:
                logger.warning('Подготовка ответа не завершена: %s; отправки не было', type(exc).__name__)
                return None
        def prepare():
            from ai_assistant import _AI_BUDGET
            origin = self.__dict__.setdefault('_chat_preparation_origin', time.perf_counter())
            started = time.perf_counter() - origin
            job = fingerprint(json.dumps(key, ensure_ascii=False))[:12]
            worker = threading.current_thread().name
            logger.info('Подготовка ответа: старт %s, worker=%s', job, worker)
            token = _AI_BUDGET.set(150)
            reply = None
            try:
                reply = self.compose_chat_reply(question, title, company, **kwargs)
                return reply
            finally:
                _AI_BUDGET.reset(token)
                end = time.perf_counter() - origin
                self.__dict__.setdefault('_chat_preparation_intervals', []).append(
                    {'job': job, 'worker': worker, 'start': started, 'end': end, 'ok': bool(reply)})
                logger.info('Подготовка ответа: конец %s, %.1f с, результат=%s', job, end - started,
                            'готов' if reply else 'нет ответа')

        if not getattr(self, '_collect_chat_replies', False) or not self._can_reopen_chat_reply(chat):
            return prepare()
        action_key = self._chat_action_key('answer', chat, employer_turn(chat))
        if not self._save_chat_action(action_key, self._action_record(
                'answer', chat, 'deferred', incoming=employer_turn(chat), preparation_pending=True,
                note='Ответ готовится в фоне; отправки ещё не было')):
            return None

        prepared[key] = self._chat_reply_pool.submit(prepare)
        self._chat_reply_queue.append((key, dict(chat), getattr(self, '_chat_reply_preview', '')))
        self._reply_queued = True
        logger.info('Ответ готовится в фоне; в очереди %s из 3', len(self._chat_reply_queue))
        return None

    def _drain_chat_reply_queue(self, chats, summary):
        self._collect_chat_replies = False
        while (self._chat_reply_queue and self.is_driver_alive() and not getattr(self, '_user_closed', False)
               and not getattr(self, '_chat_actions_blocked', False)):
            futures = self._chat_reply_futures
            if not any(futures[key] is None or futures[key].done() for key, _, _ in self._chat_reply_queue):
                wait([futures[key] for key, _, _ in self._chat_reply_queue], timeout=155, return_when=FIRST_COMPLETED)
            index = next((i for i, (key, _, _) in enumerate(self._chat_reply_queue)
                          if futures[key] is None or futures[key].done()), 0)
            key, expected, preview = self._chat_reply_queue.pop(index)
            try:
                if not self.goto(expected['chat_url']):
                    raise RuntimeError('Не удалось открыть чат с подготовленным ответом')
                for _ in range(40):
                    current = self._read_open_chat()
                    if self._current_chat_matches(expected) and (current.get('messages') or current.get('messages_loaded')):
                        break
                    time.sleep(.25)
                else:
                    raise RuntimeError('Не подтверждён адресат подготовленного ответа')
                # Переход по ссылке ставит галочку «Только непрочитанные» в исходное положение и
                # сначала показывает список: до появления поля ввода ответ не отправляем.
                self._wait_chat_view_ready()
                current = self._read_open_chat() or current
                current['identity'] = expected['identity']
                if normalized(employer_turn(current)) != normalized(employer_turn(expected)):
                    logger.info('Переписка обновилась во время подготовки; старый ответ не используется')
                    old_key = self._chat_action_key('answer', expected, employer_turn(expected))
                    previous = self._load_chat_actions().get(old_key, {})
                    if previous.get('status') == 'deferred' and previous.get('preparation_pending'):
                        self._save_chat_action(old_key, self._action_record('answer', expected, 'resolved',
                                               note='Переписка изменилась до отправки; подготовленный ответ отменён'))
                rejection = self._handle_chat(current, preview)
                if rejection:
                    chats.append(rejection)
                    summary['rejections'] += 1
            except Exception as exc:
                from rejection_analyzer import is_dead_session_message
                if is_dead_session_message(exc):
                    raise
                summary['failed'] += 1
                logger.warning('Подготовленный ответ не отправлен: %s; действие сохранено для проверки', type(exc).__name__)
            finally:
                future = self._chat_reply_futures.pop(key)
                if future is not None:
                    future.cancel()
        if not self._back_to_chat_list():
            self._block_messenger('Список чатов не вернулся после отправки подготовленных ответов')
            return False
        return True

    def _chat_state_path(self):
        # Resolve at call time: profiles and tests override the data directory.
        from rejection_analyzer import SCRIPT_DIR
        return os.path.join(SCRIPT_DIR, 'chat_actions.json')

    def _load_chat_actions(self):
        if hasattr(self, '_chat_actions'):
            return self._chat_actions
        path = self._chat_state_path()
        self._chat_actions = {}
        if not os.path.exists(path):
            return self._chat_actions
        try:
            with open(path, encoding='utf-8') as stream:
                state = json.load(stream)
            if (not isinstance(state, dict) or state.get('version') != 1
                    or not isinstance(state.get('actions'), dict)
                    or any(not isinstance(v, dict) for v in state['actions'].values())):
                raise ValueError('Invalid chat action history')
            self._chat_actions = state['actions']
        except (OSError, ValueError) as exc:
            self._chat_actions_blocked = True
            logger.error('История ответов чата не читается; отправка остановлена, чтобы не создавать дубли: %s', exc)
        return self._chat_actions

    def _save_chat_action(self, key, action):
        actions = self._load_chat_actions()
        if getattr(self, '_chat_actions_blocked', False):
            return False
        updated = dict(actions, **{key: action})
        path = self._chat_state_path()
        directory = os.path.dirname(os.path.abspath(path))
        tmp = None
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                             prefix='.chat-actions-', suffix='.tmp', delete=False) as stream:
                tmp = stream.name
                json.dump({'version': 1, 'actions': updated}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(.05 * (2 ** attempt))
            self._chat_actions = updated
            return True
        except OSError as exc:
            self._chat_actions_blocked = True
            logger.error('Не удалось сохранить историю чата; новые сообщения не отправляю: %s', exc)
            return False
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _chat_action_key(self, kind, chat, incoming=''):
        identity = (chat.get('vacancy_url') if kind == 'reason' else None) or chat.get('identity') or (
            chat.get('vacancy_title', '') + '|' + chat.get('company_name', ''))
        if kind == 'reason':
            vacancy = re.search(r'/vacancy/(\d+)', identity)
            if vacancy:
                identity = 'vacancy:' + vacancy.group(1)
        # Ask the reason once per conversation, not once per repeated rejection.
        return fingerprint(kind + '|' + identity + '|' + (incoming if kind != 'reason' else ''))

    def _action_record(self, kind, chat, status, **extra):
        return dict(kind=kind, status=status, chat_url=chat.get('chat_url', ''),
                    identity=chat.get('identity', ''), title=chat.get('vacancy_title', ''),
                    company=chat.get('company_name', ''), updated_at=datetime.now().isoformat(), **extra)

    def _current_chat_matches(self, chat, incoming=''):
        current = self._read_open_chat()
        if not current.get('vacancy_title') or not current.get('company_name'):
            return False
        if (normalized(current['vacancy_title']) != normalized(chat.get('vacancy_title'))
                or normalized(current['company_name']) != normalized(chat.get('company_name'))):
            return False
        expected_id = re.search(r'/vacancy/(\d+)', chat.get('vacancy_url', ''))
        current_id = re.search(r'/vacancy/(\d+)', current.get('vacancy_url', ''))
        if expected_id and (not current_id or current_id.group(1) != expected_id.group(1)):
            return False
        return not incoming or normalized(employer_turn(current)) == normalized(incoming)

    @staticmethod
    def _choice_advanced(chat, question):
        last = next((m for m in reversed(chat.get('messages') or []) if not m.get('isOut')), {})
        text = last.get('text', '')
        return bool(last.get('isBot') and normalized(text) != normalized(question)
                    and (chat.get('choice_options') or '?' in text
                         or re.search(r'ваши ответы (?:отправлены|переданы) работодателю', text, re.I)))

    def _click_chat_choice(self, chat, choice, incoming):
        from selenium.common.exceptions import (
            ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException,
        )
        # Retry only before dispatch; a lost confirmation never authorizes another click.
        for attempt in range(3):
            if not self._current_chat_matches(chat, incoming):
                self._chat_send_failure = 'choice_not_clicked'
                return False
            button = self.driver.execute_script(r"""
            const question = document.querySelector('[data-bot-recruiter-question="current"]');
            const region = document.querySelector('[data-bot-chat-options-root="current"]');
            const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            const buttons = region ? [...region.querySelectorAll('[data-bot-chat-choice]')]
                .filter(el => visible(el) && el.innerText.trim() === arguments[0]) : [];
            const button = buttons.length === 1 ? buttons[0] : null;
            const content = question && (question.querySelector('[data-qa="chat-bubble-text"]')
                || question.querySelector('[data-qa^="chatik-chat-message"][data-qa$="-text"]') || question);
            if (!visible(question) || !content) return 'question_unavailable';
            if (content.innerText.trim() !== arguments[1]) return 'question_changed';
            if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') return 'button_unavailable';
            const later = [...document.querySelectorAll(arguments[2])].some(el =>
                visible(el) && !el.contains(question) && !question.contains(el)
                && !el.matches('input, textarea, button, [contenteditable="true"]')
                && !/input|button|send|list|container|scroll|messages$/.test(el.getAttribute('data-qa') || '')
                && (question.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
            if (later) return 'new_message';
            if ([...region.querySelectorAll('textarea, [contenteditable="true"], input[data-qa*="chat"]')].some(el =>
                visible(el) && !el.closest(arguments[3]) && (el.value || el.innerText || '').trim())) return 'draft';
            return button;
            """, choice['label'], chat['messages'][-1].get('raw_text', chat['messages'][-1]['text']),
                MESSAGE_SELECTOR, CARD_SELECTOR)
            if isinstance(button, str):
                if button in ('button_unavailable', 'question_unavailable') and attempt < 2:
                    logger.info('Анкета перерисована; повторно проверяю вопрос и выбранный вариант')
                    time.sleep(.25)
                    continue
                self._chat_send_failure = 'draft' if button == 'draft' else 'choice_not_clicked'
                logger.warning('Клик по варианту анкеты не выполнен: %s', {
                    'question_changed': 'вопрос изменился', 'new_message': 'появилось новое сообщение',
                    'question_unavailable': 'поле вопроса временно недоступно',
                    'button_unavailable': 'выбранная кнопка недоступна', 'draft': 'в поле сохранён черновик',
                }.get(button, button))
                return False
            try:
                button.click()
                break
            except (StaleElementReferenceException, ElementClickInterceptedException, ElementNotInteractableException) as error:
                if attempt == 2:
                    self._chat_send_failure = 'choice_not_clicked'
                    logger.warning('Клик по варианту анкеты не выполнен: %s', type(error).__name__)
                    return False
                logger.info('Кнопка анкеты временно недоступна (%s); заново проверяю вопрос и кнопки', type(error).__name__)
                time.sleep(.25)
            except Exception:
                logger.warning('Результат клика по анкете неизвестен; проверяю подтверждение без повторного клика', exc_info=True)
                break
        question = chat['messages'][-1]['text']
        for _ in range(40):
            time.sleep(.25)
            current = self._read_open_chat()
            if not self._current_chat_matches(chat):
                return False
            if self._choice_advanced(current, question):
                return True
        return False

    def _answer_recruiter_choices(self, chat):
        while chat.get('choice_options') and not getattr(self, '_user_closed', False):
            cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
            if not cfg.get('enabled', True):
                return None
            incoming = employer_turn(chat)
            choices = [c for c in chat['choice_options'] if not c.get('disabled')]
            question = chat['messages'][-1]['text']
            key = self._chat_action_key('choice', chat, incoming)
            previous = self._load_chat_actions().get(key, {})
            if previous.get('status') in ('sent', 'pending_confirmation'):
                return None
            ai = getattr(self, 'ai_assistant', None)
            try:
                index = ai.answer_question(question, question_type='radio',
                                           options=[c['label'] for c in choices],
                                           vacancy_context=chat.get('vacancy_title', '')) if ai and choices else None
            except Exception:
                logger.warning('Не удалось выбрать вариант анкеты; вопрос сохранён для проверки', exc_info=True)
                index = None
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(choices):
                self._save_chat_action(key, self._action_record('choice', chat, 'deferred', incoming=incoming,
                                       note='Не выбран доступный вариант анкеты; свободный текст не отправлен'))
                logger.warning('Не выбран вариант робота-рекрутера в чате %s; текст вместо кнопки не отправляю', chat.get('company_name'))
                return None
            choice = choices[index]
            if not self._send_chat_action('choice', chat, choice['label'], incoming, choice=choice):
                return None
            chat = dict(chat, **self._read_open_chat())
        return chat

    def _send_chat_action(self, kind, chat, text, incoming='', choice=None, resume_draft=False):
        cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
        if not cfg.get('enabled', True):
            return False
        key = self._chat_action_key(kind, chat, incoming)
        previous = self._load_chat_actions().get(key, {})
        if resume_draft and not (kind == 'answer' and previous.get('status') == 'deferred'
                                and previous.get('note') == 'Отправка не выполнялась'
                                and previous.get('reply') == text):
            return False
        if getattr(self, '_chat_actions_blocked', False):
            return False
        if previous.get('status') == 'pending_confirmation' and previous.get('reply'):
            confirmed = (self._choice_advanced(chat, previous.get('question', '')) if kind == 'choice'
                         else confirms_turn_reply(chat, previous['reply'], previous.get('incoming', '')))
            if confirmed:
                self._save_chat_action(key, dict(previous, status='sent'))
        if previous.get('status') in ('sent', 'pending_confirmation'):
            # An uncertain submission is never automatically repeated.
            return False
        if kind != 'choice' and confirms_turn_reply(chat, text, incoming):
            self._save_chat_action(key, self._action_record(kind, chat, 'sent'))
            return False
        limit = int(cfg.get('max_per_run') or 0)
        if limit > 0 and getattr(self, '_chat_sent_this_run', 0) >= limit:
            self._save_chat_action(key, self._action_record(kind, chat, 'deferred', note='Локальный лимит ответов'))
            return False
        if not self._current_chat_matches(chat, incoming):
            self._save_chat_action(key, self._action_record(kind, chat, 'deferred',
                                   incoming=incoming, note='Чат или сообщение изменились до отправки'))
            logger.warning('Чат %s изменился до отправки; ответ отложен для повторной проверки',
                           chat.get('company_name'))
            return False
        if kind != 'choice' and self.find_chat_message_input() is None:
            # 21.10: после перехода по ссылке поле появляется через несколько секунд (на снимке 16:50:35
            # был ещё список, на 16:50:43 уже чат с полем). 1.25 с не хватало — ответ откладывался зря.
            for _ in range(32):
                composer = self.chat_composer_status()
                if composer['state'] != 'editor_not_found':
                    break
                time.sleep(.25)
            if composer['state'] != 'ready':
                restricted = composer['state'] in ('invitation_required', 'employer_disabled', 'vacancy_archived')
                note = composer.get('notice') if restricted else 'Редактор не найден; явного запрета переписки нет'
                action = self._action_record(kind, chat, 'composer_disabled' if restricted else 'deferred',
                                             incoming=incoming, composer_state=composer['state'], note=note)
                if not restricted:
                    self._last_chat_open_problem = {'code': 'editor_not_found', 'reason': note}
                    action['diagnostic'] = self._capture_chat_diagnostics('composer')
                    self._chat_deferred_this_run = getattr(self, '_chat_deferred_this_run', 0) + 1
                self._save_chat_action(key, action)
                log = logger.info if restricted else logger.warning
                log('Поле ответа в чате %s: %s; отправки не было', chat.get('company_name'), note)
                return False
        action = self._action_record(kind, chat, 'pending_confirmation', reply=text,
                                     incoming=incoming)
        if kind == 'choice':
            action['question'] = chat['messages'][-1]['text']
        if not self._save_chat_action(key, action):
            return False
        self._chat_send_failure = None
        self._last_chat_send_snapshot = None
        try:
            sent = (self._click_chat_choice(chat, choice, incoming) if kind == 'choice'
                    else self.send_chat_reply(text, resume_draft=True) if resume_draft else self.send_chat_reply(text))
        except Exception:
            logger.exception('Отправку в чате не удалось подтвердить')
            sent = False
        if sent:
            action['status'] = 'sent'
            self._chat_sent_this_run = getattr(self, '_chat_sent_this_run', 0) + 1
            self.__dict__.setdefault('_chat_confirmed_keys', []).append(key)
            receipt = getattr(self, '_last_chat_send_snapshot', None)
            if kind != 'choice' and receipt is not None:
                action['visible_reply_count'] = visible_turn_reply_count(receipt, text, incoming)
            self._save_chat_action(key, action)
            logger.info('Подтверждена отправка: action=%s, kind=%s, исходящих=%s',
                        key[:12], kind, action.get('visible_reply_count', 'native-choice'))
            logger.info('  [%s] %s: %s', 'ПРИЧИНА ОТКАЗА' if kind == 'reason' else 'ОТВЕТ',
                        chat.get('company_name'), text)
        else:
            failure = getattr(self, '_chat_send_failure', None)
            if kind == 'choice':
                self._capture_chat_diagnostics('choice')
            if failure == 'not_dispatched':
                action.update(status='deferred', note='Отправка не выполнялась')
                self._save_chat_action(key, action)
            if failure in ('draft', 'choice_not_clicked'):
                action.update(status='deferred', note='Сохранён существующий черновик' if failure == 'draft'
                              else 'Вопрос или кнопки изменились; клик не выполнен')
                self._save_chat_action(key, action)
            counter = '_chat_unconfirmed_this_run' if action['status'] == 'pending_confirmation' else '_chat_deferred_this_run'
            setattr(self, counter, getattr(self, counter, 0) + 1)
            reason = {'draft': 'в поле уже есть черновик', 'choice_not_clicked': 'кнопка варианта не нажалась или вопрос сменился',
                      'not_dispatched': 'команда отправки не выполнялась',
                      'restricted_content': 'в тексте запрещённые личные сведения'}.get(
                          failure, 'поле ответа не найдено или сообщение не появилось в чате')
            logger.warning('  Отправка в чат не подтверждена (%s, %s). Повторять автоматически не буду: %s',
                           'вариант анкеты' if kind == 'choice' else 'текст', reason, chat.get('company_name'))
        return bool(sent)

    def _ask_rejection_reason(self, chat):
        cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
        if not cfg.get('ask_rejection_reason', True):
            return False
        for message in chat.get('messages') or []:
            if message.get('isOut') and re.search(
                    r'причин.{0,35}отказ|почему.{0,35}отказ|каких.{0,30}(навык|опыт).{0,25}не хват',
                    message.get('text', ''), re.I):
                return False
        # Compare the same snapshot: another cleanup can remove repeated lines across messages.
        return self._send_chat_action('reason', chat, REASON_REQUEST, employer_turn(chat))

    def _open_external_interview(self, url):
        if not supported_interview_link(url):
            return False
        main = self.driver.current_window_handle
        try:
            self.driver.switch_to.new_window('tab')
            handle = self.driver.current_window_handle
            self._external_chat_tabs = getattr(self, '_external_chat_tabs', set()) | {handle}
            self.driver.get(url)
            return True
        except Exception as exc:
            logger.warning('Не удалось открыть внешнее интервью: %s', exc)
            return False
        finally:
            if main in self.driver.window_handles:
                self.driver.switch_to.window(main)

    def _handle_external_invitation(self, chat, incoming):
        turn_links = []
        for message in chat.get('messages') or []:
            if message.get('isOut'):
                turn_links = []
            else:
                turn_links.extend(message.get('links') or [])
        links = external_interview_links(incoming + '\n' + '\n'.join(turn_links))
        if not links:
            return False
        # Prefer a browser-accessible Telegram link over launching the MAX app.
        supported = sorted((url for url in links if supported_interview_link(url)),
                           key=lambda url: urlsplit(url).hostname == 'max.ru')
        url = supported[0] if supported else links[0]
        key = self._chat_action_key('external', chat, url)
        if key not in self._load_chat_actions():
            action = self._action_record('external', chat, 'external_interview_required',
                                         url=url, urls=links, opened=False)
            if self._save_chat_action(key, action):
                cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
                if supported and cfg.get('open_external_interviews', True):
                    action['opened'] = self._open_external_interview(url)
                    self._save_chat_action(key, action)
                logger.warning('  ВНЕШНЕЕ ИНТЕРВЬЮ: %s — %s. Требуется прохождение в другом сервисе: %s',
                               chat.get('company_name'), chat.get('vacancy_title'), url)
        return True

    def _handle_chat(self, chat, preview='', depth=0):
        from rejection_analyzer import clean_employer_messages
        self._reply_queued = False
        if chat.get('unavailable_reason'):
            logger.info('Чат %s недоступен: %s; обход продолжается',
                        chat.get('company_name'), chat['unavailable_reason'])
            return None
        for key, action in list(self._load_chat_actions().items()):
            if (action.get('kind') == 'bot_wait' and action.get('identity') == chat.get('identity')
                    and chat.get('assistant_left')):
                self._save_chat_action(key, dict(action, status='completed'))
            if action.get('status') != 'pending_confirmation' or not action.get('reply'):
                continue
            if action.get('identity') != chat.get('identity'):
                continue
            confirmed = (self._choice_advanced(chat, action.get('question', '')) if action.get('kind') == 'choice'
                         else confirms_turn_reply(chat, action['reply'], action.get('incoming', '')))
            if confirmed:
                self._save_chat_action(key, dict(action, status='sent'))
        if (chat.get('choice_options') and (chat.get('messages') or [{}])[-1].get('isBot')
                and not OPEN_QUESTION.search((chat.get('messages') or [{}])[-1].get('text', ''))):
            chat = self._answer_recruiter_choices(chat)
            if chat is None:
                return None
        incoming = employer_turn(chat)
        last_incoming_message = next((m for m in reversed(chat.get('messages') or [])
                                      if not m.get('isOut')), {})
        last_incoming = last_incoming_message.get('text', '')
        explicit_refusal = any(p in normalized(last_incoming) for p in REJECTION_MARKERS)
        # Ответ на наш вопрос о причине отказа — это и есть причина («Отсутствие профильного
        # образования», 14.10). Спорить с ней или писать письмо в ответ не нужно:
        # сохраняем как отказ с причиной от работодателя.
        messages = chat.get('messages') or []
        last_out = next((m for m in reversed(messages) if m.get('isOut')), {})
        reason_answer = bool(incoming) and normalized(REASON_REQUEST)[:40] in normalized(last_out.get('text', ''))
        refusal = explicit_refusal or reason_answer or (self.looks_like_rejection_card(preview)
                                                        and not self.looks_like_question_card(incoming)
                                                        and not wants_contact(incoming))
        # Only a visible HH outcome updates an existing application; never infer an invitation from a question.
        invited = re.search(r'(?im)^\s*(?:Собеседование|Приглашение)\s*$', preview)
        if (refusal or invited) and getattr(self, 'db', None):
            url = urlsplit(chat.get('vacancy_url', ''))
            vacancy = re.fullmatch(r'/vacancy/(\d+)/?', url.path)
            if vacancy and (url.hostname == 'hh.ru' or (url.hostname or '').endswith('.hh.ru')):
                self.db.update_application_status(vacancy[1], 'discarded' if refusal else 'invited')
        if refusal:
            # Preserve employer evidence before sending our follow-up.
            history = [{'sender': 'Соискатель' if m.get('isOut') else 'Работодатель', 'text': m['text']}
                       for m in chat.get('messages') or []]
            employer_messages = clean_employer_messages([m['text'] for m in history if m['sender'] == 'Работодатель'])
            rejection = dict(chat_url=chat.get('chat_url', ''), vacancy_title=chat.get('vacancy_title', ''),
                             company_name=chat.get('company_name', ''), vacancy_url=chat.get('vacancy_url', ''),
                             chat_history=history, employer_messages=employer_messages,
                             cover_letter=next((m['text'] for m in history if m['sender'] == 'Соискатель'), ''),
                             rejection_message='\n'.join(employer_messages) or 'Отказ в чате',
                             date=datetime.now().strftime('%Y-%m-%d'))
            self._ask_rejection_reason(chat)
            return rejection
        if not incoming:
            return None
        if chat.get('assistant_left') and last_incoming_message.get('isBot'):
            return None
        if (last_incoming_message.get('isBot') and '?' not in last_incoming
                and BOT_DONE.search(last_incoming)):
            logger.debug('Анкета робота-рекрутера завершена; повторный ответ не требуется')
            if depth == 0 and chat.get('assistant_active'):
                return self._answer_followup(chat, incoming, depth)
            return None
        if self._handle_external_invitation(chat, incoming):
            return None
        if TG_REFUSED.search(incoming):
            # 15.10: «в тг, к сожалению, неудобно… наберите мне 7 900…». Ссылку на Telegram
            # повторять нельзя, а позвонить может только пользователь: сохраняем номер и шумно предупреждаем.
            phones = sorted({re.sub(r'\s+', ' ', m.strip()) for m in _PHONE.findall(incoming)})
            key = self._chat_action_key('answer', chat, incoming)
            self._save_chat_action(key, self._action_record(
                'answer', chat, 'deferred', incoming=incoming,
                note='Просят позвонить, Telegram не подходит: ' + (', '.join(phones) or 'номер не указан')))
            logger.warning('  ТРЕБУЕТСЯ ВАШ ЗВОНОК: %s просит набрать %s (Telegram не подходит)',
                           chat.get('company_name'), ', '.join(phones) or 'номер в чате')
            return None
        rest, has_salary = split_salary(incoming)
        is_question = bool(rest) and self.looks_like_question_card(rest)
        is_contact = wants_contact(incoming)
        selection_update = not is_question and not has_salary and is_selection_update(incoming)
        if incoming:
            cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
            if not cfg.get('enabled', True):
                return None
            key = self._chat_action_key('answer', chat, incoming)
            if self._load_chat_actions().get(key, {}).get('status') in ('sent', 'pending_confirmation'):
                previous = self._load_chat_actions()[key]
                if previous.get('status') == 'pending_confirmation' and previous.get('reply'):
                    self._send_chat_action('answer', chat, previous['reply'], incoming)
                return None
            if is_acknowledgement(incoming) and not selection_update:
                self._save_chat_action(key, self._action_record('answer', chat, 'acknowledged',
                                       incoming=incoming, note='ответ не требуется'))
                logger.info('  %s: сообщение не требует ответа, чат отмечен прочитанным', chat.get('company_name'))
                return None
            profile = getattr(getattr(self, 'ai_assistant', None), 'candidate_profile', {}) or {}
            # 15.10: Telegram и почту пишем только живому человеку, который просит связаться,
            # а не ИИ-помощнику, который задаёт анкетные вопросы.
            bot_chat = bool(last_incoming_message.get('isBot') or chat.get('assistant_active'))
            is_contact = is_contact and not bot_chat
            external_contact = not bot_chat and bool(EXTERNAL_CONTACT_REQUEST.search(incoming))
            share_contacts = is_contact and not external_contact and contact_details_requested(incoming)
            if external_contact:
                contact_key = self._chat_action_key('external_contact', chat, incoming)
                if contact_key not in self._load_chat_actions():
                    self._save_chat_action(contact_key, self._action_record(
                        'external_contact', chat, 'deferred', incoming=incoming,
                        note='Работодатель просит написать вне HH. Свяжитесь самостоятельно; '
                             'бот внешнее сообщение не отправлял.'))
                    logger.warning('Нужна ваша связь вне HH: %s. Контакт сохранён в chat_followups.md; '
                                   'бот туда не пишет.', chat.get('company_name'))
            money = chat_salary_line()
            tenure = tenure_line(cfg) if tenure_asked_again(chat, incoming) else None
            if tenure:
                # Повторный вопрос о стаже: сам вопрос отвечаем цифрой, остальное — как обычно.
                rest = ' '.join(s for s in re.split(r'(?<=[.!?])\s+|\n+', rest or '')
                                if s.strip() and not TENURE_QUESTION.search(s))
                is_question = bool(rest) and self.looks_like_question_card(rest)
            # Шаблоны без ИИ: прямой вопрос о деньгах, «есть ли опыт с X?», «давайте связаться».
            redirect = None if bot_chat else telegram_redirect(profile, incoming)
            pushback = not bot_chat and not has_salary and salary_pushback(chat, cfg, incoming)
            if selection_update:
                reply = 'Спасибо за обратную связь! Сохраняю интерес к вакансии и буду ждать вашего решения.'
            elif redirect:
                reply = redirect + ('\n\n' + money if has_salary else '')
            elif pushback:
                reply = random.choice(SALARY_PUSHBACK_REPLIES)
            elif tenure and not is_question and not is_contact:
                reply = tenure + ('\n\n' + money if has_salary else '')
            elif has_salary and not is_question and not is_contact:
                reply = money
            elif (cfg.get('experience_yes_template', False)
                  and not has_salary and not is_contact and experience_yes_no(incoming)):
                reply = random.choice(EXPERIENCE_YES_VARIANTS)
            elif is_contact and not is_question:
                reply = self._prepared_chat_reply(
                    chat, rest or incoming, chat.get('vacancy_title', ''), chat.get('company_name', ''),
                    with_contacts=share_contacts, continuing=any(m.get('isOut') for m in messages),
                )
                if self._reply_queued:
                    return None
                reply = reply or contact_reply(profile, incoming)
                if has_salary:
                    reply += '\n\n' + money
            else:
                reply = self._prepared_chat_reply(chat, rest or incoming, chat.get('vacancy_title', ''), chat.get('company_name', ''),
                                                with_contacts=share_contacts,
                                                continuing=any(m.get('isOut') for m in messages))
                if self._reply_queued:
                    return None
                telegram = str((profile.get('contacts') or {}).get('telegram') or '').strip()
                if reply and share_contacts and telegram and telegram.lstrip('@').lower() not in reply.lower():
                    reply += f'\n\nДетали удобно обсудить в Telegram: {telegram}'
                if reply and tenure:
                    reply += '\n\n' + tenure
                if reply and has_salary:
                    reply += '\n\n' + money
            if reply:
                reply = strip_external_action_claims(reply)
                if not share_contacts and not redirect:
                    reply = strip_unsolicited_contacts(reply, profile)
                reply = strip_phone_numbers(strip_politeness_tells(reply))
                if any(m.get('isOut') for m in messages):
                    # Диалог уже идёт: второе «Здравствуйте!» выдаёт шаблон.
                    reply = re.sub(r'^(?:здравствуйте|добрый день|добрый вечер|доброе утро)[!,.]?\s*(\w)',
                                   lambda m: m.group(1).upper(), reply, flags=re.I)
            if reply:
                if self._send_chat_action('answer', chat, reply, incoming) and depth == 0:
                    return self._answer_followup(chat, incoming, depth)
            else:
                self._save_chat_action(key, self._action_record('answer', chat, 'deferred',
                                       incoming=incoming, preparation_pending=True, note='ИИ не дал подходящий ответ'))
                self._chat_deferred_this_run = getattr(self, '_chat_deferred_this_run', 0) + 1
                logger.warning('  Вопросы %s сохранены для повторной обработки: ИИ не дал ответ', chat.get('company_name'))
        return None

    FOLLOWUP_WAIT_SECONDS = 60
    BOT_FOLLOWUP_WAIT_SECONDS = 60

    def _answer_followup(self, chat, answered, depth):
        """Continue new incoming turns without recursion; never repeat an uncertain send."""
        active_bot = chat.get('assistant_active', any(m.get('isBot') for m in chat.get('messages') or []))
        deadline = time.time() + (self.BOT_FOLLOWUP_WAIT_SECONDS if active_bot else self.FOLLOWUP_WAIT_SECONDS)
        while time.time() < deadline and not getattr(self, '_user_closed', False):
            time.sleep(1)
            try:
                current = self._read_open_chat()
            except Exception:
                return None
            if not self._current_chat_matches(chat):
                return None
            if current.get('assistant_left'):
                self._handle_chat(dict(chat, **current), '', 1)
                logger.info('ИИ-помощник покинул чат; беседа завершена')
                return None
            turn = employer_turn(current)
            if turn and normalized(turn) != normalized(answered):
                updated = dict(chat, **current)
                updated['identity'] = chat.get('identity', '')
                logger.info('  Работодатель ответил сразу — отвечаю в том же чате: %s', chat.get('company_name'))
                before = getattr(self, '_chat_sent_this_run', 0)
                rejection = self._handle_chat(updated, '', 1)
                if rejection:
                    return rejection
                finished = BOT_DONE.search(turn)
                if getattr(self, '_chat_sent_this_run', 0) == before and not finished:
                    return None
                chat, answered = updated, turn
                active_bot = current.get('assistant_active', any(m.get('isBot') for m in current.get('messages') or []))
                deadline = time.time() + (self.BOT_FOLLOWUP_WAIT_SECONDS if active_bot else self.FOLLOWUP_WAIT_SECONDS)
        if active_bot:
            key = self._chat_action_key('bot_wait', chat, answered)
            self._save_chat_action(key, self._action_record('bot_wait', chat, 'deferred',
                                   note=f'ИИ-помощник не завершил интервью и не ответил за {self.BOT_FOLLOWUP_WAIT_SECONDS} секунд'))
            logger.warning('ИИ-помощник не завершил интервью; ожидание сохранено в незавершённых действиях')
        return None

    REVISIT_HOURS = 72
    REVISIT_MAX = 15

    def _recently_answered_chats(self, seen=()):
        """(identity, chat_url) чатов, куда бот отвечал за последние REVISIT_HOURS часов."""
        cutoff = datetime.now() - timedelta(hours=self.REVISIT_HOURS)
        result, urls = [], set()
        actions = sorted(self._load_chat_actions().values(), key=lambda a: a.get('updated_at', ''), reverse=True)
        for action in actions:
            if action.get('kind') not in ('answer', 'reason', 'choice') or action.get('status') != 'sent':
                continue
            url, identity = action.get('chat_url') or '', action.get('identity') or ''
            try:
                fresh = datetime.fromisoformat(action.get('updated_at', '')) >= cutoff
            except ValueError:
                fresh = False
            if not fresh or not re.search(r'/chat/\d+', url) or url in urls or identity in seen:
                continue
            urls.add(url)
            result.append((identity or url, url))
            if len(result) >= self.REVISIT_MAX:
                break
        return result

    def _revisit_answered_chats(self, seen, summary):
        """Перепроверяет недавно отвеченные чаты и отвечает, если работодатель написал снова."""
        rejections = []
        targets = self._recently_answered_chats(seen)
        if not targets:
            return rejections
        logger.info('Перепроверяю чаты, где уже отвечал: %s', len(targets))
        for identity, url in targets:
            if getattr(self, '_user_closed', False) or getattr(self, '_chat_actions_blocked', False):
                break
            try:
                self.driver.get(url)
                time.sleep(3)
                chat = self._read_open_chat()
            except Exception as exc:
                logger.debug('Чат %s не открылся при перепроверке: %s', url, str(exc)[:120])
                continue
            if not chat.get('messages_loaded') or not employer_turn(chat):
                continue
            chat['identity'] = identity
            summary['viewed'] += 1
            # Карточки чата здесь нет, а отказ виден по истории: передаём его как подсказку,
            # чтобы ответ работодателя с причиной отказа попал в разбор, а не остался без внимания.
            refused = any(not m.get('isOut') and any(p in normalized(m.get('text', '')) for p in REJECTION_MARKERS)
                          for m in chat.get('messages') or [])
            try:
                rejection = self._handle_chat(chat, 'Отказ' if refused else '')
            except Exception as exc:
                logger.warning('Ошибка перепроверки чата %s: %s', url, str(exc)[:120])
                continue
            if rejection:
                rejections.append(rejection)
        try:
            self.goto('https://hh.ru/chat')
        except Exception:
            pass
        return rejections

    def _set_unread_filter(self, enabled):
        """Включает или выключает галочку «Только непрочитанные».

        21.10: на странице открытого чата галочки нет, и одна такая ошибка останавливала весь цикл
        (чаты, разбор, резюме, отклики). Перед тем как сдаться, возвращаемся к списку: сначала
        кнопкой «назад», потом переходом на /chat. Пока галочка не подтверждена, карточки не читаем.
        """
        for attempt in range(3):
            if self._set_unread_filter_once(enabled):
                return True
            if attempt == 2 or getattr(self, '_user_closed', False):
                break
            if attempt == 0:
                logger.info('Галочка «Только непрочитанные» не найдена: возвращаюсь к списку чатов')
                self._back_to_chat_list()
            else:
                logger.info('Галочка не появилась после кнопки «назад»: открываю список чатов заново')
                self.goto('https://hh.ru/chat')
        return False

    def _set_unread_filter_once(self, enabled):
        script = r"""
            const desired = arguments[0];
            for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                const label = cb.closest('label') || (cb.id && document.querySelector('label[for="' + CSS.escape(cb.id) + '"]'));
                const text = label ? label.innerText || label.textContent : cb.getAttribute('aria-label');
                if (/только непрочитанные/i.test(text || '') && (label || cb).getClientRects().length) {
                    if (cb.checked === desired) return 'confirmed';
                    if (arguments[1]) cb.click();
                    return 'waiting';
                }
            }
            return 'missing';
        """
        clicked = False
        for _ in range(20):
            state = self.driver.execute_script(script, bool(enabled), not clicked)
            if state == 'confirmed':
                return True
            if state == 'waiting' and not clicked:
                clicked = True
                # HH updates the filtered list asynchronously after toggling.
                time.sleep(.5)
            time.sleep(.25)
        return False

    def _chat_list_empty(self):
        return bool(self.driver.execute_script("""
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            if ([...document.querySelectorAll('[role="progressbar"], [data-qa*="loading"], [data-qa*="spinner"]')].some(visible)) return false;
            return [...document.querySelectorAll('[data-qa*="empty"], [class*="empty-state"]')]
                .some(e => visible(e) && /нет|пуст|не найден/i.test(e.innerText))
                || [...document.querySelectorAll('[class*="magritte-placeholder"] [data-qa="title"]')]
                    .some(e => visible(e) && /^Нет непрочитанных чатов[.!]?$/i.test((e.innerText || '').trim()));
        """))

    def _read_chat_cards(self):
        cards = self.driver.execute_script(r"""
            const selector = arguments[0];
            document.querySelectorAll('[data-bot-chat]').forEach(e => e.removeAttribute('data-bot-chat'));
            const all = [...document.querySelectorAll(selector)].filter(e => e.getClientRects().length && (e.innerText || '').trim());
            const cards = all.filter(e => !all.some(other => other !== e && other.contains(e)));
            return cards.map((card, idx) => {
                card.setAttribute('data-bot-chat', String(idx));
                const text = card.innerText.trim();
                const link = card.matches('a[href*="/chat/"]') ? card : card.querySelector('a[href*="/chat/"]');
                const href = link ? link.href : '';
                const id = card.getAttribute('data-chat-id') || card.getAttribute('data-topic-id') || href;
                const qa = card.getAttribute('data-qa') || '';
                const title = card.querySelector('[data-qa="chat-cell-title"], [class*="chat-cell-title"]');
                const company = card.querySelector('[data-qa="chat-cell-subtitle"], [class*="chat-cell-subtitle"]');
                const stable = id || (/chatik-open-chat[-_].+/.test(qa) ? qa : '');
                return {idx, text, href, key: stable, stableIdentity: !!stable,
                        vacancy_title: title ? title.innerText.trim() : '',
                        company_name: company ? company.innerText.trim() : ''};
            });
        """, CARD_SELECTOR) or []
        for card in cards:
            card.update(chat_card_names(card))
            if not card.get('stableIdentity'):
                card['key'] = card['vacancy_title'] + '|' + card['company_name']
        return cards

    def _scroll_chat_list(self, reset=False):
        return self.driver.execute_script(r"""
            const card = document.querySelector('[data-bot-chat], ' + arguments[0]);
            for (let el = card && card.parentElement; el; el = el.parentElement) {
                if (el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(el).overflowY)) {
                    const before = el.scrollTop;
                    el.scrollTop = arguments[1] ? 0 : Math.min(el.scrollHeight, before + Math.max(100, el.clientHeight * .8));
                    return {moved: Math.abs(el.scrollTop - before) > 1, position: el.scrollTop};
                }
            }
            return {moved: false};
        """, CARD_SELECTOR, bool(reset)) or {'moved': False}

    def _read_open_chat(self):
        from rejection_analyzer import clean_text_blob, is_ui_noise
        data = self.driver.execute_script(r"""
            const candidates = [...document.querySelectorAll(arguments[0])].filter(el => {
                const qa = el.getAttribute('data-qa') || '';
                return el.getClientRects().length && !/input|button|send|list|container|scroll|messages$/.test(qa)
                    && !el.matches('input, textarea, button, [contenteditable="true"]');
            });
            const roots = candidates.filter(el => !candidates.some(other => other !== el && other.contains(el)));
            // HH wraps a bubble with date markers and application actions; they are not message text.
            const isBubble = el => [...el.classList].some(name => /^chat-bubble(?:--|$)/.test(name));
            const bubbles = roots.map(el => isBubble(el) ? el
                : [...el.querySelectorAll('[class*="chat-bubble"]')].find(isBubble) || el);
            const messages = bubbles.map(el => {
                const content = el.querySelector('[data-qa="chat-bubble-text"]')
                    || el.querySelector('[data-qa^="chatik-chat-message"][data-qa$="-text"]') || el;
                let out = false, bot = false;
                for (let parent = el; parent && parent !== document.body; parent = parent.parentElement) {
                    if (/outgoing|outbound|message_my|my-message|author_me/i.test(String(parent.className))) out = true;
                    if (parent.getAttribute('data-is-own') === 'true' || parent.getAttribute('data-sender') === 'applicant') out = true;
                    if (/chat-bubble_bot(?:--|\b)/.test(String(parent.className))) bot = true;
                }
                if (/(?:^|\n)(?:Робот-рекрутер|ИИ-помощник)(?:\n|$)/i.test(el.innerText.trim())) bot = true;
                const state = String(el.className) + ' ' + (el.getAttribute('data-status') || '');
                return {text: content.innerText.trim(), isOut: out, isBot: bot, id: el.getAttribute('data-message-id') || '',
                        failed: /failed|error|не отправлено/i.test(state)
                            || !!el.querySelector('[data-qa*="message-error"], [data-qa*="message-retry"]'),
                        pending: /pending|sending/i.test(state)
                            || !!el.querySelector('[data-qa*="message-pending"], [data-qa*="message-sending"]'),
                        links: [...el.querySelectorAll('a[href]')].map(a => a.href)};
            });
            const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            let assistantLeft = false, assistantActive = false;
            for (const message of messages) {
                if (/Пользователь\s+(?:ИИ-помощник|Робот-рекрутер)\s+покинул чат/i.test(message.text)) {
                    assistantLeft = true; assistantActive = false;
                } else if (message.isBot) { assistantLeft = false; assistantActive = true; }
            }
            const companies = [...document.querySelectorAll('[data-qa="participant-info-details"]')].filter(visible);
            const companyNames = [...new Set(companies.map(el => el.innerText.trim().split('\n')[0]).filter(Boolean))];
            const comp = companyNames.length === 1 ? companies[0] : null;
            // Only inspect the conversation header, never vacancy links inside messages or the sidebar.
            let root = comp;
            while (root && !bubbles.some(bubble => root.contains(bubble))) root = root.parentElement;
            root = root || document.body;
            const outsideConversation = el => !el.closest(arguments[1])
                && !bubbles.some(bubble => bubble.contains(el));
            const unavailable = [...root.querySelectorAll('[class*="dialog-splash-screen"] [data-qa="title"]')]
                .find(el => visible(el) && outsideConversation(el)
                    && /^Компания заблокирована$/i.test((el.innerText || '').trim()));
            const vacancyUrl = el => {
                const a = el.matches('a[href]') ? el : el.querySelector('a[href*="/vacancy/"]');
                if (!a) return '';
                try {
                    const url = new URL(a.href);
                    return url.protocol === 'https:' && !url.username && !url.password
                        && (!url.port || url.port === '443')
                        && /(^|\.)hh\.ru$/i.test(url.hostname) && /^\/vacancy\/\d+\/?$/.test(url.pathname)
                        ? url.origin + url.pathname.replace(/\/$/, '') : '';
                } catch (_) { return ''; }
            };
            const cleanTitle = text => (text || '').replace(/\s+/g, ' ').trim()
                .replace(/^(?:Вакансия(?:\s+в\s+архиве)?|в\s+архиве)(?:\s+|$)/i, '')
                .replace(/(?:^|\s+)Перейти$/i, '').trim();
            const readTitle = el => {
                let title = cleanTitle(el.innerText);
                if (title) return title;
                // On HH the link can say only "Перейти", with the title in an adjacent element.
                for (let parent = el.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth++) {
                    if ((comp && parent.contains(comp)) || bubbles.some(bubble => parent.contains(bubble))
                            || parent.querySelector(arguments[1])) break;
                    const text = (parent.innerText || '').trim();
                    if (/^(?:Вакансия|в\s+архиве)(?:\s+|$)/i.test(text)) {
                        title = cleanTitle(text);
                        if (title && title.length <= 500) return title;
                    }
                }
                return '';
            };
            const headers = [...root.querySelectorAll('[data-qa="chatik-header-vacancy-link"], a[href*="/vacancy/"]')]
                .filter(el => visible(el) && outsideConversation(el) && (vacancyUrl(el)
                    || (el.matches('[data-qa="chatik-header-vacancy-link"]')
                        && !el.matches('a[href]') && !el.querySelector('a[href]'))));
            const urls = [...new Set(headers.map(vacancyUrl).filter(Boolean))];
            const titles = [...new Set(headers.map(readTitle).filter(Boolean))];
            const unambiguous = !!comp && urls.length <= 1 && titles.length === 1;
            document.querySelectorAll('[data-bot-chat-choice]').forEach(el => el.removeAttribute('data-bot-chat-choice'));
            document.querySelectorAll('[data-bot-recruiter-question], [data-bot-chat-options-root]').forEach(el => {
                el.removeAttribute('data-bot-recruiter-question'); el.removeAttribute('data-bot-chat-options-root');
            });
            const last = bubbles[bubbles.length - 1], message = messages[messages.length - 1];
            let choices = [];
            if (unambiguous && last && message && message.isBot && !message.isOut) {
                last.setAttribute('data-bot-recruiter-question', 'current');
                root.setAttribute('data-bot-chat-options-root', 'current');
                choices = [...root.querySelectorAll('button, [role="button"]')].filter(el => {
                    const label = (el.innerText || '').trim();
                    return visible(el) && label && label.length <= 200 && !el.closest(arguments[1])
                        && !bubbles.some(b => b !== last && b.contains(el))
                        && (last.contains(el) || (last.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))
                        && !/^(?:отправить|прикрепить|назад|закрыть|перейти|скачать|добавить файл)$/i.test(label)
                        // Подсказки hh для соискателя под чатом — вопросы работодателю, а не ответы
                        // робота-рекрутера («Где находится место работы?», «…»). Их нажатие отправило бы
                        // работодателю наш вопрос (07.10: «Отправка не подтверждена» ).
                        && !/[?？]$/.test(label) && !/^[.…⋯]+$/.test(label)
                        && !/^(?:у меня есть профильный опыт|можно без опыта|какой график работы|какая схема оплаты|где находится место работы|могу работать в гибком графике|здравствуйте|добрый день|добрый вечер|доброе утро|привет)/i.test(label);
                }).map((el, index) => {
                    el.setAttribute('data-bot-chat-choice', index);
                    return {index, label:el.innerText.trim(), disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true'};
                });
            }
            return {company_name: comp ? companyNames[0] : '',
                    vacancy_title: unambiguous ? titles[0] : '',
                    vacancy_url: unambiguous ? urls[0] || '' : '', messages, choice_options:choices,
                    assistant_left:assistantLeft, assistant_active:assistantActive,
                    unavailable_reason: unavailable ? unavailable.innerText.trim() : '',
                    messages_loaded: bubbles.some(el => (el.innerText || '').trim())};
        """, MESSAGE_SELECTOR, CARD_SELECTOR) or {}
        cleaned = []
        for message in data.get('messages') or []:
            text = clean_text_blob(message.get('text', ''))
            if text and not is_ui_noise(text):
                message['raw_text'] = message.get('text', '')
                message['text'] = text
                # Separate outgoing bubbles are delivery evidence, even when their text is identical.
                if message.get('isOut') or not cleaned or (text, message.get('isOut')) != (cleaned[-1]['text'], cleaned[-1].get('isOut')):
                    cleaned.append(message)
        data['messages'] = cleaned
        data['links'] = [url for message in cleaned if not message.get('isOut')
                         for url in message.get('links') or []]
        data['chat_url'] = self.driver.current_url
        return data

    def chat_composer_status(self):
        details = self.driver.execute_script(r"""
            const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            const excluded = el => el.closest(arguments[0]) || el.closest(arguments[1]);
            const text = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
            const notices = [...document.querySelectorAll('body *')].filter(el =>
                visible(el) && !excluded(el) && /^(?:Вакансия\s+[«"].+[»"]\s+была перенесена в архив|Переписка будет доступна после приглашения работодателя|Работодатель отключил переписку по данной вакансии|Переписка (?:по данной вакансии )?(?:отключена|недоступна))[.!]?$/i
                    .test(text(el)));
            const editors = [...document.querySelectorAll('textarea, input[data-qa*="chat"], [contenteditable], [role="textbox"]')]
                .filter(el => !excluded(el)).map(el => ({tag:el.tagName, qa:el.getAttribute('data-qa'),
                    role:el.getAttribute('role'), editable:el.getAttribute('contenteditable'),
                    visible:visible(el), disabled:!!el.disabled, readonly:!!el.readOnly,
                    aria_disabled:el.getAttribute('aria-disabled')}));
            return {notice:notices.length ? text(notices[0]) : '', editors};
        """, MESSAGE_SELECTOR, CARD_SELECTOR) or {}
        details['state'] = ('ready' if self.find_chat_message_input() is not None
                            else 'vacancy_archived' if re.match(r'Вакансия\s+[«"]', details.get('notice', ''), re.I)
                            else 'invitation_required' if re.match(r'Переписка будет доступна после приглашения',
                                                                  details.get('notice', ''), re.I)
                            else 'employer_disabled' if details.get('notice') else 'editor_not_found')
        return details

    def resume_chat_draft(self, action_prefix):
        """Explicitly resume one journalled pre-dispatch failure; never replay an uncertain send."""
        report = {'status': 'invalid_action', 'sent': 0}
        if not re.fullmatch(r'[a-f0-9]{12,64}', action_prefix):
            return report
        matches = [(key, row) for key, row in self._load_chat_actions().items() if key.startswith(action_prefix)]
        if len(matches) != 1:
            return report
        key, action = matches[0]
        if action.get('kind') != 'answer' or not action.get('reply') or not self._can_reopen_chat_reply(action):
            return report
        if action.get('status') not in ('deferred', 'sent', 'pending_confirmation'):
            return report
        self._chat_sent_this_run, self._chat_confirmed_keys = 0, []
        if (not self.is_driver_alive() and not self._init_driver()) or not self.goto(action['chat_url']):
            return dict(report, status='navigation_failed')
        expected = dict(vacancy_title=action.get('title'), company_name=action.get('company'))
        chat = {}
        for _ in range(40):
            chat = self._read_open_chat()
            if (chat.get('messages_loaded') and
                    normalized(chat.get('company_name')) == normalized(expected['company_name']) and
                    normalized(chat.get('vacancy_title')) == normalized(expected['vacancy_title']) and
                    self._current_chat_matches(expected)):
                break
            time.sleep(.25)
        else:
            return dict(report, status='chat_not_confirmed')
        chat['identity'] = action.get('identity')
        if self._chat_action_key('answer', chat, action.get('incoming', '')) != key:
            return report
        delivered = confirms_turn_reply(chat, action['reply'], action.get('incoming', ''))
        seed_visible_count = visible_turn_reply_count(chat, action['reply'], action.get('incoming', ''))
        if delivered:
            if not self._save_chat_action(key, dict(action, status='sent',
                    visible_reply_count=visible_turn_reply_count(chat, action['reply'], action.get('incoming', '')))):
                return dict(report, status='history_failed')
            logger.info('Сохранённый ответ уже виден в чате; повторной отправки не будет')
        else:
            if action.get('status') != 'deferred' or action.get('note') != 'Отправка не выполнялась':
                return dict(report, status='previous_send_not_retryable')
            if normalized(employer_turn(chat)) != normalized(action.get('incoming')):
                return dict(report, status='question_changed')
            if not self._send_chat_action('answer', chat, action['reply'], action['incoming'], resume_draft=True):
                return dict(report, status='not_confirmed')
        if not chat.get('assistant_left'):
            self._answer_followup(chat, action.get('incoming', ''), 0)
        final = self._read_open_chat()
        self._capture_chat_diagnostics('resumed-chat')
        keys = self._chat_confirmed_keys
        result = dict(status='assistant_left' if final.get('assistant_left') else
                      'bot_form_completed' if BOT_DONE.search(employer_turn(final)) else 'not_completed',
                      sent=self._chat_sent_this_run, seed_already_delivered=delivered,
                      seed_visible_reply_count=seed_visible_count if delivered else
                          visible_turn_reply_count(final, action['reply'], action.get('incoming', '')),
                      action_prefixes=[key[:12] for key in keys],
                      duplicate_dispatch_keys=len(keys) - len(set(keys)),
                      duplicate_visible_replies=sum((self._load_chat_actions()[key].get('visible_reply_count') or 0) > 1
                                                    for key in keys))
        self._write_chat_followups()
        return result

    def inspect_chat_inputs(self, limit=4):
        """Read recent skipped conversations without generation, dispatch or action-state changes."""
        actions = sorted((a for a in self._load_chat_actions().values()
                          if (a.get('status') == 'composer_disabled' or
                              (a.get('status') == 'deferred' and (a.get('composer_state') == 'editor_not_found'
                               or a.get('note') == 'Отправка не выполнялась')))
                          and self._can_reopen_chat_reply(a)),
                         key=lambda a: a.get('updated_at', ''), reverse=True)
        targets = {a['chat_url']: a for a in reversed(actions)}
        targets = sorted(targets.values(), key=lambda a: (a.get('status') == 'deferred', a.get('updated_at', '')),
                         reverse=True)[:max(1, limit)]
        if not targets or (not self.is_driver_alive() and not self._init_driver()):
            return []
        self._chat_diagnostic_limit = len(targets)
        report = []
        for action in targets:
            expected = dict(vacancy_title=action.get('title'), company_name=action.get('company'))
            if not self.goto(action['chat_url']):
                report.append({'state': 'navigation_failed', 'company': action.get('company')})
                continue
            result = {'state': 'chat_not_confirmed'}
            for _ in range(40):
                if self._current_chat_matches(expected):
                    result = dict(self.chat_composer_status())
                    if result['state'] != 'editor_not_found':
                        break
                time.sleep(.25)
            result['company'] = action.get('company')
            result['controls'] = self.driver.execute_script(r"""
                const editor = arguments[0], reply = arguments[1];
                const root = editor && (editor.closest('[data-qa="chatik-message-input"], form') || editor.parentElement);
                const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
                const buttons = [...document.querySelectorAll('button, [role="button"]')].filter(el => visible(el)
                    && (/chatik.*(?:send|submit)/i.test(el.getAttribute('data-qa') || '')
                        || /^Отправить(?: сообщение)?$/i.test(el.getAttribute('aria-label') || el.getAttribute('title') || '')
                        || el.getAttribute('type') === 'submit'));
                const draft = editor ? (editor.value || editor.innerText || '').trim() : '';
                return {draft_length:draft.length, draft_matches_prepared:!!draft && draft === reply.trim(),
                    root_tag:root && root.tagName, root_qa:root && root.getAttribute('data-qa'),
                    send_buttons:buttons.map(el => ({tag:el.tagName, qa:el.getAttribute('data-qa'),
                        disabled:!!el.disabled, aria_disabled:el.getAttribute('aria-disabled'),
                        in_editor_root:!!root && root.contains(el)}))};
            """, self.find_chat_message_input(), action.get('reply', '')) or {}
            self._last_chat_open_problem = {'code': result['state'], 'reason': result.get('notice', '')}
            result['diagnostic'] = self._capture_chat_diagnostics('composer')
            report.append(result)
            logger.info('Проверка поля %s/%s: %s; причина: %s', len(report), len(targets),
                        result['state'], result.get('notice') or 'явной надписи о запрете нет')
        directory = os.path.join(os.path.dirname(self._chat_state_path()), '.live_runs')
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-chat-inputs.json')
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        logger.info('Проверка полей завершена без отправки: %s; отчёт: %s', len(report), path)
        return report

    def _capture_chat_diagnostics(self, stage):
        if getattr(self, '_chat_diagnostic_count', 0) >= getattr(self, '_chat_diagnostic_limit', 3):
            return
        self._chat_diagnostic_count = getattr(self, '_chat_diagnostic_count', 0) + 1
        try:
            directory = os.path.join(os.path.dirname(self._chat_state_path()), '.chat_diagnostics')
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + stage)
            details = self.driver.execute_script("""
                const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
                const nodes = [...document.querySelectorAll('[data-qa*="chat"], [class*="chat-bubble"], [role="textbox"]')].filter(visible);
                return {url: location.origin + location.pathname,
                    nodes: nodes.slice(0, 100).map(el => ({tag:el.tagName, qa:el.getAttribute('data-qa'),
                        class:typeof el.className === 'string' ? el.className : '', role:el.getAttribute('role'),
                        text_length:(el.innerText || '').trim().length})),
                    loading: [...document.querySelectorAll('[role="progressbar"], [data-qa*="loading"], [data-qa*="spinner"]')].some(visible)};
            """)
            with open(path + '.json', 'w', encoding='utf-8') as stream:
                json.dump({'stage': stage, 'problem': getattr(self, '_last_chat_open_problem', {}),
                           'dom': details}, stream, ensure_ascii=False, indent=2)
            self.driver.save_screenshot(path + '.png')
            logger.warning('Состояние браузера сохранено для диагностики: %s', path + '.png')
            return path
        except Exception:
            logger.debug('Не удалось сохранить диагностику чата', exc_info=True)

    def _open_chat_card(self, card):
        names = chat_card_names(card)
        self._last_chat_open_problem = dict(names, reason='Карточка чата исчезла до открытия')
        clicked = self.driver.execute_script("""
            const card = document.querySelector('[data-bot-chat="' + arguments[0] + '"]');
            if (!card) return false;
            card.scrollIntoView({block:'nearest'}); card.click(); return true;
        """, card['idx'])
        if not clicked:
            return None
        for _ in range(40):
            time.sleep(.25)
            chat = self._read_open_chat()
            href = card.get('href')
            # Never answer a stale previous conversation while a new one is loading.
            company = normalized(chat.get('company_name'))
            title_match = chat_name_matches(names['vacancy_title'], chat.get('vacancy_title'))
            company_match = not names['company_name'] or chat_name_matches(names['company_name'], company)
            same_url = bool(href and urlsplit(href)._replace(query='', fragment='')
                            == urlsplit(self.driver.current_url)._replace(query='', fragment=''))
            unavailable_chat = bool(chat.get('unavailable_reason') and same_url and company_match and company)
            missing_title = not chat.get('vacancy_title') and bool(company)
            self._last_chat_open_problem = dict(names,
                actual_title=chat.get('vacancy_title', ''), actual_company=chat.get('company_name', ''),
                code='missing_vacancy_title' if missing_title else 'chat_not_confirmed',
                reason=('Не найдено название вакансии в открытом чате' if missing_title
                        else 'Переписка не загрузилась' if title_match and company_match
                        else 'Заголовок открытого чата не соответствует карточке'))
            if unavailable_chat or ((chat.get('messages') or chat.get('messages_loaded'))
                    and title_match and (company_match or (href and self.driver.current_url == href and company))):
                chat['identity'] = (card['key'] if card.get('stableIdentity')
                                    else chat.get('vacancy_url') or card['key'])
                chat['vacancy_title'] = chat.get('vacancy_title') or names['vacancy_title']
                chat['company_name'] = chat.get('company_name') or names['company_name']
                return chat
        logger.debug('Чат не подтверждён: ожидалось %s / %s, открыто %s / %s',
                     names['vacancy_title'], names['company_name'],
                     self._last_chat_open_problem.get('actual_title'), self._last_chat_open_problem.get('actual_company'))
        self._capture_chat_diagnostics('open')
        return None

    def _recover_messenger(self, unread_only=True):
        composer = self.find_chat_message_input()
        if composer is not None:
            draft = composer.get_attribute('value')
            if draft is None:
                draft = composer.text
            if str(draft or '').strip():
                self._messenger_recovery_problem = 'В открытом чате есть черновик; перезагрузка отменена, чтобы сохранить его'
                return False
        if not self.is_driver_alive() or getattr(self, '_user_closed', False):
            return False
        self.driver.refresh()
        for _ in range(20):
            if not self.is_driver_alive() or getattr(self, '_user_closed', False):
                return False
            if 'login' in (self.driver.current_url or '').lower():
                self._messenger_recovery_problem = 'HH требует повторного входа. Запустите бот с окном браузера и войдите'
                return False
            if not self._set_unread_filter(unread_only) and unread_only:
                self._messenger_recovery_problem = 'Не удалось включить фильтр «Только непрочитанные». Проверьте чаты в режиме с окном браузера'
                return False
            self._scroll_chat_list(reset=True)
            if self._read_chat_cards():
                return True
            time.sleep(.25)
        # Narrow HH layouts show only the active conversation, even after refresh.
        if self._back_to_chat_list():
            if not self._set_unread_filter(unread_only) and unread_only:
                self._messenger_recovery_problem = 'Не удалось включить фильтр «Только непрочитанные» после возврата к списку'
                return False
            self._scroll_chat_list(reset=True)
            if self._read_chat_cards():
                return True
        self._messenger_recovery_problem = 'После перезагрузки список чатов не загрузился. Проверьте HH в режиме с окном браузера'
        return False

    def _defer_unopened_chat(self, card):
        names = chat_card_names(card)
        problem = getattr(self, '_last_chat_open_problem', {})
        note = problem.get('reason') or 'Чат не загрузился или его заголовок не подтверждён'
        if problem.get('actual_title') or problem.get('actual_company'):
            note += (f". Ожидалось: {names['vacancy_title']} / {names['company_name']}; "
                     f"открыто: {problem.get('actual_title', '')} / {problem.get('actual_company', '')}")
        chat = dict(names, identity=card['key'], chat_url=card.get('href') or 'https://hh.ru/chat')
        self._save_chat_action(self._chat_action_key('chat_open', chat),
                               self._action_record('chat_open', chat, 'deferred', note=note))
        return f"{names['vacancy_title']} / {names['company_name']}: {note}"

    def _wait_chat_view_ready(self, timeout=10.0):
        """Ждёт, пока вместо списка откроется сам чат: появится поле ввода или кнопка «назад»."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.find_chat_message_input() is not None or self.driver.execute_script(
                        "const b = document.querySelector('[data-qa=\"chatik-back-to-chats-button\"]');"
                        "return !!(b && b.getClientRects().length);"):
                    return True
            except Exception:
                return False
            time.sleep(.25)
        return False

    def _block_messenger(self, reason):
        if 'Только непрочитанные' in str(reason):
            # Иначе в диагностике оставалась чужая причина (editor_not_found из прошлого чата).
            self._last_chat_open_problem = {'code': 'unread_filter_unconfirmed', 'reason': str(reason)}
        hint = ('Откройте чаты в видимом браузере, проверьте вход и загрузку переписки. '
                'Затем выберите повторную проверку в терминале.')
        if getattr(self, '_last_chat_open_problem', {}).get('code') == 'missing_vacancy_title':
            hint += (' Компания найдена, но название вакансии не прочитано. Если оно видно в браузере, '
                     'а ошибка повторяется, требуется исправить чтение разметки HH; удалять кеш или профиль не нужно.')
        self.messenger_summary.update(blocked=True, reason=reason, complete=False,
                                      recovery_hint=hint,
                                      followups_path=os.path.join(os.path.dirname(self._chat_state_path()), 'chat_followups.md'))
        logger.error('[СТОП] Обход чатов остановлен: %s. Анализ, правки резюме и отклики не продолжатся до устранения сбоя.', reason)
        logger.warning('Что делать: %s', hint)
        self._capture_chat_diagnostics('blocked')

    def _write_chat_followups(self):
        actions = self._load_chat_actions()
        pending = [a for a in actions.values() if a.get('status') in (
            'deferred', 'pending_confirmation', 'external_interview_required')]
        path = os.path.join(os.path.dirname(self._chat_state_path()), 'chat_followups.md')
        lines = ['# Незавершённые действия в чатах HH', '',
                 f"Отложено: {sum(a.get('status') == 'deferred' for a in pending)}",
                 f"Отправка не подтверждена: {sum(a.get('status') == 'pending_confirmation' for a in pending)}",
                 f"Внешнее интервью: {sum(a.get('status') == 'external_interview_required' for a in pending)}", '',
                 'Неподтверждённые сообщения не отправлять повторно: сначала проверить доставку в чате.',
                 'Отложенные действия: причина указана под каждым пунктом; история не очищается.',
                 'Внешнее интервью требует участия пользователя; открытие ссылки не означает прохождение.', '']
        for action in pending:
            lines.extend([f"## {action.get('company', '')}: {action.get('title', '')}",
                          f"Статус: {action.get('status')}", action.get('url') or action.get('chat_url', ''),
                          action.get('note', ''), action.get('incoming', ''), ''])
        try:
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(lines))
        except OSError as exc:
            logger.warning('Отчёт о незавершённых действиях не сохранён: %s', exc)
        if pending:
            logger.warning('Незавершённых действий в чатах: %s. Подробности: %s', len(pending), path)
            logger.info('Чаты: %s; %s; %s. Это не счётчик ошибок текущего обхода.',
                        lines[2], lines[3], lines[4])

    def _scan_messenger_chats(self, max_chats=0, unread_only=True):
        from rejection_analyzer import is_dead_session_message, short_error
        summary = self.messenger_summary = {'viewed': 0, 'rejections': 0, 'sent': 0,
                                             'failed': 0, 'complete': False}
        self._chat_sent_this_run = 0
        self._chat_unconfirmed_this_run = 0
        self._chat_deferred_this_run = 0
        self._chat_confirmed_keys = []
        self._chat_preparation_intervals = []
        self._chat_preparation_origin = time.perf_counter()
        if getattr(self, '_user_closed', False):
            return []
        if not self.is_driver_alive() and not self._init_driver():
            self._live_fetch_failed = True
            return []
        # 21.10: браузер мог вернуться на открытый чат (/chat/<id>) — там нет списка и галочки.
        # Список — только сам /chat; всё остальное ведём на него.
        if not re.fullmatch(r'/chat/?', urlsplit(self.driver.current_url or '').path.lower()):
            if not self.goto('https://hh.ru/chat'):
                return []
            if not re.fullmatch(r'/chat/?', urlsplit(self.driver.current_url or '').path.lower()):
                self._back_to_chat_list()
        if 'login' in (self.driver.current_url or '').lower():
            logger.warning('Для обработки чатов требуется вход в HH')
            return []
        chats, seen, failures = [], set(), {}
        # 15.10: чат, куда работодатель написал снова, пока мы отвечали, остаётся непрочитанным,
        # но ключ уже «просмотрен» — такие диалоги бот бросал на середине.
        # Пока в списке есть непрочитанные с НОВЫМ текстом, заходим снова,
        # но не больше трёх раз в один чат.
        visits, last_preview = {}, {}
        stalled = 0
        recovery_used = False
        consecutive_open_failures = 0
        self._last_chat_open_problem = {}
        self._messenger_recovery_problem = ''
        target = None
        self._chat_reply_futures = {}
        self._chat_reply_queue = []
        self._chat_reply_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='hh-chat-prepare')
        self._collect_chat_replies = True
        resume_preparations = True
        logger.info('Проверяю только непрочитанные чаты: отказы, вопросы и приглашения'
                    if unread_only else 'Проверяю весь доступный список чатов: отказы, вопросы и приглашения')
        try:
            while self.is_driver_alive() and not getattr(self, '_user_closed', False):
                if not self._set_unread_filter(unread_only) and unread_only:
                    summary['failed'] += 1
                    self._block_messenger('Не удалось подтвердить галочку «Только непрочитанные». '
                                          'Прочитанные диалоги не открываю; проверьте фильтр в видимом браузере')
                    break
                if not seen:
                    self._scroll_chat_list(reset=True)
                if getattr(self, '_chat_actions_blocked', False):
                    summary['failed'] += 1
                    self._block_messenger('История сообщений недоступна для записи. '
                                          'Закройте программы, блокирующие chat_actions.json, и повторите запуск')
                    break
                if resume_preparations:
                    resume_preparations = False
                    for action_key, action in list(self._load_chat_actions().items()):
                        if action.get('status') != 'deferred' or not action.get('preparation_pending'):
                            continue
                        expected = dict(identity=action.get('identity'), chat_url=action.get('chat_url'),
                                        vacancy_title=action.get('title'), company_name=action.get('company'),
                                        messages=[{'text': action.get('incoming', ''), 'isOut': False}])
                        if self._can_reopen_chat_reply(expected):
                            key = ('resume', action_key)
                            self._chat_reply_futures[key] = None
                            self._chat_reply_queue.append((key, expected, ''))
                    if self._chat_reply_queue:
                        logger.info('Восстанавливаю неотправленные ответы после прерванной подготовки: %s',
                                    len(self._chat_reply_queue))
                        if not self._drain_chat_reply_queue(chats, summary):
                            break
                        self._collect_chat_replies = True
                if max_chats > 0 and len(seen) >= max_chats:
                    if self._chat_reply_queue:
                        self._drain_chat_reply_queue(chats, summary)
                    logger.warning('Обход чатов остановлен по явно заданному ограничению %s', max_chats)
                    break
                cards = self._read_chat_cards()
                target = next((c for c in cards if c['key'] not in seen
                               or (unread_only and c['key'] in last_preview and visits.get(c['key'], 0) < 3
                                   and c.get('text', '') != last_preview[c['key']])), None)
                if target is None:
                    if self._chat_reply_queue:
                        if not self._drain_chat_reply_queue(chats, summary):
                            break
                        self._collect_chat_replies = True
                        continue
                    progress = self._scroll_chat_list()
                    stalled = 0 if progress.get('moved') else stalled + 1
                    if stalled >= (12 if not cards and not seen else 3):
                        summary['complete'] = bool(seen) and not summary['failed']
                        if not cards:
                            # Empty is only confirmed by an explicit messenger empty state.
                            summary['complete'] = self._chat_list_empty() and not summary['failed']
                            if not summary['complete']:
                                self._capture_chat_diagnostics('list-end')
                        break
                    time.sleep(.4)
                    continue
                stalled = 0
                chat = self._open_chat_card(target)
                if chat is None:
                    failures[target['key']] = failures.get(target['key'], 0) + 1
                    if not recovery_used:
                        recovery_used = True
                        logger.info('Чат не подтверждён. Одна попытка восстановления мессенджера перед продолжением.')
                        if self._recover_messenger(unread_only):
                            continue
                        summary['failed'] += 1
                        note = self._defer_unopened_chat(target)
                        self._block_messenger(self._messenger_recovery_problem or note)
                        break
                    if failures[target['key']] >= 2:
                        seen.add(target['key'])
                        summary['failed'] += 1
                        consecutive_open_failures += 1
                        note = self._defer_unopened_chat(target)
                        if consecutive_open_failures >= 2:
                            self._block_messenger('Два чата подряд не подтверждены после попытки восстановления. ' + note)
                            break
                        logger.warning('Не удалось подтвердить чат; сохранён для повторной проверки: %s', note)
                    if not self._back_to_chat_list():
                        self._block_messenger('Не удалось вернуть список чатов после сбоя открытия')
                        break
                    continue
                consecutive_open_failures = 0
                seen.add(target['key'])
                visits[target['key']] = visits.get(target['key'], 0) + 1
                last_preview[target['key']] = target.get('text', '')
                summary['viewed'] += 1
                pending_chat = dict(chat_card_names(target), identity=target['key'])
                open_key = self._chat_action_key('chat_open', pending_chat)
                if open_key in self._load_chat_actions():
                    self._save_chat_action(open_key, self._action_record('chat_open', pending_chat, 'resolved'))
                try:
                    self._mark_chat_read()
                    sent_before = getattr(self, '_chat_sent_this_run', 0)
                    self._chat_reply_preview = target.get('text', '')
                    rejection = self._handle_chat(chat, target.get('text', ''))
                    if getattr(self, '_chat_sent_this_run', 0) == sent_before and not getattr(self, '_reply_queued', False):
                        # Мы ничего не писали — работодатель не мог «дописать, пока бот отвечал».
                        # Иначе молчаливые чаты (ответ не нужен, нет поля) открывались по три раза.
                        visits[target['key']] = 3
                    if rejection:
                        chats.append(rejection)
                        summary['rejections'] += 1
                except Exception as exc:
                    if is_dead_session_message(exc):
                        raise
                    summary['failed'] += 1
                    logger.warning('Ошибка обработки чата %s; продолжаю с остальными: %s', target['key'], exc)
                if summary['viewed'] % 25 == 0:
                    logger.info('Обход продолжается: проверено чатов %s, отправлено сообщений %s, ошибок %s',
                                summary['viewed'], getattr(self, '_chat_sent_this_run', 0), summary['failed'])
                if not self._back_to_chat_list():
                    summary['failed'] += 1
                    self._block_messenger('Список чатов не вернулся после обработки диалога')
                    break
                if unread_only:
                    # Reading removes cards from this queue, changing virtual row offsets.
                    self._scroll_chat_list(reset=True)
                if len(self._chat_reply_queue) >= 3:
                    if not self._drain_chat_reply_queue(chats, summary):
                        break
                    self._collect_chat_replies = True
        except Exception as exc:
            if is_dead_session_message(exc):
                self._user_closed = True
            else:
                if target and target['key'] not in seen:
                    self._defer_unopened_chat(target)
                self._block_messenger('Сбой мессенджера: ' + short_error(exc))
            summary['failed'] += 1
            logger.debug('Ошибка обхода чатов', exc_info=True)
        finally:
            self._collect_chat_replies = False
            self._chat_deferred_this_run += len(self._chat_reply_queue)
            for future in self._chat_reply_futures.values():
                if future is not None:
                    future.cancel()
            self._chat_reply_pool.shutdown(wait=False, cancel_futures=True)
            summary['sent'] = getattr(self, '_chat_sent_this_run', 0)
            summary['send_unconfirmed'] = getattr(self, '_chat_unconfirmed_this_run', 0)
            summary['send_deferred'] = getattr(self, '_chat_deferred_this_run', 0)
            summary['preparation_peak'] = peak_parallelism(self._chat_preparation_intervals)
            summary['duplicate_dispatch_keys'] = len(self._chat_confirmed_keys) - len(set(self._chat_confirmed_keys))
            summary['duplicate_visible_replies'] = sum(
                (self._load_chat_actions().get(key, {}).get('visible_reply_count') or 0) > 1
                for key in self._chat_confirmed_keys)
            self._write_chat_followups()
        logger.info('Чаты: просмотрено %s, отказов %s, отправлено сообщений %s, ошибок обхода %s. Обход %s.',
                    summary['viewed'], summary['rejections'], summary['sent'], summary['failed'],
                    'завершён' if summary['complete'] else 'не подтверждён до конца')
        logger.info('Отправки текущего обхода: не подтверждено %s, отложено до отправки %s. '
                    'Старые действия в эти счётчики не входят.', summary['send_unconfirmed'], summary['send_deferred'])
        return chats
