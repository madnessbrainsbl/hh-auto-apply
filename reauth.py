"""
Скрипт для переавторизации с правильными правами
"""
import requests
import json
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs
import os

from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR

CLIENT_ID = os.environ.get("HH_CLIENT_ID") or input("Client ID приложения с dev.hh.ru: ").strip()
CLIENT_SECRET = os.environ.get("HH_CLIENT_SECRET") or input("Client Secret: ").strip()
REDIRECT_URI = "https://localhost/hh-auth"

# Запрашиваем ВСЕ нужные права
SCOPE = "vacancy_response" # Только отклики - минимально необходимое

print("=" * 60)
print("ПЕРЕАВТОРИЗАЦИЯ HH.RU")
print("=" * 60)

# Формируем URL авторизации
params = {
    'response_type': 'code',
    'client_id': CLIENT_ID,
    'redirect_uri': REDIRECT_URI,
    'scope': SCOPE
}

auth_url = f"https://hh.ru/oauth/authorize?{urlencode(params)}"

print(f"\nЗапрашиваемые права (scope): {SCOPE}")
print(f"\nОткройте эту ссылку в браузере:")
print(auth_url)
print()

webbrowser.open(auth_url)

print("[!] ВАЖНО: После авторизации вы будете перенаправлены на localhost")
print(" Браузер покажет ошибку - это нормально!")
print(" Скопируйте ПОЛНЫЙ URL из адресной строки")
print()

callback_url = input("Вставьте полный URL после авторизации: ").strip()

# Извлекаем код
parsed = urlparse(callback_url)
query = parse_qs(parsed.query)

if 'code' not in query:
    print("[X] Код авторизации не найден в URL!")
    print(f" Полученный URL: {callback_url}")
    exit(1)

auth_code = query['code'][0]
print(f"\n[OK] Код авторизации получен: {auth_code[:20]}...")

# Обмениваем код на токен
print("\nПолучаем токен...")

token_data = {
    'grant_type': 'authorization_code',
    'client_id': CLIENT_ID,
    'client_secret': CLIENT_SECRET,
    'redirect_uri': REDIRECT_URI,
    'code': auth_code
}

response = requests.post('https://hh.ru/oauth/token', data=token_data)

print(f" Статус: {response.status_code}")
print(f" Ответ: {response.text}")

if response.status_code == 200:
    token_info = response.json()
    
    # Сохраняем токен
    token_file = os.path.join(SCRIPT_DIR, 'hh_token.json')
    with open(token_file, 'w', encoding='utf-8') as f:
        json.dump(token_info, f, ensure_ascii=False, indent=2)
    
    print(f"\n[OK] Доступ к hh.ru сохранён")
    
    # Проверяем права
    print("\nПроверяем права токена...")
    
    headers = {
        'Authorization': f"Bearer {token_info['access_token']}",
        'User-Agent': 'AutoJobApplyBot/1.0'
    }
    
    # Тест /me
    r = requests.get('https://api.hh.ru/me', headers=headers)
    print(f" /me: {r.status_code}")
    
    # Тест /negotiations
    r = requests.get('https://api.hh.ru/negotiations', headers=headers)
    print(f" /negotiations: {r.status_code}")
    if r.status_code == 403:
        print(f" Ошибка: {r.text}")
    
    # Тест /resumes/mine
    r = requests.get('https://api.hh.ru/resumes/mine', headers=headers)
    print(f" /resumes/mine: {r.status_code}")
    if r.status_code == 403:
        print(f" Ошибка: {r.text}")
        
else:
    print(f"\n[X] Ошибка получения токена!")
    print(f" {response.text}")
