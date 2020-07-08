import datetime
from threading import RLock


class Status:

    def __init__(self):
        self.rlock = RLock()
        self.status_data = {}
        self.created_at = datetime.datetime.now()

    def start_tunnel(self, tunnel_name):
        with self.rlock:
            if tunnel_name in self.status_data:
                self.status_data[tunnel_name]['started_times'] += 1
            else:
                self.status_data[tunnel_name] = {'started_times': 1}
            self.status_data[tunnel_name]['last_start'] = datetime.datetime.now().timestamp()

    def to_dict(self):
        with self.rlock:
            return {'created_at': self.created_at.timestamp(),
                    'status_data': self.status_data}
