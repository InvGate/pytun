import os
import shutil
import tempfile
import zipfile
from http import HTTPStatus
from http.server import HTTPServer
from http.server import SimpleHTTPRequestHandler
import json

class RequestHandlerClassFactory:

    def get_handler(self, config_path, tunnel_manager_id, log_path, status):
        class TunnelRequestHandler(SimpleHTTPRequestHandler):

            server_version = "Pytun Introspection web server/1.0"
            sys_version = "Python/3"

            def _zipdir(self,path, ziph, filter_callable=None):
                # ziph is zipfile handle
                for root, dirs, files in os.walk(path):
                    for file in files:
                        if filter_callable is None or filter_callable(file):
                            ziph.write(os.path.join(root, file))

            def do_GET(self):
                try:
                    if self.path == '/configs':
                        return self.handle_configs()
                    elif self.path == '/status':
                        res = self.handle_status()
                    elif self.path == '/logs':
                        return self.handle_logs()
                    else:
                        res = self.handle_ping()
                    res['tunnel_manager_id'] = tunnel_manager_id
                    json_str = json.dumps(res)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_str.encode(encoding='utf_8'))
                except Exception as e:
                    self.return_error(e)

            def return_error(self, e):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(
                    json.dumps({"error": str(e), "tunnel_manager_id": tunnel_manager_id}).encode(encoding='utf_8'))

            def handle_configs(self):
                try:
                    temp_dir = tempfile.gettempdir()
                    zipf = zipfile.ZipFile(os.path.join(temp_dir, 'configs.zip'), 'w', zipfile.ZIP_DEFLATED)
                    self._zipdir(config_path, zipf)
                    zipf.close()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-type", 'application/zip')
                    with open(os.path.join(temp_dir, 'configs.zip'), 'rb') as f:
                        fs = os.fstat(f.fileno())
                        self.send_header("Content-Length", str(fs[6]))
                        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
                        self.end_headers()
                        self.copyfile(f, self.wfile)
                except Exception as e:
                    self.return_error(e)

            def handle_status(self):
                res = status.to_dict()
                return res

            def handle_logs(self):
                try:
                    temp_dir = tempfile.gettempdir()
                    zipf = zipfile.ZipFile(os.path.join(temp_dir, 'logs.zip'), 'w', zipfile.ZIP_DEFLATED)
                    self._zipdir(log_path, zipf, lambda path: path.endswith(".log"))
                    zipf.close()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-type", 'application/zip')
                    with open(os.path.join(temp_dir, 'logs.zip'), 'rb') as f:
                        fs = os.fstat(f.fileno())
                        self.send_header("Content-Length", str(fs[6]))
                        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
                        self.end_headers()
                        self.copyfile(f, self.wfile)
                except Exception as e:
                    self.return_error(e)

            def handle_ping(self):
                return {'status':'ok'}

        return TunnelRequestHandler


def inspection_http_server(config_path, tunnel_manager_id, log_path, status, port):
    handler_class = RequestHandlerClassFactory().get_handler(config_path, tunnel_manager_id, log_path, status)
    http_server = HTTPServer(("127.0.0.1", port), handler_class)
    return http_server
