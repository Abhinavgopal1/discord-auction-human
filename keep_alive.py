import os
from threading import Thread

from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Football Auction web endpoint is running. Check deployment logs for Discord connection status."

def run():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')), debug=False, use_reloader=False)

def keep_alive():
    # Let a failed Discord login terminate the process so the host can restart it.
    t = Thread(target=run, daemon=True)
    t.start()
    return t
