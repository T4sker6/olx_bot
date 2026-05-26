import argparse
import asyncio
import json
import random
import sqlite3
import urllib.request
from pathlib import Path
from google import genai
from playwright.async_api import async_playwright

# ==========================================
GEMINI_API_KEY = "TUTAJ_WSTAW_SWOJ_KLUCZ_API"
DISCORD_WEBHOOK = "TUTAJ_WSTAW_LINK_WEBHOOKA"
DB_PATH = Path(__file__).parent / "prices.db"
# ==========================================

MAX_ADS_PER_RUN = 35
MAX_SEEN_PER_CATEGORY = 2000
FETCH_CONCURRENCY = 4  # ile opisów pobieramy równolegle

KATEGORIE = {
    "gpu": {
        "url": "https://www.olx.pl/elektronika/komputery/podzespoly-i-czesci/karty-graficzne/?search%5Border%5D=created_at:desc",
        "tag": "KARTY GRAFICZNE",
        "specyfika": "Szukaj modeli z uszkodzonym chłodzeniem lub wentylatorami (tania naprawa). Zwróć uwagę na czy karta nie jest niedoszacowana. Odrzucaj ewidentnie martwe karty bez opisu usterki.",
    },
    "cpu": {
        "url": "https://www.olx.pl/elektronika/komputery/podzespoly-i-czesci/procesory/?search%5Border%5D=created_at:desc",
        "tag": "PROCESORY",
        "specyfika": "Zwracaj uwagę na podstawkę (socket) i generację. Odrzucaj te z wygiętymi pinami, skup się na efektywnych energetycznie modelach lub takich z potencjałem podkręcania.",
    },
    "mobo": {
        "url": "https://www.olx.pl/elektronika/komputery/podzespoly-i-czesci/plyty-glowne/?search%5Border%5D=created_at:desc",
        "tag": "PŁYTY GŁÓWNE",
        "specyfika": "Szukaj płyt z dobrą sekcją zasilania pod homelab/podkręcanie. Odrzucaj z uszkodzonym socketem, chyba że cena to grosze. Zwracaj uwagę na ilość slotów RAM i portów pod dyski tak aby móc rozbudować w przyszłości.",
    },
    "ram": {
        "url": "https://www.olx.pl/elektronika/komputery/podzespoly-i-czesci/pamieci-ram/?search%5Border%5D=created_at:desc",
        "tag": "PAMIĘĆ RAM",
        "specyfika": "Szukaj kości DDR4 i DDR5, najlepiej w zestawach Dual Channel (2x8GB, 2x16GB) o wysokich taktowaniach i niskim opóźnieniu CL. Interesuje nas zarówno RAM do komputerów jak i laptopów.",
    },
    "dyski": {
        "url": "https://www.olx.pl/elektronika/komputery/podzespoly-i-czesci/dyski/?search%5Border%5D=created_at:desc",
        "tag": "DYSKI SSD/HDD",
        "specyfika": "Szukaj dysków NVMe M.2 o dużych pojemnościach (256GB+). Interesują nas również dyski SATA SSD ale także HDD jeśli mają dużo większe pojemności. Sprawdzaj czy sprzedawca wspomina o żywotności (Health/TBW). Bonusowe punkty jeśli są screenshoty z CrystalDiskInfo albo SMART.",
    },
    "obudowy": {
        "url": "https://www.olx.pl/elektronika/komputery/podzespoly-i-czesci/obudowy/?search%5Border%5D=created_at:desc",
        "tag": "OBUDOWY / CHŁODZENIA",
        "specyfika": "Szukaj przewiewnych obudów typu mesh z dobrą wentylacją. Odrzucaj uszkodzone lub takie których ewidentnie nie da się doczyścić ani naprawić. Zwracaj uwagę na te z dołączonymi wentylatorami lub chłodzeniem.",
    },
}

client = genai.Client(api_key=GEMINI_API_KEY)


# --- DB ---

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_olx_table():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS PRICE_TRACKING_OLX (
                id         INTEGER   PRIMARY KEY AUTOINCREMENT,
                link       TEXT      UNIQUE NOT NULL,
                kategoria  TEXT      NOT NULL,
                tytul      TEXT,
                cena       TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def load_seen_links(kategoria: str) -> set:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT link FROM PRICE_TRACKING_OLX WHERE kategoria = ?", (kategoria,)
        ).fetchall()
    return {row["link"] for row in rows}


