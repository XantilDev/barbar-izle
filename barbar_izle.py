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
        return f"{a['x']}|{a['y']} çevresi, {a['r']} alan yarıçap"
    g, y = a["x2"] - a["x1"] + 1, a["y2"] - a["y1"] + 1
    return (f"X {a['x1']}–{a['x2']}, Y {a['y1']}–{a['y2']} "
            f"(köşeler {a['x1']}|{a['y1']} ve {a['x2']}|{a['y2']}, {g}×{y} = {g * y} alan)")


def dikdortgen(x1, x2, y1, y2):
    return {"tip": "dikdortgen", "x1": min(x1, x2), "x2": max(x1, x2),
            "y1": min(y1, y2), "y2": max(y1, y2)}


def alan_kutusu(a):
    """Alanı çevreleyen kutu: (x1, y1, x2, y2)."""
    if a["tip"] == "daire":
        return a["x"] - a["r"], a["y"] - a["r"], a["x"] + a["r"], a["y"] + a["r"]
    return a["x1"], a["y1"], a["x2"], a["y2"]


RENKLER = {
    "mavi": (52, 120, 246), "açıkmavi": (120, 205, 255), "lacivert": (35, 55, 150),
    "sarı": (250, 215, 40), "kırmızı": (210, 40, 40), "pembe": (240, 120, 190),
    "mor": (150, 90, 220), "turuncu": (255, 145, 30), "yeşil": (150, 235, 90),
    "beyaz": (245, 245, 245), "siyah": (15, 15, 15),
}
BARBAR_RENK, OYUNCU_RENK, ALAN_RENK = (150, 150, 150), (181, 112, 54), (235, 64, 52)


def renk_adi_coz(yazi):
    """'Açık Mavi', 'acikmavi', 'kirmizi' gibi yazımları RENKLER anahtarına çevirir."""
    y = tr_kucuk(yazi).replace(" ", "")
    ascii_ = {"acikmavi": "açıkmavi", "sari": "sarı", "kirmizi": "kırmızı", "yesil": "yeşil"}
    y = ascii_.get(y, y)
    return y if y in RENKLER else None


def klan_anahtari(yazi):
    """'UDW 54', 'UDW-54', 'udw54' aynı klanı göstersin."""
    return re.sub(r"[\s\-_.]", "", tr_kucuk(yazi or ""))


def boyama_hazirla(ayar, durum, uid=None):
    """Köy -> (renk, açıklama) eşlemesi yapan fonksiyon döndürür.
    Öncelik: haritayı isteyen kişinin /nick ile bağladığı köyler (sarı) > klan rengi > varsayılan."""
    oyuncular = durum.get("oyuncular", {})
    kayitli = {**{klan_anahtari(k): v for k, v in ayar.get("klan_renkleri", {}).items()},
               **durum.get("klan_renkleri", {})}
    klan_renk = {}
    for kid, tag in durum.get("klanlar", {}).items():
        ad = durum.get("klan_adlari", {}).get(kid, "")
        renk = kayitli.get(klan_anahtari(tag)) or (kayitli.get(klan_anahtari(ad)) if ad else None)
        if renk in RENKLER:
            klan_renk[kid] = (RENKLER[renk], tag)
    benim = durum.get("kisiler", {}).get(str(uid), {}).get("oyuncu_id") if uid else None
    benim = str(benim) if benim else None

    def boya(k):
        if k["oyuncu"] == 0:
            return BARBAR_RENK, "Barbar"
        pid = str(k["oyuncu"])
        if pid == benim:
            return RENKLER["sarı"], "Senin köylerin"
        o = oyuncular.get(pid)
        if o and str(o.get("kl", 0)) in klan_renk:
            return klan_renk[str(o["kl"])]
        return OYUNCU_RENK, "Diğer oyuncular"
    return boya


