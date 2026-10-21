#!/usr/bin/env python3
"""
Klanlar barbar köyü izleyici (Telegram grubu, arkadaş etiketlemeli)
-------------------------------------------------------------------
- Klanlar'ın herkese açık köy listesini (/map/village.txt) saatte bir indirir.
  Giriş yapmaz, hiçbir hesabı kullanmaz, oyun sayfasına dokunmaz.
- Haritada YENİ beliren barbar köylerini Telegram grubuna bildirir.
- Grup üyeleri kendi alanlarını komutla kaydeder; köy birinin alanına düşerse
  o kişi bildirimde etiketlenir.

Grup komutları (bot yanıtları bir sonraki çalışmada, en geç ~30 dk içinde gelir):
  /kur                      -> (yalnızca bot sahibi) bu grubu bildirim grubu yapar
  /alan 421-429 513-521     -> alan ekler (X aralığı, Y aralığı)
  /alan 421|513 429|521     -> aynı alan, köşe biçimiyle
  /alan 500|500 15          -> daire alan ekler (merkez + yarıçap)
  /alanlarim                -> kayıtlı alanlarınızı listeler
  /alansil                  -> tüm alanlarınızı siler
  /yardim                   -> komut listesi

Ayarlar barbar.json'da, durum (alanlar, bilinen köyler) barbar_durum.json'da tutulur.
Telegram bilgileri: TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenleri
(GitHub Secrets) ya da aynı klasördeki config.json. TELEGRAM_CHAT_ID, bot
sahibinin kendi özel sohbet ID'sidir (= Telegram kullanıcı ID'si).

Komutlar:
  python barbar_izle.py bir-kez   -> Bir tur çalışır (GitHub Actions bunu kullanır)
  python barbar_izle.py           -> Bilgisayarda sürekli çalışır (30 dk aralıkla)
"""
import gzip
import html
import json
import math
import os
import re
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

VARSAYILAN_AYAR = {
    "dunya": "https://trp13.klanlar.org",
    "tum_harita": True,     # False: yalnızca birinin alanına düşen köyler bildirilir
    "min_puan": 0,          # bu puanın altındaki barbar köyleri yok sayılır
    "kisi_basi_max_alan": 10,
}
INDIRME_ARALIGI_SN = 55 * 60   # Klanlar listeyi saatte bir günceller; daha sık indirmiyoruz
MESAJ_SINIRI = 3800            # Telegram sınırı 4096


def zaman():
    return datetime.now().strftime("%d.%m %H:%M")


def json_oku(yol, varsayilan):
    if not yol.exists():
        return varsayilan
    with open(yol, encoding="utf-8") as f:
        return json.load(f)


def durum_yaz(durum):
    with open(DURUM, "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=1, sort_keys=True)


# --------------------------------------------------------------------------- #
# Köy verisi
# --------------------------------------------------------------------------- #
def koyleri_indir(dunya):
    """village.txt satırları: id,ad,x,y,oyuncu_id,puan,bonus"""
    url = dunya.rstrip("/") + "/map/village.txt.gz"
    istek = urllib.request.Request(url, headers={"User-Agent": "barbar-izle/2.0 (kisisel kullanim)"})
    with urllib.request.urlopen(istek, timeout=60) as r:
        ham = r.read()
    try:
        metin = gzip.decompress(ham).decode("utf-8")
    except OSError:
        metin = ham.decode("utf-8")
    return koyleri_ayristir(metin)


def koyleri_ayristir(metin):
    koyler = []
    for satir in metin.splitlines():
        p = satir.strip().split(",")
        if len(p) < 6:
            continue
        try:
            koyler.append({
                "id": int(p[0]), "ad": urllib.parse.unquote_plus(p[1]),
                "x": int(p[2]), "y": int(p[3]), "oyuncu": int(p[4]), "puan": int(p[5]),
            })
        except ValueError:
            continue
    return koyler


def alanda_mi(k, a):
    if a["tip"] == "daire":
        return math.hypot(k["x"] - a["x"], k["y"] - a["y"]) <= a["r"]
    return a["x1"] <= k["x"] <= a["x2"] and a["y1"] <= k["y"] <= a["y2"]


def alan_metni(a):
    if a["tip"] == "daire":
        return f"{a['x']}|{a['y']} çevresi, {a['r']} alan"
    return f"{a['x1']}|{a['y1']} – {a['x2']}|{a['y2']}"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, token, sahip_id):
        self.token = token.strip()
        self.sahip_id = str(sahip_id).strip()

    def _istek(self, metod, veri):
        url = f"https://api.telegram.org/bot{self.token}/{metod}"
        govde = urllib.parse.urlencode(veri).encode()
        with urllib.request.urlopen(url, data=govde, timeout=30) as r:
            return json.loads(r.read().decode())

    def gonder(self, chat_id, metin, yanit_id=None):
        veri = {"chat_id": chat_id, "text": metin, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}
        if yanit_id:
            veri["reply_to_message_id"] = yanit_id
            veri["allow_sending_without_reply"] = "true"
        try:
            self._istek("sendMessage", veri)
            return True
        except Exception as e:
            print(f"[{zaman()}] Telegram hatası: {e}")
            return False

    def guncellemeler(self, offset):
        veri = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if offset:
            veri["offset"] = offset
        return self._istek("getUpdates", veri).get("result", [])


