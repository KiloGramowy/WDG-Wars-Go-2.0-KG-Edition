# ESP32 Watch Dogs — Open-World Hacking RPG

> Historyczny polski opis oryginalnego projektu LOCOSP. Zachowano go jako materiał źródłowy; nie jest specyfikacją bieżącego forka ani potwierdzeniem działania wszystkich funkcji. Aktualny angielski opis zmian i obsługi jest w [README](../README.md), [Feature guide](FEATURE_GUIDE.md) oraz [Validation](VALIDATION.md). Meshtastic, lokalne AUTO GPS, harmonogram WiFi/BLE i aktualizator opisano w tych bieżących dokumentach.

## Czym jest ta gra?

Wyobraź sobie Watch Dogs, ale zamiast fikcyjnego hackingu — prawdziwy. Żadnych symulacji, żadnego udawania. Twoje urządzenie naprawdę skanuje sieci WiFi wokół Ciebie, naprawdę wykrywa urządzenia Bluetooth, naprawdę przechwytuje pakiety lecące w powietrzu. A Ty dostajesz za to punkty doświadczenia, odznaki i awansujesz z poziomu NOOB na CYBER_DEMON.

ESP32 Watch Dogs to gra RPG z otwartym światem, która działa na przenośnej konsoli ClockworkPi uConsole — kieszonkowym komputerze z Linuxem, który wygląda jak rekwizyt z Blade Runnera. Na ekranie widzisz prawdziwą mapę świata z OpenStreetMap, na której pojawiają się sieci WiFi, urządzenia Bluetooth, samoloty, czujniki IoT i węzły sieci mesh — wszystko wykryte w czasie rzeczywistym przez sprzęt, który nosisz ze sobą.

## Skąd pomysł?

Pentesterzy i badacze bezpieczeństwa od lat korzystają z dziesiątek różnych narzędzi — Wireshark, aircrack-ng, Flipper Zero, SDR, skanery BLE. Każde z nich ma swoją konsolę, swój interfejs, swoje pliki. Pomysł był prosty: co gdyby to wszystko połączyć w jedną grę? Gdzie każdy skan to misja, każdy przechwycony handshake to osiągnięcie, a Twój poziom rośnie z każdym odkryciem?

Inspiracja? Watch Dogs Ubisoft — ale prawdziwy. Nie klikasz "hack" na ekranie i nie czekasz na pasek postępu. Tutaj musisz wiedzieć co robisz, a gra daje Ci do tego narzędzia i motywację w postaci systemu progresji rodem z RPG.

## Jak to działa?

### Serce systemu — ESP32

Do uConsole podpinasz malutki moduł ESP32-C5 XIAO (wielkości paznokcia) z dedykowanym firmware'em [projectZero](https://github.com/LOCOSP/projectZero). To on robi całą brudną robotę w eterze — skanuje, sniffuje, wysyła pakiety deautentykacji, stawia fałszywe access pointy. Gra komunikuje się z nim przez port szeregowy i wyświetla wszystko na mapie z ładnym interfejsem w stylu cyberpunk.

Nie masz ESP32-C5? Nie szkodzi — zegarek (o nim za chwilę) może go zastąpić.

### Moduły AIO v2

Jeśli masz rozszerzenie AIO v2 do uConsole, gra automatycznie obsługuje:

- **GPS** — Twoja pozycja na mapie, wardriving z logowaniem współrzędnych
- **LoRa SX1262** — szyfrowana komunikacja mesh poza zasięgiem internetu (MeshCore)
- **SDR (RTL2832U)** — odbiór sygnałów radiowych: śledzenie samolotów (ADS-B), dekodowanie czujników 433 MHz
- **USB Hub** — zasilanie ESP32 XIAO bezpośrednio z płytki

Każdy moduł włączasz i wyłączasz z menu gry — jeden przycisk.

### PipBoy Watch — zegarek-kompan

To najnowszy dodatek i prawdziwy game changer. LilyGo T-Watch Ultra z naszym firmware'em PipBoy-3000 łączy się z grą przez Bluetooth i daje Ci:

- **NFC** — skanowanie tagów NFC (karty dostępowe, tagi NDEF), zapis na kartę SD zegarka, transfer do gry w formacie Flipper Zero
- **LoRa** — drugi kanał komunikacji mesh, zintegrowany z czatem MeshCore w grze (wiadomości z zegarka oznaczone tagiem [W])
- **WiFi Recon** — skanowanie sieci i urządzeń ze zgięcia nadgarstka
- **Ataki** — deauth, blackout, evil twin, sniffer — wszystko sterowane z gry ale wykonywane przez zegarek
- **GPS + Kompas** — dodatkowe źródło lokalizacji
- **Detektor deauth (TSCM)** — pasywny monitoring ataków w Twojej okolicy

Zegarek działa też samodzielnie — ma własny interfejs web i ekran z czaszką kiedy jest połączony z grą. Dla kogoś kto nie ma uConsole z AIO i ESP32-C5, sam zegarek wystarczy jako pełnoprawne narzędzie do pentestingu sterowane z gry.

### Flipper Zero

Masz Flippera? Podpinasz go przez USB i gra przejmuje kontrolę — odczyt tagów NFC, SubGHz RX/TX, wszystko z poziomu interfejsu gry.

### SDR — Radio definiowane programowo

Włączasz moduł SDR z menu i uruchamiasz:

- **Radar ADS-B** — na Twojej mapie pojawiają się samoloty w czasie rzeczywistym. Zielone nisko, żółte średnio, niebieskie wysoko. Każdy nowy samolot to +10 XP.
- **Skaner 433 MHz** — gra dekoduje sygnały z czujników pogodowych, czujników ciśnienia w oponach (TPMS), czujników dymu, dzwonków do drzwi i setek innych urządzeń ISM. Każdy nowy sensor to +5 XP.

