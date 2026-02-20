from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat")
async def chat():
    return {"message": "Agent API is running. Full implementation coming soon."}
