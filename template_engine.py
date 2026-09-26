"""
Модуль динамической шаблонизации и Spintax для сопроводительных писем и сообщений.
Позволяет рандомизировать текст с помощью синтаксиса {вариант1|вариант2} и
подставлять контекстные переменные: %(vacancy_name)s, %(employer_name)s и др.
Защищает от спам-фильтров HeadHunter, делая каждый отклик уникальным.
"""

import re
import random
from typing import Dict, Any, Optional


SPINTAX_PATTERN = re.compile(r'\{([^{}]+)\}')


def parse_spintax(text: str) -> str:
    """
    Разбирает Spintax-выражения {вариант1|вариант2|вариант3} с поддержкой вложенности.
    Выбирает случайный вариант из каждой группы.
    """
    if not text or not isinstance(text, str):
        return ""

    result = text
    # Обрабатываем самые глубокие вложенные фигурные скобки до тех пор, пока они есть
    max_depth = 20
    depth = 0
    while '{' in result and '}' in result and depth < max_depth:
        new_result = SPINTAX_PATTERN.sub(
            lambda m: random.choice(m.group(1).split('|')),
            result
        )
        if new_result == result:
            break
        result = new_result
        depth += 1

    return result


def format_placeholders(text: str, context: Optional[Dict[str, Any]] = None) -> str:
    """
    Подставляет переменные контекста в текст.
    Поддерживает оба формата:
    - Python printf-стиль: %(vacancy_name)s, %(employer_name)s, %(first_name)s
    - Шаблонный стиль: {vacancy_title}, {company_name}, {first_name}
    """
    if not text or not isinstance(text, str):
        return ""

    if not context:
        return text

    ctx = dict(context)

    # Нормализуем синонимы ключей
    vacancy_title = ctx.get('vacancy_name') or ctx.get('vacancy_title') or ctx.get('title') or ''
    company_name = ctx.get('employer_name') or ctx.get('company_name') or ctx.get('company') or ''
    first_name = ctx.get('first_name') or ctx.get('name') or ''
    last_name = ctx.get('last_name') or ''
    resume_title = ctx.get('resume_title') or ''
    resume_url = ctx.get('resume_url') or ''
    email = ctx.get('email') or ''
    phone = ctx.get('phone') or ''

    substitutions = {
        'vacancy_name': vacancy_title,
        'vacancy_title': vacancy_title,
        'employer_name': company_name,
        'company_name': company_name,
        'first_name': first_name,
        'last_name': last_name,
        'resume_title': resume_title,
        'resume_url': resume_url,
        'email': email,
        'phone': phone,
    }

    result = text

    # 1. Заменяем printf-стиль %(key)s
    for key, val in substitutions.items():
        placeholder_printf = f"%({key})s"
        if placeholder_printf in result:
            result = result.replace(placeholder_printf, str(val))

    # 2. Заменяем фигурные скобки {key}, если они не являются spintax-группами (без |)
    for key, val in substitutions.items():
        placeholder_brace = "{" + key + "}"
        if placeholder_brace in result:
            result = result.replace(placeholder_brace, str(val))

    return result


def render_template(template: str, context: Optional[Dict[str, Any]] = None) -> str:
    """
    Полный рендеринг: сначала подставляет контекст, затем раскрывает Spintax.
    """
    if not template:
        return ""
    # Сначала раскрываем контекст (чтобы вставленные переменные не ломали синтаксис скобок)
    text = format_placeholders(template, context)
    # Затем раскрываем Spintax рандомизацию
    text = parse_spintax(text)
    return text.strip()
