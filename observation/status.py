import datetime
from threading import RLock

from observation.metrics import TunnelMetrics


class Status:

    def __init__(self, mac_address: str):
        self.rlock = RLock()
        self.status_data = {}
        self.created_at = datetime.datetime.now()
        self.mac_address = mac_address
        self._tunnels = {}  # config file -> (tunnel name, TunnelMetrics)

    def start_tunnel(self, config_file, tunnel_name=None):
        """Record a (re)start and return the tunnel's shared metrics, the same object on every restart."""
        with self.rlock:
            if config_file in self.status_data:
                self.status_data[config_file]['started_times'] += 1
            else:
                self.status_data[config_file] = {'started_times': 1}
            self.status_data[config_file]['last_start'] = datetime.datetime.now().timestamp()

            metrics = self._tunnels[config_file][1] if config_file in self._tunnels else TunnelMetrics()
            self._tunnels[config_file] = (tunnel_name or config_file, metrics)
            return metrics

    def tunnel_metrics(self):
        """[(config file, tunnel name, values)], with the restart count merged into the values."""
        with self.rlock:
            return [
                (config_file, name,
                 dict(metrics.snapshot(), restarts=self.status_data[config_file]['started_times'] - 1))
                for config_file, (name, metrics) in self._tunnels.items()
            ]

    def to_dict(self):
        with self.rlock:
            return {
                'created_at': self.created_at.timestamp(),
                'mac_address': self.mac_address,
                'status_data': self.status_data
            }
