#!/usr/bin/env python3
"""
Klanlar barbar köyü izleyici (Telegram grubu, arkadaş etiketlemeli)
-------------------------------------------------------------------
- Klanlar'ın herkese açık köy listesini (/map/village.txt) saatte bir indirir.
  Giriş yapmaz, hiçbir hesabı kullanmaz, oyun sayfasına dokunmaz.
- Haritada YENİ beliren barbar köylerini Telegram grubuna bildirir.
- Grup üyeleri kendi alanlarını komutla kaydeder; köy birinin alanına düşerse
  o kişi bildirimde etiketlenir.

Grup komutları (bot yanıtları bir sonraki çalışmada, en geç ~5 dk içinde gelir):
  /kur                      -> (yalnızca bot sahibi) bu grubu bildirim grubu yapar
  /alan 421-429 513-521     -> alan ekler (X aralığı, Y aralığı)
  /alan 421|513 429|521     -> aynı alan, köşe biçimiyle
  /alan 500|500 15          -> daire alan ekler (merkez + yarıçap)
  /alanlarim                -> kayıtlı alanlarınızı listeler
  /alansil                  -> tüm alanlarınızı siler
  /oyuncu İsim              -> oyuncu bilgisi, aktiflik durumu ve profil bağlantısı
  /pasifler [gün]           -> X gündür (varsayılan 5) hiç gelişmeyen oyuncular
  /yardim                   -> komut listesi

Ayarlar barbar.json'da, durum (alanlar, bilinen köyler) barbar_durum.json'da tutulur.
Telegram bilgileri: TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenleri
(GitHub Secrets) ya da aynı klasördeki config.json. TELEGRAM_CHAT_ID, bot
sahibinin kendi özel sohbet ID'sidir (= Telegram kullanıcı ID'si).

Komutlar:
  python barbar_izle.py bir-kez   -> Bir tur çalışır (GitHub Actions bunu kullanır)
  python barbar_izle.py           -> Bilgisayarda sürekli çalışır (5 dk aralıkla)
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
    "pasif_liste_gun": 5,       # /pasifler varsayılanı
    "pasif_bildirim": True,     # 7 gündür hareketsiz oyuncuları otomatik bildir
    "pasif_bildirim_gun": 7,
}
TUR = {}   # tek çalışma içinde paylaşılan veri (indirilen köy listesi gibi)
INDIRME_ARALIGI_SN = 55 * 60   # sürüm bilgisi alınamazsa en fazla saatte bir indir
UA = "barbar-izle/3.0 (kisisel kullanim)"
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
def _url(dunya, ad):
    return f"{dunya.rstrip('/')}/map/{ad}.txt.gz"


def dosya_surumu(dunya, ad):
    """Dosyayı indirmeden yalnızca sürüm bilgisini (Last-Modified / ETag) sorar."""
    istek = urllib.request.Request(_url(dunya, ad), method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(istek, timeout=30) as r:
        return r.headers.get("Last-Modified") or r.headers.get("ETag")


def dosya_indir(dunya, ad):
    istek = urllib.request.Request(_url(dunya, ad), headers={"User-Agent": UA})
    with urllib.request.urlopen(istek, timeout=60) as r:
        ham = r.read()
        surum = r.headers.get("Last-Modified") or r.headers.get("ETag")
    try:
        return gzip.decompress(ham).decode("utf-8"), surum
    except OSError:
        return ham.decode("utf-8"), surum


def liste_surumu(dunya):
    return dosya_surumu(dunya, "village")


def koyleri_indir(dunya):
    """village.txt satırları: id,ad,x,y,oyuncu_id,puan,bonus"""
    metin, surum = dosya_indir(dunya, "village")
    return koyleri_ayristir(metin), surum


def satirlar_csv(metin):
    for satir in metin.splitlines():
        p = satir.strip().split(",")
        if len(p) >= 3:
            yield p


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
            TUR["gonderilen"] = TUR.get("gonderilen", 0) + 1
            return True
        except Exception as e:
            aciklama = str(e)
            if hasattr(e, "read"):
                try:
                    aciklama += " — " + json.loads(e.read().decode()).get("description", "")
                except Exception:
                    pass
            hedef_turu = "grup" if str(chat_id).startswith("-") else "özel sohbet"
            print(f"::warning::Telegram gönderilemedi ({hedef_turu}): {aciklama}")
            TUR["hata"] = True
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
    "<b>/barbarlar</b> — alanlarındaki mevcut barbar köyleri\n"
    "<b>/durum</b> — bot çalışıyor mu, en son ne zaman baktı\n"
    "<b>/oyuncu İsim</b> — oyuncu bilgisi ve aktiflik durumu\n"
    "<b>/pasifler</b> — 5 gündür hiç gelişmeyen oyuncular (<b>/pasifler 3</b> gibi gün de yazılabilir)\n\n"
    "⚫ 7 gündür hareketsiz oyuncuları da bildiririm; köyleri alanınızdaysa sizi etiketlerim.\n\n"
    "⏱️ Komutlara en geç ~5 dakika içinde yanıt veririm. "
    "Klanlar köy listesini saatte bir yayınlar; yayınlandıktan en geç 5 dk sonra bildiririm."
)


def komutlari_isle(tg, durum, ayar):
    try:
        guncellemeler = tg.guncellemeler(durum.get("offset"))
    except Exception as e:
        aciklama = str(e)
        if hasattr(e, "read"):
            try:
                aciklama += " — " + json.loads(e.read().decode()).get("description", "")
            except Exception:
                pass
        print(f"::warning::Telegram komutları okunamadı: {aciklama}")
        TUR["hata"] = True
        return
    ozet = {}
    for u in guncellemeler:
        m = u.get("message") or {}
        t = (m.get("text") or "")
        tur = m.get("chat", {}).get("type", "?")
        anahtar = f"{tur}:{t.split(' ')[0].split('@')[0] if t.startswith('/') else ('metin' if t else 'diğer')}"
        ozet[anahtar] = ozet.get(anahtar, 0) + 1
    print(f"::notice::Telegram güncellemeleri: {len(guncellemeler)} {ozet or ''} | grup kurulu: {'evet' if durum.get('grup_id') else 'hayır'}")
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
                print(f"[{zaman()}] Bildirim grubu kuruldu.")
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
        elif komut == "/durum":
            komut_durum(tg, durum, ayar, chat_id, mid)
        elif komut == "/barbarlar":
            komut_barbarlar(tg, durum, ayar, chat_id, mid, uid)
        elif komut == "/oyuncu":
            komut_oyuncu(tg, durum, ayar, chat_id, mid, arg)
        elif komut == "/pasifler":
            sayi = re.findall(r"\d+", arg)
            komut_pasifler(tg, durum, ayar, chat_id, mid, int(sayi[0]) if sayi else ayar["pasif_liste_gun"])


# --------------------------------------------------------------------------- #
# Oyuncu takibi ve aktiflik
# --------------------------------------------------------------------------- #
GUN = 86400


def tr_kucuk(s):
    return s.replace("İ", "i").replace("I", "ı").lower()


def nokta(n):
    return f"{n:,}".replace(",", ".")


def sure_metni(sn):
    sn = max(0, int(sn))
    g, kalan = divmod(sn, GUN)
    sa = kalan // 3600
    if g:
        return f"{g} gün {sa} sa" if sa else f"{g} gün"
    return f"{sa} sa" if sa else f"{max(1, kalan // 60)} dk"


def oyuncu_linki(ayar, pid, o):
    url = f"{ayar['dunya'].rstrip('/')}/game.php?screen=info_player&id={pid}"
    return f'<a href="{url}">{html.escape(o["n"])}</a>' + twstats_linki(ayar, pid)


def twstats_linki(ayar, pid):
    """Oyuncunun TWStats sayfası (puan geçmişi grafiği) — yalnızca klanlar.org dünyalarında."""
    host = urllib.parse.urlparse(ayar["dunya"]).hostname or ""
    if not host.endswith("klanlar.org"):
        return ""
    dunya_kodu = host.split(".")[0]
    return f' <a href="https://tr.twstats.com/{dunya_kodu}/index.php?page=player&id={pid}">📊</a>'


def klan_metni(ayar, durum, o):
    tag = durum.get("klanlar", {}).get(str(o.get("kl", 0)))
    if not tag:
        return ""
    url = f"{ayar['dunya'].rstrip('/')}/game.php?screen=info_ally&id={o['kl']}"
    return f' <a href="{url}">[{html.escape(tag)}]</a>'


def hareketsizlik(durum, o, simdi):
    """(saniye, kesin_mi): son gelişmeden bu yana geçen süre. Takip başlangıcından
    beri hiç gelişme yoksa süre 'en az' anlamındadır (kesin değildir)."""
    return simdi - o["d"], o["d"] > durum.get("takip_baslangic", 0)


def oyunculari_guncelle(tg, durum, ayar):
    dunya = ayar["dunya"]
    surumler = durum.setdefault("surumler", {})
    try:
        surum = dosya_surumu(dunya, "player")
    except Exception as e:
        print(f"[{zaman()}] Oyuncu listesi sürümü sorulamadı: {e}")
        return
    if surum and surum == surumler.get("player"):
        return
    try:
        oyuncu_txt, surum = dosya_indir(dunya, "player")
        klan_txt, _ = dosya_indir(dunya, "ally")
        oda_txt, _ = dosya_indir(dunya, "kill_att")
        ods_txt, _ = dosya_indir(dunya, "kill_sup")
    except Exception as e:
        print(f"[{zaman()}] Oyuncu verileri indirilemedi: {e}")
        return
    surumler["player"] = surum

    oda = {p[1]: int(p[2]) for p in satirlar_csv(oda_txt) if p[2].isdigit()}
    ods = {p[1]: int(p[2]) for p in satirlar_csv(ods_txt) if p[2].isdigit()}
    durum["klanlar"] = {p[0]: urllib.parse.unquote_plus(p[2]) for p in satirlar_csv(klan_txt)}

    simdi = int(time.time())
    durum.setdefault("takip_baslangic", simdi)
    eski = durum.get("oyuncular", {})
    yeni = {}
    for p in satirlar_csv(oyuncu_txt):
        if len(p) < 6:
            continue
        pid = p[0]
        try:
            v = {"n": urllib.parse.unquote_plus(p[1]), "kl": int(p[2]), "k": int(p[3]),
                 "p": int(p[4]), "r": int(p[5]), "a": oda.get(pid, 0), "s": ods.get(pid, 0)}
        except ValueError:
            continue
        o = eski.get(pid)
        # Gelişme: puan, köy, saldırı (ODA) ya da destek (ODS) artışı.
        # Savunma puanı (ODD) sayılmaz: hareketsiz oyuncu saldırı yedikçe de artar.
        if o is None:
            v["d"], v["b"] = simdi, False
        elif v["p"] > o["p"] or v["k"] > o["k"] or v["a"] > o["a"] or v["s"] > o["s"]:
            v["d"], v["b"] = simdi, False
        else:
            v["d"], v["b"] = o["d"], o.get("b", False)
        yeni[pid] = v
    durum["oyuncular"] = yeni
    print(f"[{zaman()}] {len(yeni)} oyuncu güncellendi.")

    if ayar["pasif_bildirim"]:
        pasifleri_bildir(tg, durum, ayar, simdi)


def pasifleri_bildir(tg, durum, ayar, simdi):
    esik = ayar["pasif_bildirim_gun"] * GUN
    if simdi - durum["takip_baslangic"] < esik:
        return   # yeterince uzun süredir takip etmiyoruz; yanlış alarm vermeyelim
    yeni_pasifler = [(pid, o) for pid, o in durum["oyuncular"].items()
                     if not o["b"] and o["k"] > 0 and simdi - o["d"] >= esik]
    if not yeni_pasifler:
        return
    for _, o in yeni_pasifler:
        o["b"] = True   # her oyuncu bir kez bildirilir; tekrar gelişirse sıfırlanır

    koyler = TUR.get("koyler")
    if koyler is None:
        try:
            koyler, _ = koyleri_indir(ayar["dunya"])
        except Exception:
            koyler = []
    oyuncu_koyleri = {}
    for k in koyler:
        oyuncu_koyleri.setdefault(str(k["oyuncu"]), []).append(k)

    satirlar = []
    for pid, o in sorted(yeni_pasifler, key=lambda t: -t[1]["p"]):
        satir = (f"👤 {oyuncu_linki(ayar, pid, o)}{klan_metni(ayar, durum, o)} · "
                 f"{o['k']} köy · {nokta(o['p'])} puan")
        etiketli = False
        for k in oyuncu_koyleri.get(pid, []):
            etiketler = [etiket(uid, kisi) for uid, kisi in durum.get("kisiler", {}).items()
                         if any(alanda_mi(k, a) for a in kisi["alanlar"])]
            if etiketler:
                link = f"{ayar['dunya'].rstrip('/')}/game.php?screen=info_village&id={k['id']}"
                satir += f'\n    📍 <a href="{link}">{k["x"]}|{k["y"]}</a> — ' + ", ".join(etiketler)
                etiketli = True
        satirlar.append((not etiketli, satir))
    satirlar.sort(key=lambda t: t[0])
    hedef = durum.get("grup_id") or tg.sahip_id
    parcali_gonder(tg, hedef,
                   f"⚫ <b>{len(satirlar)} OYUNCU {ayar['pasif_bildirim_gun']} GÜNDÜR HAREKETSİZ</b>\n"
                   "Puan, köy, ODA ve ODS değişmedi — yakında barbara düşebilir.\n\n",
                   [s for _, s in satirlar])


def komut_oyuncu(tg, durum, ayar, chat_id, mid, sorgu):
    oyuncular = durum.get("oyuncular")
    if not oyuncular:
        tg.gonder(chat_id, "Oyuncu listesi henüz yüklenmedi, biraz sonra tekrar dene.", mid)
        return
    q = tr_kucuk(sorgu.strip())
    if not q:
        tg.gonder(chat_id, "Örnek: <b>/oyuncu OyuncuAdı</b>", mid)
        return
    bulunan = [pid for pid, o in oyuncular.items() if tr_kucuk(o["n"]) == q]
    if not bulunan:
        bulunan = [pid for pid, o in oyuncular.items() if q in tr_kucuk(o["n"])]
    if not bulunan:
        tg.gonder(chat_id, f"“{html.escape(sorgu.strip())}” adında oyuncu bulamadım.", mid)
        return
    if len(bulunan) > 1:
        bulunan.sort(key=lambda pid: -oyuncular[pid]["p"])
        liste = "\n".join(f"• {oyuncu_linki(ayar, pid, oyuncular[pid])}{klan_metni(ayar, durum, oyuncular[pid])}"
                          for pid in bulunan[:10])
        ek = f"\n… ve {len(bulunan) - 10} oyuncu daha" if len(bulunan) > 10 else ""
        tg.gonder(chat_id, f"Birden fazla oyuncu bulundu, tam adını yaz:\n{liste}{ek}", mid)
        return

    pid = bulunan[0]
    o = oyuncular[pid]
    simdi = int(time.time())
    sn, kesin = hareketsizlik(durum, o, simdi)
    gun = sn / GUN
    if gun < 2:
        durum_metni = "🟢 Aktif"
    elif gun < 5:
        durum_metni = "🟡 Az aktif"
    elif gun < 7:
        durum_metni = "🔴 Pasif"
    else:
        durum_metni = "⚫ Uzun süredir hareketsiz — barbara düşebilir"
    if kesin:
        son = f"{sure_metni(sn)} önce"
    else:
        son = f"takip başladığından beri yok (en az {sure_metni(sn)})"
        if gun < 2:
            durum_metni = "⚪ Henüz bilinmiyor (takip yeni başladı)"
    tg.gonder(chat_id,
              f"👤 <b>{oyuncu_linki(ayar, pid, o)}</b>{klan_metni(ayar, durum, o)}\n"
              f"🏘️ {o['k']} köy · {nokta(o['p'])} puan · {o['r']}. sıra\n"
              f"⚔️ ODA {nokta(o['a'])} · 🤝 ODS {nokta(o['s'])}\n"
              f"📈 Son gelişme: {son}\n"
              f"{durum_metni}", mid)


def komut_pasifler(tg, durum, ayar, chat_id, mid, gun):
    oyuncular = durum.get("oyuncular")
    if not oyuncular:
        tg.gonder(chat_id, "Oyuncu listesi henüz yüklenmedi, biraz sonra tekrar dene.", mid)
        return
    simdi = int(time.time())
    takip = simdi - durum["takip_baslangic"]
    esik = max(1, gun) * GUN
    if takip < esik:
        # Henüz yeterli geçmiş yok: takip başlangıcından beri hiç gelişmeyenleri göster
        hazir = datetime.fromtimestamp(durum["takip_baslangic"] + esik).strftime("%d.%m %H:%M")
        adaylar = sorted(((pid, o) for pid, o in oyuncular.items()
                          if o["k"] > 0 and o["d"] <= durum["takip_baslangic"]), key=lambda t: -t[1]["p"])
        satirlar = [f"• {oyuncu_linki(ayar, pid, o)}{klan_metni(ayar, durum, o)} · {o['k']} köy · "
                    f"{nokta(o['p'])} puan" for pid, o in adaylar[:60]]
        if len(adaylar) > 60:
            satirlar.append(f"… ve {len(adaylar) - 60} oyuncu daha")
        parcali_gonder(tg, chat_id,
                       f"⏳ <b>Takip {sure_metni(takip)} önce başladı.</b> O zamandan beri hiç gelişmeyen "
                       f"<b>{len(adaylar)}</b> oyuncu (puana göre sıralı).\n"
                       f"Süre kısa olduğu için bu liste kesin değil; gerçek {gun} günlük liste <b>{hazir}</b>'de hazır olur. "
                       "📊 bağlantısından oyuncunun geçmiş puanlarına bakabilirsiniz.\n\n",
                       satirlar or ["(henüz yok)"], mid)
        return
    pasifler = sorted(((pid, o) for pid, o in oyuncular.items()
                       if o["k"] > 0 and simdi - o["d"] >= esik), key=lambda t: -t[1]["p"])
    if not pasifler:
        tg.gonder(chat_id, f"{gun} gündür hareketsiz oyuncu yok.", mid)
        return
    satirlar = [f"• {oyuncu_linki(ayar, pid, o)}{klan_metni(ayar, durum, o)} · {o['k']} köy · "
                f"{nokta(o['p'])} puan · {sure_metni(simdi - o['d'])}"
                for pid, o in pasifler[:60]]
    if len(pasifler) > 60:
        satirlar.append(f"… ve {len(pasifler) - 60} oyuncu daha")
    parcali_gonder(tg, chat_id, f"😴 <b>{gun} gündür gelişmeyen {len(pasifler)} oyuncu</b>\n"
                                "(puan, köy, ODA, ODS değişmedi — puana göre sıralı)\n\n", satirlar, mid)


def once(ts, simdi):
    return f"{sure_metni(simdi - ts)} önce" if ts else "henüz yok"


def komut_durum(tg, durum, ayar, chat_id, mid):
    simdi = int(time.time())
    onceki = durum.get("son_calisma")
    satirlar = [
        "🤖 <b>Bot çalışıyor</b>",
        f"⏱️ Bir önceki çalışma: {once(onceki, simdi)}",
        f"🗺️ Köy listesi en son indirildi: {once(durum.get('son_indirme'), simdi)}",
    ]
    if durum.get("liste_surumu"):
        try:
            from email.utils import parsedate_to_datetime
            yayin = parsedate_to_datetime(durum["liste_surumu"]).astimezone().strftime("%d.%m %H:%M")
        except Exception:
            yayin = html.escape(str(durum["liste_surumu"]))
        satirlar.append(f"📅 Klanlar listeyi en son yayınladı: {yayin}")
    satirlar += [
        f"🏚️ Haritadaki barbar köyü: <b>{durum.get('barbar_sayisi', len(durum.get('bilinen', [])))}</b>",
        f"🆕 En son yeni barbar: {once(durum.get('son_yeni_barbar'), simdi)}",
        f"👥 Takip edilen oyuncu: <b>{len(durum.get('oyuncular', {}))}</b> "
        f"(takip başlangıcı: {once(durum.get('takip_baslangic'), simdi)})",
        f"📍 Alan kaydeden kişi: <b>{sum(1 for k in durum.get('kisiler', {}).values() if k['alanlar'])}</b>",
    ]
    if onceki and simdi - onceki > 20 * 60:
        satirlar.append("⚠️ Bir önceki çalışmadan bu yana 20 dakikadan fazla geçmiş — tetikleyici aksıyor olabilir.")
    tg.gonder(chat_id, "\n".join(satirlar), mid)


def komut_barbarlar(tg, durum, ayar, chat_id, mid, uid):
    kisi = durum.get("kisiler", {}).get(uid)
    if not kisi or not kisi["alanlar"]:
        tg.gonder(chat_id, "Kayıtlı alanın yok. Önce: <b>/alan 421-429 513-521</b>", mid)
        return
    koyler = TUR.get("koyler")
    if koyler is None:
        try:
            koyler, _ = koyleri_indir(ayar["dunya"])
        except Exception:
            tg.gonder(chat_id, "Köy listesine şu an ulaşamadım, biraz sonra tekrar dene.", mid)
            return
        TUR["koyler"] = koyler
    bulunan = sorted((k for k in koyler if k["oyuncu"] == 0 and k["puan"] >= ayar["min_puan"]
                      and any(alanda_mi(k, a) for a in kisi["alanlar"])), key=lambda k: -k["puan"])
    if not bulunan:
        tg.gonder(chat_id, f"{etiket(uid, kisi)} alanlarında şu an barbar köyü yok.", mid)
        return
    satirlar = []
    for k in bulunan[:50]:
        link = f"{ayar['dunya'].rstrip('/')}/game.php?screen=info_village&id={k['id']}"
        satirlar.append(f'📍 <a href="{link}"><b>{k["x"]}|{k["y"]}</b></a> · {nokta(k["puan"])} puan')
    if len(bulunan) > 50:
        satirlar.append(f"… ve {len(bulunan) - 50} köy daha")
    parcali_gonder(tg, chat_id, f"🏚️ {etiket(uid, kisi)} alanlarında <b>{len(bulunan)}</b> barbar köyü:\n\n",
                   satirlar, mid)


def parcali_gonder(tg, hedef, baslik, satirlar, yanit_id=None):
    parca = baslik
    for s in satirlar:
        if len(parca) + len(s) + 1 > MESAJ_SINIRI:
            tg.gonder(hedef, parca, yanit_id)
            parca = "<b>(devam)</b>\n\n"
        parca += s + "\n"
    tg.gonder(hedef, parca, yanit_id)


# --------------------------------------------------------------------------- #
# Barbar taraması
# --------------------------------------------------------------------------- #
def surum_zamani(surum):
    """Last-Modified başlığını zaman damgasına çevirir; çevrilemezse None."""
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(surum).timestamp())
    except Exception:
        return None


HATIRLAMA_SN = 7 * 86400   # bildirilen barbar köyü 7 gün boyunca tekrar bildirilmez


def barbarlari_tara(tg, durum, ayar):
    # Liste değişmediyse indirme (sunucuyu gereksiz yere yormamak için)
    try:
        surum = liste_surumu(ayar["dunya"])
    except Exception as e:
        print(f"[{zaman()}] Liste sürümü sorulamadı: {e}")
        surum = None
    if surum and surum == durum.get("liste_surumu"):
        print(f"[{zaman()}] Köy listesi değişmemiş, indirilmedi.")
        return True
    # Bazı sunucular kısa süre eski listeyi verebiliyor: bildiğimizden eskiyse hiç indirme
    yeni_zaman = surum_zamani(surum) if surum else None
    if yeni_zaman and durum.get("liste_zamani") and yeni_zaman < durum["liste_zamani"]:
        print(f"::notice::Eski köy listesi geldi ({surum}), yok sayıldı.")
        return True
    if not surum and time.time() - durum.get("son_indirme", 0) < INDIRME_ARALIGI_SN:
        return True
    try:
        koyler, surum = koyleri_indir(ayar["dunya"])
    except Exception as e:
        print(f"[{zaman()}] Köy listesi indirilemedi: {e}")
        return False
    yeni_zaman = surum_zamani(surum) if surum else None
    if yeni_zaman and durum.get("liste_zamani") and yeni_zaman < durum["liste_zamani"]:
        print(f"::notice::Eski köy listesi indirildi ({surum}), yok sayıldı.")
        return True
    durum["son_indirme"] = int(time.time())
    durum["liste_surumu"] = surum
    if yeni_zaman:
        durum["liste_zamani"] = yeni_zaman
    TUR["koyler"] = koyler

    barbarlar = {k["id"]: k for k in koyler if k["oyuncu"] == 0 and k["puan"] >= ayar["min_puan"]}
    hedef = durum.get("grup_id") or tg.sahip_id
    imza = f"{ayar['dunya']}|{ayar['min_puan']}"

    simdi = int(time.time())
    durum["barbar_sayisi"] = len(barbarlar)

    if durum.get("imza") != imza:
        durum["imza"] = imza
        durum["gorulen"] = {str(i): simdi for i in barbarlar}
        durum.pop("bilinen", None)
        tg.gonder(hedef, f"✅ Barbar izleme başladı. Haritada şu an <b>{len(barbarlar)}</b> barbar köyü var; "
                         "bundan sonra yalnızca yeni çıkanları bildireceğim.\n\nKomutlar için /yardim")
        print(f"[{zaman()}] İlk tarama: {len(barbarlar)} barbar köyü kaydedildi.")
        return True

    # "gorulen": barbar olarak gördüğümüz her köy ve en son görülme zamanı.
    # Bir köy, son 7 gün içinde barbar olarak görüldüyse tekrar bildirilmez — böylece
    # sunucunun arada eski liste vermesi ya da köyün kısa süre el değiştirmesi tekrar üretmez.
    if "gorulen" not in durum:   # eski sürümden geçiş
        durum["gorulen"] = {str(i): simdi for i in durum.pop("bilinen", [])}
    gorulen = durum["gorulen"]
    yeniler = [barbarlar[i] for i in sorted(barbarlar)
               if simdi - gorulen.get(str(i), 0) > HATIRLAMA_SN]
    for i in barbarlar:
        gorulen[str(i)] = simdi
    for i in [i for i, t in gorulen.items() if simdi - t > HATIRLAMA_SN]:
        del gorulen[i]
    if yeniler:
        durum["son_yeni_barbar"] = simdi

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

    print(f"::notice::{len(barbarlar)} barbar köyü, {len(yeniler)} yeni, {len(satirlar)} bildirilecek ({surum}).")
    if not satirlar:
        return True

    satirlar.sort(key=lambda t: t[0])
    parcali_gonder(tg, hedef, f"🏚️ <b>{len(satirlar)} YENİ BARBAR KÖYÜ</b>\n\n", [s for _, s in satirlar])
    return True


def tek_tur():
    TUR.clear()
    TUR["baslangic"] = int(time.time())
    ayar = {**VARSAYILAN_AYAR, **json_oku(AYAR, {})}
    tg = telegram_olustur()
    durum = json_oku(DURUM, {})
    # barbar.json'daki "sahip_alanlari" bir kez bot sahibinin alanlarına eklenir
    if ayar.get("sahip_alanlari") and not durum.get("sahip_tohumlandi"):
        kisi = durum.setdefault("kisiler", {}).setdefault(tg.sahip_id, {"ad": "Kurucu", "alanlar": []})
        kisi["alanlar"].extend(ayar["sahip_alanlari"])
        durum["sahip_tohumlandi"] = True
    oyunculari_guncelle(tg, durum, ayar)   # komutlar güncel veriyle yanıtlansın
    komutlari_isle(tg, durum, ayar)
    if durum.get("grup_id") and not durum.get("grup_test_1"):
        ok = tg.gonder(durum["grup_id"], "🔧 Test: bot bu gruba yazabiliyor. Komutlar için /yardim")
        print(f"::notice::Grup test mesajı: {'gönderildi' if ok else 'GÖNDERİLEMEDİ'}")
        durum["grup_test_1"] = True
    basarili = barbarlari_tara(tg, durum, ayar)
    durum["son_calisma"] = TUR["baslangic"]
    durum_yaz(durum)
    print(f"::notice::Gönderilen mesaj: {TUR.get('gonderilen', 0)}")
    return basarili and not TUR.get("hata")


def main():
    komut = sys.argv[1] if len(sys.argv) > 1 else "izle"
    try:
        if komut == "bir-kez":
            sys.exit(0 if tek_tur() else 1)
        elif komut == "izle":
            print("Barbar izleme başladı (5 dk aralıkla). Durdurmak için Ctrl+C.")
            while True:
                tek_tur()
                time.sleep(5 * 60)
        else:
            print(__doc__)
    except KeyboardInterrupt:
        print("\nDurduruldu.")


if __name__ == "__main__":
    main()
