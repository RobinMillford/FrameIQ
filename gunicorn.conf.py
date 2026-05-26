import os

bind = "0.0.0.0:8080"
workers = int(os.environ.get("WEB_WORKERS", 2))
threads = int(os.environ.get("WEB_THREADS", 4))
timeout = 120
keepalive = 5
accesslog = "-"
errorlog = "-"
