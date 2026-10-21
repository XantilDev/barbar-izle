#!/usr/bin/env python3
"""
Klanlar barbar köyü izleyici
----------------------------
Klanlar'ın herkese açık köy listesini (/map/village.txt) saatte bir indirir,
belirlediğiniz koordinat alanlarında YENİ bir barbar köyü belirirse Telegram'a
bildirim atar. Giriş yapmaz, hesabınızı kullanmaz, oyun sayfasına dokunmaz.

Telegram bilgilerini aynı klasördeki config.json'dan (fiyat botuyla ortak) okur.
Alanları barbar.json'dan okur (ilk çalıştırmada örnek dosya oluşturulur).

Komutlar:
  py barbar_izle.py           -> İzlemeyi başlatır (Ctrl+C ile durur)
  py barbar_izle.py liste     -> Alanlardaki mevcut barbar köylerini listeler
"""
import gzip
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

KLASOR = Path(__file__).resolve().parent
TG_CONFIG = KLASOR / "config.json"
AYAR = KLASOR / "barbar.json"
DURUM = KLASOR / "barbar_durum.json"

ORNEK_AYAR = {
    "dunya": "https://trp13.klanlar.org",
    "kontrol_araligi_dk": 60,
    "min_puan": 0,
    "alanlar": [
        {"ad": "Ana bölge", "x1": 480, "y1": 480, "x2": 520, "y2": 520},
        {"ad": "Köyüm çevresi", "merkez_x": 500, "merkez_y": 500, "yaricap": 15},
    ],
}


def zaman():
    return datetime.now().strftime("%d.%m %H:%M")


def json_oku(yol, varsayilan=None):
    if not yol.exists():
        return varsayilan
    with open(yol, encoding="utf-8") as f:
        return json.load(f)


def json_yaz(yol, veri):
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Veri
# --------------------------------------------------------------------------- #
def koyleri_indir(dunya):
    """village.txt satırları: id,ad,x,y,oyuncu_id,puan,bonus"""
    url = dunya.rstrip("/") + "/map/village.txt.gz"
    istek = urllib.request.Request(url, headers={"User-Agent": "barbar-izle/1.0 (kisisel kullanim)"})
    with urllib.request.urlopen(istek, timeout=60) as r:
        ham = r.read()
    try:
        metin = gzip.decompress(ham).decode("utf-8")
    except OSError:
        metin = ham.decode("utf-8")
    return koyleri_ayristir(metin)


def koyleri_ayristir(metin):
    koyler = []
    for satir in io.StringIO(metin):
        p = satir.strip().split(",")
        if len(p) < 6:
            continue
        try:
            koyler.append({
                "id": int(p[0]),
                "ad": urllib.parse.unquote_plus(p[1]),
                "x": int(p[2]),
                "y": int(p[3]),
                "oyuncu": int(p[4]),
                "puan": int(p[5]),
            })
        except ValueError:
            continue
    return koyler


def alanda_mi(k, a):
    if "merkez_x" in a:
        return math.hypot(k["x"] - a["merkez_x"], k["y"] - a["merkez_y"]) <= a["yaricap"]
    return (min(a["x1"], a["x2"]) <= k["x"] <= max(a["x1"], a["x2"])
            and min(a["y1"], a["y2"]) <= k["y"] <= max(a["y1"], a["y2"]))