def telegram_olustur():
    token = os.environ.get("TELEGRAM_TOKEN")
    sahip = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and sahip):
        cfg = json_oku(TG_CONFIG, {})
        token, sahip = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not (token and sahip):
        print("Telegram bilgileri yok (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID veya config.json).")
        sys.exit(1)
    return Telegram(token, sahip)


def etiket(uid, kisi):
    return f'<a href="tg://user?id={uid}">{html.escape(kisi.get("ad") or "üye")}</a>'


# --------------------------------------------------------------------------- #
# Grup komutları
# --------------------------------------------------------------------------- #
YARDIM = (
    "🏚️ <b>Barbar köyü botu</b>\n\n"
    "Haritada yeni barbar köyü çıktığında bu gruba yazarım. Köy sizin alanınızdaysa sizi etiketlerim.\n\n"
    "<b>/alan 421-429 513-521</b> — alan ekle (önce X aralığı, sonra Y aralığı)\n"
    "<b>/alan 500|500 15</b> — köy çevresi ekle (merkez + yarıçap)\n"
    "<b>/alanlarim</b> — alanlarını gör\n"
    "<b>/alansil</b> — tüm alanlarını sil\n\n"
    "⏱️ Komutlara en geç ~30 dakika içinde yanıt veririm. "
    "Köy listesi saatte bir güncellenir."
)


def komutlari_isle(tg, durum, ayar):
    try:
        guncellemeler = tg.guncellemeler(durum.get("offset"))
    except Exception as e:
        print(f"[{zaman()}] Komutlar okunamadı: {e}")
        return
    kisiler = durum.setdefault("kisiler", {})
    for u in guncellemeler:
        durum["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        metin = (msg.get("text") or "").strip()
        if not metin.startswith("/"):
            continue
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))
        kimden = msg.get("from", {})
        uid = str(kimden.get("id"))
        komut, _, arg = metin.partition(" ")
        komut = komut.split("@")[0].lower()
        mid = msg.get("message_id")

        if komut == "/kur":
            if chat.get("type") not in ("group", "supergroup"):
                tg.gonder(chat_id, "Bu komutu bildirimlerin gideceği grupta yazın.", mid)
            elif uid != tg.sahip_id:
                tg.gonder(chat_id, "Grubu yalnızca botun sahibi kurabilir.", mid)
            else:
                durum["grup_id"] = chat_id
                if uid in durum.get("kisiler", {}):
                    durum["kisiler"][uid]["ad"] = kimden.get("first_name") or "Kurucu"
                tg.gonder(chat_id, "✅ Bu grup bildirim grubu olarak ayarlandı.\n\n" + YARDIM, mid)
                print(f"[{zaman()}] Grup kuruldu: {chat_id}")
            continue

        # Diğer komutlar yalnızca kayıtlı grupta (ya da grup yoksa sahibin özelinde) geçerli
        hedef = durum.get("grup_id") or tg.sahip_id
        if chat_id != hedef:
            continue

        if komut in ("/yardim", "/start", "/help"):
            tg.gonder(chat_id, YARDIM, mid)
        elif komut == "/alan":
            sayilar = [int(s) for s in re.findall(r"\d+", arg)]
            if len(sayilar) == 4:
                if "|" in arg:   # köşe biçimi: 421|513 429|521
                    x1, y1, x2, y2 = sayilar
                else:            # aralık biçimi: 421-429 513-521  (önce X, sonra Y)
                    x1, x2, y1, y2 = sayilar
                alan = {"tip": "dikdortgen", "x1": min(x1, x2), "y1": min(y1, y2),
                        "x2": max(x1, x2), "y2": max(y1, y2)}
            elif len(sayilar) == 3 and 1 <= sayilar[2] <= 100:
                alan = {"tip": "daire", "x": sayilar[0], "y": sayilar[1], "r": sayilar[2]}
            else:
                tg.gonder(chat_id, "Anlayamadım. Örnek:\n<b>/alan 421-429 513-521</b>  (X aralığı, Y aralığı)\n"
                                   "<b>/alan 500|500 15</b>", mid)
                continue
            kisi = kisiler.setdefault(uid, {"alanlar": []})
            kisi["ad"] = kimden.get("first_name") or kimden.get("username") or "üye"
            if len(kisi["alanlar"]) >= ayar["kisi_basi_max_alan"]:
                tg.gonder(chat_id, f"En fazla {ayar['kisi_basi_max_alan']} alan ekleyebilirsin. "
                                   "Önce /alansil ile temizle.", mid)
                continue
            kisi["alanlar"].append(alan)
            tg.gonder(chat_id, f"✅ {etiket(uid, kisi)} alanın eklendi: <b>{alan_metni(alan)}</b>", mid)
        elif komut == "/alanlarim":
            kisi = kisiler.get(uid)
            if not kisi or not kisi["alanlar"]:
                tg.gonder(chat_id, "Kayıtlı alanın yok. Eklemek için: <b>/alan 421-429 513-521</b>", mid)
            else:
                satirlar = "\n".join(f"• {alan_metni(a)}" for a in kisi["alanlar"])
                tg.gonder(chat_id, f"📍 {etiket(uid, kisi)} alanların:\n{satirlar}", mid)
        elif komut == "/alansil":
            kisiler.pop(uid, None)
            tg.gonder(chat_id, "🗑️ Tüm alanların silindi.", mid)


