from pathlib import Path
from dotenv import load_dotenv

# Always read the settings file next to this script, regardless of launch directory.
load_dotenv(Path(__file__).resolve().parent / '.env', override=True)

from app import app
from automatic_research import start_scheduler
from automatic_sending import start_sending

if __name__ == '__main__':
    start_scheduler()
    start_sending()
    app.run(host='127.0.0.1', port=5050, debug=False)
