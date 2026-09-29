import configparser
import os
import platform
import tempfile
import zipfile
from concurrent.futures.thread import ThreadPoolExecutor
from http import HTTPStatus
from os.path import realpath

from observation.connection_check import ConnectionCheck

try:
    from http.server import ThreadingHTTPServer as HttpServer
except ImportError:
    from http.server import HTTPServer as HttpServer
from http.server import SimpleHTTPRequestHandler
import json


WINDOWS_11_FIRST_BUILD = 22000
_WINDOWS_VERSION_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


def _read_windows_product():
    """Return (ProductName, CurrentBuildNumber) from the registry, or None off Windows."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_VERSION_KEY) as key:
            product_name = winreg.QueryValueEx(key, "ProductName")[0]
            build = winreg.QueryValueEx(key, "CurrentBuildNumber")[0]
    except OSError:
        return None
    return product_name, build


def get_os_name():
    """Commercial OS name, e.g. "Windows Server 2016 Standard"; empty off Windows."""
    product = _read_windows_product()
    if product is None:
        return ""
    product_name, build = product
    # Windows 11 still reports "Windows 10" in ProductName; the build tells them apart.
    try:
        is_windows_11 = int(build) >= WINDOWS_11_FIRST_BUILD
    except ValueError:
        is_windows_11 = False
    if is_windows_11 and product_name.startswith("Windows 10"):
        return "Windows 11" + product_name[len("Windows 10"):]
    return product_name


def get_platform_info():
    # On Windows platform.machine() reads PROCESSOR_ARCHITEW6432 first,
    # so it returns the real OS architecture even from a 32-bit executable.
    return {
        "os": platform.platform(),
        "os_name": get_os_name(),
        "machine": platform.machine(),
    }


class RequestHandlerClassFactory:

    def get_handler(self, config_path, tunnel_manager_id, log_path, status, version_string, logger):
        class TunnelRequestHandler(SimpleHTTPRequestHandler):

            server_version = "Pytun Introspection web server/" + version_string
            sys_version = "Python/3"
            pytun_Version = version_string

            def _zipdir(self, path, ziph, filter_callable=None):
                # ziph is zipfile handle
                path = os.path.normpath(path)
                # Hack: os.walk was not working if the path started with "\\?\"
                if path.startswith("\\\\?\\"):
                    path = path.replace("\\\\?\\", "")
                logger.debug("going to walk " + path)
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
                    logger.exception("Error processing HTTP Request %s: %s" % (self.path, e))
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
                res.update(self.add_services_status())
                return res

            def handle_logs(self):
                try:
                    temp_dir = tempfile.gettempdir()
                    zipf = zipfile.ZipFile(os.path.join(temp_dir, 'logs.zip'), 'w', zipfile.ZIP_DEFLATED)
                    self._zipdir(log_path, zipf, lambda path: ".log" in path)
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

            def add_services_status(self):
                res = {}
                path = config_path
                if path.startswith("\\\\?\\"):
                    path = path.replace("\\\\?\\", "")
                connection_checker = ConnectionCheck(logger)
                pool = ThreadPoolExecutor(4)
                jobs = {}
                for root, dirs, files in os.walk(path):
                    for file in files:
                        if file[-4:] == ".ini":
                            try:
                                config = configparser.ConfigParser()
                                config.read(os.path.join(path, file))
                                defaults = config['tunnel']
                                remote_host = defaults['remote_host']
                                remote_port = int(defaults.get('remote_port'))
                                tunnel_name = defaults.get('tunnel_name', realpath(file))
                                res[tunnel_name] = {'remote_host':remote_host, 'remote_port':remote_port}
                                jobs[tunnel_name] = pool.submit(connection_checker.test_connection, tunnel_name, remote_host, remote_port)
                            except Exception as e:
                                logger.exception("Error getting status for %s" % (file,))
                                continue
                    for name, job_future in jobs.items():
                        res[name]['status'] = job_future.result()
                pool.shutdown()
                return res


            def handle_ping(self):
                return dict({'status': 'ok', "version": self.pytun_Version}, **get_platform_info())

        return TunnelRequestHandler


def inspection_http_server(config_path, tunnel_manager_id, log_path, status, version_string, address, logger):
    handler_class = RequestHandlerClassFactory().get_handler(config_path, tunnel_manager_id, log_path, status,
                                                             version_string, logger)

    http_server = HttpServer(address, handler_class)
    return http_server
