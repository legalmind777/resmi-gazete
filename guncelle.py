"""
Resmî Gazete günlük özetleyici
Her sabah GitHub Actions tarafından çalıştırılır:
  1. O günün Resmî Gazete içindekiler sayfasını (ve varsa mükerrerlerini) indirir.
  2. Maddelerin metinlerini okur.
  3. Gemini'ye hukukçu gözüyle özetletir.
  4. Sonucu data/arsiv.json dosyasına ekler; web sayfası bu dosyayı okur.
"""
import datetime as dt
import json
import os
import re
import sys
import time
from io import BytesIO
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString

BASE = "https://www.resmigazete.gov.tr/eskiler"
ARSIV = "data/arsiv.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}
MODELS = [m for m in [os.getenv("GEMINI_MODEL"), "gemini-flash-latest",
                      "gemini-2.5-flash", "gemini-flash-lite-latest"] if m]
MADDE_METIN_SINIRI = 7000      # bir maddeden okunacak en fazla karakter
TOPLAM_METIN_SINIRI = 150000   # Gemini'ye gönderilecek toplam metin
METNI_OKUNACAK_MADDE = 30      # en fazla bu kadar maddenin metni indirilir


def log(*a):
    print(*a, flush=True)


def indir(url, deneme=3):
    """Sayfayı indirir; sayfa yoksa None döner."""
    for i in range(deneme):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == deneme - 1:
                raise
            log(f"  tekrar deneniyor ({e})")
            time.sleep(5 * (i + 1))


def temiz(s):
    return " ".join(str(s).split())


# ---------------------------------------------------------------- içindekiler
def icindekiler(gun, ek=""):
    ymd = gun.strftime("%Y%m%d")
    url = f"{BASE}/{gun:%Y}/{gun:%m}/{ymd}{ek}.htm"
    r = indir(url)
    if r is None:
        return None
    soup = BeautifulSoup(r.content, "html.parser")
    govde = soup.body or soup
    sayi = re.search(r"Say[ıi]\s*:?\s*(\d{4,6})", temiz(govde.get_text(" ")))
    maddeler, gorulen, bolum = [], set(), ""
    for node in govde.descendants:
        if getattr(node, "name", None) == "a":
            href = node.get("href", "")
            baslik = temiz(node.get_text(" ")).lstrip("–—-−• ").strip()
            if not baslik or ymd not in href or "ilan" in href.lower():
                continue
            link = urljoin(url, href)
            if link in gorulen or link.rstrip("/") == url:
                continue
            gorulen.add(link)
            maddeler.append({"bolum": bolum, "baslik": baslik, "link": link})
        elif isinstance(node, NavigableString) and node.parent and node.parent.name != "a":
            t = temiz(node)
            harfler = [c for c in t if c.isalpha()]
            if 3 < len(t) < 90 and harfler and all(c.isupper() for c in harfler):
                bolum = t
    return {"url": url, "sayi": sayi.group(1) if sayi else "", "maddeler": maddeler}


def madde_metni(link):
    try:
        r = indir(link)
    except Exception as e:
        log(f"  okunamadı: {link} ({e})")
        return ""
    if r is None:
        return ""
    if link.lower().endswith(".pdf") or "pdf" in r.headers.get("Content-Type", ""):
        try:
            from pypdf import PdfReader
            pdf = PdfReader(BytesIO(r.content))
            metin = " ".join((p.extract_text() or "") for p in pdf.pages[:15])
        except Exception:
            return ""
    else:
        soup = BeautifulSoup(r.content, "html.parser")
        for s in soup(["script", "style"]):
            s.decompose()
        metin = soup.get_text(" ")
    return temiz(metin)[:MADDE_METIN_SINIRI]


# ---------------------------------------------------------------- Gemini
TALIMAT = """Sen deneyimli bir Türk hukukçususun. Aşağıda {tarih} tarihli Resmî Gazete'nin
içindekiler listesi ve maddelerin metinleri var. Avukatlar için kısa, doğru ve tarafsız bir
günlük özet hazırla.

Kurallar:
- Yalnızca verilen metinlere dayan. Metinde olmayan bilgi ekleme, tahmin yürütme.
- "genel": günün en önemli gelişmelerini anlatan 2-3 cümle. Önemli bir şey yoksa bunu açıkça söyle.
- Her madde için "ozet": 1-3 cümle; ne düzenlendi veya değişti, kimi etkiliyor.
- "yururluk": metinde yürürlük tarihi yazıyorsa kısaca yaz (ör. "yayımı tarihinde", "1/1/2027"), yoksa boş bırak.
- "onem":
  "yuksek" = kanunlar, Cumhurbaşkanlığı kararnameleri, Anayasa Mahkemesi kararları, yargılama,
  ceza, infaz, avukatlık, vergi, iş ve sosyal güvenlik gibi geniş etkili düzenlemeler;
  "orta" = genel nitelikli yönetmelik, tebliğ, genelge ve kurul kararları;
  "dusuk" = atamalar, üniversite ve kurum içi yönetmelikler, yerel nitelikli kararlar.
- "etiketler": ilgili hukuk alanları, en fazla 3 tane (ör. Ceza, İnfaz, Ceza Usulü, Hukuk Usulü,
  İş, Sosyal Güvenlik, Vergi, İdare, Ticaret, Aile, Avukatlık, Anayasa, Sağlık, Eğitim, Ekonomi).
- "no" içindekiler listesindeki sıra numarasıdır; listedeki HER madde için bir kayıt üret.

Yanıtı yalnızca şu JSON biçiminde ver:
{{"genel": "...", "maddeler": [{{"no": 1, "ozet": "...", "yururluk": "", "onem": "yuksek", "etiketler": ["..."]}}]}}

İÇİNDEKİLER VE METİNLER:
{icerik}
"""


