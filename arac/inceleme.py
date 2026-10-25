"""Tek seferlik inceleme: misafir haritası veriyi nereden çekiyor, saatlik listeden daha güncel mi?"""
import gzip, http.cookiejar, json, re, time, urllib.request
from email.utils import parsedate_to_datetime

D = "https://trp13.klanlar.org"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
op.addheaders = [("User-Agent", "Mozilla/5.0 (inceleme)")]

def al(url):
    with op.open(url, timeout=30) as r:
        ham = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            ham = gzip.decompress(ham)
        return r.status, dict(r.headers), ham

def n(m): print(f"::notice::{m[:900]}")

# 1) Misafir harita sayfası
st, h, ham = al(f"{D}/guest.php?screen=map")
s = ham.decode("utf-8", "replace")
n(f"guest.php durum {st}, {len(s)} bayt, çerezler: {[c.name for c in jar]}")
for kalip in [r"map\.php[^\"' ]{0,80}", r"TWMap\.[a-zA-Z_]+\s*=\s*[^;]{0,120}", r"sector[^\"' ]{0,60}", r"\"?mapsize\"?[^,;]{0,40}"]:
    bul = sorted(set(re.findall(kalip, s)))[:6]
    if bul: n(f"[{kalip}] " + " | ".join(bul))

# 2) Harita verisi: 420_500 bölümü (20x20)
for url in [f"{D}/map.php?v=2&e={int(time.time()*1000)}&420_500=1",
            f"{D}/map.php?v=2&420_500=1"]:
    try:
        st, h, ham = al(url)
        t = ham.decode("utf-8", "replace")
        n(f"{url.split('/')[-1]} -> {st} {h.get('Content-Type')} {len(t)} bayt | {t[:400]}")
        try:
            veri = json.loads(t)
            TUR = veri
            break
        except Exception as e:
            n(f"JSON değil: {e}")
    except Exception as e:
        n(f"{url} hata: {e}")
else:
    TUR = None

# 3) Saatlik liste ile karşılaştır
st, h, ham = al(f"{D}/map/village.txt.gz")
lm = h.get("Last-Modified")
try:
    txt = gzip.decompress(ham).decode()
except OSError:
    txt = ham.decode()
liste = {}
for satir in txt.splitlines():
    p = satir.split(",")
    if len(p) >= 6:
        x, y = int(p[2]), int(p[3])
        if 420 <= x < 440 and 500 <= y < 520:
            liste[int(p[0])] = (x, y, int(p[4]), int(p[5]))
yas = int(time.time() - parsedate_to_datetime(lm).timestamp()) // 60 if lm else -1
n(f"village.txt Last-Modified {lm} ({yas} dk önce); 420-439/500-519 bölgesinde {len(liste)} köy, "
  f"{sum(1 for v in liste.values() if v[2]==0)} barbar")

if TUR is not None:
    # Yapıyı keşfet ve köyleri çıkar
    def yapi(o, d=0):
        if d > 3: return "…"
        if isinstance(o, dict): return "{" + ", ".join(f"{k}:{yapi(v, d+1)}" for k, v in list(o.items())[:4]) + "}"
        if isinstance(o, list): return "[" + ", ".join(yapi(v, d+1) for v in o[:2]) + (f" …({len(o)})" if len(o) > 2 else "") + "]"
        return type(o).__name__
    n("Yapı: " + yapi(TUR))
    harita = {}
    def tara(o):
        if isinstance(o, dict):
            if "villages" in o and "x" in o and "y" in o:
                bx, by = int(o["x"]), int(o["y"])
                for dx, sutun in (o["villages"].items() if isinstance(o["villages"], dict) else enumerate(o["villages"])):
                    if isinstance(sutun, dict):
                        it = sutun.items()
                    elif isinstance(sutun, list):
                        it = enumerate(sutun)
                    else:
                        continue
                    for dy, v in it:
                        if isinstance(v, list) and v:
                            try:
                                harita[int(v[0])] = (bx + int(dx), by + int(dy), v)
                            except Exception:
                                pass
            for v in o.values(): tara(v)
        elif isinstance(o, list):
            for v in o: tara(v)
    tara(TUR)
    n(f"Haritadan {len(harita)} köy çıkarıldı. Örnek: {list(harita.items())[:2]}")
    sadece_harita = [k for k in harita if k not in liste]
    sadece_liste = [k for k in liste if k not in harita]
    farkli_sahip = []
    for k, (x, y, v) in harita.items():
        if k in liste:
            # sahip alanını tahmin et: listede sahip id'si haritadaki dizide geçiyor mu
            if str(liste[k][2]) not in [str(a) for a in v]:
                farkli_sahip.append((k, liste[k], v[:8]))
    n(f"Karşılaştırma: sadece haritada {len(sadece_harita)}, sadece listede {len(sadece_liste)}, "
      f"sahip/puan farklı görünen {len(farkli_sahip)}. Örnek farklar: {farkli_sahip[:3]} | yeni: "
      f"{[harita[k] for k in sadece_harita[:2]]}")
