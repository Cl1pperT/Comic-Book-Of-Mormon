import os
from dataclasses import dataclass
from dotenv import load_dotenv

@dataclass
class Config:
    api_key: str = ""
    text_model: str = ""
    validator_model: str = ""
    image_model: str = ""
    @classmethod
    def load(cls):
        load_dotenv()
        return cls(*(os.getenv(name, "") for name in ("GEMINI_API_KEY", "GEMINI_TEXT_MODEL", "GEMINI_VALIDATOR_MODEL", "GEMINI_IMAGE_MODEL")))
    def require(self, kind):
        model = getattr(self, kind + "_model")
        if not self.api_key or not model:
            raise ValueError(f"Set GEMINI_API_KEY and GEMINI_{kind.upper()}_MODEL in .env")
        return model