def save_new_ads(kategoria: str, ads: list):
    with get_connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO PRICE_TRACKING_OLX (link, kategoria, tytul, cena) VALUES (?, ?, ?, ?)",
            [(ad["link"], kategoria, ad["tytul"], ad["cena"]) for ad in ads],
        )
        # Rotacja: zostaw tylko ostatnie MAX_SEEN_PER_CATEGORY wpisów dla tej kategorii
        conn.execute(
            """DELETE FROM PRICE_TRACKING_OLX
               WHERE kategoria = ? AND id NOT IN (
                   SELECT id FROM PRICE_TRACKING_OLX
                   WHERE kategoria = ?
                   ORDER BY id DESC
                   LIMIT ?
               )""",
            (kategoria, kategoria, MAX_SEEN_PER_CATEGORY),
        )


# --- Scraping ---

async def fetch_description(semaphore, context, url):
    async with semaphore:
        await asyncio.sleep(random.uniform(0.5, 1.5))
        try:
            page = await context.new_page()
            await page.goto(url, timeout=15000)
            await page.wait_for_timeout(1500)
            desc_el = await page.query_selector('div[data-cy="ad_description"]')
            text = await desc_el.text_content() if desc_el else "Brak opisu w kodzie strony."
            return text.strip()
        except Exception as e:
            return f"Nie udało się pobrać opisu: {e}"
        finally:
            await page.close()


# --- Discord ---

def _send_block(webhook_url, block):
    """Wysyła jeden blok (jedną ofertę). Jeśli przekracza 1900 znaków, tnie po liniach."""
    if len(block) <= 1900:
        chunks = [block]
    else:
        chunks, current = [], ""
        for line in block.split("\n"):
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line
            else:
                current = (current + "\n" + line) if current else line
        if current:
            chunks.append(current)

    for chunk in chunks:
        data = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.getcode() in [200, 204]:
                    print("🚀 Oferta wysłana na Discorda!")
        except Exception as e:
            print(f"❌ Błąd podczas wysyłania na Discorda: {e}")


def _send_chunks(webhook_url, text):
    """Parsuje odpowiedź Gemini na bloki (separator ---) i wysyła każdy osobno."""
    blocks = [b.strip() for b in text.split("---") if b.strip()]
    if not blocks:
        print("⚠️ Brak ofert do wysłania (Gemini nie zwrócił żadnych bloków).")
        return
    for block in blocks:
        _send_block(webhook_url, block)


async def send_to_discord(webhook_url, text):
    if webhook_url == "TUTAJ_WSTAW_LINK_WEBHOOKA":
        print("⚠️ Brak skonfigurowanego Webhooka Discorda. Pomijam wysyłkę.")
        return
    await asyncio.to_thread(_send_chunks, webhook_url, text)


# --- Main ---