def alandaki_barbarlar(koyler, ayar):
    """{koy_id: (koy, alan_adi)}"""
    sonuc = {}
    for k in koyler:
        if k["oyuncu"] != 0 or k["puan"] < ayar.get("min_puan", 0):
            continue
        for a in ayar["alanlar"]:
            if alanda_mi(k, a):
                sonuc[k["id"]] = (k, a["ad"])
                break
    return sonuc


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg_gonder(tg, metin):
    try:
        url = f"https://api.telegram.org/bot{tg['telegram_token'].strip()}/sendMessage"
        veri = urllib.parse.urlencode({
            "chat_id": tg["telegram_chat_id"], "text": metin,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=veri, timeout=15).read()
        return True
    except Exception as e:
        print(f"[{zaman()}] Telegram hatası: {e}")
        return False


def bildirim_metni(k, alan, dunya):
    import html
    link = f"{dunya.rstrip('/')}/game.php?screen=info_village&id={k['id']}"
    return (f"🏚️ <b>YENİ BARBAR KÖYÜ</b> — {html.escape(alan)}\n"
            f"📍 <b>{k['x']}|{k['y']}</b>  ·  {k['puan']} puan\n"
            f"{html.escape(k['ad'])}\n"
            f'<a href="{link}">Köyü oyunda aç</a>')


# --------------------------------------------------------------------------- #
# Komutlar
# --------------------------------------------------------------------------- #
def ayarlari_yukle():
    ayar = json_oku(AYAR)
    if ayar is None:
        json_yaz(AYAR, ORNEK_AYAR)
        print(f"Örnek ayar dosyası oluşturuldu: {AYAR.name}")
        print("Not Defteri ile açıp kendi koordinatlarınızı yazın, sonra tekrar çalıştırın.")
        sys.exit(0)
    return ayar


def komut_liste():
    ayar = ayarlari_yukle()
    barbarlar = alandaki_barbarlar(koyleri_indir(ayar["dunya"]), ayar)
    if not barbarlar:
        print("Alanlarınızda şu anda barbar köyü yok.")
        return
    for k, alan in sorted(barbarlar.values(), key=lambda t: (t[1], t[0]["x"], t[0]["y"])):
        print(f"  {alan:<16} {k['x']}|{k['y']}  {k['puan']:>6} puan  {k['ad']}")
    print(f"Toplam {len(barbarlar)} barbar köyü.")


def telegram_bilgisi():
    """Sunucuda ortam değişkenlerinden (GitHub Secrets), PC'de config.json'dan okur."""
    import os
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        return {"telegram_token": os.environ["TELEGRAM_TOKEN"],
                "telegram_chat_id": os.environ["TELEGRAM_CHAT_ID"]}
    tg = json_oku(TG_CONFIG, {})
    if not tg.get("telegram_token") or not tg.get("telegram_chat_id"):
        print("Telegram bilgileri yok (config.json veya TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).")
        sys.exit(1)
    return tg


def tek_tur(ayar, tg):
    """Bir kez tarar, yenileri bildirir, durumu kaydeder. Hata olursa False döner."""
    try:
        barbarlar = alandaki_barbarlar(koyleri_indir(ayar["dunya"]), ayar)
    except Exception as e:
        print(f"[{zaman()}] Köy listesi indirilemedi: {e}")
        return False

    simdiki = set(barbarlar)
    imza = json.dumps([ayar["alanlar"], ayar.get("min_puan", 0)], sort_keys=True)
    durum = json_oku(DURUM, {})
    if durum.get("imza") != imza:
        # İlk çalışma ya da alanlar değişti: mevcut köyleri sessizce kaydet
        tg_gonder(tg, f"✅ Barbar izleme başladı. Alanlarınızda şu an <b>{len(simdiki)}</b> "
                      f"barbar köyü var; bundan sonra yalnızca yenileri bildirilecek.")
        print(f"[{zaman()}] İlk tarama: {len(simdiki)} barbar köyü kaydedildi.")
    else:
        bilinen = set(durum.get("bilinen", []))
        yeniler = simdiki - bilinen
        for kid in sorted(yeniler):
            k, alan = barbarlar[kid]
            if tg_gonder(tg, bildirim_metni(k, alan, ayar["dunya"])):
                print(f"[{zaman()}] BİLDİRİM: {k['x']}|{k['y']} ({alan})")
        print(f"[{zaman()}] {len(simdiki)} barbar köyü, {len(yeniler)} yeni.")

    # Alınan köyler listeden düşer; tekrar barbar olursa yine bildirilir
    json_yaz(DURUM, {"imza": imza, "bilinen": sorted(simdiki)})
    return True


def komut_izle():
    ayar = ayarlari_yukle()
    tg = telegram_bilgisi()
    aralik = max(60, int(ayar.get("kontrol_araligi_dk", 60))) * 60  # en az saatte bir
    print(f"Barbar izleme başladı ({aralik // 60} dk aralıkla). Durdurmak için Ctrl+C.")
    while True:
        tek_tur(ayar, tg)
        time.sleep(aralik)


def komut_bir_kez():
    """Sunucu (GitHub Actions) için: bir tur çalışır ve çıkar."""
    if not tek_tur(ayarlari_yukle(), telegram_bilgisi()):
        sys.exit(1)


def main():
    komut = sys.argv[1] if len(sys.argv) > 1 else "izle"
    try:
        {"izle": komut_izle, "liste": komut_liste, "bir-kez": komut_bir_kez}.get(
            komut, lambda: print(__doc__))()
    except KeyboardInterrupt:
        print("\nDurduruldu.")


if __name__ == "__main__":
    main()