def gemini(istem):
    anahtar = os.getenv("GEMINI_API_KEY")
    if not anahtar:
        raise RuntimeError("GEMINI_API_KEY bulunamadı (GitHub > Settings > Secrets).")
    govde = {
        "contents": [{"role": "user", "parts": [{"text": istem}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    son_hata = ""
    for model in MODELS:
        for i in range(3):
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": anahtar, "Content-Type": "application/json"},
                json=govde, timeout=240)
            if r.status_code == 200:
                parcalar = r.json()["candidates"][0]["content"]["parts"]
                metin = "".join(p.get("text", "") for p in parcalar if not p.get("thought"))
                metin = re.sub(r"^```(?:json)?|```$", "", metin.strip()).strip()
                return json.loads(metin), model
            son_hata = f"{model}: HTTP {r.status_code} {r.text[:300]}"
            log("  Gemini hatası:", son_hata)
            if r.status_code in (429, 500, 503):
                time.sleep(30 * (i + 1))
                continue
            break  # model yok ya da istek reddedildi: sıradaki modeli dene
    raise RuntimeError(son_hata)


# ---------------------------------------------------------------- ana akış
def main():
    tarih_env = (os.getenv("TARIH") or "").strip()
    gun = dt.date.fromisoformat(tarih_env) if tarih_env else dt.datetime.now(ZoneInfo("Europe/Istanbul")).date()
    log(f"Tarih: {gun}")

    ana = icindekiler(gun)
    if ana is None:
        log("Bu tarihli Resmî Gazete bulunamadı (henüz yayımlanmamış olabilir). Çıkılıyor.")
        return
    maddeler = list(ana["maddeler"])
    mukerrerler = []
    for n in range(1, 5):
        m = icindekiler(gun, f"M{n}")
        if m is None:
            break
        mukerrerler.append(m["url"])
        for x in m["maddeler"]:
            x["bolum"] = f"{n}. Mükerrer" + (f" · {x['bolum']}" if x["bolum"] else "")
        maddeler += m["maddeler"]
    log(f"{len(maddeler)} madde bulundu, {len(mukerrerler)} mükerrer.")

    giris = {"tarih": gun.isoformat(), "sayi": ana["sayi"], "link": ana["url"],
             "mukerrer": mukerrerler, "genel": "", "maddeler": [], "model": "",
             "olusturma": dt.datetime.now(ZoneInfo("Europe/Istanbul")).isoformat(timespec="minutes")}

    if not maddeler:
        giris["genel"] = "İçindekiler listesi okunamadı. Resmî Gazete bağlantısından bakabilirsiniz."
    else:
        parcalar, toplam = [], 0
        for i, x in enumerate(maddeler, 1):
            metin = madde_metni(x["link"]) if i <= METNI_OKUNACAK_MADDE and toplam < TOPLAM_METIN_SINIRI else ""
            toplam += len(metin)
            parcalar.append(f"### {i}. [{x['bolum']}] {x['baslik']}\n{metin or '(metin alınmadı)'}")
            time.sleep(0.5)
        try:
            sonuc, model = gemini(TALIMAT.format(tarih=gun.strftime("%d.%m.%Y"), icerik="\n\n".join(parcalar)))
            ozetler = {int(o.get("no", 0)): o for o in sonuc.get("maddeler", []) if str(o.get("no", "")).isdigit()}
            giris["genel"] = temiz(sonuc.get("genel", ""))
            giris["model"] = model
        except Exception as e:
            log("Özet oluşturulamadı:", e)
            ozetler = {}
            giris["genel"] = "Bugün yapay zekâ özeti oluşturulamadı; başlıklar aşağıda."
        for i, x in enumerate(maddeler, 1):
            o = ozetler.get(i, {})
            onem = o.get("onem", "")
            giris["maddeler"].append({
                **x,
                "ozet": temiz(o.get("ozet", "")),
                "yururluk": temiz(o.get("yururluk", "")),
                "onem": onem if onem in ("yuksek", "orta", "dusuk") else "orta",
                "etiketler": [temiz(t) for t in o.get("etiketler", [])][:3],
            })

    os.makedirs(os.path.dirname(ARSIV), exist_ok=True)
    arsiv = []
    if os.path.exists(ARSIV):
        with open(ARSIV, encoding="utf-8") as f:
            arsiv = json.load(f)
    arsiv = [g for g in arsiv if g.get("tarih") != giris["tarih"]] + [giris]
    arsiv.sort(key=lambda g: g["tarih"], reverse=True)
    with open(ARSIV, "w", encoding="utf-8") as f:
        json.dump(arsiv, f, ensure_ascii=False, indent=1)
    log("Arşiv güncellendi.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("HATA:", e)
        sys.exit(1)
