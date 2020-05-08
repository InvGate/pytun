# pytun
Tunnel tool to allow services in a local network from cloud using python 3.6+

Installation:
* Download the repo
* Install dependencies: pip install -r requirements.txt 

Usage:
```
python pytun.py --config_ini CONFIG_FILE
```

In that file you can configure:

```ini
[pytun]
tunnel_dirs=./configs   # Directory with the configuration of your tunnels
log_level=DEBUG
log_to_console=True
```

To configure a tunnel, you have to create an ini file like:

```ini
[tunnel]
# We are able to reach 10.0.0.184 and able to reach 10.0.1.63
# 10.0.0.184 needs to reach 10.0.1.63 but it cannot directly
# With this example configuration we are exposing the service on  10.0.1.63 : 389
# on port 10389 of the server 10.0.0.184 establisihng a tunnel with it

# Cloud endpoint to connect
server_host=10.0.0.184
server_port=22

# Port in the cloud endpoint
port=14389

# Keep alive timeout (seconds)
keep_alive_time=30


# Service Endpoint to connect
remote_host=10.0.1.63
remote_port=636

# Key file to use to authenticate
keyfile=PATH_TO_YOUR_PEM_FILE_PASSWORDLESS

# Username to use to authenticate
username=USERNAME_YOU_WANT_TO_USE_TO_SSH

# Public key of the cloud endpoint
server_key=kwnonserver
```

This file, will create a tunnel from the computer running the command to the server 10.0.0.184 and will listen there on the
port 14389. When a connection is received there, it is forwarded to 10.0.1.63:636. This would allow someone who can 
reach 10.0.0.184 to reach 10.0.1.63 using the computer running the script.