def harita_ciz(koyler, alanlar, boya=None):
    """Alanları ve çevresindeki köyleri çizer; PNG bayt döner (Pillow yoksa None)."""
    if boya is None:
        boya = lambda k: (BARBAR_RENK, "Barbar") if k["oyuncu"] == 0 else (OYUNCU_RENK, "Oyuncu")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    kutular = [alan_kutusu(a) for a in alanlar]
    x1, y1 = min(k[0] for k in kutular), min(k[1] for k in kutular)
    x2, y2 = max(k[2] for k in kutular), max(k[3] for k in kutular)
    pay = max(4, int(max(x2 - x1, y2 - y1) * 0.3))
    x1, y1, x2, y2 = x1 - pay, y1 - pay, x2 + pay, y2 + pay
    # En az 20×20 alan göster
    for eks in ("x", "y"):
        a, b = (x1, x2) if eks == "x" else (y1, y2)
        if b - a < 20:
            ek = (20 - (b - a)) // 2 + 1
            a, b = a - ek, b + ek
        if eks == "x":
            x1, x2 = a, b
        else:
            y1, y2 = a, b
    gen, yuk = x2 - x1 + 1, y2 - y1 + 1
    hucre = max(6, min(28, 700 // max(gen, yuk)))
    sol, ust = 44, 52
    W = sol + gen * hucre + 12

    # Görünen köylerin renkleri ve gösterge öğeleri
    gorunen, gosterge = [], {}
    for k in koyler:
        if x1 <= k["x"] <= x2 and y1 <= k["y"] <= y2:
            renk, yazi = boya(k)
            gorunen.append((k, renk))
            gosterge.setdefault(yazi, renk)
    sira = ["Senin köylerin"]
    ogeler = sorted(gosterge.items(), key=lambda t: (t[0] not in sira, t[0] in ("Diğer oyuncular", "Oyuncu"),
                                                      t[0] == "Barbar", t[0]))
    ogeler.append(("Alan", ALAN_RENK))
    # Göstergeyi satırlara yerleştir
    satirlar, x_simdi, satir = [], sol, []
    for yazi, renk in ogeler:
        gen_oge = 22 + 8 * len(yazi) + 18
        if satir and x_simdi + gen_oge > W - 8:
            satirlar.append(satir)
            satir, x_simdi = [], sol
        satir.append((x_simdi, yazi, renk))
        x_simdi += gen_oge
    satirlar.append(satir)
    H = ust + yuk * hucre + 14 + 22 * len(satirlar)

    img = Image.new("RGB", (W, H), (34, 40, 49))
    d = ImageDraw.Draw(img)
    font = kucuk = None
    for yol in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            font, kucuk = ImageFont.truetype(yol, 13), ImageFont.truetype(yol, 11)
            break
        except OSError:
            continue
    turkce = font is not None
    if not turkce:
        try:
            font, kucuk = ImageFont.load_default(size=13), ImageFont.load_default(size=11)
        except TypeError:
            font = kucuk = ImageFont.load_default()
    sade = lambda t: t if turkce else t.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))

    def px(x, y):
        return sol + (x - x1) * hucre, ust + (y - y1) * hucre

    d.rectangle([sol, ust, sol + gen * hucre, ust + yuk * hucre], fill=(76, 110, 58))
    adim = 5 if max(gen, yuk) <= 60 else 10
    for x in range(x1, x2 + 2):
        if x % adim == 0:
            X, _ = px(x, y1)
            d.line([X, ust, X, ust + yuk * hucre], fill=(92, 128, 72), width=1)
            d.text((X + 2, ust - 16), str(x), fill=(200, 200, 200), font=kucuk)
    for y in range(y1, y2 + 2):
        if y % adim == 0:
            _, Y = px(x1, y)
            d.line([sol, Y, sol + gen * hucre, Y], fill=(92, 128, 72), width=1)
            d.text((4, Y + 1), str(y), fill=(200, 200, 200), font=kucuk)

    m = max(1, hucre // 6)
    for k, renk in gorunen:
        X, Y = px(k["x"], k["y"])
        d.rectangle([X + m, Y + m, X + hucre - m - 1, Y + hucre - m - 1], fill=renk,
                    outline=(25, 25, 25) if hucre >= 10 else None)

    for a in alanlar:
        if a["tip"] == "daire":
            cx, cy = px(a["x"], a["y"])
            cx, cy, r = cx + hucre / 2, cy + hucre / 2, (a["r"] + 0.5) * hucre
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ALAN_RENK, width=3)
        else:
            X1, Y1 = px(a["x1"], a["y1"])
            X2, Y2 = px(a["x2"] + 1, a["y2"] + 1)
            d.rectangle([X1, Y1, X2, Y2], outline=ALAN_RENK, width=3)

    # Gösterge
    for i, satir in enumerate(satirlar):
        gy = ust + yuk * hucre + 12 + 22 * i
        for gx, yazi, renk in satir:
            if yazi == "Alan":
                d.rectangle([gx, gy, gx + 12, gy + 12], outline=renk, width=2)
            else:
                d.rectangle([gx, gy, gx + 12, gy + 12], fill=renk)
            d.text((gx + 18, gy - 1), sade(yazi), fill=(220, 220, 220), font=font)
    d.text((sol, 8), f"X {x1}-{x2}   Y {y1}-{y2}", fill=(230, 230, 230), font=font)

    import io
    tampon = io.BytesIO()
    img.save(tampon, format="PNG", optimize=True)
    return tampon.getvalue()


