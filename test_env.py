import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")

for variable_name in ("OPENROUTER_API_KEY", "BLOTATO_API_KEY"):
    status = "讀取成功" if os.getenv(variable_name) else "讀取失敗"
    print(f"{variable_name}：{status}")
