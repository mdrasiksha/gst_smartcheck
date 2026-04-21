from fastapi import FastAPI

print("API module loaded successfully")

app = FastAPI()


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "running"}
