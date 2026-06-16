import time, asyncio
from fastapi import FastAPI, Response, HTTPException, Query
from playwright.async_api import async_playwright

app = FastAPI()
CACHE_TTL = 3600
_cache = {}
_lock = asyncio.Lock()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

async def tefas_fiyat(fon: str):
    fon = fon.upper()
    sayfa_url = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{fon}"
    api_url = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="tr-TR")
        page = await ctx.new_page()
        try:
            # Sayfayı aç ki WAF cookie + token tarayıcıya yerlessin
            await page.goto(sayfa_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)

            # Istegi tarayicinin kendi context'inden biz atiyoruz
            resp = await page.request.post(api_url, data={
                "fonKodu": fon, "dil": "TR", "periyod": 12
            }, headers={"Referer": sayfa_url, "Origin": "https://www.tefas.gov.tr"})
            data = await resp.json()
        finally:
            await browser.close()

    if not data or not data.get("resultList"):
        raise HTTPException(502, f"TEFAS verisi bos: {data}")

    son = data["resultList"][-1]
    fiyat = son.get("fiyat") or son.get("FIYAT") or son.get("price")
    if fiyat is None:
        raise HTTPException(502, f"Fiyat alani yok: {son}")
    return float(fiyat)

@app.get("/")
async def kok():
    return Response("ok", media_type="text/plain")

@app.get("/ham")
async def ham(fon: str = Query(...)):
    import json
    fon = fon.upper()
    sayfa_url = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{fon}"
    api_url = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="tr-TR")
        page = await ctx.new_page()
        try:
            await page.goto(sayfa_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            resp = await page.request.post(api_url, data={"fonKodu": fon, "dil": "TR", "periyod": 5},
                                           headers={"Referer": sayfa_url, "Origin": "https://www.tefas.gov.tr"})
            data = await resp.json()
            arr = data.get("resultList") or []
            return Response(json.dumps(arr[-3:], ensure_ascii=False, indent=2), media_type="application/json")
        finally:
            await browser.close()
    
@app.get("/fiyat")
async def fiyat(fon: str = Query(...), format: str = "plain"):
    fon = fon.upper()
    now = time.time()
    async with _lock:
        c = _cache.get(fon)
        if c and now - c[0] < CACHE_TTL:
            price = c[1]
        else:
            price = await tefas_fiyat(fon)
            _cache[fon] = (now, price)
    return Response(str(price), media_type="text/csv" if format == "csv" else "text/plain")