## Co widzisz na ekranie?

```
┌──────────────────────────────────────────────┐
│ Lv.7 HACKER ████████░░ XP:12450  [WDR][MSH] │  ← Twój poziom, XP, odznaki
├──────────────────────────────────────────────┤
│                                         ┌──┐ │
│     Mapa OpenStreetMap z liniami         │◎ │ │  ← Radar z kropkami
│     brzegowymi, kafelkami, markerami    └──┘ │
│     WiFi (●) BLE (■) samolotów (▲)     📡   │  ← Ikona SDR
│     czujników (~) i Twoją pozycją      📻   │  ← Ikona LoRa radio
│                                              │
├──────────────────────────────────────────────┤
│ [LoRa] [public] WatchDogs_a3f1: cześć!       │  ← Terminal z logami
│ [ADS-B] New: LOT523 alt:35000ft              │
│ [433] Bresser temp:22.5 humidity:65          │
├──────────────────────────────────────────────┤
│ BLE:42 WiFi:128 HS:3 PWD:1 PWN:5  ADS-B:7   │  ← Statystyki sesji
└──────────────────────────────────────────────┘
```

## System progresji

### Poziomy

Zaczynasz jako **NOOB** (0 XP). Za każdą aktywność dostajesz punkty doświadczenia:

| Aktywność | XP |
|-----------|-----|
| Nowa sieć WiFi | +15 |
| Nowe urządzenie BLE | +10 |
| Przechwycony handshake WPA | +200 |
| Credential z Evil Twin | +150 |
| Tag NFC (zegarek) | +25 |
| Nowy samolot (ADS-B) | +10 |
| Nowy czujnik (433 MHz) | +5 |
| Nowy kontakt MeshCore | +20 |

Awansujesz przez 16 poziomów:

**NOOB → SCRIPT_KIDDIE → WANNABE → PACKET_MONKEY → WARDRIVER → HACKER → NETRUNNER → PHREAKER → EXPLOIT_DEV → ELITE → SHADOW_OPS → GHOST → ZERO_DAY → APT_AGENT → CYBER_DEMON**

### Odznaki

Specjalne osiągnięcia za konkretne aktywności — WARDRIVER, HANDSHAKE, EVIL TWIN, MESHCORE, SKYWATCH (ADS-B), IOT HUNTER (433 MHz) i inne. Widoczne w pasku HUD na górze ekranu.

### Baza łupów

Wszystko co odkryjesz jest zapisywane:
- Sieci WiFi z pozycją GPS (wardriving CSV)
- Urządzenia BLE
- Handshake'i WPA w trzech formatach (PCAP, HCCAPX, Hashcat)
- Przechwycone hasła z Evil Twin
- Tagi NFC (format Flipper Zero)
- Dane samolotów i czujników
- Logi czatu MeshCore

Baza przetrwa między sesjami — Twój postęp się kumuluje.

## MeshCore Messenger — komunikacja bez internetu

Wbudowany szyfrowany czat mesh działający przez LoRa na częstotliwości 869.618 MHz. Zasięg? Kilometry w otwartym terenie, setki metrów w mieście. Zero internetu, zero infrastruktury — czysta komunikacja peer-to-peer.

- Kanały publiczne i prywatne (klucz z hashtaga)
- Szyfrowane wiadomości bezpośrednie (ECDH + AES-128)
- Lista kontaktów z notatkami
- Kompatybilność z urządzeniami MeshCore/MeshOS
- Działa przez LoRa na AIO LUB przez zegarek PipBoy (automatyczny fallback)

## Dla kogo jest ta gra?

- **Pentesterzy** — profesjonalne narzędzie z grywalizacją. Zamiast terminala i dwudziestu skryptów — jeden interfejs, automatyczny zapis loot'u, mapa z wszystkimi targetami.
- **Badacze bezpieczeństwa** — wardriving, analiza sieci, TSCM (wykrywanie ataków), zbieranie danych do raportów.
- **Entuzjaści hardware** — jeśli lubisz ESP32, LoRa, SDR, Flipper Zero, NFC — ta gra łączy to wszystko w jednym miejscu.
- **Hobbyści radiowi** — śledzenie samolotów, dekodowanie czujników, nasłuch na różnych częstotliwościach.
- **Survivaliści** — komunikacja mesh bez internetu, GPS offline z mapami OpenStreetMap.
- **Gracze** — jeśli lubisz RPG z systemem progresji i lubisz odkrywanie prawdziwego świata — to jest Pokémon Go dla hackerów.

## Wymagania sprzętowe

| Poziom | Co potrzebujesz | Co możesz robić |
|--------|----------------|-----------------|
| **Minimum** | uConsole + zegarek PipBoy | WiFi/BLE scan, deauth, NFC, LoRa, GPS |
| **Standard** | uConsole + AIO v2 + ESP32-C5 | Wszystko powyżej + handshake capture, evil twin z portalem, 5GHz |
| **Full** | Wszystko + SDR + Flipper | Wszystko + ADS-B radar, 433 MHz, SubGHz, pełny NFC |

Gra automatycznie wykrywa co masz podpięte i udostępnia odpowiednie funkcje. Nie musisz nic konfigurować — podpinasz i grasz.

## Open source

Cały kod gry, firmware ESP32 i firmware zegarka są open source. Możesz modyfikować, rozszerzać, dodawać własne moduły. Gra jest napisana w Pythonie (pyxel), firmware w C/C++ (ESP-IDF / Arduino).

---

*ESP32 Watch Dogs — bo prawdziwy hacking jest ciekawszy od symulowanego.*
