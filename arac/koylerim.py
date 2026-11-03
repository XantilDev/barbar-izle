"""Tek seferlik: bir oyuncunun köylerini listeler (herkese açık veriden)."""
import gzip, urllib.request, urllib.parse
D = "https://trp13.klanlar.org"
def al(ad):
    r = urllib.request.urlopen(urllib.request.Request(f"{D}/map/{ad}.txt.gz", headers={"User-Agent": "barbar-izle"}), timeout=60)
    ham = r.read()
    try: return gzip.decompress(ham).decode()
    except OSError: return ham.decode()
hedef = "xantil"
oyuncu = [p.split(",") for p in al("player").splitlines()]
bul = [p for p in oyuncu if urllib.parse.unquote_plus(p[1]).lower() == hedef]
if not bul:
    bul = [p for p in oyuncu if hedef in urllib.parse.unquote_plus(p[1]).lower()]
for p in bul:
    print(f"::notice::Oyuncu {urllib.parse.unquote_plus(p[1])} id={p[0]} köy={p[3]} puan={p[4]}")
pid = bul[0][0]
koyler = []
for s in al("village").splitlines():
    v = s.split(",")
    if len(v) >= 6 and v[4] == pid:
        koyler.append((urllib.parse.unquote_plus(v[1]), int(v[2]), int(v[3]), int(v[5]), v[0]))
koyler.sort()
satir = " ; ".join(f"{a}|{x}|{y}|{pt}|{i}" for a, x, y, pt, i in koyler)
for i in range(0, len(satir), 900):
    print(f"::notice::K{i//900}: {satir[i:i+900]}")
