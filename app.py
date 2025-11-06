from fastapi import FastAPI
from api.v1 import districtcourt, hccourt, hc2, cc
app = FastAPI(title="Secure Flow By GYL")

@app.get("/")
async def root():
    return {"message": "Hello World"}

app.include_router(districtcourt.app, prefix="/api/v1", tags=["Districtcourt"])
app.include_router(hccourt.app, prefix="/api/v1", tags=["Hccourt"])
app.include_router(hc2.app, prefix="/api/v1", tags=["Hccourt2"])
app.include_router(cc.app, prefix="/api/v1", tags=["Consumer"])



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8000)
