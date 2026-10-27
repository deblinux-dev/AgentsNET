"""WSGI entry point (PythonAnywhere, gunicorn, uwsgi, ...).

PythonAnywhere setup:
  1. Upload the agentsnet folder (or git clone) e.g. to /home/USER/agentsnet
  2. Web app -> Add new -> Manual configuration -> Python 3.10+
  3. Source directory: /home/USER/agentsnet
  4. WSGI configuration file: replace its content with:
         import sys
         sys.path.insert(0, "/home/USER/agentsnet")
         from wsgi import application
  5. (Optional) pip install flask waitress into your venv; config via config.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import app as application  # noqa: E402,F401
