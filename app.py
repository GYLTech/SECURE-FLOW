from fastapi import FastAPI
import sentry_sdk
from api.v1 import districtcourt, hc2, cc, nclt,sci
app = FastAPI(title="Secure Flow By GYL")

sentry_sdk.init(
    dsn="https://d5ba717dbd1a1f3eec57fb1ec6798284@o4508364047712256.ingest.us.sentry.io/4510724946853888",
    send_default_pii=True,
)

@app.get("/")
async def root():
    return {"message": "Hello World"}

@app.get("/sentry-debug")
async def trigger_error():
    division_by_zero = 1 / 0

app.include_router(districtcourt.app, prefix="/api/v1", tags=["Districtcourt"])
app.include_router(hc2.app, prefix="/api/v1", tags=["Hccourt2"])
app.include_router(cc.app, prefix="/api/v1", tags=["Consumer"])
app.include_router(nclt.app, prefix="/api/v1", tags=["Nclt"])
app.include_router(sci.app, prefix="/api/v1", tags=["Sci"])





if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8000)