async def main():
    parser = argparse.ArgumentParser(description="OLX Scraper z podziałem na kategorie")
    parser.add_argument(
        "--kategoria",
        required=True,
        choices=KATEGORIE.keys(),
        help="Wybierz kategorię do skanowania",
    )
    args = parser.parse_args()

    config = KATEGORIE[args.kategoria]
    kategoria = args.kategoria

    if GEMINI_API_KEY == "TUTAJ_WSTAW_SWOJ_KLUCZ_API":
        print("❌ BŁĄD: Brak klucza API!")
        return

    init_olx_table()
    seen_links = load_seen_links(kategoria)

    async with async_playwright() as p:
        print(f"🌐 [{config['tag']}] Odpalam przeglądarkę Chromium...")
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"🔎 Wchodzę na OLX: {config['url']}")
        await page.goto(config["url"])
        await page.mouse.wheel(0, 1000)
        await page.wait_for_timeout(3000)

        ads = await page.query_selector_all('div[data-cy="l-card"]')

        # Faza 1: zbierz nowe oferty z listingu (bez opisów)
        print("📦 Zbieram najświeższe oferty...")
        new_ads = []
        links_in_session = set()

        for ad in ads:
            if len(new_ads) >= MAX_ADS_PER_RUN:
                break

            link_el = await ad.query_selector("a")
            link = await link_el.get_attribute("href") if link_el else ""

            if not link or "promoted" in link:
                continue
            if link.startswith("/d/"):
                link = "https://www.olx.pl" + link
            if link in seen_links or link in links_in_session:
                continue

            title_el = await ad.query_selector("h6, h4, h3")
            title = (await title_el.text_content() if title_el else "Brak tytułu").strip()

            price_el = await ad.query_selector('p[data-testid="ad-price"]')
            price = (await price_el.text_content() if price_el else "Brak ceny")
            price = price.replace("\n", " ").replace("złdo", "zł do").strip()

            new_ads.append({"tytul": title, "cena": price, "link": link})
            links_in_session.add(link)

        await page.close()

        if not new_ads:
            print(f"☕ [{config['tag']}] Brak nowych ogłoszeń. Kończę.")
            await browser.close()
            return

        # Faza 2: równoległe pobieranie opisów
        print(f"🔍 Znaleziono {len(new_ads)} nowych ofert. Pobieram opisy (równolegle x{FETCH_CONCURRENCY})...")
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
        descriptions = await asyncio.gather(*[
            fetch_description(semaphore, context, ad["link"]) for ad in new_ads
        ])

        await browser.close()

    scraped_data = [
        {**ad, "id": i + 1, "opis_sprzedawcy": desc}
        for i, (ad, desc) in enumerate(zip(new_ads, descriptions))
    ]

    save_new_ads(kategoria, scraped_data)

    json_payload = json.dumps(scraped_data, ensure_ascii=False, indent=2)

    prompt = f"""
    Jesteś ekspertem od wyceny, rynku wtórnego i naprawy sprzętu komputerowego.
    Analizujesz przedmioty z kategorii: {config['tag']}.

    Twoje wytyczne dla tej kategorii: {config['specyfika']}

    Oceń opłacalność zakupu poniższych urządzeń pod kątem dalszej odsprzedaży z zyskiem lub rozbudowy własnego homelabu.

    SKALA OCEN — trzymaj się jej ściśle:
    9-10: Cena znacznie poniżej rynku LUB usterka tania i oczywista do naprawy; wysoki potencjał zysku
    7-8:  Cena lekko poniżej rynku, sensowny potencjał naprawy lub odsprzedaży
    <7:   Brak potencjału — CAŁKOWICIE POMIŃ, nie uwzględniaj w odpowiedzi

    WYMÓG KRYTYCZNY: Raportuj WYŁĄCZNIE oferty z oceną 7/10 lub wyższą. Jeśli żadna oferta nie spełnia progu — zwróć pustą odpowiedź. Nie dodawaj ŻADNEGO tekstu przed ani po blokach. Trzymaj się dokładnie tego wzoru:

    # 🔥 [{config['tag']}] NOWA OKAZJA!
    **Tytuł:** [Tytuł ogłoszenia]
    **Cena:** [Cena]
    **Ocena:** [Twoja ocena w skali 1-10]/10
    **Uzasadnienie:** [Tylko JEDNO konkretne zdanie uzasadniające ocenę, bazujące na usterce lub cenie rynkowej]
    **Link:** [Link do ogłoszenia]
    ---

    Oto dane do analizy:
    {json_payload}
    """

    print(f"\n🤖 Wysyłam dane [{config['tag']}] do Gemini...")

    fallback_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    sukces = False

    for model_name in fallback_models:
        try:
            print(f"   ➤ Pukam do serwera: {model_name}...")
            response = client.models.generate_content(model=model_name, contents=prompt)
            await send_to_discord(DISCORD_WEBHOOK, response.text)
            sukces = True
            break
        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"   ⚠️ {model_name} zapchany. Przełączam...")
                continue
            else:
                print(f"   ❌ Błąd modelu {model_name}: {e}")
                break

    if not sukces:
        print("\n🚨 Serwery leżą. Spróbuj za godzinę.")


if __name__ == "__main__":
    asyncio.run(main())
