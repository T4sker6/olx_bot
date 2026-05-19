import argparse
import asyncio
import json
import random
import urllib.request
from google import genai
from playwright.async_api import async_playwright

# ==========================================
GEMINI_API_KEY = "TUTAJ_WSTAW_SWOJ_KLUCZ_API"
DISCORD_WEBHOOK = "TUTAJ_WSTAW_LINK_WEBHOOKA"
# ==========================================

# CONFIG MAPA - Tutaj trzymamy specyfikę każdej kategorii
KATEGORIE = {
    "laptopy": {
        "url": "https://www.olx.pl/elektronika/komputery/laptopy/?search%5Border%5D=created_at:desc",
        "tag": "LAPTOPY",
        "specyfika": "Zwróć uwagę na baterię, potencjał wymiany lub rozbudowy SSD a także dodanie RAMu. Odrzuć modele z uszkodzonym ekranem lub obudową, interesują Cię te z potencjałem taniej naprawy i łatwej odsprzedaży",
    },
    "komputery": {
        "url": "https://www.olx.pl/elektronika/komputery/komputery-stacjonarne/?search%5Border%5D=created_at:desc",
        "tag": "CAŁE PC",
        "specyfika": "Oceń konfigurację jako całość. Szukaj zestawów które można rozbudować lub tanio naprawić. Odrzucaj takie bez potencjału rozbudowy. Zawsze rozważ także możliwość rozebrania na części, zwłaszcza jeśli cena jest atrakcyjna.",
    },
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


async def get_ad_description(context, url):
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
    if webhook_url == "TUTAJ_WSTAW_LINK_WEBHOOKA":
        print("⚠️ Brak skonfigurowanego Webhooka Discorda. Pomijam wysyłkę.")
        return

    chunks = []
    current_chunk = ""

    for line in text.split("\n"):
        if len(current_chunk) + len(line) + 1 > 1900:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = current_chunk + "\n" + line if current_chunk else line

    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        payload = {"content": chunk}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req) as response:
                if response.getcode() in [200, 204]:
                    print("🚀 Raport pomyślnie wysłany na Discorda!")
        except Exception as e:
            print(f"❌ Błąd podczas wysyłania na Discorda: {e}")


async def main():
    # OBŁSUGA ARGUMENTÓW STARTOWYCH
    parser = argparse.ArgumentParser(description="OLX Scraper z podziałem na kategorie")
    parser.add_argument(
        "--kategoria",
        required=True,
        choices=KATEGORIE.keys(),
        help="Wybierz kategorię do skanowania",
    )
    args = parser.parse_args()

    config = KATEGORIE[args.kategoria]
    cache_file = (
        f"seen_links_{args.kategoria}.txt"  # Osobny plik pamięci dla każdej kategorii
    )

    if GEMINI_API_KEY == "TUTAJ_WSTAW_SWOJ_KLUCZ_API":
        print("❌ BŁĄD: Brak klucza API!")
        return

    try:
        with open(cache_file, "r") as f:
            all_links = f.read().splitlines()
    except FileNotFoundError:
        all_links = []

    seen_links = set(all_links)
    nowe_linki_w_tej_sesji = []

    async with async_playwright() as p:
        print(f"🌐 [{config['tag']}] Odpalam przeglądarkę Chromium...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"🔎 Wchodzę na OLX: {config['url']}")
        await page.goto(config["url"])
        await page.mouse.wheel(0, 1000)
        await page.wait_for_timeout(3000)

        ads = await page.query_selector_all('div[data-cy="l-card"]')

        scraped_data = []
        organic_count = 0

        print("📦 Zbieram najświeższe oferty...")

        for ad in ads:
            if organic_count >= 35:
                break

            link_el = await ad.query_selector("a")
            link = await link_el.get_attribute("href") if link_el else ""

            if "promoted" in link or not link:
                continue

            if link.startswith("/d/"):
                link = "https://www.olx.pl" + link

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

    if not scraped_data:
        print(f"☕ [{config['tag']}] Brak nowych ogłoszeń. Kończę.")
        return

    if nowe_linki_w_tej_sesji:
        all_links.extend(nowe_linki_w_tej_sesji)
        all_links = all_links[-2000:]
        with open(cache_file, "w") as f:
            f.write("\n".join(all_links) + "\n")

    json_payload = json.dumps(scraped_data, ensure_ascii=False, indent=2)

    # DYNAMICZNY PROMPT - Wstrzykuje specyfikę wybranej kategorii
    prompt = f"""
    Jesteś ekspertem od wyceny, rynku wtórnego i naprawy sprzętu komputerowego.
    Analizujesz przedmioty z kategorii: {config['tag']}.
    
    Twoje wytyczne dla tej kategorii: {config['specyfika']}
    
    Oceń opłacalność zakupu poniższych urządzeń pod kątem dalszej odsprzedaży z zyskiem lub rozbudowy własnego homelabu.
    Odrzuć oferty bez potencjału lub skrajnie przewartościowane.

    WYMÓG KRYTYCZNY: Zwróć wynik STRICTE w poniższym formacie dla każdego zatwierdzonego ogłoszenia. Nie dodawaj ŻADNEGO tekstu przed ani po. Trzymaj się dokładnie tego wzoru:

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

            # WYSYŁKA NA DISCORDA
            send_to_discord(DISCORD_WEBHOOK, response.text)

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
