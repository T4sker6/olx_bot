import asyncio
import random
import json
import urllib.request
from google import genai
from playwright.async_api import async_playwright

# ==========================================
GEMINI_API_KEY = "TUTAJ_WSTAW_SWOJ_KLUCZ_API"
# ==========================================

client = genai.Client(api_key=GEMINI_API_KEY)


async def get_ad_description(context, url):
    """Funkcja wchodzi w link ogłoszenia i wyciąga z niego pełny opis"""
    try:
        page = await context.new_page()
        await page.goto(url, timeout=15000)
        await page.wait_for_timeout(2000)

        desc_el = await page.query_selector('div[data-cy="ad_description"]')
        if desc_el:
            text = await desc_el.text_content()
            return text.strip()
        return "Brak opisu w kodzie strony."
    except Exception as e:
        return f"Nie udało się pobrać opisu: {str(e)}"
    finally:
        await page.close()


def send_to_discord(webhook_url, text):
    """Wysyła tekst na Discorda, dzieląc go na części do 2000 znaków"""
    if webhook_url == "TUTAJ_WSTAW_LINK_WEBHOOKA":
        print("⚠️ Brak skonfigurowanego Webhooka Discorda. Pomijam wysyłkę.")
        return

    # Jeśli tekst jest za długi, dzielimy go na bloki (np. po linijkach)
    chunks = []
    current_chunk = ""

    for line in text.split("\n"):
        if len(current_chunk) + len(line) + 1 > 1900:  # Bezpieczny bufor
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = current_chunk + "\n" + line if current_chunk else line

    if current_chunk:
        chunks.append(current_chunk)

    # Fizyczna wysyłka każdego kawałka
    for chunk in chunks:
        payload = {"content": chunk}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req) as response:
                if response.getcode() in [200, 204]:
                    print("🚀 Raport pomyślnie wysłany na Discorda!")
        except Exception as e:
            print(f"❌ Błąd podczas wysyłania na Discorda: {e}")


async def main():
    # Zabezpieczenie przed brakiem klucza
    if GEMINI_API_KEY == "TUTAJ_WSTAW_SWOJ_KLUCZ_API":
        print("❌ BŁĄD: Zapomniałeś wkleić swój klucz API do zmiennej GEMINI_API_KEY!")
        return

    # === BAZA LINKÓW (Rotacyjny bufor max 2000 linków) ===
    try:
        with open("seen_links.txt", "r") as f:
            all_links = f.read().splitlines()
    except FileNotFoundError:
        all_links = []

    seen_links = set(all_links)
    nowe_linki_w_tej_sesji = []

    async with async_playwright() as p:
        print("🌐 Odpalam przeglądarkę Chromium...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        url = "https://www.olx.pl/elektronika/komputery/laptopy/?search%5Border%5D=created_at:desc"
        print(f"🔎 Wchodzę na OLX (Najnowsze): {url}")
        await page.goto(url)
        await page.mouse.wheel(0, 1000)
        await page.wait_for_timeout(3000)

        ads = await page.query_selector_all('div[data-cy="l-card"]')

        scraped_data = []
        organic_count = 0

        print("📦 Zbieram najświeższe oferty i wyciągam opisy...")

        for ad in ads:
            # Skanujemy max 35 najnowszych (na wypadek dużej aktywności)
            if organic_count >= 35:
                break

            link_el = await ad.query_selector("a")
            link = await link_el.get_attribute("href") if link_el else ""

            # Odrzucamy promowane i puste
            if "promoted" in link or not link:
                continue

            # Klejenie uciętych linków
            if link.startswith("/d/"):
                link = "https://www.olx.pl" + link

            # === ANTY-DUPLIKAT ===
            if link in seen_links:
                continue

            organic_count += 1

            title_el = await ad.query_selector("h6, h4, h3")
            title = await title_el.text_content() if title_el else "Brak tytułu"
            title = title.strip()

            price_el = await ad.query_selector('p[data-testid="ad-price"]')
            price = await price_el.text_content() if price_el else "Brak ceny"
            price = price.replace("\n", " ").replace("złdo", "zł do").strip()

            print(
                f"   [{organic_count}/35] Nowa oferta! Pobieram opis dla: {title[:30]}..."
            )
            description = await get_ad_description(context, link)

            scraped_data.append(
                {
                    "id": organic_count,
                    "tytul": title,
                    "cena": price,
                    "link": link,
                    "opis_sprzedawcy": description,
                }
            )

            nowe_linki_w_tej_sesji.append(link)
            await asyncio.sleep(random.uniform(1.0, 3.0))

        await browser.close()

    # === ZABEZPIECZENIE PRZED PUSTYM CZATEM ===
    if not scraped_data:
        print("☕ Brak nowych ogłoszeń na OLX w ciągu ostatniej godziny. Kończę pracę.")
        return

    # === AKTUALIZACJA PLIKU (Zapis rotacyjny) ===
    if nowe_linki_w_tej_sesji:
        all_links.extend(nowe_linki_w_tej_sesji)
        all_links = all_links[-2000:]  # Ucinamy stare, zostawiamy 2000 najnowszych

        with open("seen_links.txt", "w") as f:
            f.write("\n".join(all_links) + "\n")

    json_payload = json.dumps(scraped_data, ensure_ascii=False, indent=2)

    # 🤖 BUDUJEMY KANAPKĘ DLA GEMINI
    prompt = f"""
    Jesteś ekspertem od naprawy sprzętu IT. Analizujesz laptopy pod kątem stworzenia z nich małego serwera lub stacji roboczej po taniej naprawie.
    
    Oceń opłacalność zakupu poniższych urządzeń z portalu ogłoszeniowego.
    Odrzuć oferty, które nie mają potencjału (np. zbyt drogie, w pełni sprawne "okazje" sklepowe) lub naprawa jest całkowicie nieopłacalna.

    WYMÓG KRYTYCZNY: Zwróć wynik STRICTE w poniższym formacie dla każdego ogłoszenia. Nie dodawaj ŻADNEGO tekstu przed ani po. Nie używaj wstępów ani podsumowań. Trzymaj się dokładnie tego wzoru:

    **Tytuł:** [Tytuł ogłoszenia]
    **Cena:** [Cena]
    **Ocena:** [Twoja ocena w skali 1-10]/10
    **Uzasadnienie:** [Tylko JEDNO konkretne zdanie uzasadniające ocenę, bazujące na usterce lub ważnym detalu z opisu]
    **Link:** [Link do ogłoszenia]
    ---

    Oto dane do analizy:
    {json_payload}
    """

    print("\n🤖 Wysyłam dane do sztucznej inteligencji Gemini...")

    fallback_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    sukces = False

    for model_name in fallback_models:
        try:
            print(f"   ➤ Pukam do serwera: {model_name}...")
            response = client.models.generate_content(model=model_name, contents=prompt)

            print("\n================ REPORT AI AGENTA ================")
            print(response.text)
            print("==================================================")

            # === WYSYŁKA NA DISCORDA ===
            DISCORD_WEBHOOK = "TUTAJ_WSTAW_TWÓJ_SKOPIOWANY_LINK_WEBHOOKA"
            send_to_discord(DISCORD_WEBHOOK, response.text)

            sukces = True
            break

        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"   ⚠️ {model_name} jest zapchany (Błąd 503). Przełączam...")
                continue
            else:
                print(f"   ❌ Wystąpił niespodziewany błąd z modelem {model_name}: {e}")
                break

    if not sukces:
        print("\n🚨 Wszystkie darmowe serwery Google leżą. Spróbuj za godzinę.")


if __name__ == "__main__":
    asyncio.run(main())
