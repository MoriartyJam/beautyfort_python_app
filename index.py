import uvicorn


if __name__ == "__main__":
    print("Starting Beautyfort Sync on 0.0.0.0:80", flush=True)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=80,
        proxy_headers=True,
        log_level="info",
        access_log=True,
    )
