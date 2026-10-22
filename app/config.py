from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Settings:
    # об’єкт налаштувань відокремлює конфігурацію вузла від логіки застосунку
    node_id: str = "node-a"
    host: str = "127.0.0.1"
    port: int = 8001
    database_path: Path = Path("data/node-a.db")
    peers: list[str] = field(default_factory=list)
    federation_secret: str = "nexus-local-development-secret"
    session_hours: int = 24
    max_upload_bytes: int = 8 * 1024 * 1024

    @property
    def public_url(self) -> str:
        return f"http://{self.host}:{self.port}"
