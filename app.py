"""TEFAS fon fiyatı servisi.

Tüketicileri: Google Sheets tabloları (IMPORTDATA ile /fiyat, /getiri) ve
finansal-operasyon-sistemi (/ham).

Çalışma mantığı: fonlar günde bir kez fiyatlanır, gün içinde ve hafta sonu
fiyat değişmez. Bu yüzden istek başına TEFAS'a gitmek yerine günde bir kez
BÜTÜN fonlar tek istekte çekilip depoya alınır; gelen istekler depodan
filtrelenerek karşılanır. TEFAS'a giden istek sayısı, kaç tüketici olduğundan
ve kaç fon sorulduğundan bağımsız olarak günde birkaç tanedir.

TEFAS dakikada 6 istek sınırlıyor; bu tasarımda o sınıra yaklaşılmıyor bile.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query, Response

app = FastAPI()

TOPLU_URL = "https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetir"
BASLIK = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}

TZ = ZoneInfo(os.getenv("TZ_ADI", "Europe/Istanbul"))
# Günlük çekim saati. Fon fiyatları ertesi iş günü yayınlandığı için sabah.
CEKIM_SAATI = os.getenv("CEKIM_SAATI", "10:30")
# Sabahki çekim o günün verisini getirmediyse akşam bir kez daha denenir.
TEYIT_SAATI = os.getenv("TEYIT_SAATI", "19:00")
# Hangi fon tipleri çekilsin. Her tip ayrı bir istek; aralarında bekleniyor.
FON_TIPLERI = os.getenv("FON_TIPLERI", "YAT,EMK,BYF").split(",")
# Kaç günlük geçmiş tutulacak. /getiri en az iki iş günü ister; tatiller için pay.
GECMIS_GUN = int(os.getenv("GECMIS_GUN", "10"))
DEPO_DOSYA = os.getenv("DEPO_DOSYA", "/tmp/tefas-depo.json")
ISTEK_TIMEOUT = 60.0
TIPLER_ARASI_BEKLE = 15

# fon kodu -> [{"tarih", "fiyat", "fonUnvan"}, ...]  tarihe göre artan
_depo: dict[str, list[dict]] = {}
_son_cekim: float = 0.0
_son_hata: str | None = None
_kilit = asyncio.Lock()


def _diske_yaz() -> None:
    try:
        gecici = f"{DEPO_DOSYA}.tmp"
        with open(gecici, "w", encoding="utf-8") as f:
            json.dump({"son_cekim": _son_cekim, "depo": _depo}, f, ensure_ascii=False)
        os.replace(gecici, DEPO_DOSYA)  # yarım dosya bırakmamak için
    except OSError:
        pass  # Kalıcılık iyileştirme; başarısızlığı servisi durdurmamalı.


def _diskten_oku() -> None:
    global _depo, _son_cekim
    try:
        with open(DEPO_DOSYA, encoding="utf-8") as f:
            veri = json.load(f)
        _depo = veri.get("depo") or {}
        _son_cekim = float(veri.get("son_cekim") or 0)
    except (OSError, ValueError, TypeError):
        pass


async def _tip_cek(client: httpx.AsyncClient, tip: str, bas: str, bit: str) -> list:
    govde = {
        "fonTipi": tip, "fonKodu": "", "aramaMetni": None, "fonTurKod": None,
        "fonGrubu": None, "sfonTurKod": None, "fonTurAciklama": None,
        "kurucuKod": None, "basTarih": bas, "bitTarih": bit,
        "basSira": 1, "bitSira": 100000, "dil": "TR",
        "sFonTurKod": "", "fonKod": "", "fonGrup": "", "fonUnvanTip": "",
    }
    r = await client.post(TOPLU_URL, json=govde, headers=BASLIK)
    r.raise_for_status()
    d = r.json()
    mesaj = d.get("errorMessage")
    # Tatil/hafta sonunda TEFAS "out of bounds" gibi mesajlar dönebiliyor;
    # bu hata değil, o aralıkta veri olmaması demek.
    bos_isaret = mesaj and any(
        m in mesaj.lower() for m in ("out of bounds", "veri bulunamadı")
    )
    if (d.get("errorCode") or mesaj) and not bos_isaret:
        raise RuntimeError(f"TEFAS hatasi: {mesaj} ({d.get('errorCode')})")
    return [] if bos_isaret else (d.get("resultList") or [])


async def topla() -> int:
    """Bütün fonları çeker, depoyu değiştirir. Kaç fon toplandığını döndürür."""
    global _depo, _son_cekim, _son_hata

    bugun = datetime.now(TZ).date()
    bas = (bugun - timedelta(days=GECMIS_GUN)).strftime("%Y%m%d")
    bit = bugun.strftime("%Y%m%d")

    yeni: dict[str, dict[str, dict]] = {}
    async with httpx.AsyncClient(timeout=ISTEK_TIMEOUT) as client:
        for sira, tip in enumerate(t.strip() for t in FON_TIPLERI if t.strip()):
            if sira:
                await asyncio.sleep(TIPLER_ARASI_BEKLE)
            for kayit in await _tip_cek(client, tip, bas, bit):
                kod = kayit.get("fonKodu")
                tarih = kayit.get("tarih")
                fiyat = kayit.get("fiyat")
                if not kod or not tarih or fiyat is None:
                    continue
                # Aynı fon+tarih tek kayıt; tarih anahtar olduğu için tekrarlar birleşir.
                yeni.setdefault(kod, {})[tarih] = {
                    "fonKodu": kod,
                    "fonUnvan": kayit.get("fonUnvan"),
                    "tarih": tarih,
                    "fiyat": float(fiyat),
                }

    if not yeni:
        raise RuntimeError("TEFAS bos liste dondu")

    _depo = {kod: [g[t] for t in sorted(g)] for kod, g in yeni.items()}
    _son_cekim = time.time()
    _son_hata = None
    _diske_yaz()
    return len(_depo)


async def _guvenli_topla() -> None:
    global _son_hata
    async with _kilit:
        try:
            adet = await topla()
            print(f"[topla] {adet} fon guncellendi", flush=True)
        except Exception as e:
            # Çekim başarısızsa eldeki depo korunur; tüketici bayat veri görür,
            # #N/A görmez. Yaş bilgisi X-Veri-Yas-Saniye ile bildiriliyor.
            _son_hata = str(e)
            print(f"[topla] HATA: {e}", flush=True)


def _sonraki(saat_metni: str) -> datetime:
    saat, dakika = (int(p) for p in saat_metni.split(":"))
    simdi = datetime.now(TZ)
    hedef = simdi.replace(hour=saat, minute=dakika, second=0, microsecond=0)
    if hedef <= simdi:
        hedef += timedelta(days=1)
    return hedef


def _depo_tarihi() -> str | None:
    """Depodaki en yeni tarih."""
    return max((k[-1]["tarih"] for k in _depo.values() if k), default=None)


async def _zamanlayici() -> None:
    while True:
        hedef = min(_sonraki(CEKIM_SAATI), _sonraki(TEYIT_SAATI))
        await asyncio.sleep(max(1.0, (hedef - datetime.now(TZ)).total_seconds()))
        await _guvenli_topla()


@app.on_event("startup")
async def baslangic() -> None:
    _diskten_oku()
    # Depo boşsa toplama BİTENE KADAR bekleniyor: servis ancak veriyle birlikte
    # hazır olsun. Kalıcı disk olmayan kurulumda (Coolify'da volume tanımlı
    # değilse) her yeniden başlatma depoyu sıfırlar; burada beklemezsek ilk
    # ~25 saniye boyunca isteklere 503 dönerdi. Beklersek yerleştirme sağlık
    # kontrolünü geçmez ve eski konteyner ayakta kalır — kimse boş cevap görmez.
    if not _depo:
        await _guvenli_topla()
    elif time.time() - _son_cekim > 24 * 3600:
        # Elde bayat da olsa veri var: hizmeti geciktirmeden arka planda tazele.
        asyncio.create_task(_guvenli_topla())
    asyncio.create_task(_zamanlayici())


def _kayitlar(fon: str) -> list[dict]:
    kayitlar = _depo.get(fon.upper())
    if not kayitlar:
        if not _depo:
            raise HTTPException(503, f"Depo hazir degil. Son hata: {_son_hata}")
        raise HTTPException(404, f"Fon bulunamadi: {fon.upper()}")
    return kayitlar


def _yas_basligi() -> dict[str, str]:
    return {"X-Veri-Yas-Saniye": str(int(time.time() - _son_cekim))}


@app.get("/")
async def kok():
    return Response("ok", media_type="text/plain")


@app.get("/saglik")
async def saglik():
    return {
        "fon_sayisi": len(_depo),
        "depo_tarihi": _depo_tarihi(),
        "son_cekim": datetime.fromtimestamp(_son_cekim, TZ).isoformat() if _son_cekim else None,
        "veri_yas_saniye": int(time.time() - _son_cekim) if _son_cekim else None,
        "son_hata": _son_hata,
        "fon_tipleri": FON_TIPLERI,
        "sonraki_cekim": min(_sonraki(CEKIM_SAATI), _sonraki(TEYIT_SAATI)).isoformat(),
    }


@app.post("/topla")
async def elle_topla():
    """Zamanı beklemeden çekim tetikler (elle müdahale / ilk kurulum)."""
    await _guvenli_topla()
    if _son_hata:
        raise HTTPException(502, _son_hata)
    return {"fon_sayisi": len(_depo), "depo_tarihi": _depo_tarihi()}


MIKRO = 1_000_000


def _bicimle(deger: float, format: str) -> str:
    """Google Sheets için ondalık ayırıcı sorunu olmayan biçim.

    IMPORTDATA gelen metni CSV olarak ayrıştırıyor: virgül sütun ayırıcı
    sayıldığı için "0,985506" iki hücreye bölünüyor ve yan sütunu eziyor.
    Nokta da işe yaramıyor; Türkçe yerelde "0.985506" 985506 olarak okunuyor.

    format=mikro fiyatı 1e6 ile çarpıp TAM SAYI döndürerek ikisini de aşıyor:
    ondalık ayırıcı hiç bulunmadığı için yerel ayarın etkisi kalmıyor. Tablo
    tarafında /1000000 ile geri ölçekleniyor ve her basamak sayısında doğru
    çalışıyor (AKU 985506 -> 0,985506; KTM 22781000 -> 22,781).
    """
    if format == "mikro":
        return str(round(deger * MIKRO))
    if format == "tr":
        return f"{deger}".replace(".", ",")
    return str(deger)


@app.get("/fiyat")
async def fiyat(fon: str = Query(...), format: str = "plain"):
    kayitlar = _kayitlar(fon)
    return Response(
        _bicimle(kayitlar[-1]["fiyat"], format),
        media_type="text/plain" if format == "plain" else "text/csv",
        headers=_yas_basligi(),
    )


@app.get("/getiri")
async def getiri(fon: str = Query(...), format: str = "plain"):
    kayitlar = _kayitlar(fon)
    if len(kayitlar) < 2:
        raise HTTPException(502, "Getiri icin yeterli veri yok")
    bugun = kayitlar[-1]["fiyat"]
    onceki = kayitlar[-2]["fiyat"]
    yuzde = f"{(bugun / onceki - 1) * 100:.2f}"
    return Response(
        yuzde.replace(".", ",") if format == "tr" else yuzde,
        media_type="text/csv",
        headers=_yas_basligi(),
    )


@app.get("/coklu")
async def coklu(fonlar: str = Query(..., description="virgulle ayrilmis fon kodlari")):
    """Birden çok fonu tek JSON'da döndürür (Apps Script gibi tüketiciler için).

    Değerler JSON sayısı olarak gider; ondalık ayırıcı / CSV ayrıştırma sorunu
    yaşayan IMPORTDATA yolunun aksine tüketici doğrudan sayıyı alır.
    """
    if not _depo:
        raise HTTPException(503, f"Depo hazir degil. Son hata: {_son_hata}")

    sonuc = {}
    for ham_kod in fonlar.split(","):
        kod = ham_kod.strip().upper()
        if not kod:
            continue
        kayitlar = _depo.get(kod)
        if not kayitlar:
            sonuc[kod] = {"bulundu": False}
            continue
        son = kayitlar[-1]
        getiri = None
        if len(kayitlar) >= 2 and kayitlar[-2]["fiyat"]:
            getiri = round((son["fiyat"] / kayitlar[-2]["fiyat"] - 1) * 100, 2)
        sonuc[kod] = {
            "bulundu": True,
            "fiyat": son["fiyat"],
            "tarih": son["tarih"],
            "unvan": son.get("fonUnvan"),
            "gunluk_getiri": getiri,
        }
    return {"veri_tarihi": _depo_tarihi(), "fonlar": sonuc}


@app.get("/ham")
async def ham(fon: str = Query(...)):
    # finansal-operasyon-sistemi bu uç noktayı kullanıyor ve parseTefasResponse
    # ile fonKodu/tarih/fiyat alanlarını okuyor. Dizi biçimi korunmalı.
    #
    # Tanınmayan fon kodunda HTTP hatası DEĞİL boş dizi dönüyoruz: tüketici
    # tarafı "böyle fon yok" (boş liste) ile "kaynak erişilemez" (HTTP hatası)
    # ayrımını buna göre yapıyor. 404 dönersek yanlış fon kodu, TEFAS çökmüş
    # gibi raporlanır.
    if not _depo:
        raise HTTPException(503, f"Depo hazir degil. Son hata: {_son_hata}")
    kayitlar = _depo.get(fon.upper()) or []
    return Response(
        json.dumps(kayitlar[-3:], ensure_ascii=False, indent=2),
        media_type="application/json",
        headers=_yas_basligi(),
    )
