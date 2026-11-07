from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {
        "project": "CrawlScope",
        "message": "Welcome to CrawlScope!",
        "status": "running",
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
    }

