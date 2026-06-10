import uvicorn
import config

if __name__ == "__main__":
    print(f"SanityProxy starting on http://{config.LISTEN_HOST}:{config.LISTEN_PORT}")
    print(f"Dashboard: http://{config.LISTEN_HOST}:{config.LISTEN_PORT}/dashboard")
    print(f"Mode: {config.MODE}")
    uvicorn.run(
        "server:app",
        host=config.LISTEN_HOST,
        port=config.LISTEN_PORT,
        log_level="info",
    )