def harita_linki(ayar, alanlar):
    kutular = [alan_kutusu(a) for a in alanlar]
    cx = (min(k[0] for k in kutular) + max(k[2] for k in kutular)) // 2
    cy = (min(k[1] for k in kutular) + max(k[3] for k in kutular)) // 2
    return f"{ayar['dunya'].rstrip('/')}/guest.php?screen=map#{cx};{cy}"


def koyleri_al(ayar):
    koyler = TUR.get("koyler")
    if koyler is None:
        try:
            koyler, _ = koyleri_indir(ayar["dunya"])
            TUR["koyler"] = koyler
        except Exception:
            koyler = []
    return koyler


def haritali_gonder(tg, ayar, durum, chat_id, metin, alanlar, mid, uid=None):
    """Metni alan haritasıyla birlikte gönderir; harita çizilemezse yalnızca metin."""
    koyler = koyleri_al(ayar)
    sayac = {"b": 0, "o": 0}
    for k in koyler:
        if any(alanda_mi(k, a) for a in alanlar):
            sayac["b" if k["oyuncu"] == 0 else "o"] += 1
    metin += (f"\n🏚️ İçinde {sayac['b']} barbar · 🏠 {sayac['o']} oyuncu köyü"
              f'\n🗺️ <a href="{harita_linki(ayar, alanlar)}">Oyun haritasında aç</a>')
    png = harita_ciz(koyler, alanlar, boyama_hazirla(ayar, durum, uid)) if koyler else None
    if png:
        tg.foto_gonder(chat_id, png, metin, mid)
    else:
        tg.gonder(chat_id, metin, mid)
    satirlar = alan_oyuncu_listesi(ayar, durum, alanlar, koyler) if durum.get("oyuncular") else []
    if satirlar:
        parcali_gonder(tg, chat_id,
                       f"👥 <b>Alandaki {len(satirlar)} oyuncu</b> — son gelişmeden bu yana geçen süre\n"
                       "(⚫ 7+ gün · 🔴 5-7 · 🟡 2-5 · 🟢 aktif · <b>+</b> = takip başladığından beri hiç değişmedi)\n\n",
                       satirlar, mid)


def alan_buyuklugu(a):
    return (a["x2"] - a["x1"] + 1) * (a["y2"] - a["y1"] + 1)


