from fastapi import FastAPI
import sentry_sdk
from api.v1 import districtcourt, hc2, hc3, cc, nclt, sci, drt, nclat, ngt, cgat, aft
app = FastAPI(title="Secure Flow By Richstream")

sentry_sdk.init(
    dsn="https://d5ba717dbd1a1f3eec57fb1ec6798284@o4508364047712256.ingest.us.sentry.io/4510724946853888",
    send_default_pii=True,
)

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/sentry-debug")
async def trigger_error():
    division_by_zero = 1 / 0

app.include_router(districtcourt.app, prefix="/api/v1", tags=["Districtcourt"])
app.include_router(hc2.app, prefix="/api/v1", tags=["Hccourt2"])
app.include_router(hc3.app, prefix="/api/v1", tags=["Hccourt3"])
app.include_router(cc.app, prefix="/api/v1", tags=["Consumer"])
app.include_router(nclt.app, prefix="/api/v1", tags=["Nclt"])
app.include_router(sci.app, prefix="/api/v1", tags=["Sci"])
app.include_router(drt.app, prefix="/api/v1", tags=["Drt"])
app.include_router(nclat.app, prefix="/api/v1", tags=["Nclat"])
app.include_router(ngt.app, prefix="/api/v1", tags=["Ngt"])
app.include_router(cgat.app, prefix="/api/v1", tags=["Cgat"])
app.include_router(aft.app, prefix="/api/v1", tags=["Aft"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8000)