# --------------------------------------------------------------------------- #
# Barbar taraması
# --------------------------------------------------------------------------- #
def barbarlari_tara(tg, durum, ayar):
    if time.time() - durum.get("son_indirme", 0) < INDIRME_ARALIGI_SN:
        return True  # liste henüz güncellenmedi, indirmeye gerek yok
    try:
        koyler = koyleri_indir(ayar["dunya"])
    except Exception as e:
        print(f"[{zaman()}] Köy listesi indirilemedi: {e}")
        return False
    durum["son_indirme"] = int(time.time())

    barbarlar = {k["id"]: k for k in koyler if k["oyuncu"] == 0 and k["puan"] >= ayar["min_puan"]}
    hedef = durum.get("grup_id") or tg.sahip_id
    imza = f"{ayar['dunya']}|{ayar['min_puan']}"

    if durum.get("imza") != imza:
        durum["imza"] = imza
        durum["bilinen"] = sorted(barbarlar)
        tg.gonder(hedef, f"✅ Barbar izleme başladı. Haritada şu an <b>{len(barbarlar)}</b> barbar köyü var; "
                         "bundan sonra yalnızca yeni çıkanları bildireceğim.\n\nKomutlar için /yardim")
        print(f"[{zaman()}] İlk tarama: {len(barbarlar)} barbar köyü kaydedildi.")
        return True

    yeniler = [barbarlar[i] for i in sorted(set(barbarlar) - set(durum.get("bilinen", [])))]
    # Alınan köyler listeden düşer; tekrar barbara dönerse yine bildirilir
    durum["bilinen"] = sorted(barbarlar)

    satirlar = []
    for k in yeniler:
        etiketler = [etiket(uid, kisi) for uid, kisi in durum.get("kisiler", {}).items()
                     if any(alanda_mi(k, a) for a in kisi["alanlar"])]
        if not etiketler and not ayar["tum_harita"]:
            continue
        link = f"{ayar['dunya'].rstrip('/')}/game.php?screen=info_village&id={k['id']}"
        satir = f'📍 <a href="{link}"><b>{k["x"]}|{k["y"]}</b></a> · {k["puan"]} puan'
        if etiketler:
            satir += " — " + ", ".join(etiketler)
        satirlar.append((not etiketler, satir))   # etiketliler üstte

    print(f"[{zaman()}] {len(barbarlar)} barbar köyü, {len(yeniler)} yeni, {len(satirlar)} bildirilecek.")
    if not satirlar:
        return True

    satirlar.sort(key=lambda t: t[0])
    baslik = f"🏚️ <b>{len(satirlar)} YENİ BARBAR KÖYÜ</b>\n\n"
    parca = baslik
    for _, s in satirlar:
        if len(parca) + len(s) + 1 > MESAJ_SINIRI:
            tg.gonder(hedef, parca)
            parca = "🏚️ <b>(devam)</b>\n\n"
        parca += s + "\n"
    tg.gonder(hedef, parca)
    return True


def tek_tur():
    ayar = {**VARSAYILAN_AYAR, **json_oku(AYAR, {})}
    tg = telegram_olustur()
    durum = json_oku(DURUM, {})
    # barbar.json'daki "sahip_alanlari" bir kez bot sahibinin alanlarına eklenir
    if ayar.get("sahip_alanlari") and not durum.get("sahip_tohumlandi"):
        kisi = durum.setdefault("kisiler", {}).setdefault(tg.sahip_id, {"ad": "Kurucu", "alanlar": []})
        kisi["alanlar"].extend(ayar["sahip_alanlari"])
        durum["sahip_tohumlandi"] = True
    komutlari_isle(tg, durum, ayar)
    basarili = barbarlari_tara(tg, durum, ayar)
    durum_yaz(durum)
    return basarili


def main():
    komut = sys.argv[1] if len(sys.argv) > 1 else "izle"
    try:
        if komut == "bir-kez":
            sys.exit(0 if tek_tur() else 1)
        elif komut == "izle":
            print("Barbar izleme başladı (30 dk aralıkla). Durdurmak için Ctrl+C.")
            while True:
                tek_tur()
                time.sleep(30 * 60)
        else:
            print(__doc__)
    except KeyboardInterrupt:
        print("\nDurduruldu.")


if __name__ == "__main__":
    main()
