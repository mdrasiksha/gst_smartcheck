import hashlib
import socket

hostname = socket.gethostname()
print("Hostname:", hostname)
print("Machine ID:", hashlib.sha256(hostname.encode()).hexdigest())

