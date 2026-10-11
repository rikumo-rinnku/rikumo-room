"""Return the saved password to OpenSSH's private askpass pipe."""
import json
import os
from pathlib import Path
import sys

try:
    config = json.loads(Path(os.environ['RIKUMO_SSH_PASSWORD_FILE']).read_text(encoding='utf-8'))
    password = config['password']
    if not isinstance(password, str) or not password:
        raise ValueError()
    sys.stdout.write(password + '\n')
except (OSError, ValueError, KeyError):
    sys.exit(1)
