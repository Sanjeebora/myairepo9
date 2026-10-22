"""WSGI entry point, e.g. `gunicorn -w 1 --threads 8 -b 127.0.0.1:5055 wsgi:app`.

Use one worker: the jump-server session lives in process memory.
"""
from oracle_jump_tool.app import create_app
from oracle_jump_tool.config import Settings, load_dotenv_file
from oracle_jump_tool.session import SessionManager

load_dotenv_file()
settings = Settings.from_env()
manager = SessionManager(settings)
manager.start_reaper()
app = create_app(settings, manager)
