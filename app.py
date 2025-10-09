from fastapi import FastAPI
from api.v1 import districtcourt, hccourt
app = FastAPI(title="Secure Flow By GYL")

@app.get("/")
async def root():
    return {"message": "Hello World"}

app.include_router(districtcourt.app, prefix="/api/v1", tags=["Districtcourt"])
app.include_router(hccourt.app, prefix="/api/v1", tags=["Hccourt"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8000)
