import time, asyncio
from fastapi import FastAPI, Response, HTTPException, Query
from playwright.async_api import async_playwright

app = FastAPI()
CACHE_TTL = 3600
_cache = {}
_lock = asyncio.Lock()

async def tefas_fiyat(fon: str):
    fon = fon.upper()
    url = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{fon}"
    captured = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()

        async def on_response(resp):
            if "fonFiyatBilgiGetir" in resp.url:
                try: captured["data"] = await resp.json()
                except Exception: pass

        page.on("response", on_response)
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(1500)
        await browser.close()

    data = captured.get("data")
    if not data or not data.get("resultList"):
        raise HTTPException(502, "TEFAS verisi alinamadi")

    son = data["resultList"][-1]
    fiyat = son.get("fiyat") or son.get("FIYAT") or son.get("price")
    if fiyat is None:
        raise HTTPException(502, f"Fiyat alani yok: {son}")
    return float(fiyat)

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