def alan_coz(arg):
    """/alan argümanını çözer. Dört sayı iki şekilde yazılabiliyor:
      421-429 513-521  -> X aralığı, Y aralığı
      423-512 429-519  -> iki köşe (423|512 ve 429|519)
    "|" varsa köşe kabul edilir; yoksa daha küçük alanı veren okuma seçilir
    (insanlar hemen her zaman küçük bir bölgeyi kasteder)."""
    sayilar = [int(s) for s in re.findall(r"\d+", arg)]
    if any(n > 999 for n in sayilar):
        return None
    if len(sayilar) == 4:
        a, b, c, d = sayilar
        kose = dikdortgen(a, c, b, d)
        if "|" in arg:
            return kose
        aralik = dikdortgen(a, b, c, d)
        return kose if alan_buyuklugu(kose) < alan_buyuklugu(aralik) else aralik
    if len(sayilar) == 3 and 1 <= sayilar[2] <= 100:
        return {"tip": "daire", "x": sayilar[0], "y": sayilar[1], "r": sayilar[2]}
    return None


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
            if "parse entities" in aciklama:
                # Biçimlendirme reddedildi: etiketleri atıp düz metin olarak yeniden dene
                duz = html.unescape(re.sub(r"<[^>]+>", "", metin))
                veri.pop("parse_mode", None)
                veri["text"] = duz
                try:
                    self._istek("sendMessage", veri)
                    TUR["gonderilen"] = TUR.get("gonderilen", 0) + 1
                    print(f"::warning::Biçimlendirme hatası, düz metin gönderildi: {aciklama}")
                    return True
                except Exception:
                    pass
            hedef_turu = "grup" if str(chat_id).startswith("-") else "özel sohbet"
            print(f"::warning::Telegram gönderilemedi ({hedef_turu}): {aciklama}")
            TUR["hata"] = True
            return False

    def foto_gonder(self, chat_id, png, aciklama, yanit_id=None):
        """PNG'yi açıklamasıyla gönderir; olmazsa yalnızca metni gönderir."""
        sinir = "----barbarizle" + str(int(time.time() * 1000))
        alanlar = {"chat_id": str(chat_id), "caption": aciklama[:1024], "parse_mode": "HTML"}
        if yanit_id:
            alanlar["reply_to_message_id"] = str(yanit_id)
            alanlar["allow_sending_without_reply"] = "true"
        govde = b""
        for k, v in alanlar.items():
            govde += (f"--{sinir}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        govde += (f"--{sinir}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"harita.png\"\r\n"
                  "Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{sinir}--\r\n".encode()
        istek = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=govde,
                                       headers={"Content-Type": f"multipart/form-data; boundary={sinir}"})
        try:
            urllib.request.urlopen(istek, timeout=60).read()
            TUR["gonderilen"] = TUR.get("gonderilen", 0) + 1
            return True
        except Exception as e:
            print(f"::warning::Harita gönderilemedi: {e}")
            return self.gonder(chat_id, aciklama, yanit_id)

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
    "<b>/alan 421|513 429|521</b> — alan ekle (sol üst ve sağ alt köşe)\n"
    "<b>/alan 421-429 513-521</b> — aynı alan, X ve Y aralığı olarak\n"
    "<b>/alan 500|500 15</b> — köy çevresi ekle (merkez + yarıçap)\n"
    "<b>/alanlarim</b> — alanlarının haritası ve alandaki oyuncuların aktifliği\n"
    "<b>/alansil 2</b> — 2. alanını sil (<b>/alansil hepsi</b> ile tümünü)\n"
    "Birden fazla alan için /alan komutunu tekrar yazman yeterli (en fazla 10).\n\n"
    "<b>/barbarlar</b> — alanlarındaki mevcut barbar köyleri\n"
    "<b>/durum</b> — bot durumu ve en uzun süredir hareketsiz 10 oyuncu\n"
    "<b>/nick OyunAdın</b> — senin istediğin haritalarda köylerin sarı görünsün\n"
    "<b>/klanrenk UDW mavi</b> — klanın köylerini o renge boya\n"
    "<b>/harita 421|513 429|521</b> — herhangi bir bölgenin haritası\n"
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
            alan = alan_coz(arg)
            if alan is None:
                tg.gonder(chat_id, "Anlayamadım. Örnekler:\n"
                                   "<b>/alan 421-429 513-521</b>  (X aralığı, Y aralığı)\n"
                                   "<b>/alan 421|513 429|521</b>  (sol üst ve sağ alt köşe)\n"
                                   "<b>/alan 425|517 10</b>  (köy çevresi, 10 alan)", mid)
                continue
            kisi = kisiler.setdefault(uid, {"alanlar": []})
            kisi["ad"] = kimden.get("first_name") or kimden.get("username") or "üye"
            if len(kisi["alanlar"]) >= ayar["kisi_basi_max_alan"]:
                tg.gonder(chat_id, f"En fazla {ayar['kisi_basi_max_alan']} alan ekleyebilirsin. "
                                   "Önce /alansil ile temizle.", mid)
                continue
            kisi["alanlar"].append(alan)
            uyari = ""
            if alan["tip"] == "dikdortgen" and alan_buyuklugu(alan) > 50 * 50:
                uyari = ("\n⚠️ Bu çok büyük bir alan. Yanlışsa <b>/alansil</b> yazıp tekrar ekle. "
                         "Örnek: <b>/alan 421|513 429|521</b> (sol üst ve sağ alt köşe)")
            haritali_gonder(tg, ayar, durum, chat_id,
                            f"✅ {etiket(uid, kisi)} alanın eklendi: <b>{alan_metni(alan)}</b>{uyari}", [alan], mid, uid)
        elif komut in ("/alanlarim", "/alanim", "/alanlarım", "/alanım"):
            kisi = kisiler.get(uid)
            if not kisi or not kisi["alanlar"]:
                tg.gonder(chat_id, "Kayıtlı alanın yok. Eklemek için: <b>/alan 421-429 513-521</b>", mid)
            else:
                satirlar = "\n".join(f"<b>{i}.</b> {alan_metni(a)}" for i, a in enumerate(kisi["alanlar"], 1))
                satirlar += "\n\nTek alanı silmek için: <b>/alansil 2</b> · hepsini silmek için: <b>/alansil hepsi</b>"
                haritali_gonder(tg, ayar, durum, chat_id, f"📍 {etiket(uid, kisi)} alanların:\n{satirlar}",
                                kisi["alanlar"], mid, uid)
        elif komut in ("/alansil", "/alanısil"):
            kisi = kisiler.get(uid)
            alanlar_ = kisi.get("alanlar", []) if kisi else []
            sayi = re.findall(r"\d+", arg)
            if not alanlar_:
                tg.gonder(chat_id, "Kayıtlı alanın yok.", mid)
            elif sayi:
                no = int(sayi[0])
                if 1 <= no <= len(alanlar_):
                    silinen = alanlar_.pop(no - 1)
                    kalan = "\n".join(f"<b>{i}.</b> {alan_metni(a)}" for i, a in enumerate(alanlar_, 1))
                    tg.gonder(chat_id, f"🗑️ {no}. alan silindi: {alan_metni(silinen)}"
                                       + (f"\n\nKalan alanların:\n{kalan}" if kalan else "\nBaşka alanın kalmadı."), mid)
                else:
                    tg.gonder(chat_id, f"{len(alanlar_)} alanın var; 1 ile {len(alanlar_)} arasında bir numara yaz. "
                                       "Numaraları görmek için /alanim", mid)
            elif tr_kucuk(arg.strip()) in ("hepsi", "tümü", "tumu", "hepsini"):
                kisi["alanlar"] = []
                tg.gonder(chat_id, "🗑️ Tüm alanların silindi.", mid)
            elif len(alanlar_) == 1:
                kisi["alanlar"] = []
                tg.gonder(chat_id, "🗑️ Alanın silindi.", mid)
            else:
                liste = "\n".join(f"<b>{i}.</b> {alan_metni(a)}" for i, a in enumerate(alanlar_, 1))
                tg.gonder(chat_id, f"{len(alanlar_)} alanın var, hangisini silelim?\n{liste}\n\n"
                                   "Örnek: <b>/alansil 2</b> · hepsi için <b>/alansil hepsi</b>", mid)
        elif komut == "/durum":
            komut_durum(tg, durum, ayar, chat_id, mid)
        elif komut == "/barbarlar":
            komut_barbarlar(tg, durum, ayar, chat_id, mid, uid)
        elif komut == "/nick":
            komut_nick(tg, durum, ayar, chat_id, mid, uid, kimden, arg)
        elif komut == "/klanrenk":
            komut_klanrenk(tg, durum, ayar, chat_id, mid, arg)
        elif komut == "/harita":
            alan = alan_coz(arg)
            if alan is None:
                tg.gonder(chat_id, "Örnek: <b>/harita 421|513 429|521</b> ya da <b>/harita 425|517 10</b>", mid)
            else:
                haritali_gonder(tg, ayar, durum, chat_id, f"🗺️ <b>{alan_metni(alan)}</b>", [alan], mid, uid)
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
    durum["klan_adlari"] = {p[0]: urllib.parse.unquote_plus(p[1]) for p in satirlar_csv(klan_txt)}

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


