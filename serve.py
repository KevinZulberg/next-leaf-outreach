"""Container entry point for an always-on Outreach Studio."""
from waitress import serve

from app import app
from automatic_research import start_scheduler
from automatic_sending import start_sending


if __name__ == '__main__':
    start_scheduler()
    start_sending()
    serve(app, host='0.0.0.0', port=5050)
