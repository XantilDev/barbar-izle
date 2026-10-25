"""Tek seferlik inceleme 2: misafir haritası anlık mı? Geniş bölgede puan/sahip karşılaştırması."""
import gzip, json, time, urllib.request
from email.utils import parsedate_to_datetime
D = "https://trp13.klanlar.org"
def al(url):
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (inceleme)"}), timeout=30)
    return r.headers, r.read()
def n(m): print(f"::notice::{m[:1500]}")

h, ham = al(f"{D}/map/village.txt.gz")
lm = h.get("Last-Modified")
try: txt = gzip.decompress(ham).decode()
except OSError: txt = ham.decode()
liste = {}
for s in txt.splitlines():
    p = s.split(",")
    if len(p) >= 6: liste[int(p[0])] = (int(p[2]), int(p[3]), int(p[4]), int(p[5]))
yas = int(time.time() - parsedate_to_datetime(lm).timestamp()) // 60

# Köylerin yoğun olduğu bölgeyi bul, 3x3 bölüm (60x60) iste — tek istekte birden çok bölüm
xs = sorted(v[0] for v in liste.values()); ys = sorted(v[1] for v in liste.values())
cx, cy = xs[len(xs)//2] // 20 * 20, ys[len(ys)//2] // 20 * 20
parcalar = "&".join(f"{cx+dx}_{cy+dy}=1" for dx in (-20, 0, 20) for dy in (-20, 0, 20))
h2, ham2 = al(f"{D}/map.php?v=2&e={int(time.time()*1000)}&{parcalar}")
veri = json.loads(ham2.decode())
n(f"Liste {yas} dk önce yayınlandı. Harita isteği: {len(veri)} bölüm, merkez {cx}|{cy}. Yanıt başlıkları: "
  f"Cache-Control={h2.get('Cache-Control')} Age={h2.get('Age')} Expires={h2.get('Expires')} Date={h2.get('Date')}")

harita = {}
for b in veri:
    d = b.get("data", {})
    bx, by = int(d.get("x", b["x"])), int(d.get("y", b["y"]))
    vs = d.get("villages") or {}
    it = vs.items() if isinstance(vs, dict) else enumerate(vs)
    for dx, sut in it:
        it2 = sut.items() if isinstance(sut, dict) else enumerate(sut) if isinstance(sut, list) else []
        for dy, v in it2:
            if isinstance(v, list) and v:
                harita[int(v[0])] = (bx + int(dx), by + int(dy), v)

ornek_oyuncu = next((v for v in harita.values() if v[2][4] not in ("0", 0)), None)
n(f"Haritada {len(harita)} köy. Oyuncu köyü örneği: {ornek_oyuncu}")
puan_fark, sahip_fark, yeni, kayip = [], [], [], []
for k, (x, y, v) in harita.items():
    if k not in liste: yeni.append((k, x, y, v[:6])); continue
    lx, ly, ls, lp = liste[k]
    try:
        hp, hs = int(str(v[3]).replace(".", "")), int(v[4])
    except Exception:
        continue
    if hp != lp: puan_fark.append((k, lp, hp))
    if hs != ls: sahip_fark.append((k, x, y, ls, hs))
bolge = {k for k, v in liste.items() if cx-20 <= v[0] < cx+40 and cy-20 <= v[1] < cy+40}
kayip = [k for k in bolge if k not in harita]
artan = sum(1 for _, a, b in puan_fark if b > a)
n(f"SONUÇ: karşılaştırılan {len(harita)} köy | puanı farklı {len(puan_fark)} (artan {artan}) | sahibi farklı "
  f"{len(sahip_fark)} | sadece haritada {len(yeni)} | sadece listede {len(kayip)}")
n(f"Puan farkı örnekleri: {puan_fark[:6]} | sahip farkı: {sahip_fark[:4]} | yeni: {yeni[:3]}")
