import uvicorn

from app.config import EnvSettings

if __name__ == "__main__":
    env = EnvSettings()
    uvicorn.run("app.main:app", host=env.host, port=env.port, log_level="warning")