def kisa_sure(durum, o, simdi):
    """'3 gün', '5 sa', '<1 sa'; takip başlangıcından beri değişmediyse sonuna '+' (en az)."""
    sn = simdi - o["d"]
    g = sn // GUN
    metin = f"{g} gün" if g else (f"{sn // 3600} sa" if sn >= 3600 else "1 sa'ten az")
    return metin + ("+" if o["d"] <= durum.get("takip_baslangic", 0) else "")


def aktiflik_ikonu(durum, o, simdi):
    gun = (simdi - o["d"]) / GUN
    if o["d"] <= durum.get("takip_baslangic", 0) and gun < 2:
        return "⚪"
    return "🟢" if gun < 2 else "🟡" if gun < 5 else "🔴" if gun < 7 else "⚫"


def alan_oyuncu_listesi(ayar, durum, alanlar, koyler):
    """Alanlarda köyü olan oyuncuları, en uzun süredir hareketsiz olandan başlayarak listeler."""
    oyuncular = durum.get("oyuncular", {})
    sayilar = {}
    for k in koyler:
        if k["oyuncu"] and any(alanda_mi(k, a) for a in alanlar):
            sayilar[str(k["oyuncu"])] = sayilar.get(str(k["oyuncu"]), 0) + 1
    simdi = int(time.time())
    kayitli = [(pid, oyuncular[pid]) for pid in sayilar if pid in oyuncular]
    kayitli.sort(key=lambda t: t[1]["d"])
    satirlar = [f"{aktiflik_ikonu(durum, o, simdi)} {oyuncu_linki(ayar, pid, o)}{klan_metni(ayar, durum, o)} · "
                f"<b>{kisa_sure(durum, o, simdi)}</b> · {sayilar[pid]} köy alanda"
                for pid, o in kayitli]
    return satirlar


