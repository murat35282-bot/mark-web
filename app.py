from flask import Flask, request, jsonify, send_from_directory
import requests
import datetime
import os
import uuid
import re
from xml.etree import ElementTree

# Wikipedia + Google Search
import wikipedia
from googlesearch import search

# Saat TR için
import pytz

# ================= AYARLAR =================
API_KEY = os.getenv("GROQ_API_KEY", "").strip()
MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
MAX_USER_LENGTH = 600

wikipedia.set_lang("tr")

app = Flask(__name__, static_folder="static")

# ================= DURUM / HAFIZA =================
jarvis_mode = {}
user_memories = {}

def get_user_memory(user_id):
    if user_id not in user_memories:
        user_memories[user_id] = {"conversation": []}
    return user_memories[user_id]

# ================= ZAMAN (TR) =================
def time_context():
    tz = pytz.timezone("Europe/Istanbul")
    now = datetime.datetime.now(tz)
    return f"Saat {now.strftime('%H:%M')} | Tarih {now.strftime('%d.%m.%Y')}"

# ================= KONTROLLER =================
def needs_currency(user):
    return any(k in user.lower() for k in ["dolar", "euro", "kur", "usd", "eur"])

def needs_google(user):
    return any(k in user.lower() for k in ["google", "ara", "bul", "internet", "netten bak", "güncel bak"])

def needs_wikipedia(user):
    return any(k in user.lower() for k in ["wikipedia", "vikipedi", "vikiden"])

def needs_live_info(user):
    """
    Modelin uydurma ihtimali yüksek olan güncel konular.
    Bu sorularda otomatik google çalıştıracağız.
    """
    u = user.lower()
    triggers = [
        "şu an", "şuan", "güncel", "bugün",
        "cumhurbaşkanı", "başkanı kim", "kim kazandı",
        "kaç oldu", "son durum", "son dakika",
        "dolar kaç", "euro kaç"
    ]
    return any(t in u for t in triggers)

def clean_query_for_search(q: str):
    q = q.lower()
    for w in ["google", "ara", "bul", "internet", "netten bak", "güncel bak"]:
        q = q.replace(w, "")
    return q.strip()

# ================= ARAÇLAR =================
def get_currency():
    try:
        url = "https://www.tcmb.gov.tr/kurlar/today.xml"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        root = ElementTree.fromstring(res.content)

        usd = root.find(".//Currency[@CurrencyCode='USD']/BanknoteSelling").text
        eur = root.find(".//Currency[@CurrencyCode='EUR']/BanknoteSelling").text
        return f"Güncel döviz: 1 USD = {usd} TL | 1 EUR = {eur} TL"
    except Exception:
        return "Döviz bilgisine şu an ulaşılamıyor."

def google_search_quick(query: str):
    try:
        q = clean_query_for_search(query)
        if not q:
            q = "Türkiye gündem"

        results = [url for url in search(q, num_results=3, lang="tr")]
        if not results:
            return "Google'da sonuç bulamadım."

        return "Güncel kaynaklar:\n" + "\n".join([f"- {u}" for u in results])
    except Exception:
        return "Google araması şu an çalışmadı."

def wikipedia_summary(query: str):
    try:
        q = query.lower().replace("wikipedia", "").replace("vikipedi", "").replace("vikiden", "").strip()
        if not q:
            q = "Türkiye"
        return wikipedia.summary(q, sentences=2, auto_suggest=False)
    except Exception:
        return "Wikipedia'da aradığını bulamadım."

# ================= AI MOTORU (Groq) =================
def ai_reply(messages):
    if not API_KEY:
        return "Patron, GROQ_API_KEY ayarlı değil. Render -> Environment kısmına ekle."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "temperature": 0.4}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return "Patron, AI şu an cevap veremedi (API / bağlantı hatası)."

# ================= ROUTES =================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True) or {}
    raw = (data.get("message") or "").strip()
    user_id = data.get("user_id") or str(uuid.uuid4())

    if not raw:
        return jsonify({"reply": "Mesaj boş, Patron.", "user_id": user_id})

    # Jarvis aç/kapat
    if raw.lower() == "jarvis aç":
        jarvis_mode[user_id] = True
        return jsonify({"reply": "Jarvis modu açıldı.", "user_id": user_id})

    if raw.lower() == "jarvis kapat":
        jarvis_mode[user_id] = False
        return jsonify({"reply": "Jarvis modu kapatıldı.", "user_id": user_id})

    user = raw[:MAX_USER_LENGTH]
    is_jarvis = jarvis_mode.get(user_id, False)

    # Saat sorarsa direkt TR saatini söyle
    if any(k in user.lower() for k in ["saat kaç", "kaç saat", "tarih ne", "bugün günlerden"]):
        return jsonify({"reply": time_context(), "user_id": user_id})

    # Döviz
    if needs_currency(user):
        return jsonify({"reply": get_currency(), "user_id": user_id})

    # Wikipedia
    if needs_wikipedia(user):
        return jsonify({"reply": wikipedia_summary(user), "user_id": user_id})

    # Google istenirse
    if needs_google(user):
        return jsonify({"reply": google_search_quick(user), "user_id": user_id})

    # Güncel bilgi soruları -> otomatik google kaynak ver
    # (Model uydurmasın diye)
    if needs_live_info(user):
        # önce kaynakları verelim, sonra AI bunu özetleyebilir
        sources = google_search_quick(user)
        return jsonify({"reply": sources, "user_id": user_id})

    # ================= AI CEVABI =================
    system_prompt = f"""
Sen Mark adlı bir asistansın.
Kullanıcıya 'Patron' diye hitap et.
Cevapların kısa ve net olsun.
Bilmiyorsan uydurma.
{time_context()}
"""
    if is_jarvis:
        system_prompt += "\nJarvis modundasın: daha teknik ve daha kısa cevap ver.\n"

    messages = [{"role": "system", "content": system_prompt}]

    # Hafıza sadece normal modda
    if not is_jarvis:
        memory = get_user_memory(user_id)
        for m in memory["conversation"][-6:]:
            messages.append(m)

    messages.append({"role": "user", "content": user})
    answer = ai_reply(messages)

    if not is_jarvis:
        memory["conversation"].append({"role": "user", "content": user})
        memory["conversation"].append({"role": "assistant", "content": answer})

    return jsonify({"reply": answer, "user_id": user_id})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))