def oyuncu_ara(durum, sorgu):
    oyuncular = durum.get("oyuncular", {})
    q = tr_kucuk(sorgu.strip())
    tam = [pid for pid, o in oyuncular.items() if tr_kucuk(o["n"]) == q]
    return tam or [pid for pid, o in oyuncular.items() if q in tr_kucuk(o["n"])]


def komut_nick(tg, durum, ayar, chat_id, mid, uid, kimden, arg):
    kisi = durum.setdefault("kisiler", {}).setdefault(uid, {"alanlar": []})
    kisi["ad"] = kimden.get("first_name") or kimden.get("username") or kisi.get("ad") or "üye"
    sorgu = arg.strip()
    if not sorgu:
        mevcut = durum.get("oyuncular", {}).get(str(kisi.get("oyuncu_id")))
        if mevcut:
            tg.gonder(chat_id, f"Oyundaki adın: {oyuncu_linki(ayar, kisi['oyuncu_id'], mevcut)}. "
                               "Değiştirmek için <b>/nick YeniAd</b>, kaldırmak için <b>/nick sil</b>.", mid)
        else:
            tg.gonder(chat_id, "Örnek: <b>/nick OyunAdın</b>", mid)
        return
    if tr_kucuk(sorgu) == "sil":
        kisi.pop("oyuncu_id", None)
        tg.gonder(chat_id, "🗑️ Oyun adın kaldırıldı.", mid)
        return
    if not durum.get("oyuncular"):
        tg.gonder(chat_id, "Oyuncu listesi henüz yüklenmedi, biraz sonra tekrar dene.", mid)
        return
    bulunan = oyuncu_ara(durum, sorgu)
    if not bulunan:
        tg.gonder(chat_id, f"“{html.escape(sorgu)}” adında oyuncu bulamadım. Oyundaki adını tam yaz.", mid)
        return
    if len(bulunan) > 1 and not any(tr_kucuk(durum["oyuncular"][p]["n"]) == tr_kucuk(sorgu) for p in bulunan):
        liste = "\n".join(f"• {html.escape(durum['oyuncular'][p]['n'])}" for p in bulunan[:10])
        tg.gonder(chat_id, f"Birden fazla oyuncu var, tam adını yaz:\n{liste}", mid)
        return
    pid = bulunan[0]
    o = durum["oyuncular"][pid]
    kisi["oyuncu_id"] = int(pid)
    tg.gonder(chat_id, f"✅ {etiket(uid, kisi)} oyundaki adın: {oyuncu_linki(ayar, pid, o)}"
                       f"{klan_metni(ayar, durum, o)} · {o['k']} köy\nSenin istediğin haritalarda köylerin 🟨 sarı görünecek.", mid)


def komut_klanrenk(tg, durum, ayar, chat_id, mid, arg):
    renkler_listesi = ", ".join(RENKLER)
    kelimeler = arg.split()
    kayitli = {**{klan_anahtari(k): v for k, v in ayar.get("klan_renkleri", {}).items()},
               **durum.get("klan_renkleri", {})}
    if not kelimeler:
        etiketler = {klan_anahtari(t): t for t in durum.get("klanlar", {}).values()}
        satirlar = [f"• {html.escape(etiketler.get(k, k))} → {v}" for k, v in kayitli.items() if v in RENKLER]
        tg.gonder(chat_id, "🎨 <b>Klan renkleri</b>\n" + ("\n".join(satirlar) or "(henüz yok)") +
                           f"\n\nRenkler: {renkler_listesi}\n"
                           "Örnek: <b>/klanrenk UDW mavi</b> · kaldırmak için <b>/klanrenk UDW sil</b>", mid)
        return
    # Renk son bir ya da iki kelime olabilir ("açık mavi")
    renk, klan = None, None
    for n in (2, 1):
        if len(kelimeler) > n:
            aday = " ".join(kelimeler[-n:])
            if tr_kucuk(aday) == "sil" or renk_adi_coz(aday):
                renk = "sil" if tr_kucuk(aday) == "sil" else renk_adi_coz(aday)
                klan = " ".join(kelimeler[:-n])
                break
    if not renk:
        tg.gonder(chat_id, f"Renk anlaşılamadı. Kullanılabilir renkler: {renkler_listesi}\n"
                           "Örnek: <b>/klanrenk UDW mavi</b>", mid)
        return
    anahtar = klan_anahtari(klan)
    eslesen = [t for kid, t in durum.get("klanlar", {}).items()
               if klan_anahtari(t) == anahtar or klan_anahtari(durum.get("klan_adlari", {}).get(kid, "")) == anahtar]
    renkler = durum.setdefault("klan_renkleri", {})
    if renk == "sil":
        renkler[anahtar] = None
        tg.gonder(chat_id, f"🗑️ {html.escape(klan)} klanının rengi kaldırıldı.", mid)
        return
    renkler[anahtar] = renk
    uyari = "" if eslesen else ("\n⚠️ Bu adda bir klan şu an listede yok; adı ya da etiketi oyundaki gibi yazdığından "
                                "emin ol. Klan kurulursa renk yine de uygulanır.")
    tg.gonder(chat_id, f"🎨 <b>{html.escape(eslesen[0] if eslesen else klan)}</b> köyleri haritalarda "
                       f"<b>{renk}</b> görünecek.{uyari}", mid)


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
    oyuncular = durum.get("oyuncular", {})
    pasif = sorted(((pid, o) for pid, o in oyuncular.items() if o["k"] > 0),
                   key=lambda t: (t[1]["d"], -t[1]["p"]))[:10]
    if pasif:
        satirlar.append("\n😴 <b>En uzun süredir hareketsiz 10 oyuncu</b>")
        satirlar += [f"{i}. {oyuncu_linki(ayar, pid, o)}{klan_metni(ayar, durum, o)} · "
                     f"<b>{kisa_sure(durum, o, simdi)}</b> · {o['k']} köy"
                     for i, (pid, o) in enumerate(pasif, 1)]
        satirlar.append("<i>+ = takip başladığından beri hiç değişmedi (en az bu kadar)</i>")
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


def alanlari_duzelt(tg, durum):
    """Eski sürüm '423-512 429-519' gibi köşe yazımlarını X/Y aralığı sanıp dev alanlar
    kaydediyordu. Bu kayıtları bir kez köşe yorumuyla düzeltir ve gruba bildirir."""
    if durum.get("alan_duzeltme_1"):
        return
    durum["alan_duzeltme_1"] = True
    satirlar = []
    for uid, kisi in durum.get("kisiler", {}).items():
        for i, a in enumerate(kisi.get("alanlar", [])):
            if a["tip"] != "dikdortgen" or alan_buyuklugu(a) <= 50 * 50:
                continue
            # Kayıtta x1..x2 = ilk iki sayı, y1..y2 = son iki sayı; köşe yorumu:
            kose = dikdortgen(a["x1"], a["y1"], a["x2"], a["y2"])
            if alan_buyuklugu(kose) < alan_buyuklugu(a):
                kisi["alanlar"][i] = kose
                satirlar.append(f"• {etiket(uid, kisi)}: <b>{alan_metni(kose)}</b>")
    if satirlar:
        hedef = durum.get("grup_id") or tg.sahip_id
        tg.gonder(hedef, "🔧 <b>Alan düzeltmesi</b>\nKöşe koordinatı olarak yazılan bazı alanlar yanlışlıkla "
                         "çok büyük kaydedilmişti, düzelttim:\n" + "\n".join(satirlar) +
                         "\n\nYanlışsa /alansil yazıp tekrar ekleyebilirsiniz.")
        print(f"::notice::{len(satirlar)} hatalı alan düzeltildi.")


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
    alanlari_duzelt(tg, durum)
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
